"""Beams discovery and shared lighting reservation integration, without network."""

import asyncio
from types import SimpleNamespace

import pytest

from halo_gtk.alarm.beams import BeamsIntent, BeamsResult
from halo_gtk.alarm.models import AlarmWriteAuthorization
from halo_gtk.alarm.rest import DEVICES_ENDPOINT
from tests.test_alarm_client_features import active_client


@pytest.mark.parametrize(
    "payload",
    [
        {"beams_bridges": [{"id": 123, "location_id": "home", "role": "owner"}]},
        {
            "base_stations": [],
            "beams_bridges": [{"id": 123, "location_id": "home", "role": "owner"}],
        },
    ],
)
async def test_actual_discovery_includes_beams_only_and_only_then_supplies_owner(
    monkeypatch, payload
):
    client = active_client(monkeypatch)
    seeds, owners = [], []

    async def reconcile(value):
        seeds.extend(value)

    class Gateway:
        async def request_json(self, url):
            return (
                payload
                if url == DEVICES_ENDPOINT
                else {"user_locations": [{"location_id": "home", "name": "Sample home"}]}
            )

    client._alarm_gateway = Gateway()
    client._alarm_service = SimpleNamespace(
        reconcile=reconcile, beams=SimpleNamespace(update_owners=owners.append)
    )
    assert await client._async_refresh_alarm_discovery()
    assert seeds[0].location_id == "home" and seeds[0].name == "Sample home"
    assert seeds[0].write_authorization is AlarmWriteAuthorization.UNKNOWN
    assert owners == [None, payload["beams_bridges"]]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"base_stations": [], "beams_bridges": {}},
        {"base_stations": [], "beams_bridges": [{"id": 123}]},
        {"base_stations": [], "beams_bridges": [{}] * 1025},
    ],
)
async def test_failed_discovery_revokes_bridge_authority_without_removing_inventory(
    monkeypatch, payload
):
    client = active_client(monkeypatch)
    owners, reconciled = [], []

    class Gateway:
        async def request_json(self, url):
            return payload if url == DEVICES_ENDPOINT else {"user_locations": []}

    async def reconcile(value):
        reconciled.append(value)

    client._alarm_gateway = Gateway()
    client._alarm_service = SimpleNamespace(
        reconcile=reconcile, beams=SimpleNamespace(update_owners=owners.append)
    )
    assert not await client._async_refresh_alarm_discovery()
    assert owners == [None] and not reconciled


async def test_beams_reserves_shared_lighting_before_preflight_and_releases_after_result(
    monkeypatch,
):
    client = active_client(monkeypatch)
    client._authenticated = True
    client._ring = object()
    client._lighting_actions.initialize(1)
    client._lighting_actions._is_current = lambda *_: True
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def refresh():
        entered.set()
        await release.wait()
        return True

    async def request(*args, **kwargs):
        assert client._lighting_actions.try_acquire() is None
        requests.append((args, kwargs))
        return BeamsResult("observed", "reported-on", True)

    monkeypatch.setattr(client, "_async_refresh_alarm_discovery", refresh)
    client._alarm_service = SimpleNamespace(beams=SimpleNamespace(request=request))
    intent = BeamsIntent(lambda: True)
    first = asyncio.create_task(client._async_beams_light("home", "bridge", "lamp", 60, 4, intent))
    await asyncio.wait_for(entered.wait(), 1)
    assert client._lighting_actions.try_acquire() is None
    second = await client._async_beams_light("home", "bridge", "lamp", None, 4, intent)
    assert second.code == "lighting-busy" and not requests
    release.set()
    assert (await first).status == "observed" and len(requests) == 1
    token = client._lighting_actions.try_acquire()
    assert token is not None
    client._lighting_actions.release(token)


async def test_preflight_cancellation_releases_shared_reservation_without_send(monkeypatch):
    client = active_client(monkeypatch)
    client._authenticated = True
    client._ring = object()
    client._lighting_actions.initialize(1)
    client._lighting_actions._is_current = lambda *_: True
    entered = asyncio.Event()

    async def refresh():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_async_refresh_alarm_discovery", refresh)
    client._alarm_service = SimpleNamespace(beams=None)
    pending = asyncio.create_task(
        client._async_beams_light("home", "bridge", "lamp", 60, 4, BeamsIntent(lambda: True))
    )
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    token = client._lighting_actions.try_acquire()
    assert token is not None
    client._lighting_actions.release(token)
