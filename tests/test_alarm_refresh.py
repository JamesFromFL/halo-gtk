"""Fixture-only health, inventory boundary and refresh lifecycle regressions."""

import asyncio
from contextlib import nullcontext

import pytest

from halo_gtk.alarm.models import (
    AlarmConnectionStatus,
    AlarmWriteAuthorization,
    TriState,
)
from halo_gtk.alarm.protocol import DeviceDocument, DeviceInfoDocList, DeviceInfoDocUpdate
from halo_gtk.alarm.provider import AlarmLocationSeed, AlarmRefreshStatus
from halo_gtk.alarm.service import RingAlarmService
from halo_gtk.alarm.transport import AlarmTicketAsset


def _panel():
    return DeviceDocument("panel", {"zid": "panel", "deviceType": "security-panel", "mode": "none"})


async def _until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


class _Transport:
    def __init__(self, assets=None, *, partial=False):
        self.assets = assets or (AlarmTicketAsset("base", "base_station_v1", "online", True),)
        self.partial = partial
        self.connected = False
        self.requests = []
        self.behavior = "reply"
        self.before_admission = None
        self.gate = None
        self.requested = asyncio.Event()
        self.mode_sends = 0

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.connected = True
        self.epoch = epoch
        self.on_message = on_message
        try:
            await on_open(self.assets)
            for asset in self.assets[:1] if self.partial else self.assets:
                await self.emit_list(asset.asset_id)
            await stop_event.wait()
        finally:
            self.connected = False

    async def emit_list(self, asset_id="base"):
        await self.on_message(
            DeviceInfoDocList(asset_id, (_panel(),) if asset_id == "base" else ())
        )

    async def request_inventory(self, asset_id, *, epoch, admission=None):
        self.requested.set()
        if self.before_admission:
            await self.before_admission()
        if self.gate:
            await self.gate.wait()
        with admission() if admission else nullcontext():
            self.requests.append((asset_id, epoch))
        if self.behavior == "reply":
            await self.emit_list(asset_id)
        elif self.behavior == "patch":
            await self.on_message(DeviceInfoDocUpdate(asset_id, (_panel(),)))
        elif self.behavior == "foreign":
            await self.emit_list("not-in-ticket")

    async def close(self):
        self.connected = False


async def _start(transport=None, **kwargs):
    transport = transport or _Transport()
    service = RingAlarmService(
        object(),
        (AlarmLocationSeed("loc", write_authorization=AlarmWriteAuthorization.ALLOWED),),
        lambda _: None,
        transport_factory=lambda *_: transport,
        poll_interval=0.001,
        refresh_timeout=kwargs.pop("refresh_timeout", 0.03),
        **kwargs,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    await _until(lambda: bool(service.snapshot.locations and service.snapshot.locations[0].devices))
    return service, transport, stop, task


@pytest.mark.asyncio
async def test_refresh_completes_for_identical_full_list_delivered_inside_send():
    service, transport, stop, task = await _start()
    try:
        before = service.snapshot.locations[0]
        result = await service.refresh("loc")
        after = service.snapshot.locations[0]
        assert result.status is AlarmRefreshStatus.REFRESHED
        assert len(transport.requests) == 1
        assert after.revision == before.revision
        assert after.assets[0].inventory_revision == before.assets[0].inventory_revision + 1
        assert after.inventory_monotonic >= before.inventory_monotonic
        assert after.assets[0].on_battery is TriState.ACTIVE
        assert after.assets[0].power_observed_monotonic is not None
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["none", "patch", "foreign"])
async def test_cached_complete_patch_or_foreign_asset_cannot_finish_refresh(behavior):
    service, transport, stop, task = await _start()
    transport.behavior = behavior
    try:
        result = await service.refresh("loc")
        assert result.status is AlarmRefreshStatus.TIMED_OUT
        assert service._inventory_waiters == {}
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
async def test_full_list_before_queued_send_admission_cannot_finish_refresh():
    service, transport, stop, task = await _start()
    transport.behavior = "none"
    transport.before_admission = transport.emit_list
    try:
        result = await service.refresh("loc")
        assert result.status is AlarmRefreshStatus.TIMED_OUT
        assert len(transport.requests) == 1
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
async def test_refresh_coalesces_caller_cancellation_and_manual_cooldown():
    service, transport, stop, task = await _start()
    transport.gate = asyncio.Event()
    try:
        first = asyncio.create_task(service.refresh("loc"))
        await transport.requested.wait()
        second = asyncio.create_task(service.refresh("loc"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        transport.gate.set()
        assert (await second).status is AlarmRefreshStatus.REFRESHED
        assert len(transport.requests) == 1
        assert (await service.refresh("loc")).status is AlarmRefreshStatus.COOLDOWN
        assert (await service.refresh("loc", force=True)).status is AlarmRefreshStatus.REFRESHED
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
async def test_close_drains_refresh_and_rejects_late_admission():
    service, transport, stop, task = await _start(refresh_timeout=1)
    transport.gate = asyncio.Event()
    refresh = asyncio.create_task(service.refresh("loc"))
    await transport.requested.wait()
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    transport.gate.set()
    stop.set()
    await task
    assert not transport.requests
    assert not service._inventory_waiters
    assert not service._refresh_tasks


@pytest.mark.asyncio
async def test_initial_partial_inventory_recovers_with_bounded_reread():
    transport = _Transport(
        (
            AlarmTicketAsset("base", "base_station_v1", "online"),
            AlarmTicketAsset("bridge", "beams_bridge_v1", "online"),
        ),
        partial=True,
    )
    service, transport, stop, task = await _start(
        transport,
        initial_sync_timeout=0.005,
        health_refresh_interval=100,
    )
    try:
        await _until(lambda: service.snapshot.locations[0].inventory.value == "complete")
        assert 1 <= len(transport.requests) <= 2
    finally:
        stop.set()
        await task


@pytest.mark.asyncio
async def test_periodic_health_refresh_is_read_only_and_stops_with_service():
    service, transport, stop, task = await _start(health_refresh_interval=0.01)
    await _until(lambda: len(transport.requests) >= 1)
    stop.set()
    await task
    count = len(transport.requests)
    await asyncio.sleep(0.02)
    assert len(transport.requests) == count
    assert transport.mode_sends == 0


@pytest.mark.asyncio
async def test_refresh_result_is_incomplete_when_only_one_asset_answers():
    transport = _Transport(
        (
            AlarmTicketAsset("base", "base_station_v1", "online"),
            AlarmTicketAsset("bridge", "beams_bridge_v1", "offline"),
        )
    )
    service, transport, stop, task = await _start(transport)
    try:
        result = await service.refresh("loc")
        assert result.status is AlarmRefreshStatus.INCOMPLETE
        assert (
            service.snapshot.locations[0].find_asset("bridge").connection
            is AlarmConnectionStatus.OFFLINE
        )
    finally:
        stop.set()
        await task
