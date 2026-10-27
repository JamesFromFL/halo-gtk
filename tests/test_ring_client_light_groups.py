"""Client-owned group publication, retired HTTP admission and cleanup order."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_gtk import ring_client
from halo_gtk.light_group_models import (
    LightGroupIntent,
    LightGroupLocationSnapshot,
    LightGroupsSnapshot,
)
from halo_gtk.ring_client import RingClient
from tests.test_alarm_rest import Response, fake_auth


def active(monkeypatch, auth=None):
    client = RingClient()
    client._ring = SimpleNamespace(
        auth=auth or object(), devices=lambda: SimpleNamespace(all_devices=[])
    )
    client._authenticated = True
    client._published_generation = 7
    client._light_groups.initialize(7)
    client._lighting_actions.initialize(7)
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 7)
    return client


def test_cached_group_state_is_read_only_and_lazy():
    client = RingClient()
    assert client.get_light_group_snapshot() == LightGroupsSnapshot(0)
    assert client._ring_json_transport is None and client._light_groups._service is None


def test_group_snapshot_dispatch_coalesces_and_retirement_discards_private_locations(monkeypatch):
    from gi.repository import GLib

    idle = []
    monkeypatch.setattr(GLib, "idle_add", lambda *args: idle.append(args))
    client = active(monkeypatch)
    seen = []
    client.add_light_group_callback(seen.append)
    snapshot = LightGroupsSnapshot(
        7, revision=1, locations=(LightGroupLocationSnapshot("home", "Private site"),)
    )
    client._light_groups._on_snapshot(snapshot)
    client._light_groups._on_snapshot(replace(snapshot, revision=2))
    assert len(idle) == 1
    callback, *args = idle.pop()
    callback(*args)
    assert seen[-1].revision == 2
    client._light_groups._on_snapshot(replace(snapshot, revision=3))
    client._light_groups.invalidate(8)
    callback, *args = idle.pop()
    callback(*args)
    assert [value.revision for value in seen] == [2]
    assert client.get_light_group_snapshot() == LightGroupsSnapshot(8)


def test_group_producer_retirement_race_cannot_restore_old_account_names(monkeypatch):
    client = active(monkeypatch)

    def race(_generation):
        client._light_groups.invalidate(8)
        return True

    monkeypatch.setattr(client._light_groups, "current", race)
    client._light_groups._on_snapshot(
        LightGroupsSnapshot(7, locations=(LightGroupLocationSnapshot("home", "Old"),))
    )
    assert client.get_light_group_snapshot() == LightGroupsSnapshot(8)


@pytest.mark.asyncio
async def test_groups_share_core_and_reservation_without_closing_other_features(monkeypatch):
    client = active(monkeypatch)
    core = client._get_ring_json_transport()
    groups = client._light_groups._get_service()
    alarm = client._get_alarm_gateway()
    assert client._light_groups._gateway._transport is core
    assert groups._lighting_actions is client._lighting_actions
    await client._light_groups.close()
    assert not core._closed
    assert not alarm._closed
    await alarm.close()
    await core.close()


@pytest.mark.asyncio
async def test_queued_group_location_read_is_blocked_before_http_after_retirement(monkeypatch):
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=gate))
    client = active(monkeypatch, auth)
    core = client._get_ring_json_transport()
    occupied = asyncio.create_task(core.request_json("https://api.ring.com/occupied"))
    async with asyncio.timeout(1):
        while not calls:
            await asyncio.sleep(0)
    read = asyncio.create_task(client._light_groups.read("refresh_locations", force=True))
    async with asyncio.timeout(1):
        while not core._background:
            await asyncio.sleep(0)
    client._light_groups.invalidate(8)
    client._lighting_actions.invalidate(8)
    gate.set()
    await occupied
    with pytest.raises(RuntimeError, match="superseded"):
        await read
    assert len(calls) == 1
    await client._light_groups.close()
    await core.close()


@pytest.mark.asyncio
async def test_unsent_group_write_is_blocked_after_shared_slot_wait_and_session_retirement(
    monkeypatch,
):
    payloads = [
        {"user_locations": [{"location_id": "home", "name": "Home"}]},
        {
            "device_groups": [
                {
                    "device_group_id": "garden",
                    "location_id": "home",
                    "name": "Garden",
                    "devices": [
                        {
                            "doorbot_id": 10,
                            "location_id": "home",
                            "type": "hp_cam_v1",
                            "name": "Lamp",
                        }
                    ],
                }
            ]
        },
        {
            "device_group_id": "garden",
            "lights_on": False,
            "devices": [{"id": "10", "lights_on": False}],
        },
    ]
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(
        *(Response(json.dumps(value).encode()) for value in payloads), Response(gate=gate)
    )
    client = active(monkeypatch, auth)
    snapshot = await client._light_groups.read("refresh_details", "home", "garden")
    group = snapshot.find_group("home", "garden")
    assert group.can_write
    core = client._get_ring_json_transport()
    occupied = asyncio.create_task(core.request_json("https://api.ring.com/occupied"))
    async with asyncio.timeout(1):
        while len(calls) != 4:
            await asyncio.sleep(0)
    intent = LightGroupIntent(7, "home", "garden", group.revision, group.identity_epoch)
    command = asyncio.create_task(
        client._light_groups.command(
            "home", "garden", True, duration=60, expected_revision=group.revision, intent=intent
        )
    )
    async with asyncio.timeout(1):
        while not core._background:
            await asyncio.sleep(0)
    client._light_groups.invalidate(8)
    client._lighting_actions.invalidate(8)
    gate.set()
    await occupied
    with pytest.raises(RuntimeError, match="superseded"):
        await command
    assert len(calls) == 4 and client._lighting_actions._active is None
    await client._light_groups.close()
    await core.close()


@pytest.mark.asyncio
async def test_concurrent_client_close_joins_group_cleanup_before_core_and_auth(monkeypatch):
    client = active(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Service:
        async def close(self):
            seen.append("groups-start")
            started.set()
            await release.wait()
            seen.append("groups-closed")

    class Core:
        async def close(self):
            seen.append("core")

    client._light_groups._service = Service()
    client._ring_json_transport = Core()

    async def close_auth(_auth):
        seen.append("auth")

    monkeypatch.setattr(client, "_close_auth", close_auth)
    first = asyncio.create_task(client._async_close())
    await started.wait()
    second = asyncio.create_task(client._async_close())
    await asyncio.sleep(0)
    assert seen == ["groups-start"]
    release.set()
    await asyncio.gather(first, second)
    assert seen == ["groups-start", "groups-closed", "core", "auth"]
