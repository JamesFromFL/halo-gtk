"""Fixture-only Beams identity, policy, socket and outcome regressions."""

import asyncio
import json
from contextlib import contextmanager, nullcontext
from dataclasses import replace

import pytest

from halo_gtk.alarm.beams import ON_DURATIONS, BeamsIntent
from halo_gtk.alarm.models import AlarmWriteAuthorization, TriState
from halo_gtk.alarm.protocol import (
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    encode_beams_light,
)
from halo_gtk.alarm.provider import (
    AlarmCommandSupersededError,
    AlarmLocationSeed,
    location_seeds_from_hubs,
)
from halo_gtk.alarm.service import RingAlarmService
from halo_gtk.alarm.transport import AlarmTicketAsset, RingAlarmTransport, _parse_ticket
from halo_gtk.beams_preferences import BeamsPreference, BeamsPreferences
from tests.test_alarm_commands import _CommandTransport, _Gateway, _stop, _wait_until
from tests.test_ring_alarm_transport import _WebSocket

IDENTITY = ("location-1", "asset-1", "light-1")
OWNER = [{"id": 123, "location_id": "location-1", "role": "owner"}]


class Reservation:
    def __init__(self):
        self.revision = 0
        self.active = object()

    def admission(self, token):
        if token is not self.active:
            raise AlarmCommandSupersededError("lighting-retired")

    def mark_sent(self, token):
        self.admission(token)
        self.revision += 1
        return True


def lamp(**fields):
    return DeviceDocument("light-1", {"zid": "light-1", **fields})


def full_lamp(**fields):
    return lamp(
        **{
            "deviceType": "switch.beams",
            "name": "Sample path light",
            "on": False,
            "commStatus": "ok",
            **fields,
        }
    )


async def start(*, fields=None, owner=None, asset=None, **kwargs):
    transport = _CommandTransport((full_lamp(**(fields or {})),))
    transport.assets = (
        asset or AlarmTicketAsset("asset-1", "beams_bridge_v1", "online", doorbot_id=123),
    )
    lighting = Reservation()
    clock = [100.0]
    service = RingAlarmService(
        _Gateway(),
        (AlarmLocationSeed("location-1"),),
        lambda _: None,
        transport_factory=lambda *_: transport,
        lighting_actions=lighting,
        monotonic_clock=lambda: clock[0],
        stable_connection_time=0,
        poll_interval=0.001,
        **kwargs,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    await _wait_until(lambda: bool(service.beams.snapshot.lights))
    service.beams._timeout = 0.03
    service.beams.update_owners(OWNER if owner is None else owner)
    transport.send_calls = []

    async def send(asset_id, zid, duration, *, epoch, admission):
        with admission():
            transport.send_calls.append((asset_id, zid, duration, epoch))
        await transport.on_message(DeviceInfoDocUpdate("asset-1", (lamp(on=True),)))

    transport.send_beams_light = send
    return service, transport, lighting, clock, stop, task


def light(service):
    return service.beams.snapshot.find(IDENTITY)


async def request(service, lighting, duration=60, *, intent=None, revision=None):
    return await service.beams.request(
        *IDENTITY,
        duration,
        revision=light(service).device.revision if revision is None else revision,
        intent=intent or BeamsIntent(lambda: True),
        reservation=lighting.active,
    )


def test_bridge_only_discovery_and_alarm_ownership_are_separate():
    bridge = {"id": 123, "location_id": "home", "role": "owner", "owned": True}
    seeds = location_seeds_from_hubs(
        [],
        [bridge],
        raw_locations={"user_locations": [{"location_id": "home", "name": "Sample home"}]},
    )
    assert len(seeds) == 1 and seeds[0].location_id == "home"
    assert (
        seeds[0].name == "Sample home"
        and seeds[0].write_authorization is AlarmWriteAuthorization.UNKNOWN
    )
    seeds = location_seeds_from_hubs([{"location_id": "home", "owned": True}], [bridge])
    assert len(seeds) == 1 and seeds[0].write_authorization is AlarmWriteAuthorization.ALLOWED


@pytest.mark.parametrize("duration", [False, True, 0, -1, 15, 901, "60", 60.0])
def test_encoder_rejects_unqualified_durations(duration):
    with pytest.raises(ValueError):
        encode_beams_light("bridge", "lamp", duration, 4)


@pytest.mark.parametrize("duration", [None, *ON_DURATIONS])
def test_encoder_is_exact_finite_on_or_automatic(duration):
    encoded = encode_beams_light("bridge", "lamp", duration, 4)
    assert encoded == {
        "channel": "message",
        "msg": {
            "msg": "DeviceInfoSet",
            "datatype": "DeviceInfoSetType",
            "dst": "bridge",
            "body": [
                {
                    "zid": "lamp",
                    "command": {
                        "v1": [
                            {
                                "commandType": "light-mode.set",
                                "data": {"lightMode": "default"}
                                if duration is None
                                else {"lightMode": "on", "duration": duration},
                            }
                        ]
                    },
                }
            ],
            "seq": 4,
        },
    }


@pytest.mark.parametrize(
    "api_id,expected", [(123, 123), (True, None), ("123", None), (0, None), (2**53, None)]
)
def test_ticket_bridge_identity_is_strict(api_id, expected):
    ticket = _parse_ticket(
        {
            "host": "sample.ring.com",
            "ticket": "fixture",
            "assets": [
                {
                    "uuid": "bridge",
                    "kind": "beams_bridge_v1",
                    "status": "online",
                    "doorbot_id": api_id,
                }
            ],
        }
    )
    assert ticket.assets[0].doorbot_id == expected


@pytest.mark.parametrize("duration", [60, None])
async def test_fresh_owner_can_send_once_and_result_never_proves_automatic(duration):
    service, transport, lighting, _, stop, task = await start()
    try:
        assert light(service).reason is None
        result = await request(service, lighting, duration)
        assert len(transport.send_calls) == 1 and lighting.revision == 1
        assert result.possibly_sent
        assert result.code == ("reported-on" if duration == 60 else "automatic-unverified")
        assert result.status == ("observed" if duration == 60 else "submitted")
        assert light(service).reported_on is TriState.ACTIVE
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize(
    "owner",
    [
        [],
        [{**OWNER[0], "role": "shared"}],
        [{**OWNER[0], "role": None}],
        [{**OWNER[0], "id": "123"}],
        [{**OWNER[0], "location_id": "elsewhere"}],
        OWNER + OWNER,
        OWNER + [{**OWNER[0], "location_id": "other", "role": "shared"}],
        None,
    ],
)
async def test_missing_ambiguous_or_shared_bridge_owner_never_writes(owner):
    service, transport, lighting, _, stop, task = await start()
    try:
        service.beams.update_owners(owner)
        result = await request(service, lighting)
        assert result.status == "rejected" and not transport.send_calls
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize(
    "fields",
    [
        {"deviceType": "switch.future.beams"},
        {"on": "true"},
        {"commStatus": "down"},
        {"commStatus": None},
    ],
)
async def test_unknown_model_or_unqualified_report_is_read_only(fields):
    service, transport, lighting, _, stop, task = await start(fields=fields)
    try:
        assert light(service).reason
        assert (await request(service, lighting)).status == "rejected"
        assert not transport.send_calls
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize(
    "mutation", ["age", "global-write", "local-revoke", "revision", "owner-revoke", "runtime"]
)
async def test_each_send_boundary_fence(mutation):
    service, transport, lighting, clock, stop, task = await start()
    intent = BeamsIntent(lambda: True)
    original = transport.send_beams_light

    async def send(*args, **kwargs):
        if mutation == "age":
            clock[0] += 121
        elif mutation == "global-write":
            lighting.revision += 1
        elif mutation == "local-revoke":
            intent.cancel()
        elif mutation == "revision":
            await transport.on_message(DeviceInfoDocUpdate("asset-1", (lamp(name="New name"),)))
        elif mutation == "owner-revoke":
            service.beams.update_owners([])
        else:
            service._runtimes["location-1"].epoch += 1
        await original(*args, **kwargs)

    transport.send_beams_light = send
    try:
        assert (await request(service, lighting, intent=intent)).status == "rejected"
        assert not transport.send_calls
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize(
    "mutation", ["offline", "owner", "epoch", "account", "contradiction", "lighting"]
)
async def test_report_wakeup_cannot_confirm_retired_or_contradicted_state(mutation):
    admitted = [True]

    @contextmanager
    def guard():
        if not admitted[0]:
            raise AlarmCommandSupersededError("retired")
        yield

    service, transport, lighting, _, stop, task = await start(command_admission=guard)

    async def send(*_args, admission, **_kwargs):
        with admission():
            transport.send_calls.append(True)
        await transport.on_message(DeviceInfoDocUpdate("asset-1", (lamp(on=True),)))
        if mutation == "offline":
            await transport.on_message(DeviceInfoDocUpdate("asset-1", (lamp(commStatus="down"),)))
        elif mutation == "owner":
            service.beams.update_owners([])
        elif mutation == "epoch":
            service._runtimes["location-1"].epoch += 1
        elif mutation == "account":
            admitted[0] = False
        elif mutation == "lighting":
            lighting.revision += 1
        else:
            await transport.on_message(DeviceInfoDocUpdate("asset-1", (lamp(on=False),)))

    transport.send_beams_light = send
    try:
        result = await request(service, lighting)
        assert result.status == "unknown" and result.possibly_sent
        assert len(transport.send_calls) == 1
    finally:
        await _stop(stop, task)


async def test_unknown_outcome_requires_new_read_and_never_replays():
    service, transport, lighting, _, stop, task = await start()

    async def send(*_args, admission, **_kwargs):
        with admission():
            transport.send_calls.append(True)

    transport.send_beams_light = send
    try:
        result = await request(service, lighting)
        assert result.status == "unknown" and result.possibly_sent
        assert (await request(service, lighting)).status == "rejected"
        assert len(transport.send_calls) == 1
        await transport.on_message(DeviceInfoDocUpdate("asset-1", (lamp(on=False),)))
        assert light(service).reason is None
    finally:
        await _stop(stop, task)


async def test_simultaneous_devices_do_not_queue_and_shutdown_joins_request():
    service, transport, lighting, _, stop, task = await start()
    entered = asyncio.Event()

    async def send(*_args, admission, **_kwargs):
        with admission():
            transport.send_calls.append(True)
        entered.set()
        await asyncio.Event().wait()

    transport.send_beams_light = send
    pending = asyncio.create_task(request(service, lighting))
    await entered.wait()
    assert (await request(service, lighting)).code == "lighting-busy"
    await _stop(stop, task)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert service.beams._pending is None and not service.beams._requests
    assert len(transport.send_calls) == 1


async def test_device_removal_during_send_does_not_retain_uncertainty():
    service, transport, lighting, _, stop, task = await start()

    async def send(*_args, admission, **_kwargs):
        with admission():
            transport.send_calls.append(True)
        await transport.on_message(DeviceInfoDocList("asset-1", ()))

    transport.send_beams_light = send
    try:
        assert (await request(service, lighting)).status == "unknown"
        assert not service.beams._observed and not service.beams._uncertain
    finally:
        await _stop(stop, task)


async def test_shared_reservation_failed_mark_never_hands_off():
    service, transport, lighting, _, stop, task = await start()
    lighting.mark_sent = lambda _token: False
    try:
        assert (await request(service, lighting)).status == "rejected"
        assert not transport.send_calls
    finally:
        await _stop(stop, task)


@pytest.mark.parametrize("kind", ["base_station_v1", "beams_bridge_future"])
async def test_transport_only_writes_qualified_bridge(kind):
    transport = RingAlarmTransport(_Gateway(), AlarmLocationSeed("location-1"))
    websocket = _WebSocket()
    transport._websocket, transport._connection_epoch = websocket, 1
    transport._assets = (AlarmTicketAsset("asset-1", kind, "online"),)
    with pytest.raises(AlarmCommandSupersededError):
        await transport.send_beams_light("asset-1", "light", 60, epoch=1, admission=nullcontext)
    assert not websocket.sent
    transport._assets = (AlarmTicketAsset("asset-1", "beams_bridge_v1", "online"),)
    await transport.send_beams_light("asset-1", "light", None, epoch=1, admission=nullcontext)
    assert len(websocket.sent) == 1
    assert json.loads(websocket.sent[0])["msg"]["body"][0]["command"]["v1"][0]["data"] == {
        "lightMode": "default"
    }
    await transport.close()


def test_private_preferences_default_scope_and_no_invalid_durations(tmp_path):
    prefs = BeamsPreferences(tmp_path / "beams.json")
    assert prefs.get("one@example.test", *IDENTITY) == BeamsPreference()
    value = prefs.set("one@example.test", *IDENTITY, enabled=True, duration=300)
    assert value.enabled and value.revision == 1
    assert BeamsPreferences(prefs.path).get("one@example.test", *IDENTITY) == value
    assert not prefs.get("two@example.test", *IDENTITY).enabled
    assert prefs.path.stat().st_mode & 0o777 == 0o600
    assert (
        "one@example.test" not in prefs.path.read_text() and "light-1" not in prefs.path.read_text()
    )
    with pytest.raises(ValueError):
        prefs.set("one@example.test", *IDENTITY, enabled=True, duration=0)
    key = next(iter(prefs._values))
    prefs._values[key] = replace(value, revision=2**53 - 2)
    with pytest.raises(ValueError):
        prefs.set("one@example.test", *IDENTITY, enabled=True, duration=60)
    assert not prefs.get("one@example.test", *IDENTITY).enabled


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"schema":1,"schema":1,"lights":{}}',
        '{"schema":true,"lights":{}}',
        '{"schema":1,"lights":[]}',
    ],
)
def test_malformed_local_policy_fails_disabled(tmp_path, text):
    path = tmp_path / "beams.json"
    path.write_text(text)
    assert not BeamsPreferences(path).get("owner", *IDENTITY).enabled
