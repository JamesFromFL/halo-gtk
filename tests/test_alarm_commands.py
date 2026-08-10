"""Safety and confirmation tests for Ring Alarm mode commands."""

import asyncio
from contextlib import contextmanager
from itertools import count

import pytest

from halo_gtk.alarm.models import (
    AlarmAssetSnapshot,
    AlarmCapability,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmLocationSnapshot,
    AlarmMode,
    AlarmPhase,
    AlarmWriteAuthorization,
)
from halo_gtk.alarm.protocol import DeviceDocument, DeviceInfoDocList, DeviceInfoDocUpdate
from halo_gtk.alarm.provider import (
    AlarmCommandStatus,
    AlarmCommandSupersededError,
    AlarmLocationSeed,
)
from halo_gtk.alarm.service import RingAlarmService
from halo_gtk.alarm.transport import AlarmTicketAsset, AlarmTransportError


def _panel(
    mode="none",
    *,
    alarm_state=None,
    faulted_devices=None,
    transition_deadline=None,
):
    data = {
        "zid": "panel-1",
        "name": "Security Panel",
        "deviceType": "security-panel",
        "mode": mode,
    }
    if alarm_state is not None or faulted_devices is not None:
        data["alarmInfo"] = {}
        if alarm_state is not None:
            data["alarmInfo"]["state"] = alarm_state
        if faulted_devices is not None:
            data["alarmInfo"]["faultedDevices"] = list(faulted_devices)
    if transition_deadline is not None:
        data["transitionDelayEndTimestamp"] = transition_deadline
    return DeviceDocument(zid="panel-1", data=data)


def _contact(*, faulted):
    return DeviceDocument(
        zid="contact-1",
        data={
            "zid": "contact-1",
            "name": "Entry Sensor",
            "deviceType": "sensor.contact",
            "faulted": faulted,
        },
    )


def _foreign_zid_collision(*, mode="none", faulted=False):
    return DeviceDocument(
        zid="panel-1",
        data={
            "zid": "panel-1",
            "name": "Foreign Sensor",
            "deviceType": "sensor.contact",
            "mode": mode,
            "faulted": faulted,
        },
    )


async def _wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.001)


class _Gateway:
    async def request_json(self, _url, **_kwargs):
        raise AssertionError("fake transport does not use REST")


class _CommandTransport:
    def __init__(self, documents, *, send_behavior="confirm"):
        self.has_foreign_collision = send_behavior in {
            "foreign-confirm",
            "reconcile-foreign-collision",
        }
        self.assets = (
            (
                AlarmTicketAsset("aaa-bridge", "beams_bridge_v1", "online"),
                AlarmTicketAsset("asset-1", "base_station_v1", "online"),
            )
            if self.has_foreign_collision
            else (AlarmTicketAsset("asset-1", "base_station_v1", "online"),)
        )
        self.documents = tuple(documents)
        self.send_behavior = send_behavior
        self.connected = False
        self.closed = False
        self.send_calls = []
        self.inventory_requests = []
        self.on_message = None
        self.epoch = 0
        self.send_started = asyncio.Event()

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.connected = True
        self.epoch = epoch
        self.on_message = on_message
        try:
            await on_open(self.assets)
            await on_message(DeviceInfoDocList("asset-1", self.documents))
            if self.has_foreign_collision:
                await on_message(DeviceInfoDocList("aaa-bridge", (_foreign_zid_collision(),)))
            await stop_event.wait()
        finally:
            self.connected = False

    async def send_mode(
        self,
        asset_id,
        panel_zid,
        mode,
        *,
        epoch,
        bypass_ids=(),
    ):
        self.send_started.set()
        self.send_calls.append((asset_id, panel_zid, mode, epoch, bypass_ids))
        if self.send_behavior == "block":
            await asyncio.Event().wait()
        if self.send_behavior == "error":
            raise AlarmTransportError("websocket-send-failed")
        if self.send_behavior == "permission-error":
            raise AlarmTransportError("permission-denied")
        if self.send_behavior == "send-timeout-error":
            raise AlarmTransportError("websocket-send-timeout")
        if self.send_behavior == "unrelated":
            await self.on_message(DeviceInfoDocUpdate("asset-1", (_panel("none"),)))
            return
        if self.send_behavior == "foreign-confirm":
            raw_mode = {
                AlarmMode.DISARMED: "none",
                AlarmMode.HOME: "some",
                AlarmMode.AWAY: "all",
            }[mode]
            await self.on_message(
                DeviceInfoDocUpdate(
                    "aaa-bridge",
                    (_foreign_zid_collision(mode=raw_mode, faulted=True),),
                )
            )
            return
        if self.send_behavior == "confirm":
            raw_mode = {
                AlarmMode.DISARMED: "none",
                AlarmMode.HOME: "some",
                AlarmMode.AWAY: "all",
            }[mode]
            # Deliver before send_mode returns. The waiter must already exist.
            await self.on_message(DeviceInfoDocUpdate("asset-1", (_panel(raw_mode),)))

    async def request_inventory(self, asset_id, *, epoch):
        self.inventory_requests.append((asset_id, epoch))
        if self.send_behavior == "reconcile-unrelated":
            await self.on_message(DeviceInfoDocUpdate("asset-1", (_contact(faulted=True),)))
        if self.send_behavior == "reconcile-foreign-collision":
            await self.on_message(
                DeviceInfoDocUpdate(
                    "aaa-bridge",
                    (_foreign_zid_collision(mode="all", faulted=True),),
                )
            )

    async def close(self):
        self.closed = True
        self.connected = False


async def _start_service(documents, *, send_behavior="confirm", **service_kwargs):
    transport = _CommandTransport(documents, send_behavior=send_behavior)
    service = RingAlarmService(
        _Gateway(),
        (
            AlarmLocationSeed(
                "location-1",
                write_authorization=service_kwargs.pop(
                    "write_authorization",
                    AlarmWriteAuthorization.ALLOWED,
                ),
            ),
        ),
        lambda _snapshot: None,
        transport_factory=lambda _request, _seed: transport,
        command_timeout=service_kwargs.pop("command_timeout", 0.02),
        reconcile_timeout=service_kwargs.pop("reconcile_timeout", 0),
        stable_connection_time=0,
        poll_interval=0.001,
        **service_kwargs,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    await _wait_until(
        lambda: (
            (location := service.snapshot.find_location("location-1")) is not None
            and bool(location.devices)
        )
    )
    return service, transport, stop, task


async def _stop(stop, task):
    stop.set()
    await task


@pytest.mark.asyncio
async def test_waiter_is_registered_before_send_and_confirms_exact_target_update():
    service, transport, stop, task = await _start_service((_panel(),))
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.HOME,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.CONFIRMED
    assert result.confirmed_mode is AlarmMode.HOME
    assert len(transport.send_calls) == 1
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_already_target_is_confirmed_without_sending():
    service, transport, stop, task = await _start_service((_panel("some"),))
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.HOME,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.CONFIRMED
    assert result.code == "already-set"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_unrelated_panel_update_does_not_confirm_and_command_is_never_retried():
    service, transport, stop, task = await _start_service(
        (_panel(),),
        send_behavior="unrelated",
        command_timeout=0.005,
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN
    assert len(transport.send_calls) == 1
    assert transport.inventory_requests == [("asset-1", 1)]
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_same_zid_update_from_foreign_asset_does_not_confirm_command():
    service, transport, stop, task = await _start_service(
        (_panel(),),
        send_behavior="foreign-confirm",
        command_timeout=0.005,
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN
    assert len(transport.send_calls) == 1
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_ambiguous_reconcile_ignores_unrelated_sensor_revision():
    service, transport, stop, task = await _start_service(
        (_panel(), _contact(faulted=False)),
        send_behavior="reconcile-unrelated",
        command_timeout=0.005,
        reconcile_timeout=0.1,
    )
    location = service.snapshot.find_location("location-1")
    command = asyncio.create_task(
        service.request_mode(
            "location-1",
            AlarmMode.AWAY,
            expected_revision=location.revision,
        )
    )

    await _wait_until(lambda: bool(transport.inventory_requests))
    await asyncio.sleep(0.005)
    assert command.done() is False

    await transport.on_message(DeviceInfoDocUpdate("asset-1", (_panel("all"),)))
    result = await command
    assert result.status is AlarmCommandStatus.CONFIRMED
    assert result.code == "confirmed-after-reconcile"
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_ambiguous_reconcile_uses_panel_asset_when_zids_collide():
    service, transport, stop, task = await _start_service(
        (_panel(),),
        send_behavior="reconcile-foreign-collision",
        command_timeout=0.005,
        reconcile_timeout=0.1,
    )
    location = service.snapshot.find_location("location-1")
    command = asyncio.create_task(
        service.request_mode(
            "location-1",
            AlarmMode.AWAY,
            expected_revision=location.revision,
        )
    )

    await _wait_until(lambda: bool(transport.inventory_requests))
    await asyncio.sleep(0.005)
    assert command.done() is False

    await transport.on_message(DeviceInfoDocUpdate("asset-1", (_panel("all"),)))
    result = await command
    assert result.status is AlarmCommandStatus.CONFIRMED
    assert result.code == "confirmed-after-reconcile"
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_send_failure_is_ambiguous_and_not_replayed():
    service, transport, stop, task = await _start_service((_panel(),), send_behavior="error")
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN
    assert result.code == "websocket-send-failed"
    assert len(transport.send_calls) == 1
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_send_timeout_is_ambiguous_without_retry_and_releases_admission():
    admission_held = False

    @contextmanager
    def admission():
        nonlocal admission_held
        admission_held = True
        try:
            yield
        finally:
            admission_held = False

    service, transport, stop, task = await _start_service(
        (_panel(),),
        send_behavior="send-timeout-error",
        command_admission=admission,
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN
    assert result.code == "websocket-send-timeout"
    assert admission_held is False
    assert len(transport.send_calls) == 1
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_transport_permission_text_is_not_claimed_as_permission_denied():
    service, transport, stop, task = await _start_service(
        (_panel(),),
        send_behavior="permission-error",
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN
    assert result.code == "permission-denied"
    assert len(transport.send_calls) == 1
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_stale_revision_is_rejected_before_send():
    service, transport, stop, task = await _start_service((_panel(),))
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision - 1,
    )

    assert result.status is AlarmCommandStatus.STALE_REVISION
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_faulted_sensor_requires_exact_explicit_revision_checked_bypass():
    service, transport, stop, task = await _start_service((_panel(), _contact(faulted=True)))
    location = service.snapshot.find_location("location-1")

    needs_bypass = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )
    assert needs_bypass.status is AlarmCommandStatus.NEEDS_BYPASS
    assert needs_bypass.bypass_ids == ("contact-1",)
    assert transport.send_calls == []

    confirmed = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
        bypass_zids=needs_bypass.bypass_ids,
    )
    assert confirmed.status is AlarmCommandStatus.CONFIRMED
    assert transport.send_calls[0][-1] == ("contact-1",)
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_more_than_wire_limit_bypass_ids_are_rejected_without_send():
    service, transport, stop, task = await _start_service((_panel(),))
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
        bypass_zids=tuple(f"sensor-{index}" for index in range(257)),
    )

    assert result.status is AlarmCommandStatus.NEEDS_BYPASS
    assert result.code == "invalid-bypass-set"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass_zids", ["contact-1", (str(index) for index in count())])
async def test_invalid_or_unbounded_bypass_iterables_are_consumed_safely(bypass_zids):
    service, transport, stop, task = await _start_service((_panel(),))
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
        bypass_zids=bypass_zids,
    )

    assert result.status is AlarmCommandStatus.NEEDS_BYPASS
    assert result.code == "invalid-bypass-set"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_authoritative_fault_list_over_wire_limit_is_read_only():
    service, transport, stop, task = await _start_service(
        (
            _panel(
                alarm_state="none",
                faulted_devices=tuple(f"sensor-{index}" for index in range(257)),
            ),
        )
    )
    location = service.snapshot.find_location("location-1")
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "faulted-device-list-invalid"

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "faulted-device-list-invalid"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_panel_fault_list_is_authoritative_for_bypass_requirement():
    service, transport, stop, task = await _start_service(
        (
            _panel(alarm_state="none", faulted_devices=("contact-1",)),
            _contact(faulted=False),
        )
    )
    location = service.snapshot.find_location("location-1")

    needs_bypass = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )
    assert needs_bypass.status is AlarmCommandStatus.NEEDS_BYPASS
    assert needs_bypass.bypass_ids == ("contact-1",)
    assert transport.send_calls == []

    confirmed = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
        bypass_zids=needs_bypass.bypass_ids,
    )
    assert confirmed.status is AlarmCommandStatus.CONFIRMED
    assert transport.send_calls[0][-1] == ("contact-1",)
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_panel_fault_list_cannot_bypass_unknown_or_unsafe_device():
    service, transport, stop, task = await _start_service(
        (_panel(alarm_state="none", faulted_devices=("missing-device",)),)
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "faulted-device-unresolved"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("authoritative", [True, False])
async def test_unknown_bypass_capable_kind_is_never_sent_as_bypass(authoritative):
    unknown = DeviceDocument(
        zid="future-1",
        data={
            "zid": "future-1",
            "deviceType": "vendor.future-sensor",
            "faulted": True,
            "bypassStatus": "not-bypassed",
        },
    )
    service, transport, stop, task = await _start_service(
        (
            _panel(
                alarm_state="none",
                faulted_devices=("future-1",) if authoritative else None,
            ),
            unknown,
        )
    )
    location = service.snapshot.find_location("location-1")
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "faulted-device-unresolved"

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "faulted-device-unresolved"
    assert transport.send_calls == []
    await _stop(stop, task)


def test_panel_fault_list_cannot_match_same_zid_from_foreign_asset():
    panel = AlarmDeviceSnapshot(
        "location-1",
        "panel-asset",
        "panel-1",
        kind=AlarmDeviceKind.SECURITY_PANEL,
        faulted_device_ids=("collision",),
    )
    foreign = AlarmDeviceSnapshot(
        "location-1",
        "bridge-asset",
        "collision",
        kind=AlarmDeviceKind.CONTACT_SENSOR,
        capabilities=frozenset({AlarmCapability.BYPASS}),
    )
    location = AlarmLocationSnapshot(
        "location-1",
        assets=(
            AlarmAssetSnapshot("location-1", "panel-asset", devices=(panel,)),
            AlarmAssetSnapshot("location-1", "bridge-asset", devices=(foreign,)),
        ),
    )
    service = RingAlarmService(_Gateway(), (), lambda _snapshot: None)

    assert service._faulted_bypass_ids(location) is None


@pytest.mark.asyncio
async def test_active_inferred_life_safety_device_blocks_all_mode_sends():
    active_smoke = DeviceDocument(
        zid="future-alarm",
        data={
            "zid": "future-alarm",
            "deviceType": "vendor.future-life-safety",
            "components": {
                "alarm.smoke": {"alarmStatus": "active"},
                "alarm.co": {"alarmStatus": "inactive"},
            },
        },
    )
    service, transport, stop, task = await _start_service((_panel(), active_smoke))
    location = service.snapshot.find_location("location-1")
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "life-safety-active"

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "life-safety-active"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_active_alarm_is_read_only_even_with_fresh_complete_inventory():
    service, transport, stop, task = await _start_service((_panel(alarm_state="burglar-alarm"),))
    location = service.snapshot.find_location("location-1")
    assert location.phase is AlarmPhase.ALARMING

    result = await service.request_mode(
        "location-1",
        AlarmMode.DISARMED,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "active-alarm"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_future_panel_state_is_read_only_and_not_reported_inactive():
    service, transport, stop, task = await _start_service(
        (_panel(alarm_state="future-alarm-state"),)
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "panel-state-unknown"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_multiple_panels_are_never_selected_for_command():
    second_panel = DeviceDocument(
        zid="panel-2",
        data={
            "zid": "panel-2",
            "name": "Second Security Panel",
            "deviceType": "security-panel",
            "mode": "none",
        },
    )
    service, transport, stop, task = await _start_service((_panel(), second_panel))
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "multiple-panels"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_unknown_write_authorization_never_reaches_transport():
    service, transport, stop, task = await _start_service(
        (_panel(),),
        write_authorization=AlarmWriteAuthorization.UNKNOWN,
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "authorization-unknown"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_explicit_denied_authorization_returns_permission_denied():
    service, transport, stop, task = await _start_service(
        (_panel(),),
        write_authorization=AlarmWriteAuthorization.DENIED,
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.PERMISSION_DENIED
    assert result.code == "authorization-denied"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "panel",
    [
        _panel("all", alarm_state="entry-delay"),
        _panel("some", transition_deadline=1_900_000_000_000),
    ],
)
async def test_disarm_is_allowed_during_recognized_transition(panel):
    service, transport, stop, task = await _start_service((panel,))
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.DISARMED,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.CONFIRMED
    assert len(transport.send_calls) == 1
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_arm_is_rejected_during_recognized_transition():
    service, transport, stop, task = await _start_service(
        (_panel("all", alarm_state="entry-delay"),)
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.HOME,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE
    assert result.code == "transition-in-progress"
    assert transport.send_calls == []
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_send_boundary_rejection_is_superseded_without_sending():
    @contextmanager
    def reject_replaced_client():
        raise AlarmCommandSupersededError("client-replaced")
        yield

    service, transport, stop, task = await _start_service(
        (_panel(),),
        command_admission=reject_replaced_client,
    )
    location = service.snapshot.find_location("location-1")

    result = await service.request_mode(
        "location-1",
        AlarmMode.AWAY,
        expected_revision=location.revision,
    )

    assert result.status is AlarmCommandStatus.SUPERSEDED
    assert result.code == "session-superseded"
    assert transport.send_calls == []
    assert service._pending == {}
    await _stop(stop, task)


@pytest.mark.asyncio
async def test_cancellation_during_blocked_send_removes_pending_waiter():
    service, transport, stop, task = await _start_service(
        (_panel(),),
        send_behavior="block",
    )
    location = service.snapshot.find_location("location-1")
    command = asyncio.create_task(
        service.request_mode(
            "location-1",
            AlarmMode.AWAY,
            expected_revision=location.revision,
        )
    )
    await transport.send_started.wait()

    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command

    assert service._pending == {}
    assert len(transport.send_calls) == 1
    await _stop(stop, task)
