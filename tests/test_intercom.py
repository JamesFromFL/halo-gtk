"""Synthetic inventory, RPC ownership, expiry and event contracts."""

from __future__ import annotations

import asyncio

import pytest

from halo_gtk.intercom import (
    IntercomIntent,
    IntercomReadStatus,
    IntercomSchemaError,
    IntercomService,
    parse_intercom_inventory,
)
from halo_gtk.intercom import (
    IntercomOperationStatus as Status,
)
from halo_gtk.intercom_rest import IntercomRestError
from halo_gtk.ring_json import RingWriteReceipt


def inventory(**changes):
    return {
        "other": [
            {
                "id": 42,
                "description": "Entrance <b>literal</b>",
                "kind": "intercom_handset_audio",
                "location_id": "home",
                "device_id": "hardware",
                "battery_life": "52",
                "alerts": {"connection": "online"},
                "settings": {"intercom_settings": {"ring_to_open": False}},
                **changes,
            }
        ]
    }


class Gateway:
    def __init__(self):
        self.inventory = inventory()
        self.reads = self.writes = 0
        self.read_gate = self.write_gate = self.before_send = None
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.error = self.read_error = None
        self.reply = {"result": {"code": 0}}
        self.accepted = True
        self.after = lambda: None
        self.closed = False

    async def read_inventory(self, **_kwargs):
        self.reads += 1
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.read_error:
            raise self.read_error
        return self.inventory

    async def unlock(self, _api_id, request_id, *, admission, on_admitted, **_kwargs):
        try:
            if self.before_send is not None:
                self.started.set()
                await self.before_send.wait()
            admission()
            on_admitted()
            self.writes += 1
            self.started.set()
            if self.write_gate is not None:
                await self.write_gate.wait()
            if self.error:
                raise self.error
            self.after()
            return RingWriteReceipt(200, response_json=self.reply)
        finally:
            self.cleaned.set()

    async def close(self):
        self.closed = True


@pytest.fixture
def state():
    gateway = Gateway()
    now, wall, current = [100.0], [1_700_000_000.0], [True]
    service = IntercomService(
        gateway,
        generation=7,
        elapsed_clock=lambda: now[0],
        clock=lambda: wall[0],
        is_current=lambda: current[0],
    )
    return service, gateway, now, wall, current


async def ready(state):
    service, *_ = state
    await service.refresh()
    return service.snapshot.find_device(42)


def intent(state, device=None, **changes):
    service, _gateway, now, *_ = state
    device = device or service.snapshot.find_device(42)
    return IntercomIntent(
        **(
            {
                "generation": 7,
                "api_id": 42,
                "revision": device.revision,
                "identity_epoch": device.identity_epoch,
                "created_monotonic": now[0],
            }
            | changes
        )
    )


async def request(state, approval=None):
    service, *_ = state
    device = service.snapshot.find_device(42)
    return await service.request_unlock(
        42, expected_revision=device.revision, intent=approval or intent(state)
    )


@pytest.mark.parametrize("kind", ["intercom_handset_audio", "intercom_handset_video"])
def test_supported_kinds_names_battery_and_unknown_values(kind):
    (device,) = parse_intercom_inventory(inventory(kind=kind))
    assert device.battery == 52 and device.ring_to_open is False
    assert device.name == "Entrance <b>literal</b>" and "Intercom" in device.model
    (device,) = parse_intercom_inventory(inventory(battery_life=True, alerts={}, settings={}))
    assert device.battery is None and device.connection == "unknown" and device.ring_to_open is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"other": None},
        {"other": [None]},
        inventory(id=True),
        inventory(id=0),
        inventory(id=2**63),
        inventory(location_id=".."),
        inventory(location_id="bad\u0000"),
        inventory(deactivated_at="garbage"),
        inventory(device_id=[]),
        {"other": inventory()["other"] * 2},
        {"other": [{}] * 513},
    ],
)
def test_invalid_inventory_cannot_imply_empty_or_removal(payload):
    with pytest.raises(IntercomSchemaError):
        parse_intercom_inventory(payload)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        (100, 100),
        (101, None),
        ("0", 0),
        ("001", 1),
        ("52.0", None),
        (-1, None),
        (False, None),
        ("9" * 1000, None),
    ],
)
def test_battery_is_bounded_and_zero_is_not_unknown(value, expected):
    assert parse_intercom_inventory(inventory(battery_life=value))[0].battery == expected


def test_explicit_empty_unrelated_kinds_and_deactivated_device():
    assert parse_intercom_inventory({"other": []}) == ()
    assert parse_intercom_inventory(inventory(kind="third_party_camera")) == ()
    assert parse_intercom_inventory(inventory(deactivated_at="2026-10-25T12:00:00Z")) == ()
    assert parse_intercom_inventory(inventory(stolen=True)) == ()


@pytest.mark.asyncio
async def test_cache_receipts_semantic_revision_and_authoritative_removal(state):
    service, gateway, now, *_ = state
    first = await ready(state)
    await service.refresh(force=True)
    assert gateway.reads == 1
    now[0] += 6
    await service.refresh()
    second = service.snapshot.find_device(42)
    assert second.revision == first.revision and second.inventory_monotonic == 106
    now[0] += 6
    gateway.inventory = {}
    await service.refresh(force=True)
    assert service.snapshot.find_device(42).stale and not service.snapshot.complete
    now[0] += 16
    gateway.inventory = {"other": []}
    await service.refresh(force=True)
    assert service.snapshot.devices == () and service.snapshot.has_inventory
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"location_id": "elsewhere"},
        {"device_id": "replacement"},
        {"kind": "intercom_handset_video"},
    ],
)
async def test_reused_api_identity_rejects_old_approval(state, change):
    service, gateway, now, *_ = state
    old = await ready(state)
    approval = intent(state)
    now[0] += 6
    gateway.inventory = inventory(**change)
    await service.refresh(force=True)
    assert service.snapshot.find_device(42).identity_epoch != old.identity_epoch
    assert (await request(state, approval)).status == Status.REJECTED
    assert gateway.writes == 0


@pytest.mark.asyncio
async def test_accepted_rpc_requires_manual_exact_ack_and_same_intent_is_single_use(state):
    service, gateway, *_ = state
    await ready(state)
    approval = intent(state)
    result = await request(state, approval)
    assert result.status == Status.ACCEPTED and gateway.writes == 1
    assert service.snapshot.find_device(42).acknowledgment_id == result.operation_id
    assert (await request(state)).code == "acknowledgment-required"
    assert not await service.acknowledge(42, result.operation_id + 1)
    assert await service.acknowledge(42, result.operation_id)
    assert (await request(state, approval)).code == "cancelled-or-used"
    assert gateway.writes == 1
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,expected",
    [
        ({"result": {"code": 0}}, Status.ACCEPTED),
        ({"result": {"code": 1}}, Status.REJECTED),
        ({"result": {"code": False}}, Status.UNKNOWN),
        ({"result": {"code": "0"}}, Status.UNKNOWN),
        ({}, Status.UNKNOWN),
        ({"result": []}, Status.UNKNOWN),
        ({"id": "wrong", "result": {"code": 0}}, Status.UNKNOWN),
    ],
)
async def test_rpc_acceptance_is_strict_and_never_physical_state(state, reply, expected):
    service, gateway, *_ = state
    await ready(state)
    gateway.reply = reply
    result = await request(state)
    assert result.status == expected and gateway.writes == 1
    assert not hasattr(service.snapshot.find_device(42), "locked")
    assert (service.snapshot.find_device(42).acknowledgment_id is not None) == (
        expected in (Status.ACCEPTED, Status.UNKNOWN)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"generation": 8},
        {"identity_epoch": 90},
        {"created_monotonic": 60},
        {"created_monotonic": 101},
        {"created_monotonic": float("nan")},
        {"created_monotonic": "100"},
        {"revision": True},
    ],
)
async def test_invalid_expired_future_or_wrong_account_approval_never_sends(state, change):
    await ready(state)
    result = await request(state, intent(state, **change))
    assert result.status == Status.REJECTED and state[1].writes == 0


@pytest.mark.asyncio
async def test_consumer_cancel_before_admission_prevents_send_and_releases_owned_slot(state):
    service, gateway, *_ = state
    await ready(state)
    gateway.before_send = asyncio.Event()
    consumer = asyncio.create_task(request(state))
    await gateway.started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    async with asyncio.timeout(1):
        while service._operation_task is not None:
            await asyncio.sleep(0)
    assert gateway.writes == 0 and gateway.cleaned.is_set()


@pytest.mark.asyncio
async def test_cancel_after_admission_keeps_owned_request_and_busy_rejects_without_queue(state):
    service, gateway, *_ = state
    await ready(state)
    gateway.write_gate = asyncio.Event()
    consumer = asyncio.create_task(request(state))
    await gateway.started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert (await request(state)).code == "busy" and gateway.writes == 1
    assert not await service.acknowledge(42, 1)
    gateway.write_gate.set()
    async with asyncio.timeout(1):
        while service._operation_task is not None:
            await asyncio.sleep(0)
    assert service.snapshot.find_device(42).operation.status == Status.ACCEPTED
    assert service.snapshot.find_device(42).acknowledgment_id == 1


@pytest.mark.asyncio
async def test_late_synchronous_return_is_unknown_and_gate_survives_refresh(state):
    service, gateway, now, *_ = state
    await ready(state)
    gateway.after = lambda: now.__setitem__(0, now[0] + 31)
    result = await request(state)
    assert result.status == Status.UNKNOWN
    await service.refresh()
    assert service.snapshot.find_device(42).acknowledgment_id == result.operation_id


@pytest.mark.asyncio
async def test_whole_deadline_includes_auth_or_slot_wait_and_close_joins_cleanup(
    state, monkeypatch
):
    from halo_gtk import intercom

    service, gateway, *_ = state
    await ready(state)
    monkeypatch.setattr(intercom, "OPERATION_SECONDS", 0.01)
    gateway.before_send = asyncio.Event()
    result = await request(state)
    assert result.status == Status.REJECTED and gateway.writes == 0 and gateway.cleaned.is_set()
    gateway.before_send = None
    gateway.write_gate = asyncio.Event()
    gateway.started.clear()
    consumer = asyncio.create_task(request(state))
    await gateway.started.wait()
    await asyncio.gather(service.close(), service.close())
    assert (await consumer).status == Status.UNKNOWN
    assert gateway.closed and service.snapshot.status == IntercomReadStatus.CLOSED


@pytest.mark.asyncio
async def test_403_stays_denied_until_deliberate_successful_reload(state):
    service, gateway, now, *_ = state
    await ready(state)
    gateway.error = IntercomRestError("http-403", status=403, possibly_sent=True)
    assert (await request(state)).status == Status.REJECTED
    assert service.snapshot.find_device(42).denied
    now[0] += 6
    await service.refresh()
    assert service.snapshot.find_device(42).denied
    now[0] += 6
    await service.refresh(force=True)
    assert not service.snapshot.find_device(42).denied


@pytest.mark.asyncio
async def test_read_failure_pauses_or_backs_off_and_does_not_remove(state):
    service, gateway, now, *_ = state
    await ready(state)
    now[0] += 6
    gateway.read_error = IntercomRestError("http-401", status=401)
    await service.refresh()
    assert not service.snapshot.poll_allowed and service.snapshot.find_device(42).stale
    now[0] += 300
    await service.refresh()
    assert gateway.reads == 2
    gateway.read_error = None
    await service.refresh(force=True)
    assert service.snapshot.complete and service.snapshot.poll_allowed


@pytest.mark.asyncio
async def test_read_coalescing_consumer_cancel_does_not_cancel_other_reader(state):
    service, gateway, *_ = state
    gateway.read_gate = asyncio.Event()
    first, second = [asyncio.create_task(service.refresh()) for _ in range(2)]
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gateway.read_gate.set()
    assert (await second).complete and gateway.reads == 1


def event(state, **changes):
    return {
        "id": 1,
        "doorbot_id": 42,
        "device_kind": "intercom_handset_audio",
        "kind": "ding",
        "now": state[3][0],
        "is_update": False,
        **changes,
    }


@pytest.mark.asyncio
async def test_events_are_bounded_deduplicated_observations_and_never_send(state):
    service, gateway, *_ = state
    await ready(state)
    assert service.observe_event(event(state)).observations[0].kind == "ding"
    assert service.observe_event(event(state)) is None
    for number in range(2, 200):
        service.observe_event(event(state, id=number, kind="intercom_unlock"))
    assert len(service._events) == 128 and len(service.snapshot.find_device(42).observations) == 16
    assert gateway.writes == 0 and service.snapshot.find_device(42).operation is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"now": 0},
        {"now": float("inf")},
        {"doorbot_id": True},
        {"device_kind": "doorbot"},
        {"id": True},
        {"is_update": True},
        {"kind": "motion"},
    ],
)
async def test_malformed_wrong_device_delayed_or_update_event_is_not_published(state, changes):
    service, *_ = state
    await ready(state)
    assert service.observe_event(event(state, **changes)) is None


@pytest.mark.asyncio
async def test_cancelled_discovery_cannot_bypass_attempt_cooldown_on_rapid_reopen(state):
    service, gateway, now, *_ = state
    gateway.read_gate = asyncio.Event()
    consumer = asyncio.create_task(service.refresh())
    while gateway.reads == 0:
        await asyncio.sleep(0)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await service.refresh(force=True)
    assert gateway.reads == 1
    now[0] += 6
    gateway.read_gate.set()
    assert (await service.refresh(force=True)).complete
    assert gateway.reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("uncancel", [False, True])
async def test_suppressed_deadline_cannot_report_acceptance_or_release_slot_early(
    uncancel, monkeypatch
):
    from halo_gtk import intercom
    from halo_gtk.light_groups import light_group_monotonic_time

    gateway = Gateway()
    service = IntercomService(gateway, generation=7)
    await service.refresh()
    cleaned = asyncio.Event()
    monkeypatch.setattr(intercom, "OPERATION_SECONDS", 0.01)

    async def late(_api_id, request_id, *, admission, on_admitted, **kwargs):
        admission()
        on_admitted()
        gateway.writes += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if uncancel:
                asyncio.current_task().uncancel()
            await asyncio.sleep(0)
            assert service._operation_task is asyncio.current_task()
            cleaned.set()
            return RingWriteReceipt(200, response_json={"result": {"code": 0}})

    gateway.unlock = late
    device = service.snapshot.find_device(42)
    approval = IntercomIntent(
        7, 42, device.revision, device.identity_epoch, light_group_monotonic_time()
    )
    result = await service.request_unlock(42, expected_revision=device.revision, intent=approval)
    assert result.status == Status.UNKNOWN and cleaned.is_set() and gateway.writes == 1
    assert service.snapshot.find_device(42).acknowledgment_id == result.operation_id
    assert service._operation_task is None
    await service.close()


@pytest.mark.asyncio
async def test_inflight_read_after_retirement_cannot_publish_private_inventory(state):
    service, gateway, _now, _wall, current = state
    gateway.read_gate = asyncio.Event()
    task = asyncio.create_task(service.refresh())
    while gateway.reads == 0:
        await asyncio.sleep(0)
    current[0] = False
    gateway.read_gate.set()
    await task
    assert service.snapshot.devices == ()
    await service.close()


@pytest.mark.asyncio
async def test_rate_limit_backoff_applies_to_manual_refresh_and_new_approval(state):
    service, gateway, now, *_ = state
    await ready(state)
    gateway.error = IntercomRestError("http-429", status=429, retry_after=300, possibly_sent=True)
    assert (await request(state)).status == Status.REJECTED
    assert service.snapshot.read_retry_after == now[0] + 300
    assert (await request(state)).code == "retry-later"
    await service.refresh(force=True)
    assert gateway.reads == gateway.writes == 1
