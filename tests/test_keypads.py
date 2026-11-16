"""Fixture-only keypad policy, protocol, observation and command regressions."""

import asyncio
import json
from contextlib import contextmanager, nullcontext

import pytest

from halo_gtk.alarm.keypads import KeypadIntent
from halo_gtk.alarm.models import (
    AlarmCapability,
    AlarmConnectionStatus,
    AlarmWriteAuthorization,
    TriState,
)
from halo_gtk.alarm.protocol import (
    AlarmSchemaError,
    AssetSessionInfo,
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    SessionInfo,
    encode_keypad_brightness,
    parse_clap_frame,
)
from halo_gtk.alarm.provider import AlarmCommandSupersededError, AlarmLocationSeed
from halo_gtk.alarm.store import keypad_brightness
from halo_gtk.alarm.transport import AlarmTicketAsset, RingAlarmTransport
from halo_gtk.keypad_preferences import KeypadPreference, KeypadPreferences
from tests.test_alarm_commands import _panel, _start_service, _stop
from tests.test_ring_alarm_transport import _Gateway, _WebSocket

IDENTITY = ("location-1", "asset-1", "keypad-1")


def keypad(*, motion=False, **fields):
    return DeviceDocument("keypad-1", {"zid": "keypad-1", **fields}, keypad_motion=motion)


def full_keypad(**fields):
    return keypad(deviceType="security-keypad", brightness=0.5, commStatus="ok", **fields)


def device(service):
    return service.snapshot.find_location(IDENTITY[0]).find_device(IDENTITY[2])


async def request(service, value=75, *, intent=None, revision=None):
    return await service.keypads.request(
        *IDENTITY,
        value,
        revision=device(service).revision if revision is None else revision,
        intent=intent or KeypadIntent(lambda: True),
    )


def motion_frame(*, list_message=False, general_zid="keypad-1", impulses=None):
    return json.dumps(
        {
            "channel": "message" if list_message else "DataUpdate",
            "msg": {
                "msg": "DeviceInfoDocGetList" if list_message else "DataUpdate",
                "datatype": "DeviceInfoDocType",
                "src": "asset-1",
                "body": [
                    {
                        "general": {"v2": {"zid": general_zid}},
                        "device": {"v1": {"zid": "keypad-1"}},
                        "impulse": {
                            "v1": impulses
                            or [{"impulseType": "ignored"}, {"impulseType": "keypad.motion"}]
                        },
                    }
                ],
            },
        }
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (1, 100),
        (0.125, 13),
        (0.995, 100),
        (True, None),
        ("0.5", None),
        (101, None),
        (-0.01, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_brightness_observation_is_exact_normalized_finite_range(raw, expected):
    assert keypad_brightness(raw) == expected


@pytest.mark.parametrize("value", [True, "50", 0.5, -1, 101, None])
def test_encoder_rejects_non_integer_or_out_of_range_targets(value):
    with pytest.raises(ValueError):
        encode_keypad_brightness("hub", "keypad", value, 1)


def test_encoder_has_only_qualified_led_setting():
    assert encode_keypad_brightness("hub", "keypad", 25, 4) == {
        "channel": "message",
        "msg": {
            "msg": "DeviceInfoSet",
            "datatype": "DeviceInfoSetType",
            "dst": "hub",
            "body": [{"zid": "keypad", "device": {"v1": {"brightness": 0.25}}}],
            "seq": 4,
        },
    }


def test_motion_requires_realtime_matching_general_identity_and_discards_raw_impulse():
    parsed = parse_clap_frame(motion_frame())
    assert parsed.devices[0].keypad_motion and "impulse" not in parsed.devices[0].data
    assert not parse_clap_frame(motion_frame(list_message=True)).devices[0].keypad_motion
    with pytest.raises(AlarmSchemaError):
        parse_clap_frame(motion_frame(general_zid="other"))
    assert (
        not parse_clap_frame(motion_frame(impulses=[{"impulseType": "motion"}]))
        .devices[0]
        .keypad_motion
    )


def test_permission_defaults_private_persistence_account_scope_and_revision(tmp_path):
    path = tmp_path / "settings.json"
    prefs = KeypadPreferences(path)
    assert prefs.get("owner@example.test", *IDENTITY) == KeypadPreference()
    allowed = prefs.set("owner@example.test", *IDENTITY, enabled=True, locked=False)
    assert allowed.allowed and allowed.revision == 1
    assert KeypadPreferences(path).get("owner@example.test", *IDENTITY) == allowed
    assert not prefs.get("another@example.test", *IDENTITY).allowed
    assert path.stat().st_mode & 0o777 == 0o600
    assert "owner@example.test" not in path.read_text() and "keypad-1" not in path.read_text()
    assert not prefs.set("owner@example.test", *IDENTITY, enabled=True, locked=True).allowed


@pytest.mark.parametrize(
    "body",
    [
        "{bad",
        '{"schema":1,"schema":1,"keypads":{}}',
        '{"schema":true,"keypads":{}}',
        '{"schema":1,"keypads":[]}',
    ],
)
def test_malformed_policy_fails_closed(tmp_path, body):
    path = tmp_path / "settings.json"
    path.write_text(body)
    assert not KeypadPreferences(path).get("owner", *IDENTITY).allowed


def test_failed_lock_persistence_still_revokes_process_permission(tmp_path, monkeypatch):
    prefs = KeypadPreferences(tmp_path / "settings.json")
    prefs.set("owner", *IDENTITY, enabled=True, locked=False)

    def fail(*_args, **_kwargs):
        raise OSError("fixture")

    monkeypatch.setattr("halo_gtk.keypad_preferences.atomic_write_text", fail)
    with pytest.raises(OSError):
        prefs.set("owner", *IDENTITY, enabled=True, locked=True)
    assert not prefs.get("owner", *IDENTITY).allowed


def test_preference_revision_limit_is_locked_on_load_and_cannot_overflow(tmp_path):
    path = tmp_path / "settings.json"
    prefs = KeypadPreferences(path)
    prefs.set("owner", *IDENTITY, enabled=True, locked=False)
    payload = json.loads(path.read_text())
    next(iter(payload["keypads"].values()))["revision"] = 2**53 - 1
    path.write_text(json.dumps(payload))
    prefs = KeypadPreferences(path)
    assert not prefs.get("owner", *IDENTITY).allowed
    with pytest.raises(ValueError, match="revision limit"):
        prefs.set("owner", *IDENTITY, enabled=True, locked=False)
    assert not prefs.get("owner", *IDENTITY).allowed


async def test_keypad_never_uses_generic_motion_and_needs_genuine_realtime_event():
    now = [100.0]
    service, transport, stop, task = await _start_service(
        (_panel(), full_keypad(motionStatus="clear")), monotonic_clock=lambda: now[0]
    )
    try:
        assert device(service).keypad_brightness == 50
        assert AlarmCapability.KEYPAD_BRIGHTNESS in device(service).capabilities
        assert AlarmCapability.MOTION not in device(service).capabilities
        assert device(service).motion is device(service).keypad_proximity is TriState.UNKNOWN
        await transport.on_message(
            DeviceInfoDocList("asset-1", (_panel(), full_keypad(motion=True)))
        )
        assert device(service).keypad_proximity is TriState.UNKNOWN
        await transport.on_message(parse_clap_frame(motion_frame()))
        assert device(service).keypad_proximity is TriState.ACTIVE
        now[0] += 19
        await transport.on_message(parse_clap_frame(motion_frame()))
        now[0] += 2
        service._publish()
        assert device(service).keypad_proximity is TriState.ACTIVE
        now[0] += 18
        service._publish()
        assert device(service).keypad_proximity is TriState.INACTIVE
        await transport.on_message(
            SessionInfo((AssetSessionInfo("asset-1", AlarmConnectionStatus.OFFLINE),))
        )
        assert device(service).keypad_proximity is TriState.UNKNOWN
        assert not service.keypads._observed and not service.keypads._motion_tasks
    finally:
        await _stop(stop, task)


async def test_command_confirms_only_post_handoff_observed_brightness_once():
    service, transport, stop, task = await _start_service((_panel(), full_keypad()))
    calls = []

    async def send(asset, zid, value, *, epoch, admission):
        with admission():
            calls.append((asset, zid, value, epoch))
            await transport.on_message(
                DeviceInfoDocUpdate(asset, (keypad(brightness=value / 100),))
            )

    transport.send_keypad_brightness = send
    try:
        result = await request(service)
        assert result.status == "confirmed" and result.possibly_sent
        assert len(calls) == 1 and device(service).keypad_brightness == 75
        assert not transport.send_calls and not service.keypads._pending
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize("communication", ["down", "error", None])
async def test_matching_brightness_with_offline_or_unknown_communication_never_confirms(
    communication,
):
    service, transport, stop, task = await _start_service((_panel(), full_keypad()))
    service.keypads._timeout = 0.01

    async def send(asset, zid, value, *, epoch, admission):
        with admission():
            await transport.on_message(
                DeviceInfoDocUpdate(
                    asset, (keypad(brightness=value / 100, commStatus=communication),)
                )
            )

    transport.send_keypad_brightness = send
    try:
        result = await request(service)
        assert result.status == "unknown" and result.possibly_sent
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize(
    "change", ["account", "runtime", "authorization", "brightness", "local-lock"]
)
async def test_confirmation_rechecks_current_account_runtime_and_report_after_wakeup(change):
    current = [True]

    @contextmanager
    def guard():
        if not current[0]:
            raise AlarmCommandSupersededError()
        yield

    service, transport, stop, task = await _start_service(
        (_panel(), full_keypad()), command_admission=guard
    )
    intent = KeypadIntent(lambda: True)

    async def send(asset, zid, value, *, epoch, admission):
        with admission():
            await transport.on_message(
                DeviceInfoDocUpdate(asset, (keypad(brightness=value / 100),))
            )
        if change == "account":
            current[0] = False
        elif change == "runtime":
            service._runtimes["location-1"].epoch += 1
        elif change == "authorization":
            service._store.register_location(
                "location-1", write_authorization=AlarmWriteAuthorization.UNKNOWN
            )
        elif change == "brightness":
            await transport.on_message(DeviceInfoDocUpdate(asset, (keypad(brightness=0.1),)))
        else:
            intent.cancel()

    transport.send_keypad_brightness = send
    try:
        result = await request(service, intent=intent)
        assert result.status == ("confirmed" if change == "local-lock" else "unknown")
        assert result.possibly_sent
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize(
    "condition", ["locked", "stale", "revision", "authorization", "brightness", "communication"]
)
async def test_unqualified_requests_never_reach_socket(condition):
    now = [100.0]
    service, transport, stop, task = await _start_service(
        (_panel(), full_keypad()), monotonic_clock=lambda: now[0]
    )
    calls = []

    async def send(*_args, **_kwargs):
        calls.append(True)

    transport.send_keypad_brightness = send
    try:
        intent = KeypadIntent(lambda: condition != "locked")
        revision = device(service).revision
        if condition == "stale":
            now[0] += 121
        elif condition == "revision":
            revision -= 1
        elif condition == "authorization":
            service._store.register_location(
                "location-1", write_authorization=AlarmWriteAuthorization.UNKNOWN
            )
        elif condition in {"brightness", "communication"}:
            field = "brightness" if condition == "brightness" else "commStatus"
            await transport.on_message(DeviceInfoDocUpdate("asset-1", (keypad(**{field: None}),)))
            revision = device(service).revision
        result = await request(service, intent=intent, revision=revision)
        assert result.status == "rejected" and not result.possibly_sent and not calls
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize("change", ["cancel", "stale", "epoch"])
async def test_queued_command_rechecks_intent_freshness_and_epoch_before_send(change):
    now = [100.0]
    service, transport, stop, task = await _start_service(
        (_panel(), full_keypad()), monotonic_clock=lambda: now[0]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    intent = KeypadIntent(lambda: True)

    async def send(*_args, admission, **_kwargs):
        entered.set()
        await release.wait()
        with admission():
            sent.append(True)

    transport.send_keypad_brightness = send
    try:
        pending = asyncio.create_task(request(service, intent=intent))
        await entered.wait()
        assert (await request(service)).code == "busy"
        if change == "cancel":
            intent.cancel()
        elif change == "stale":
            now[0] += 121
        else:
            service._runtimes["location-1"].epoch += 1
        release.set()
        result = await pending
        assert result.status == "rejected" and not result.possibly_sent and not sent
    finally:
        release.set()
        await _stop(stop, task)


async def test_ack_without_readback_is_unknown_and_needs_fresh_brightness_before_retry():
    service, transport, stop, task = await _start_service((_panel(), full_keypad()))
    service.keypads._timeout = 0.01
    calls = []

    async def send(*_args, admission, **_kwargs):
        with admission():
            calls.append(True)
            await transport.on_message(DeviceInfoDocUpdate("asset-1", (keypad(batteryLevel=90),)))

    transport.send_keypad_brightness = send
    try:
        result = await request(service)
        assert result.status == "unknown" and result.possibly_sent
        assert device(service).keypad_brightness == 50
        assert (await request(service)).code == "refresh-required" and len(calls) == 1
        await transport.on_message(DeviceInfoDocUpdate("asset-1", (keypad(brightness=0.5),)))
        assert (await request(service)).status == "unknown" and len(calls) == 2
    finally:
        await _stop(stop, task)


async def test_close_joins_pending_command_and_proximity_tasks():
    service, transport, stop, task = await _start_service((_panel(), full_keypad()))
    entered = asyncio.Event()

    async def send(*_args, admission, **_kwargs):
        with admission():
            entered.set()
            await asyncio.Event().wait()

    transport.send_keypad_brightness = send
    await transport.on_message(parse_clap_frame(motion_frame()))
    pending = asyncio.create_task(request(service))
    await entered.wait()
    await _stop(stop, task)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (
        not service.keypads._pending
        and not service.keypads._requests
        and not service.keypads._motion_tasks
    )


@pytest.mark.parametrize("unqualified_kind", ["beams_bridge_v1", "base_station_future"])
async def test_typed_transport_requires_base_station_and_final_admission(unqualified_kind):
    transport = RingAlarmTransport(_Gateway(), AlarmLocationSeed("location-1"))
    websocket = _WebSocket()
    transport._websocket = websocket
    transport._connection_epoch = 1
    transport._assets = (AlarmTicketAsset("asset-1", unqualified_kind, "online"),)
    with pytest.raises(AlarmCommandSupersededError):
        await transport.send_keypad_brightness(
            "asset-1", "keypad", 25, epoch=1, admission=nullcontext
        )
    assert not websocket.sent
    transport._assets = (AlarmTicketAsset("asset-1", "base_station_v1", "online"),)
    await transport.send_keypad_brightness("asset-1", "keypad", 25, epoch=1, admission=nullcontext)
    assert len(websocket.sent) == 1
    assert json.loads(websocket.sent[0])["msg"]["body"] == [
        {"zid": "keypad", "device": {"v1": {"brightness": 0.25}}}
    ]
    await transport.close()
