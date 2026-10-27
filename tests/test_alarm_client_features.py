import asyncio
import concurrent.futures
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_gtk import ring_client
from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmMode,
    AlarmServiceStatus,
    AlarmSignal,
)
from halo_gtk.ring_client import RingClient, SessionSupersededError


def state(*, signal=AlarmSignal.NONE, revision=1):
    panel = AlarmDeviceSnapshot(
        "home", "hub", "panel", kind=AlarmDeviceKind.SECURITY_PANEL, signal=signal, epoch=1
    )
    asset = AlarmAssetSnapshot(
        "home", "hub", connection=AlarmConnectionStatus.ONLINE, devices=(panel,), epoch=1
    )
    location = AlarmLocationSnapshot(
        "home",
        connection=AlarmConnectionStatus.ONLINE,
        inventory=AlarmInventoryStatus.COMPLETE,
        assets=(asset,),
        mode=AlarmMode.DISARMED,
        epoch=1,
    )
    return AlarmAccountSnapshot(
        generation=1, revision=revision, locations=(location,), status=AlarmServiceStatus.ONLINE
    )


def active_client(monkeypatch):
    client = RingClient()
    client._published_generation = 1
    monkeypatch.setattr(
        ring_client,
        "_is_active_client_generation",
        lambda value, generation: value is client and generation == 1,
    )
    client._alarm_snapshot = state()
    return client


def test_latest_state_rendering_coalesces_without_losing_intermediate_critical_signal(monkeypatch):
    from gi.repository import GLib

    from halo_gtk import notifications

    client = active_client(monkeypatch)
    scheduled, shown, rendered = [], [], []
    monkeypatch.setattr(GLib, "idle_add", lambda *args: scheduled.append(args))
    monkeypatch.setattr(notifications, "send_alarm_notification", shown.append)
    client.add_alarm_callback(rendered.append)
    client._on_alarm_snapshot(state())
    client._on_alarm_snapshot(state(signal=AlarmSignal.FIRE, revision=2))
    latest = state(revision=3)
    client._on_alarm_snapshot(latest)
    assert len(scheduled) == 1
    callback, *args = scheduled[0]
    callback(*args)
    assert rendered == [latest]
    assert any(event.critical for event in shown)
    assert not client._alarm_dispatch_pending


@pytest.mark.asyncio
async def test_history_result_is_fenced_after_session_changes(monkeypatch):
    client = active_client(monkeypatch)
    release = asyncio.Event()

    async def fetch(*args, **kwargs):
        await release.wait()
        return "private history"

    client._alarm_history_service = SimpleNamespace(fetch_page=fetch)
    task = asyncio.create_task(client._async_alarm_history("home"))
    await asyncio.sleep(0)
    monkeypatch.setattr(ring_client, "_is_active_client_generation", lambda *_args: False)
    release.set()
    with pytest.raises(SessionSupersededError):
        await task


def test_worker_history_cancel_event_cancels_submitted_future(monkeypatch):
    client = active_client(monkeypatch)
    cancelled = threading.Event()
    future = concurrent.futures.Future()

    def submit(coro):
        coro.close()
        cancelled.set()
        return future

    monkeypatch.setattr(client, "submit", submit)
    with pytest.raises(concurrent.futures.CancelledError):
        client.alarm_history("home", cancel_event=cancelled)
    assert future.cancelled()


def test_already_cancelled_history_never_submits(monkeypatch):
    client = active_client(monkeypatch)
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(client, "submit", lambda *_args: pytest.fail("submitted cancelled history"))
    with pytest.raises(concurrent.futures.CancelledError):
        client.alarm_history("home", cancel_event=cancelled)


def test_authoritative_location_removal_purges_history_and_pending_events(monkeypatch):
    from gi.repository import GLib

    client = active_client(monkeypatch)
    revoked = []
    client._alarm_history_service = SimpleNamespace(revoke_location=revoked.append)
    monkeypatch.setattr(GLib, "idle_add", lambda *_args: 1)
    client._on_alarm_snapshot(
        AlarmAccountSnapshot(generation=1, status=AlarmServiceStatus.NO_SYSTEM)
    )
    assert revoked == ["home"]
    assert client._alarm_event_buffer.drain()[0] == ()


def test_transient_error_is_not_authoritative_location_removal(monkeypatch):
    from gi.repository import GLib

    client = active_client(monkeypatch)
    revoked = []
    client._alarm_history_service = SimpleNamespace(revoke_location=revoked.append)
    monkeypatch.setattr(GLib, "idle_add", lambda *_args: 1)
    client._on_alarm_snapshot(replace(state(), status=AlarmServiceStatus.ERROR))
    assert revoked == []


@pytest.mark.asyncio
async def test_alarm_discovery_refresh_coalesces_and_reconciles_owner_evidence(monkeypatch):
    from halo_gtk.alarm.models import AlarmWriteAuthorization
    from halo_gtk.alarm.rest import DEVICES_ENDPOINT

    client = active_client(monkeypatch)
    calls, reconciled = [], []

    class Gateway:
        async def request_json(self, url):
            calls.append(url)
            await asyncio.sleep(0)
            if url == DEVICES_ENDPOINT:
                return {"base_stations": [{"id": 42, "location_id": "home", "owned": True}]}
            return {"user_locations": [{"location_id": "home", "name": "Home"}]}

    async def reconcile(seeds):
        reconciled.append(seeds)

    client._alarm_gateway = Gateway()
    client._alarm_service = SimpleNamespace(reconcile=reconcile)
    assert await asyncio.gather(
        client._async_refresh_alarm_discovery(), client._async_refresh_alarm_discovery()
    ) == [True, True]
    assert len(calls) == 2 and len(reconciled) == 1
    assert reconciled[0][0].write_authorization is AlarmWriteAuthorization.ALLOWED


@pytest.mark.asyncio
async def test_malformed_discovery_is_not_authoritative_removal(monkeypatch):
    from halo_gtk.alarm.rest import DEVICES_ENDPOINT

    client = active_client(monkeypatch)
    reconciled = []

    class Gateway:
        async def request_json(self, url):
            return {} if url == DEVICES_ENDPOINT else {"user_locations": []}

    async def reconcile(seeds):
        reconciled.append(seeds)

    client._alarm_gateway = Gateway()
    client._alarm_service = SimpleNamespace(reconcile=reconcile)
    assert not await client._async_refresh_alarm_discovery()
    assert not reconciled
    assert client.get_alarm_snapshot().find_location("home") is not None


@pytest.mark.asyncio
async def test_preflight_does_not_issue_approval_when_discovery_cannot_refresh(monkeypatch):
    client = active_client(monkeypatch)

    async def unavailable():
        return False

    async def prepare(_location):
        pytest.fail("permission evidence refresh failed")

    monkeypatch.setattr(client, "_async_refresh_alarm_discovery", unavailable)
    client._alarm_service = SimpleNamespace(prepare_mode=prepare)
    result = await client._async_prepare_alarm_mode("home")
    assert result.approval_id is None and result.code == "discovery-unavailable"


@pytest.mark.asyncio
async def test_discovery_failure_cancels_sibling_request(monkeypatch):
    from halo_gtk.alarm.rest import DEVICES_ENDPOINT

    client = active_client(monkeypatch)
    cleaned = asyncio.Event()
    started = asyncio.Event()

    class Gateway:
        async def request_json(self, url):
            if url == DEVICES_ENDPOINT:
                await started.wait()
                raise RuntimeError("synthetic error")
            try:
                started.set()
                await asyncio.Future()
            finally:
                cleaned.set()

    client._alarm_gateway = Gateway()
    client._alarm_service = SimpleNamespace(reconcile=lambda *_: None)
    assert not await client._async_refresh_alarm_discovery()
    assert cleaned.is_set()
