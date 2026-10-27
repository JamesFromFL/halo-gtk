"""Chime account fencing, GTK delivery and shared transport ownership."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_gtk import ring_client
from halo_gtk.chime import ChimeAccountSnapshot, ChimeIntent, ChimeSnapshot
from halo_gtk.chime_client import ChimeClientState
from halo_gtk.ring_client import RingClient
from tests.test_alarm_rest import Response, fake_auth


def active_client(monkeypatch, generation=7):
    client = RingClient()
    client._ring = SimpleNamespace(auth=object())
    client._authenticated = True
    client._published_generation = generation
    client._chimes.initialize(generation)
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", generation)
    return client


def test_cached_chime_access_never_creates_transport_or_service():
    client = RingClient()
    assert client.get_chime_snapshot().devices == ()
    assert client._ring_json_transport is None
    assert client._chimes._service is None


def test_snapshot_delivery_coalesces_and_retirement_discards_queued_private_data(monkeypatch):
    from gi.repository import GLib

    pending = []
    monkeypatch.setattr(GLib, "idle_add", lambda *args: pending.append(args))
    client = active_client(monkeypatch)
    seen = []
    client.add_chime_callback(seen.append)
    snapshot = ChimeAccountSnapshot(
        7, revision=1, devices=(ChimeSnapshot(1, "Hall", "chime", "Chime"),)
    )
    client._chimes._on_snapshot(snapshot)
    client._chimes._on_snapshot(replace(snapshot, revision=2))
    assert len(pending) == 1
    callback, *args = pending.pop()
    callback(*args)
    assert [item.revision for item in seen] == [2]
    client._chimes._on_snapshot(replace(snapshot, revision=3))
    client._chimes.invalidate(8)
    callback, *args = pending.pop()
    callback(*args)
    assert [item.revision for item in seen] == [2]
    assert client.get_chime_snapshot() == ChimeAccountSnapshot(8)
    client._chimes._on_snapshot(replace(snapshot, revision=4))
    assert client.get_chime_snapshot().devices == ()


def test_local_fence_rejects_producer_that_passed_global_check_before_retirement(monkeypatch):
    client = active_client(monkeypatch)
    snapshot = ChimeAccountSnapshot(7, devices=(ChimeSnapshot(1, "Private", "chime", "Chime"),))

    def race(_generation):
        client._chimes.invalidate(8)
        return True

    monkeypatch.setattr(client._chimes, "current", race)
    client._chimes._on_snapshot(snapshot)
    assert client.get_chime_snapshot() == ChimeAccountSnapshot(8)


def test_intent_cancellation_has_no_registry_and_does_not_cancel_operation_future():
    intent = ChimeIntent(7, 1, 2, 1)
    RingClient.cancel_chime_intent(intent)
    assert intent.cancelled
    ChimeClientState.cancel_intent(None)


@pytest.mark.asyncio
async def test_alarm_and_chime_share_core_and_feature_close_keeps_it_open(monkeypatch):
    client = active_client(monkeypatch)
    core = client._get_ring_json_transport()
    alarm = client._get_alarm_gateway()
    chime = client._chimes._get_service()
    assert alarm._transport is core
    assert client._chimes._gateway._transport is core
    assert chime is client._chimes._get_service()
    await alarm.close()
    assert not core._closed
    await client._chimes.close()
    assert not core._closed
    await core.close()


@pytest.mark.asyncio
async def test_close_orders_feature_work_then_shared_core_then_auth(monkeypatch):
    client = active_client(monkeypatch)
    calls = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def close(self):
            calls.append(self.name)
            if self.name == "chime-service":
                await client._chimes._gateway.close()

    client._alarm_gateway = Resource("alarm")
    client._chimes._service = Resource("chime-service")
    client._chimes._gateway = Resource("chime-gateway")
    client._ring_json_transport = Resource("core")

    async def close_auth(_auth):
        calls.append("auth")

    monkeypatch.setattr(client, "_close_auth", close_auth)
    await client._async_close()
    assert calls == ["alarm", "chime-service", "chime-gateway", "core", "auth"]


@pytest.mark.asyncio
async def test_retired_read_result_does_not_return_old_account_data(monkeypatch):
    client = active_client(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    class Service:
        async def refresh(self, **_kwargs):
            started.set()
            await release.wait()
            return ChimeAccountSnapshot(7, devices=(ChimeSnapshot(1, "Old", "chime", "Chime"),))

    client._chimes._service = Service()
    task = asyncio.create_task(client._chimes.refresh())
    await started.wait()
    client._chimes.invalidate(8)
    release.set()
    with pytest.raises(RuntimeError, match="superseded"):
        await task


@pytest.mark.asyncio
async def test_concurrent_close_joins_chime_cleanup_before_auth(monkeypatch):
    client = active_client(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Service:
        async def close(self):
            calls.append("chime-start")
            started.set()
            await release.wait()
            calls.append("chime-closed")

    client._chimes._service = Service()

    async def close_auth(_auth):
        calls.append("auth")

    monkeypatch.setattr(client, "_close_auth", close_auth)
    first = asyncio.create_task(client._async_close())
    await started.wait()
    second = asyncio.create_task(client._async_close())
    await asyncio.sleep(0)
    assert "auth" not in calls
    release.set()
    await asyncio.gather(first, second)
    assert calls == ["chime-start", "chime-closed", "auth"]


@pytest.mark.asyncio
async def test_retirement_while_shared_slot_is_busy_blocks_unsent_sound(monkeypatch):
    client = active_client(monkeypatch)
    gate = asyncio.Event()
    inventory = {
        "chimes": [
            {
                "id": 1,
                "kind": "chime",
                "owned": True,
                "settings": {"volume": 2},
                "alerts": {"connection": "online"},
            }
        ]
    }
    auth, calls, _ = fake_auth(Response(json.dumps(inventory).encode()), Response(gate=gate))
    client._ring.auth = auth
    service = client._chimes._get_service()
    await service.refresh()
    device = service.snapshot.find_device(1)
    core = client._get_ring_json_transport()
    occupied = asyncio.create_task(core.request_json("https://api.ring.com/occupied"))
    async with asyncio.timeout(1):
        while len(calls) != 2:
            await asyncio.sleep(0)
    intent = ChimeIntent(7, 1, device.revision, device.identity_epoch)
    command = asyncio.create_task(
        service.request_test_sound(1, "ding", expected_revision=device.revision, intent=intent)
    )
    async with asyncio.timeout(1):
        while not core._background:
            await asyncio.sleep(0)
    client._chimes.invalidate(8)
    gate.set()
    await occupied
    outcome = await command
    assert outcome.code == "session-changed"
    assert len(calls) == 2
    assert client.get_chime_snapshot().devices == ()
    await client._chimes.close()
    await core.close()


@pytest.mark.asyncio
async def test_retirement_blocks_queued_read_before_old_account_http(monkeypatch):
    client = active_client(monkeypatch)
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=gate))
    client._ring.auth = auth
    core = client._get_ring_json_transport()
    occupied = asyncio.create_task(core.request_json("https://api.ring.com/occupied"))
    async with asyncio.timeout(1):
        while not calls:
            await asyncio.sleep(0)
    read = asyncio.create_task(client._chimes.refresh(force=True))
    async with asyncio.timeout(1):
        while not core._background:
            await asyncio.sleep(0)
    client._chimes.invalidate(8)
    gate.set()
    await occupied
    with pytest.raises(RuntimeError, match="superseded"):
        await read
    assert len(calls) == 1
    assert client.get_chime_snapshot().devices == ()
    await client._chimes.close()
    await core.close()
