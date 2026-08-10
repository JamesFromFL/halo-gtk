"""Tests for multi-location Alarm supervision and publication."""

import asyncio
from types import SimpleNamespace

import pytest

import halo_gtk.alarm.store as alarm_store
from halo_gtk.alarm.models import (
    AlarmConnectionStatus,
    AlarmInventoryStatus,
    AlarmMode,
    AlarmServiceStatus,
    AlarmWriteAuthorization,
)
from halo_gtk.alarm.protocol import (
    AssetSessionInfo,
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    SessionInfo,
    UnknownMessage,
)
from halo_gtk.alarm.provider import AlarmLocationSeed, location_seeds_from_base_stations
from halo_gtk.alarm.service import RingAlarmService
from halo_gtk.alarm.transport import AlarmTicketAsset, AlarmTransportError


def _panel(mode="none"):
    return DeviceDocument(
        zid="panel-1",
        data={
            "zid": "panel-1",
            "name": "Security Panel",
            "deviceType": "security-panel",
            "mode": mode,
        },
    )


def _owned_seed(location_id, *, name="", asset_ids=()):
    return AlarmLocationSeed(
        location_id,
        name=name,
        asset_ids=asset_ids,
        write_authorization=AlarmWriteAuthorization.ALLOWED,
    )


async def _wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.001)


@pytest.mark.parametrize(
    ("phases", "expected"),
    (
        (("connecting", "offline"), AlarmServiceStatus.CONNECTING),
        (("connecting", "reconnecting"), AlarmServiceStatus.RECONNECTING),
        (("online", "reconnecting"), AlarmServiceStatus.DEGRADED),
    ),
)
def test_service_status_preserves_mixed_location_progress(phases, expected):
    service = RingAlarmService(
        _Gateway(),
        tuple(AlarmLocationSeed(f"location-{index}") for index in range(len(phases))),
        lambda _snapshot: None,
    )
    service._runtimes = {
        f"location-{index}": SimpleNamespace(phase=phase) for index, phase in enumerate(phases)
    }

    assert service._derive_status() is expected


def test_discovery_enriches_names_and_only_owner_evidence_allows_writes():
    seeds = location_seeds_from_base_stations(
        {
            1: {
                "location_id": "owned-location",
                "device_id": "owned-hint",
                "owned": True,
            },
            2: {
                "location_id": "shared-location",
                "device_id": "shared-hint",
                "owned": False,
            },
            3: {
                "location_id": "unknown-location",
                "device_id": "unknown-hint",
            },
        },
        raw_locations={
            "user_locations": [
                {"location_id": "owned-location", "name": "Home"},
                {"location_id": "shared-location", "name": "Office"},
            ]
        },
    )

    assert [(seed.location_id, seed.name) for seed in seeds] == [
        ("owned-location", "Home"),
        ("shared-location", "Office"),
        ("unknown-location", ""),
    ]
    assert seeds[0].write_authorization is AlarmWriteAuthorization.ALLOWED
    assert seeds[1].write_authorization is AlarmWriteAuthorization.UNKNOWN
    assert seeds[2].write_authorization is AlarmWriteAuthorization.UNKNOWN


@pytest.mark.parametrize(
    "kwargs",
    [
        {"location_id": "x" * 513},
        {"location_id": "home\nsecret"},
        {"location_id": "home", "name": "x" * 257},
        {"location_id": "home", "name": "Home\nsecret"},
        {"location_id": "home", "asset_ids": ("x" * 513,)},
        {"location_id": "home", "asset_ids": ("asset\nsecret",)},
        {"location_id": "home", "asset_ids": tuple(f"asset-{i}" for i in range(129))},
    ],
)
def test_location_seed_rejects_unbounded_or_control_character_discovery(kwargs):
    with pytest.raises(ValueError):
        AlarmLocationSeed(**kwargs)


def test_discovery_discards_invalid_fields_and_bounds_supervisor_count():
    records = [
        {
            "location_id": "invalid\nlocation",
            "device_id": "asset-invalid",
            "address": "must-not-be-used",
        },
        {
            "location_id": "valid-location",
            "device_id": "invalid\nasset",
            "address": "must-not-be-used",
        },
        *(
            {
                "location_id": f"location-{index:03}",
                "device_id": f"asset-{index}",
                "owned": True,
                "location_name": "Fallback",
            }
            for index in range(140)
        ),
    ]

    seeds = location_seeds_from_base_stations(
        records,
        raw_locations={
            "user_locations": [
                {"location_id": "location-000", "name": "x" * 257},
                {"location_id": "location-001", "name": "API Name"},
            ]
        },
    )

    assert len(seeds) == 128
    assert seeds[0].name == "Fallback"
    assert seeds[1].name == "API Name"
    assert all("\n" not in seed.location_id for seed in seeds)
    assert all(seed.name != "must-not-be-used" for seed in seeds)
    valid = next(seed for seed in seeds if seed.location_id == "valid-location")
    assert valid.asset_ids == ()
    assert valid.name == ""


def test_service_rejects_duplicate_location_seeds():
    with pytest.raises(ValueError, match="unique location ids"):
        RingAlarmService(
            _Gateway(),
            (AlarmLocationSeed("duplicate"), AlarmLocationSeed("duplicate")),
            lambda _snapshot: None,
        )


class _Gateway:
    async def request_json(self, _url, **_kwargs):
        raise AssertionError("fake transport does not use the REST gateway")


class _StreamingTransport:
    def __init__(self, assets, messages=()):
        self.assets = tuple(assets)
        self.messages = tuple(messages)
        self.calls = 0
        self.connected = False
        self.closed = False
        self.inventory_requests = []

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.calls += 1
        self.connected = True
        try:
            await on_open(self.assets)
            for message in self.messages:
                await on_message(message)
            await stop_event.wait()
        finally:
            self.connected = False

    async def close(self):
        self.closed = True
        self.connected = False

    async def request_inventory(self, asset_id, *, epoch):
        self.inventory_requests.append((asset_id, epoch))


@pytest.mark.asyncio
async def test_public_service_api_reports_no_system_and_stops_cleanly():
    snapshots = []
    service = RingAlarmService(_Gateway(), (), snapshots.append, generation=7)
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))

    await _wait_until(lambda: service.snapshot.status is AlarmServiceStatus.NO_SYSTEM)
    assert service.snapshot.generation == 7
    assert service.snapshot.locations == ()

    await service.request_refresh()
    stop.set()
    await task
    assert service.snapshot.status is AlarmServiceStatus.STOPPED
    assert snapshots[-1].status is AlarmServiceStatus.STOPPED


@pytest.mark.asyncio
async def test_locations_sync_independently_and_offline_asset_is_degraded():
    online_asset = AlarmTicketAsset("asset-good", "base_station_v1", "online")
    partial_assets = (
        AlarmTicketAsset("asset-panel", "base_station_v1", "online"),
        AlarmTicketAsset("asset-bridge", "beams_bridge_v1", "offline"),
    )
    transports = {
        "good": _StreamingTransport(
            (online_asset,),
            (DeviceInfoDocList("asset-good", (_panel(),)),),
        ),
        "partial": _StreamingTransport(
            partial_assets,
            (DeviceInfoDocList("asset-panel", (_panel(),)),),
        ),
    }
    service = RingAlarmService(
        _Gateway(),
        (_owned_seed("good"), _owned_seed("partial")),
        lambda _snapshot: None,
        transport_factory=lambda _request, seed: transports[seed.location_id],
        initial_sync_timeout=0.01,
        stable_connection_time=0,
        poll_interval=0.001,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))

    await _wait_until(
        lambda: (
            service.snapshot.find_location("good") is not None
            and service.snapshot.find_location("good").inventory is AlarmInventoryStatus.COMPLETE
            and service.snapshot.find_location("partial") is not None
            and service.snapshot.find_location("partial").inventory is AlarmInventoryStatus.PARTIAL
        )
    )
    good = service.snapshot.find_location("good")
    partial = service.snapshot.find_location("partial")
    assert good.mode is AlarmMode.DISARMED
    assert good.can_set_mode is True
    assert partial.can_set_mode is True
    assert partial.command_unavailable_reason is None
    assert service.snapshot.status is AlarmServiceStatus.DEGRADED

    partial_runtime = service._runtimes["partial"]
    partial_runtime.backoff = 8
    await service._mark_connection_stable(partial_runtime, partial_runtime.epoch)
    assert partial_runtime.backoff == 1

    stop.set()
    await task
    assert all(transport.closed for transport in transports.values())


@pytest.mark.asyncio
async def test_frames_for_assets_outside_current_ticket_are_ignored():
    asset = AlarmTicketAsset("alarm-asset", "base_station_v1", "online")
    transport = _StreamingTransport(
        (asset,),
        (
            DeviceInfoDocList("alarm-asset", (_panel(),)),
            SessionInfo(
                (
                    AssetSessionInfo(
                        "camera-floodlight",
                        AlarmConnectionStatus.ONLINE,
                        "floodlight_v2",
                    ),
                )
            ),
            DeviceInfoDocList("camera-floodlight", (_panel(),)),
            DeviceInfoDocUpdate("camera-floodlight", (_panel("all"),)),
        ),
    )
    service = RingAlarmService(
        _Gateway(),
        (_owned_seed("location-1"),),
        lambda _snapshot: None,
        transport_factory=lambda _request, _seed: transport,
        stable_connection_time=0,
        poll_interval=0.001,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))

    await _wait_until(
        lambda: (
            service.snapshot.find_location("location-1") is not None
            and service.snapshot.find_location("location-1").can_set_mode
        )
    )
    location = service.snapshot.find_location("location-1")
    assert tuple(asset.asset_id for asset in location.assets) == ("alarm-asset",)
    assert location.mode is AlarmMode.DISARMED
    assert transport.inventory_requests == []

    stop.set()
    await task


@pytest.mark.asyncio
async def test_reconcile_adds_and_removes_locations_without_restarting_healthy_socket():
    asset = AlarmTicketAsset("asset-1", "base_station_v1", "online")
    transports = {
        location_id: _StreamingTransport(
            (asset,),
            (DeviceInfoDocList("asset-1", (_panel(),)),),
        )
        for location_id in ("first", "second")
    }
    service = RingAlarmService(
        _Gateway(),
        (_owned_seed("first"),),
        lambda _snapshot: None,
        transport_factory=lambda _request, seed: transports[seed.location_id],
        stable_connection_time=0,
        poll_interval=0.001,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    await _wait_until(
        lambda: (
            service.snapshot.find_location("first") is not None
            and service.snapshot.find_location("first").can_set_mode
        )
    )

    await service.reconcile((_owned_seed("first"), _owned_seed("second")))
    await _wait_until(
        lambda: (
            service.snapshot.find_location("second") is not None
            and service.snapshot.find_location("second").can_set_mode
        )
    )
    assert transports["first"].calls == 1

    await service.reconcile((_owned_seed("second"),))
    assert service.snapshot.find_location("first") is None
    assert transports["first"].closed is True
    assert transports["second"].calls == 1

    stop.set()
    await task


@pytest.mark.asyncio
async def test_reconcile_preserves_ticket_assets_instead_of_raw_discovery_hints():
    asset = AlarmTicketAsset("ticket-asset", "base_station_v1", "online")
    transport = _StreamingTransport(
        (asset,),
        (DeviceInfoDocList("ticket-asset", (_panel(),)),),
    )
    seed = _owned_seed("location-1", asset_ids=("raw-device-hint",))
    service = RingAlarmService(
        _Gateway(),
        (seed,),
        lambda _snapshot: None,
        transport_factory=lambda _request, _seed: transport,
        stable_connection_time=0,
        poll_interval=0.001,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    await _wait_until(
        lambda: (
            service.snapshot.find_location("location-1") is not None
            and service.snapshot.find_location("location-1").can_set_mode
        )
    )

    await service.reconcile((seed,))
    location = service.snapshot.find_location("location-1")
    assert location.find_asset("ticket-asset") is not None
    assert location.find_asset("raw-device-hint") is None
    assert location.security_panel is not None
    assert transport.calls == 1

    stop.set()
    await task


@pytest.mark.asyncio
async def test_asset_returning_online_requests_fresh_inventory_before_becoming_fresh():
    asset = AlarmTicketAsset("asset-1", "base_station_v1", "offline")
    transport = _StreamingTransport(
        (asset,),
        (
            DeviceInfoDocList("asset-1", (_panel(),)),
            SessionInfo(
                sessions=(
                    AssetSessionInfo(
                        "asset-1",
                        AlarmConnectionStatus.ONLINE,
                        "base_station_v1",
                    ),
                )
            ),
        ),
    )
    service = RingAlarmService(
        _Gateway(),
        (AlarmLocationSeed("location-1"),),
        lambda _snapshot: None,
        transport_factory=lambda _request, _seed: transport,
        poll_interval=0.001,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))

    await _wait_until(lambda: bool(transport.inventory_requests))
    assert transport.inventory_requests == [("asset-1", 1)]
    assert service.snapshot.find_location("location-1").inventory is AlarmInventoryStatus.STALE

    stop.set()
    await task


class _FlappingTransport:
    def __init__(self, stop):
        self.stop = stop
        self.calls = 0
        self.connected = False
        self.assets = ()

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.calls += 1
        self.connected = True
        await on_open((AlarmTicketAsset("asset-1", "base_station_v1", "online"),))
        if self.calls == 1:
            await on_message(UnknownMessage("DataUpdate", "Other", "Other"))
        self.connected = False
        if self.calls >= 3:
            self.stop.set()
        raise AlarmTransportError("websocket-closed")

    async def close(self):
        self.connected = False


class _RetryAfterTransport:
    def __init__(self, stop):
        self.stop = stop
        self.calls = []
        self.connected = False
        self.assets = ()

    async def connect_once(self, **_kwargs):
        self.calls.append(asyncio.get_running_loop().time())
        if len(self.calls) == 1:
            raise AlarmTransportError("rate-limited", retry_after=0.03)
        self.stop.set()

    async def close(self):
        self.connected = False


class _OverflowingTransport:
    def __init__(self):
        self.assets = (AlarmTicketAsset("asset-1", "base_station_v1", "online"),)
        self.calls = 0
        self.connected = False
        self.reconnected = asyncio.Event()
        self.error_codes = []

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.calls += 1
        self.connected = True
        try:
            await on_open(self.assets)
            if self.calls == 1:
                await on_message(DeviceInfoDocList("asset-1", (_panel(),)))
                try:
                    await on_message(
                        DeviceInfoDocUpdate(
                            "asset-1",
                            (
                                DeviceDocument(
                                    "contact-1",
                                    {
                                        "zid": "contact-1",
                                        "deviceType": "sensor.contact",
                                    },
                                ),
                            ),
                        )
                    )
                except AlarmTransportError as exc:
                    self.error_codes.append(exc.code)
                    raise
            self.reconnected.set()
            await stop_event.wait()
        finally:
            self.connected = False

    async def close(self):
        self.connected = False


@pytest.mark.asyncio
async def test_arbitrary_message_does_not_reset_reconnect_backoff():
    stop = asyncio.Event()
    transport = _FlappingTransport(stop)
    observed_backoffs = []

    def jitter(delay):
        observed_backoffs.append(delay)
        return 0

    service = RingAlarmService(
        _Gateway(),
        (AlarmLocationSeed("location-1"),),
        lambda _snapshot: None,
        transport_factory=lambda _request, _seed: transport,
        reconnect_min=1,
        reconnect_max=8,
        stable_connection_time=60,
        jitter=jitter,
        poll_interval=0.001,
    )

    await service.run(stop)
    assert observed_backoffs[:2] == [1, 2]


@pytest.mark.asyncio
async def test_retained_state_overflow_reconnects_and_marks_location_stale(monkeypatch):
    monkeypatch.setattr(alarm_store, "MAX_DEVICE_DOCUMENTS", 1)
    stop = asyncio.Event()
    transport = _OverflowingTransport()
    service = RingAlarmService(
        _Gateway(),
        (_owned_seed("location-1"),),
        lambda _snapshot: None,
        transport_factory=lambda _request, _seed: transport,
        reconnect_min=0,
        reconnect_max=0,
        jitter=lambda _delay: 0,
        poll_interval=0.001,
    )
    task = asyncio.create_task(service.run(stop))

    await asyncio.wait_for(transport.reconnected.wait(), timeout=1)
    await _wait_until(
        lambda: service.snapshot.find_location("location-1").inventory is AlarmInventoryStatus.STALE
    )
    location = service.snapshot.find_location("location-1")

    assert transport.calls == 2
    assert transport.error_codes == ["retained-state-limit"]
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "state-stale"

    stop.set()
    await task


@pytest.mark.asyncio
async def test_retry_after_is_honored_without_using_exponential_backoff():
    stop = asyncio.Event()
    transport = _RetryAfterTransport(stop)
    jitter_calls = []
    service = RingAlarmService(
        _Gateway(),
        (AlarmLocationSeed("location-1"),),
        lambda _snapshot: None,
        transport_factory=lambda _request, _seed: transport,
        reconnect_min=1,
        jitter=lambda delay: jitter_calls.append(delay) or 0,
        poll_interval=0.001,
    )

    await asyncio.wait_for(service.run(stop), timeout=1)

    assert len(transport.calls) == 2
    assert transport.calls[1] - transport.calls[0] >= 0.025
    assert jitter_calls == []
