"""Offline adversarial tests for Chime service admission and lifecycle."""

import asyncio
from copy import deepcopy

import pytest

from halo_gtk.chime import (
    ChimeIntent,
    ChimeReadStatus,
    ChimeService,
)
from halo_gtk.chime import (
    ChimeOperationStatus as Status,
)
from halo_gtk.chime_rest import ChimeRestError
from halo_gtk.ring_json import RingWriteReceipt


def row(api_id=10, **changes):
    return {
        "id": api_id,
        "description": "Hall",
        "device_id": f"hardware-{api_id}",
        "kind": "chime",
        "owned": True,
        "settings": {"volume": 2},
        "alerts": {"connection": "online"},
        **changes,
    }


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Gateway:
    def __init__(self, rows=None):
        self.payload = {"chimes": rows if rows is not None else [row()]}
        self.reads = 0
        self.health_reads = 0
        self.writes = []
        self.before_admission = None
        self.after_admission = None
        self.read_hook = None
        self.health_hook = None
        self.receipt = RingWriteReceipt(204)
        self.write_error = None
        self.closed = False
        self.apply_volume = True

    async def read_inventory(self, *, timeout):
        self.reads += 1
        if self.read_hook:
            await self.read_hook()
        return deepcopy(self.payload)

    async def read_health(self, api_id, *, timeout):
        self.health_reads += 1
        if self.health_hook:
            await self.health_hook()
        return {"device_health": {"id": api_id, "latest_signal_strength": -39}}

    async def _write(self, api_id, value, kind, *, timeout, admission, on_admitted):
        if self.before_admission:
            await self.before_admission()
        admission()
        on_admitted()
        self.writes.append((api_id, value, kind))
        if self.after_admission:
            await self.after_admission()
        if self.write_error:
            raise self.write_error
        if value is not None and self.apply_volume:
            next(r for r in self.payload["chimes"] if r["id"] == api_id)["settings"]["volume"] = (
                value
            )
        return self.receipt

    async def set_legacy_volume(self, api_id, value, **kwargs):
        return await self._write(api_id, value, None, **kwargs)

    async def request_test_tone(self, api_id, kind, **kwargs):
        return await self._write(api_id, None, kind, **kwargs)

    async def close(self):
        self.closed = True


async def ready(gateway=None, **kwargs):
    clock = Clock()
    gateway = gateway or Gateway()
    service = ChimeService(gateway, generation=7, clock=clock, elapsed_clock=clock, **kwargs)
    await service.refresh()
    return service, gateway, clock


def intent_for(service, api_id=10):
    device = service.snapshot.find_device(api_id)
    return ChimeIntent(service.snapshot.generation, api_id, device.revision, device.identity_epoch)


async def volume(service, value=5, api_id=10, intent=None):
    intent = intent or intent_for(service, api_id)
    return await service.request_volume(
        api_id, value, expected_revision=intent.revision, intent=intent
    )


async def sound(service, api_id=10, intent=None):
    intent = intent or intent_for(service, api_id)
    return await service.request_test_sound(
        api_id, "ding", expected_revision=intent.revision, intent=intent
    )


@pytest.mark.asyncio
async def test_inventory_receipt_and_semantic_revision_distinct():
    service, gateway, clock = await ready()
    first = service.snapshot
    assert first.complete and first.devices[0].can_write
    clock.now += 6
    await service.refresh()
    assert gateway.reads == 2
    assert service.snapshot.revision > first.revision
    assert service.snapshot.devices[0].revision == first.devices[0].revision
    assert service.snapshot.devices[0].inventory_monotonic == 106
    gateway.payload["chimes"][0]["settings"]["volume"] = 3
    await service.refresh(force=True)
    assert service.snapshot.devices[0].revision > first.devices[0].revision
    await service.close()


@pytest.mark.asyncio
async def test_invalid_inventory_preserves_devices_and_disables_writes():
    service, gateway, _ = await ready()
    gateway.payload = {}
    await service.refresh(force=True)
    assert len(service.snapshot.devices) == 1
    assert not service.snapshot.complete
    assert service.snapshot.devices[0].stale
    assert not service.snapshot.devices[0].can_write
    assert (await volume(service)).status == Status.REJECTED
    assert not gateway.writes
    gateway.payload = {"chimes": []}
    await service.refresh(force=True)
    assert service.snapshot.complete and not service.snapshot.devices
    gateway.payload = {}
    await service.refresh(force=True)
    assert service.snapshot.has_inventory
    assert not service.snapshot.devices and not service.snapshot.complete
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,code",
    [
        ({"owned": False}, "ownership-unverified"),
        ({"owned": None}, "ownership-unverified"),
        ({"kind": "unknown"}, "unsupported-model"),
        ({"settings": {}}, "unsupported-volume"),
        ({"alerts": {"connection": "offline"}}, "offline"),
        ({"alerts": {}}, "connection-unknown"),
    ],
)
async def test_conservative_write_gate(changes, code):
    service, gateway, _ = await ready(Gateway([row(**changes)]))
    assert service.snapshot.devices[0].unavailable_reason == code
    assert (await volume(service)).code == code
    assert not gateway.writes
    await service.close()


@pytest.mark.asyncio
async def test_explicit_volume_reports_only_after_authoritative_readback():
    service, gateway, _ = await ready()
    result = await volume(service)
    assert result.status == Status.REPORTED and result.reported_volume == 5
    assert gateway.writes == [(10, 5, None)]
    assert gateway.reads == 2
    assert not service.snapshot.devices[0].operation.active
    await service.close()


@pytest.mark.asyncio
async def test_volume_no_match_has_two_readbacks_no_retry_or_rollback():
    service, gateway, _ = await ready()
    gateway.apply_volume = False
    result = await volume(service)
    assert result.status == Status.UNKNOWN
    assert len(gateway.writes) == 1 and gateway.reads == 3
    assert service.snapshot.devices[0].volume == 2
    assert service.snapshot.devices[0].stale
    assert not service.snapshot.devices[0].operation.active
    await service.close()


@pytest.mark.asyncio
async def test_2xx_incomplete_reply_still_uses_readback_not_put_retry():
    service, gateway, _ = await ready()
    gateway.receipt = RingWriteReceipt(200, False, "response-size-limit")
    assert (await volume(service)).status == Status.REPORTED
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
async def test_sound_gate_retained_until_exact_manual_ack_no_timer():
    service, gateway, clock = await ready()
    result = await sound(service)
    device = service.snapshot.devices[0]
    assert result.status == Status.ACCEPTED
    assert device.sound_operation_id == result.operation_id
    assert not device.can_write
    clock.now += 500
    await service.refresh(force=True)
    assert service.snapshot.devices[0].sound_operation_id == result.operation_id
    assert (await volume(service)).code == "sound-awaiting-confirmation"
    assert not await service.acknowledge_sound_finished(10, result.operation_id + 1)
    assert await service.acknowledge_sound_finished(10, result.operation_id)
    assert not await service.acknowledge_sound_finished(10, result.operation_id)
    assert service.snapshot.devices[0].can_write
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status",
    [
        (ChimeRestError("http-error", status=403, possibly_sent=True), Status.REJECTED),
        (ChimeRestError("request-timeout", possibly_sent=True), Status.UNKNOWN),
    ],
)
async def test_sound_uncertainty_and_rejection_never_replay_or_clear_gate(error, status):
    service, gateway, _ = await ready()
    gateway.write_error = error
    result = await sound(service)
    assert result.status == status
    assert len(gateway.writes) == 1
    assert service.snapshot.devices[0].sound_operation_id == result.operation_id
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("cancel", "cancelled"),
        ("volume", "device-changed"),
        ("owner", "device-changed"),
        ("offline", "device-changed"),
        ("kind", "device-changed"),
        ("hardware", "device-changed"),
        ("remove", "device-removed"),
        ("stale", "refresh-required"),
        ("session", "session-changed"),
    ],
)
async def test_final_admission_rechecks_after_transport_wait(mutation, expected):
    active = [True]
    service, gateway, clock = await ready(is_current=lambda: active[0])
    intent = intent_for(service)

    async def mutate():
        if mutation == "cancel":
            intent.cancel()
        elif mutation == "stale":
            clock.now += 31
        elif mutation == "session":
            active[0] = False
        else:
            r = gateway.payload["chimes"][0]
            if mutation == "volume":
                r["settings"]["volume"] = 8
            elif mutation == "owner":
                r["owned"] = False
            elif mutation == "offline":
                r["alerts"]["connection"] = "offline"
            elif mutation == "kind":
                r["kind"] = "chime_pro"
            elif mutation == "hardware":
                r["device_id"] = "replacement"
            elif mutation == "remove":
                gateway.payload["chimes"] = []
            await service.refresh(force=True)

    gateway.before_admission = mutate
    result = await volume(service, intent=intent)
    assert result.status == Status.REJECTED and result.code == expected
    assert not gateway.writes
    await service.close()


@pytest.mark.asyncio
async def test_suspend_aged_inventory_refreshes_before_send_without_receipt_revision_failure():
    service, gateway, clock = await ready()
    intent = intent_for(service)
    clock.now += 31
    assert not service.snapshot.devices[0].can_write
    result = await volume(service, intent=intent)
    assert result.status == Status.REPORTED
    assert gateway.reads == 3
    await service.close()


@pytest.mark.asyncio
async def test_identity_epoch_retired_on_removal_reappearance_and_reuse():
    service, gateway, _ = await ready()
    old = intent_for(service)
    gateway.payload = {"chimes": []}
    await service.refresh(force=True)
    gateway.payload = {"chimes": [row()]}
    await service.refresh(force=True)
    assert service.snapshot.devices[0].identity_epoch != old.identity_epoch
    assert (await volume(service, intent=old)).code == "device-changed"
    await service.close()


@pytest.mark.asyncio
async def test_details_reload_reads_volume_and_health_and_caches_separately():
    service, gateway, clock = await ready()
    await service.refresh_details(10)
    assert gateway.reads == 1 and gateway.health_reads == 1
    clock.now += 6
    await service.refresh_details(10)
    assert gateway.reads == 2 and gateway.health_reads == 1
    gateway.payload["chimes"][0]["settings"]["volume"] = 7
    await service.refresh_details(10, force=True)
    assert gateway.reads == 3 and gateway.health_reads == 2
    assert service.snapshot.devices[0].volume == 7
    clock.now += 61
    assert service.snapshot.devices[0].health.stale
    assert not service.snapshot.devices[0].can_write
    await service.close()


@pytest.mark.asyncio
async def test_coalesced_read_cancellation_detaches_only_consumer():
    gateway = Gateway()
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        started.set()
        await release.wait()

    gateway.read_hook = hold
    service = ChimeService(gateway, generation=7)
    first = asyncio.create_task(service.refresh())
    second = asyncio.create_task(service.refresh())
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert gateway.reads == 1
    release.set()
    assert (await second).complete
    assert not service._reads
    await service.close()


@pytest.mark.asyncio
async def test_final_read_consumer_cancel_cleans_shared_job():
    gateway = Gateway()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hold():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gateway.read_hook = hold
    service = ChimeService(gateway, generation=7)
    consumer = asyncio.create_task(service.refresh())
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.wait_for(cancelled.wait(), 1)
    await asyncio.sleep(0)
    assert not service._reads
    await service.close()


@pytest.mark.asyncio
async def test_global_two_intents_and_one_per_device_reject_busy_no_queue():
    service, gateway, _ = await ready(Gateway([row(i) for i in (10, 11, 12)]))
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def hold():
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        await release.wait()

    gateway.before_admission = hold
    first = asyncio.create_task(volume(service, api_id=10))
    second = asyncio.create_task(volume(service, api_id=11))
    await started.wait()
    assert (await volume(service, api_id=10)).code == "busy"
    assert (await volume(service, api_id=12)).code == "busy"
    release.set()
    await asyncio.gather(first, second)
    assert len(gateway.writes) == 2
    await service.close()


@pytest.mark.asyncio
async def test_view_close_after_admission_does_not_cancel_sound_or_discard_outcome():
    service, gateway, _ = await ready()
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        started.set()
        await release.wait()

    gateway.after_admission = hold
    intent = intent_for(service)
    caller = asyncio.create_task(sound(service, intent=intent))
    await started.wait()
    assert service.snapshot.devices[0].operation.possibly_sent
    assert not await service.acknowledge_sound_finished(
        10, service.snapshot.devices[0].sound_operation_id
    )
    intent.cancel()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.gather(*service._operations.values())
    assert service.snapshot.devices[0].operation.status == Status.ACCEPTED
    assert service.snapshot.devices[0].sound_operation_id is not None
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
async def test_close_marks_admitted_operation_unknown_and_cancels_owned_jobs():
    service, gateway, _ = await ready()
    started = asyncio.Event()

    async def hold():
        started.set()
        await asyncio.Event().wait()

    gateway.after_admission = hold
    caller = asyncio.create_task(sound(service))
    await started.wait()
    await service.close()
    assert (await caller).status == Status.UNKNOWN
    assert gateway.closed
    assert not service._operations and not service._reads
    assert service.snapshot.status == ChimeReadStatus.CLOSED
    assert not service.snapshot.devices[0].operation.active
    assert service.snapshot.devices[0].sound_operation_id is not None


@pytest.mark.asyncio
async def test_error_text_and_callback_exception_do_not_escape_into_state_or_duplicate_send():
    service, gateway, _ = await ready(
        on_snapshot=lambda snapshot: (_ for _ in ()).throw(RuntimeError("UI failed"))
    )
    gateway.write_error = RuntimeError("https://private.example/?token=secret")
    result = await sound(service)
    assert result.code == "request-failed" and result.status == Status.UNKNOWN
    assert "secret" not in repr(service.snapshot)
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_rapid_reopen_waits_for_canceling_old_read_and_starts_replacement(late_error):
    gateway = Gateway()
    started, cancel_started, finish_cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold():
        if gateway.reads == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_started.set()
                await finish_cancel.wait()
                if late_error:
                    raise ChimeRestError("retired-read-error") from None
                # Deliberately return a late invalid response after cancellation.
                gateway.payload = {"chimes": [row(description="Retired response")]}

    gateway.read_hook = hold
    service = ChimeService(gateway, generation=7)
    old = asyncio.create_task(service.refresh())
    await started.wait()
    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old
    await cancel_started.wait()
    reopened = asyncio.create_task(service.refresh())
    await asyncio.sleep(0)
    assert gateway.reads == 1
    finish_cancel.set()
    assert (await reopened).complete
    assert gateway.reads == 2
    # Only replacement publishes; old suppressed cancellation is not authority.
    assert service.snapshot.revision == 1
    await service.close()


@pytest.mark.asyncio
async def test_details_read_bound_is_four_distinct_keys():
    service, gateway, _ = await ready(Gateway([row(i) for i in range(10, 15)]))
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        if gateway.health_reads == 4:
            started.set()
        await release.wait()

    gateway.health_hook = hold
    pending = [asyncio.create_task(service.refresh_details(i)) for i in range(10, 14)]
    await started.wait()
    snapshot = await service.refresh_details(14)
    assert snapshot.find_device(14).health.code == "busy"
    assert gateway.health_reads == 4
    release.set()
    await asyncio.gather(*pending)
    await service.close()


@pytest.mark.asyncio
async def test_late_health_does_not_attach_to_replacement_incarnation():
    service, gateway, _ = await ready()
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A deliberately cancellation-resistant fake still cannot publish.
            await release.wait()

    gateway.health_hook = hold
    pending = asyncio.create_task(service.refresh_details(10))
    await started.wait()
    gateway.payload["chimes"][0]["device_id"] = "new-hardware"
    await service.refresh(force=True)
    release.set()
    await pending
    assert service.snapshot.devices[0].health is None
    await service.close()


@pytest.mark.asyncio
async def test_view_cancel_before_admission_releases_pending_operation():
    service, gateway, _ = await ready()
    started = asyncio.Event()

    async def hold():
        started.set()
        await asyncio.Event().wait()

    gateway.before_admission = hold
    intent = intent_for(service)
    pending = asyncio.create_task(sound(service, intent=intent))
    await started.wait()
    intent.cancel()
    result = await asyncio.wait_for(pending, 1)
    assert result.status == Status.REJECTED and result.code == "cancelled"
    assert not gateway.writes and not service._operations
    assert service.snapshot.devices[0].sound_operation_id is None
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,budget", [("volume", 30.0), ("sound", 15.0)])
async def test_whole_operation_budget_retains_uncertain_sent_outcome(monkeypatch, kind, budget):
    service, gateway, _ = await ready()
    real_timeout = asyncio.timeout
    budgets = []

    def short_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(0.01)

    async def hold():
        await asyncio.Event().wait()

    monkeypatch.setattr("halo_gtk.chime.asyncio.timeout", short_timeout)
    gateway.after_admission = hold
    result = await (volume(service) if kind == "volume" else sound(service))
    assert budgets == [budget]
    assert result.status == Status.UNKNOWN
    assert len(gateway.writes) == 1 and not service._operations
    if kind == "sound":
        assert service.snapshot.devices[0].sound_operation_id == result.operation_id
    else:
        assert service.snapshot.devices[0].stale
    await service.close()


@pytest.mark.asyncio
async def test_inventory_started_before_write_cannot_confirm_volume():
    service, gateway, _ = await ready()
    gateway.apply_volume = False
    write_started, finish_write, old_read_started, finish_old_read = (
        asyncio.Event() for _ in range(4)
    )

    async def hold_write():
        write_started.set()
        await finish_write.wait()

    original_read = gateway.read_inventory

    async def old_report(*, timeout):
        if gateway.reads == 1:
            gateway.reads += 1
            old_read_started.set()
            await finish_old_read.wait()
            return {"chimes": [row(settings={"volume": 5})]}
        return await original_read(timeout=timeout)

    gateway.read_inventory = old_report
    gateway.after_admission = hold_write
    operation = asyncio.create_task(volume(service))
    await write_started.wait()
    assert service.snapshot.devices[0].volume == 2
    old_read = asyncio.create_task(service.refresh(force=True))
    await old_read_started.wait()
    finish_write.set()
    await asyncio.sleep(0)
    finish_old_read.set()
    await old_read
    result = await operation
    assert result.status == Status.UNKNOWN
    assert gateway.reads == 4  # Initial, old concurrent read, two post-write reads.
    assert len(gateway.writes) == 1
    await service.close()
