"""Intercom publication, bounded event work and client shutdown ordering."""

import asyncio
import json
from dataclasses import replace

import pytest

from halo_gtk.intercom import IntercomAccountSnapshot, IntercomIntent
from tests.test_alarm_rest import Response, fake_auth
from tests.test_intercom import inventory
from tests.test_ring_client_light_groups import active as lighting_active


def active(monkeypatch, auth=None):
    client = lighting_active(monkeypatch, auth)
    client._intercoms.initialize(7)
    return client


def test_intercom_cached_snapshot_is_lazy_and_retirement_clears_names(monkeypatch):
    from gi.repository import GLib

    jobs = []
    monkeypatch.setattr(GLib, "idle_add", lambda *args: jobs.append(args))
    client = active(monkeypatch)
    assert client.get_intercom_snapshot() == IntercomAccountSnapshot(7)
    assert client._ring_json_transport is None
    seen = []
    client.add_intercom_callback(seen.append)
    snapshot = IntercomAccountSnapshot(7, revision=1)
    client._intercoms._on_snapshot(snapshot)
    client._intercoms._on_snapshot(replace(snapshot, revision=2))
    assert len(jobs) == 1
    callback, *args = jobs.pop()
    callback(*args)
    assert seen == [replace(snapshot, revision=2)]
    client._intercoms._on_snapshot(replace(snapshot, revision=3))
    client._intercoms.invalidate(8)
    for callback, *args in jobs:
        callback(*args)
    assert len(seen) == 1 and client.get_intercom_snapshot() == IntercomAccountSnapshot(8)


@pytest.mark.asyncio
async def test_actual_client_gateway_queued_unlock_is_fenced_before_send_on_retirement(monkeypatch):
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(json.dumps(inventory()).encode()), Response(gate=gate))
    client = active(monkeypatch, auth)
    await client._intercoms.refresh()
    device = client.get_intercom_snapshot().find_device(42)
    core = client._get_ring_json_transport()
    blocker = asyncio.create_task(core.request_json("https://api.ring.com/occupied"))
    while len(calls) < 2:
        await asyncio.sleep(0)
    approval = IntercomIntent(7, 42, device.revision, device.identity_epoch)
    release = asyncio.create_task(
        client._intercoms.command(42, expected_revision=device.revision, intent=approval)
    )
    while not core._background:
        await asyncio.sleep(0)
    client._intercoms.invalidate(8)
    gate.set()
    await blocker
    with pytest.raises(RuntimeError, match="superseded"):
        await release
    assert [method for method, *_ in calls] == ["GET", "GET"]
    await client._intercoms.close()
    assert not core._closed
    await core.close()


@pytest.mark.asyncio
async def test_intercom_event_work_is_bounded_and_joined_on_close(monkeypatch):
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(json.dumps(inventory()).encode(), gate=gate))
    client = active(monkeypatch, auth)
    for _ in range(100):
        client._intercoms.queue_event({"kind": "ding"})
    assert len(client._intercoms._event_tasks) == 4
    await asyncio.sleep(0)
    await asyncio.gather(client._intercoms.close(), client._intercoms.close())
    assert not client._intercoms._event_tasks
    assert client._intercoms._service is None
    assert len(calls) <= 1
    await client._get_ring_json_transport().close()


@pytest.mark.asyncio
async def test_incoming_call_can_discover_lazily_without_devices_page_and_never_writes(monkeypatch):
    import time

    from gi.repository import GLib

    jobs = []
    monkeypatch.setattr(GLib, "idle_add", lambda *args: jobs.append(args))
    auth, calls, _ = fake_auth(Response(json.dumps(inventory()).encode()))
    client = active(monkeypatch, auth)
    event = {
        "doorbot_id": 42,
        "id": 12,
        "kind": "ding",
        "device_kind": "intercom_handset_audio",
        "now": time.time(),
    }
    client._on_ring_event(event)
    while client._intercoms._event_tasks:
        await asyncio.sleep(0)
    device = client.get_intercom_snapshot().find_device(42)
    assert device.observations[0].kind == "ding"
    assert [method for method, *_ in calls] == ["GET"]
    assert any(callback == client._dispatch_intercom_event for callback, *_ in jobs)
    await client._intercoms.close()
    await client._get_ring_json_transport().close()
