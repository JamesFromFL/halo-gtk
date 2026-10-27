"""Discovery can remove locations only after complete bounded normalization."""

import asyncio
from types import SimpleNamespace

import pytest

from halo_gtk import ring_client
from halo_gtk.alarm.rest import DEVICES_ENDPOINT
from halo_gtk.ring_client import RingClient


@pytest.fixture
def discovery(monkeypatch):
    client = RingClient(token_updater=lambda _token: None)
    client._published_generation = 1
    client._initialize_alarm_generation(1)
    monkeypatch.setattr(
        ring_client,
        "_is_active_client_generation",
        lambda owner, gen: owner is client and gen == client._published_generation,
    )
    records = []
    calls, reconciled = [], []

    class Gateway:
        async def request_json(self, url):
            calls.append(url)
            await asyncio.sleep(0)
            return {"base_stations": records} if url == DEVICES_ENDPOINT else {"user_locations": []}

    async def reconcile(seeds):
        reconciled.append(tuple(seed.location_id for seed in seeds))

    client._alarm_gateway = Gateway()
    client._alarm_service = SimpleNamespace(reconcile=reconcile)
    return client, records, calls, reconciled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity", [None, True, False, "", "  ", "a" * 513, "ho\nme", "home\x00", "\thome"]
)
async def test_invalid_identity_cannot_reconcile_empty_or_partial_inventory(discovery, identity):
    client, records, _calls, reconciled = discovery
    records.extend(({"location_id": "valid"}, {"location_id": identity}))
    assert not await client._async_refresh_alarm_discovery()
    assert reconciled == []
    assert client._alarm_discovery_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["a" * 512, 0, -42, " home "])
async def test_provider_valid_identity_normalization_is_preserved(discovery, identity):
    client, records, _calls, reconciled = discovery
    records.append({"location_id": identity})
    assert await client._async_refresh_alarm_discovery()
    assert reconciled == [(str(identity).strip(),)]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [None, True, "", "a" * 513, "ho\nme"])
async def test_provider_field_fallback_is_matched_before_counting_locations(discovery, first):
    client, records, _calls, reconciled = discovery
    records.extend(
        (
            {"location_id": first, "locationId": " home "},
            {"location_id": "home"},
        )
    )
    assert await client._async_refresh_alarm_discovery()
    assert reconciled == [("home",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("count, allowed", [(128, True), (129, False)])
async def test_location_limit_rejects_truncation_before_reconcile(discovery, count, allowed):
    client, records, _calls, reconciled = discovery
    records.extend({"location_id": f"location-{index}"} for index in range(count))
    assert await client._async_refresh_alarm_discovery() is allowed
    assert len(reconciled) == int(allowed)
    if allowed:
        assert len(reconciled[0]) == count
    else:
        assert client._alarm_discovery_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("count, allowed", [(1024, True), (1025, False)])
async def test_record_limit_allows_duplicate_stations_but_not_truncated_response(
    discovery, count, allowed
):
    client, records, _calls, reconciled = discovery
    records.extend({"location_id": "home"} for _ in range(count))
    assert await client._async_refresh_alarm_discovery() is allowed
    assert reconciled == ([("home",)] if allowed else [])


@pytest.mark.asyncio
async def test_empty_valid_discovery_is_authoritative(discovery):
    client, _records, _calls, reconciled = discovery
    assert await client._async_refresh_alarm_discovery()
    assert reconciled == [()]


@pytest.mark.asyncio
async def test_any_normalized_identity_set_mismatch_is_not_authoritative(discovery, monkeypatch):
    client, records, _calls, reconciled = discovery
    records.extend(({"location_id": "home"}, {"location_id": "other"}))
    original = ring_client.location_seeds_from_base_stations
    # Simulate a future forgiving parser dropping a record for another reason.
    monkeypatch.setattr(
        ring_client,
        "location_seeds_from_base_stations",
        lambda *args, **kwargs: original(*args, **kwargs)[:1],
    )
    assert not await client._async_refresh_alarm_discovery()
    assert reconciled == []


@pytest.mark.asyncio
async def test_service_replacement_while_reconciling_cannot_cache_success(discovery):
    client, records, calls, reconciled = discovery
    records.append({"location_id": "home"})
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_reconcile(_seeds):
        entered.set()
        await release.wait()

    client._alarm_service = SimpleNamespace(reconcile=blocked_reconcile)
    task = asyncio.create_task(client._async_refresh_alarm_discovery())
    await entered.wait()

    async def replacement_reconcile(seeds):
        reconciled.append(tuple(seed.location_id for seed in seeds))

    replacement = SimpleNamespace(reconcile=replacement_reconcile)
    client._alarm_service = replacement
    release.set()
    assert not await task
    assert client._alarm_discovery_at is None
    assert await client._async_refresh_alarm_discovery()
    assert client._alarm_discovery_service is replacement
    assert reconciled == [("home",)]
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_retirement_while_reconciling_cannot_cache_success(discovery, monkeypatch):
    client, records, _calls, _reconciled = discovery
    records.append({"location_id": "home"})
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_reconcile(_seeds):
        entered.set()
        await release.wait()

    client._alarm_service = SimpleNamespace(reconcile=blocked_reconcile)
    task = asyncio.create_task(client._async_refresh_alarm_discovery())
    await entered.wait()
    monkeypatch.setattr(ring_client, "_is_active_client_generation", lambda *_args: False)
    client._initialize_alarm_generation(2)
    release.set()
    assert not await task
    assert client._alarm_discovery_at is None


@pytest.mark.asyncio
async def test_success_cache_is_bound_to_generation_and_service(discovery):
    client, records, calls, reconciled = discovery
    records.append({"location_id": "home"})
    assert await client._async_refresh_alarm_discovery()
    assert await client._async_refresh_alarm_discovery()
    assert len(calls) == 2
    client._published_generation = 2
    client._initialize_alarm_generation(2)
    assert await client._async_refresh_alarm_discovery()
    assert len(calls) == 4
    original = client._alarm_service
    client._alarm_service = SimpleNamespace(reconcile=original.reconcile)
    assert await client._async_refresh_alarm_discovery()
    assert len(calls) == 6
    assert reconciled == [("home",)] * 3
