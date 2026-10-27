from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from halo_gtk.alarm.history import (
    COVERAGE,
    AlarmHistoryService,
    AlarmHistoryStatus,
    parse_history,
)
from halo_gtk.alarm.provider import AlarmRequestError

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def row(kind="security-panel.mode-switched.some", timestamp=1792972800000, actor="Pat"):
    return {
        "msg": "DataUpdate",
        "datatype": "DeviceInfoDocType",
        "context": {
            "eventOccurredTsMs": timestamp,
            "initiatingEntityName": actor,
            "initiatingEntityId": "not-an-event-id",
            "initiatingEntityType": "user",
        },
        "body": [{"general": {"v2": {"zid": "panel"}}, "impulse": {"v1": [{"impulseType": kind}]}}],
    }


def parsed(rows, **kwargs):
    return parse_history(
        "loc", rows, offset=kwargs.get("offset", 0), limit=kwargs.get("limit", 50), retrieved_at=NOW
    )


def test_source_grounded_fields_have_separate_local_identity_and_provenance():
    event = parsed([row()]).events[0]
    assert event.title == "Armed Home"
    assert event.zid == "panel"
    assert event.event_id is None and event.asset_id is None
    assert event.actor == "Pat"
    assert event.occurred_at == NOW
    assert event.retrieved_at == NOW
    assert event.provenance == "Ring cloud history" and event.experimental
    assert parsed([row()]).coverage == COVERAGE


@pytest.mark.parametrize(
    "kind,title",
    [
        ("security-panel.mode-switched.none", "Disarmed"),
        ("security-panel.mode-switched.all", "Armed Away"),
        ("security-panel.exit-delay", "Exit delay reported"),
        ("security-panel.alarm-cleared", "Alarm cleared"),
    ],
)
def test_only_known_impulses_receive_specific_titles(kind, title):
    assert parsed([row(kind)]).events[0].title == title


def test_unknown_type_and_missing_actor_or_time_remain_generic_not_invented():
    page = parsed([row("sensor.unknown", timestamp=None, actor=None)])
    event = page.events[0]
    assert page.status is AlarmHistoryStatus.PARTIAL
    assert event.title == "Unrecognized Alarm event"
    assert event.occurred_at is None and event.actor is None
    assert event.event_id is None


@pytest.mark.parametrize(
    "value", [True, "1792972800000", float("nan"), float("inf"), 10**1000, 1792972800, -1, 0]
)
def test_invalid_timestamp_never_falls_back_to_arrival_or_guesses_units(value):
    page = parsed([row(timestamp=value)])
    assert page.events[0].occurred_at is None
    assert page.status is AlarmHistoryStatus.PARTIAL


@pytest.mark.parametrize(
    "rows",
    [{}, [None], [{"kind": "ding"}], [{"msg": "DataUpdate", "datatype": "Other", "body": []}]],
)
def test_unrecognized_payload_is_unsupported_not_empty(rows):
    page = parsed(rows)
    assert page.status is AlarmHistoryStatus.UNSUPPORTED
    assert not page.events


def test_empty_page_is_distinct_from_unsupported_and_full_page_is_uncertain():
    assert parsed([]).status is AlarmHistoryStatus.EMPTY
    page = parsed([row()], limit=1)
    assert page.next_offset == 1 and page.truncated
    assert "unverified" in page.coverage
    assert parsed([row(), row()], limit=1).status is AlarmHistoryStatus.UNSUPPORTED


def test_duplicate_occurrences_preserved_and_multi_impulse_has_no_invented_single_event():
    page = parsed([row(), row()])
    assert len(page.events) == 2 and page.events[0].key != page.events[1].key
    multi = row()
    multi["body"][0]["impulse"]["v1"].append({"impulseType": "security-panel.alarm-cleared"})
    event = parsed([multi]).events[0]
    assert event.title == "Multiple Alarm updates"
    assert len(event.impulse_types) == 2


def test_actor_control_characters_oversized_impulses_and_missing_body_are_bounded():
    assert parsed([row(actor="secret\ntext")]).events[0].actor is None
    assert parsed([row(actor="x" * 257)]).events[0].actor is None
    bad = row()
    bad["body"][0]["impulse"]["v1"] *= 33
    assert parsed([bad]).status is AlarmHistoryStatus.UNSUPPORTED
    assert parsed([row("<script>")]).status is AlarmHistoryStatus.UNSUPPORTED


class Gateway:
    def __init__(self, pages=None):
        self.pages = pages or {0: [row()]}
        self.calls = []
        self.error = None

    async def request_history(self, location_id, *, offset, limit):
        self.calls.append((location_id, offset, limit))
        if self.error:
            raise self.error
        return deepcopy(self.pages.get(offset, []))


async def test_cache_five_minute_policy_and_explicit_forced_refresh(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("halo_gtk.alarm.history.time.monotonic", lambda: clock[0])
    gateway = Gateway()
    service = AlarmHistoryService(gateway)
    await service.fetch_page("loc")
    assert (await service.fetch_page("loc")).from_cache
    clock[0] = 301
    assert not (await service.fetch_page("loc")).from_cache
    await service.fetch_page("loc", force=True)
    assert len(gateway.calls) == 3


@pytest.mark.parametrize(
    "code,status",
    [
        ("authentication-required", AlarmHistoryStatus.AUTHENTICATION_REQUIRED),
        ("permission-denied", AlarmHistoryStatus.PERMISSION_DENIED),
        ("rate-limited", AlarmHistoryStatus.RATE_LIMITED),
        ("invalid-response", AlarmHistoryStatus.UNSUPPORTED),
        ("response-too-large", AlarmHistoryStatus.UNSUPPORTED),
        ("unsupported-encoding", AlarmHistoryStatus.UNSUPPORTED),
        ("timeout", AlarmHistoryStatus.TIMED_OUT),
        ("busy", AlarmHistoryStatus.BUSY),
        ("token-refresh-failed", AlarmHistoryStatus.UNAVAILABLE),
    ],
)
async def test_gateway_status_mapping_and_safe_errors(code, status):
    gateway = Gateway()
    gateway.error = AlarmRequestError(code)
    page = await AlarmHistoryService(gateway).fetch_page("loc")
    assert page.status is status
    assert not page.events
    assert "token" not in page.message and "http" not in page.message


async def test_network_error_uses_labeled_stale_cache_but_denial_purges_location():
    gateway = Gateway()
    service = AlarmHistoryService(gateway)
    await service.fetch_page("loc")
    await service.fetch_page("other")
    gateway.error = AlarmRequestError("network-unavailable")
    page = await service.fetch_page("loc", force=True)
    assert page.from_cache and page.stale and page.events
    gateway.error = AlarmRequestError("permission-denied")
    denied = await service.fetch_page("loc", force=True)
    assert not denied.events and not any(key[0] == "loc" for key in service._cache)
    assert any(key[0] == "other" for key in service._cache)


async def test_cache_row_byte_and_empty_page_caps_and_clear():
    gateway = Gateway()
    service = AlarmHistoryService(gateway, max_cache_rows=1)
    await service.fetch_page("loc")
    await service.fetch_page("other")
    assert sum(len(item[1].events) for item in service._cache.values()) == 1
    tiny = AlarmHistoryService(gateway, max_cache_bytes=1)
    await tiny.fetch_page("loc")
    assert not tiny._cache
    service.clear()
    assert not service._cache


@pytest.mark.parametrize("failure", ["auth", "schema", "transport-schema"])
async def test_known_bad_or_unauthorized_page_is_not_later_served_as_fresh_cache(failure):
    gateway = Gateway()
    service = AlarmHistoryService(gateway)
    await service.fetch_page("loc")
    if failure == "schema":
        gateway.pages[0] = [{"unexpected": "shape"}]
    else:
        gateway.error = AlarmRequestError(
            "authentication-required" if failure == "auth" else "invalid-response"
        )
    page = await service.fetch_page("loc", force=True)
    assert not page.events and not service._cache
    assert not (await service.fetch_page("loc")).from_cache


async def test_coalescing_cancelled_waiter_does_not_cancel_other_and_no_slot_leak():
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Slow(Gateway):
        async def request_history(self, location_id, *, offset, limit):
            self.calls.append((location_id, offset, limit))
            started.set()
            try:
                await release.wait()
                return [row()]
            finally:
                cancelled.set()

    gateway = Slow()
    service = AlarmHistoryService(gateway)
    a = asyncio.create_task(service.fetch_page("loc"))
    b = asyncio.create_task(service.fetch_page("loc"))
    await started.wait()
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    assert not cancelled.is_set()
    release.set()
    assert (await b).events
    assert len(gateway.calls) == 1 and service._slots._value == 1


async def test_last_waiter_cancel_and_cross_thread_clear_cancel_actual_request():
    started, stopped = asyncio.Event(), asyncio.Event()

    class Slow:
        async def request_history(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    for action in ("cancel", "clear"):
        started.clear()
        stopped.clear()
        service = AlarmHistoryService(Slow())
        task = asyncio.create_task(service.fetch_page("loc"))
        await started.wait()
        if action == "clear":
            thread = threading.Thread(target=service.clear)
            thread.start()
            thread.join(1)
            assert not thread.is_alive()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), 1)
        assert service._slots._value == 1 and not service._jobs


async def test_admission_queue_bounded_and_whole_deadline_covers_wait():
    started = asyncio.Event()

    class Slow:
        async def request_history(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    service = AlarmHistoryService(Slow(), timeout=0.03)
    tasks = [asyncio.create_task(service.fetch_page(str(i))) for i in range(3)]
    await started.wait()
    assert (await service.fetch_page("excess")).status is AlarmHistoryStatus.BUSY
    pages = await asyncio.gather(*tasks)
    assert all(page.status is AlarmHistoryStatus.TIMED_OUT for page in pages)
    assert service._slots._value == 1 and not service._jobs


async def test_close_awaits_cancelled_request_cleanup_even_after_last_waiter_retired():
    started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Slow:
        async def request_history(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()

    service = AlarmHistoryService(Slow())
    task = asyncio.create_task(service.fetch_page("loc"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cleanup.wait()
    assert not service._jobs and service._owned_tasks
    close = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    assert not service._owned_tasks and service._slots._value == 1
    assert (await service.fetch_page("loc")).status is AlarmHistoryStatus.CANCELLED


async def test_load_stops_at_five_pages_and_reports_uncertain_coverage():
    gateway = Gateway({offset: [row(timestamp=1792972800000 - offset)] for offset in range(10)})
    page = await AlarmHistoryService(gateway).load("loc", limit=1)
    assert len(gateway.calls) == 5 and len(page.events) == 5
    assert page.truncated and page.next_offset == 5


async def test_load_repeated_page_stops_without_claiming_all_duplicates_are_invalid():
    gateway = Gateway({0: [row()], 1: [row()]})
    page = await AlarmHistoryService(gateway).load("loc", limit=1)
    assert len(gateway.calls) == 2 and len(page.events) == 1
    assert page.status is AlarmHistoryStatus.PARTIAL and page.truncated
    assert page.next_offset is None


@pytest.mark.parametrize("code", ["permission-denied", "authentication-required"])
async def test_later_page_access_denial_discards_accumulated_rows(code):
    class Denied(Gateway):
        async def request_history(self, location_id, *, offset, limit):
            if offset:
                raise AlarmRequestError(code)
            return [row()]

    service = AlarmHistoryService(Denied())
    page = await service.load("loc", limit=1)
    assert not page.events and not service._cache and page.next_offset is None
    assert page.status in {
        AlarmHistoryStatus.PERMISSION_DENIED,
        AlarmHistoryStatus.AUTHENTICATION_REQUIRED,
    }


async def test_whole_load_budget_includes_later_pages_and_preserves_partial_result():
    class Slow(Gateway):
        async def request_history(self, location_id, *, offset, limit):
            if offset:
                await asyncio.Event().wait()
            return [row()]

    service = AlarmHistoryService(Slow(), timeout=0.03)
    page = await service.load("loc", limit=1)
    assert len(page.events) == 1 and page.status is AlarmHistoryStatus.TIMED_OUT
    assert page.truncated and page.offset == 0
    await service.close()
    assert not service._owned_tasks


@pytest.mark.parametrize(
    "kwargs", [{"limit": True}, {"limit": 51}, {"offset": -1}, {"offset": 10001}, {"offset": "0"}]
)
async def test_invalid_query_never_reaches_gateway(kwargs):
    gateway = Gateway()
    with pytest.raises(ValueError):
        await AlarmHistoryService(gateway).fetch_page("loc", **kwargs)
    assert not gateway.calls
