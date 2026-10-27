"""Bounded, session-only experimental Ring cloud Alarm history.

The consumer contract is pinned in the planning evidence: ring-mqtt 5.9.3
security-panel.js consumes DeviceInfoDocType/impulse.v1 and context timestamps.
There are no live fixtures or verified unique history IDs. Local row keys are
presentation identities, never server event IDs or proof of complete coverage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from halo_gtk.alarm.provider import AlarmRequestError

COVERAGE = "Experimental Ring cloud history; event coverage and pagination are unverified."
_TITLES = {
    "security-panel.mode-switched.none": ("mode", "Disarmed"),
    "security-panel.mode-switched.some": ("mode", "Armed Home"),
    "security-panel.mode-switched.all": ("mode", "Armed Away"),
    "security-panel.exit-delay": ("mode", "Exit delay reported"),
    "security-panel.alarm-cleared": ("alarm", "Alarm cleared"),
}


class HistoryGateway(Protocol):
    async def request_history(self, location_id: str, *, limit: int, offset: int) -> list:
        """Return a bounded list from the fixed, authenticated Alarm endpoint."""


class AlarmHistoryStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission-denied"
    AUTHENTICATION_REQUIRED = "authentication-required"
    RATE_LIMITED = "rate-limited"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed-out"
    BUSY = "busy"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AlarmHistoryEvent:
    key: str
    location_id: str
    kind: str
    title: str
    occurred_at: datetime | None
    retrieved_at: datetime
    zid: str | None = None
    asset_id: str | None = None
    event_id: str | None = None
    actor: str | None = None
    impulse_types: tuple[str, ...] = ()
    provenance: str = "Ring cloud history"
    experimental: bool = True


@dataclass(frozen=True, slots=True)
class AlarmHistoryPage:
    location_id: str
    events: tuple[AlarmHistoryEvent, ...] = ()
    status: AlarmHistoryStatus = AlarmHistoryStatus.UNAVAILABLE
    offset: int = 0
    next_offset: int | None = None
    truncated: bool = False
    from_cache: bool = False
    stale: bool = False
    unsupported_rows: int = 0
    message: str = "Alarm history is unavailable."
    coverage: str = COVERAGE


def _text(value: object, limit: int = 256) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value.strip()


def _occurred_at(value: object) -> datetime | None:
    # This exact field is milliseconds in the inspected consumer. Do not guess
    # seconds/microseconds/ISO units or substitute the retrieval time.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not 946684800000 <= value < 4102444800000:
        return None  # local plausibility policy: 2000 <= year < 2100
    return datetime.fromtimestamp(value / 1000, UTC)


def parse_history(
    location_id: str, rows: list, *, offset: int, limit: int, retrieved_at: datetime
) -> AlarmHistoryPage:
    """Normalize only the source-grounded envelope; never invent IDs/actors."""
    if not isinstance(rows, list) or len(rows) > limit:
        return AlarmHistoryPage(
            location_id,
            status=AlarmHistoryStatus.UNSUPPORTED,
            offset=offset,
            message="Unsupported Alarm history response.",
        )
    events = []
    unsupported = 0
    for index, row in enumerate(rows):
        if (
            not isinstance(row, Mapping)
            or row.get("msg") != "DataUpdate"
            or (row.get("datatype") != "DeviceInfoDocType")
        ):
            unsupported += 1
            continue
        body, context = row.get("body"), row.get("context")
        if not isinstance(body, list) or not 1 <= len(body) <= 128:
            unsupported += 1
            continue
        types, zids = [], set()
        invalid = False
        for document in body:
            if not isinstance(document, Mapping):
                invalid = True
                break
            general = document.get("general")
            general = general.get("v2") if isinstance(general, Mapping) else None
            zid = _text(general.get("zid"), 512) if isinstance(general, Mapping) else None
            if zid:
                zids.add(zid)
            impulses = document.get("impulse")
            impulses = impulses.get("v1") if isinstance(impulses, Mapping) else None
            if not isinstance(impulses, list) or not 1 <= len(impulses) <= 32:
                invalid = True
                break
            for impulse in impulses:
                kind = (
                    _text(impulse.get("impulseType"), 128) if isinstance(impulse, Mapping) else None
                )
                if kind is None or not all(
                    c.isascii() and (c.isalnum() or c in ".-_") for c in kind
                ):
                    invalid = True
                    break
                types.append(kind)
            if invalid or len(types) > 32:
                invalid = True
                break
        if invalid or not types:
            unsupported += 1
            continue
        context = context if isinstance(context, Mapping) else {}
        occurred = _occurred_at(context.get("eventOccurredTsMs"))
        actor = _text(context.get("initiatingEntityName"))
        # Entity type/ID is not a person's display name. No enrichment POST.
        unique_types = tuple(dict.fromkeys(types))
        kind, title = _TITLES.get(unique_types[0], ("other", "Unrecognized Alarm event"))
        if len(unique_types) > 1:
            kind, title = "other", "Multiple Alarm updates"
        partial = occurred is None or any(t not in _TITLES for t in unique_types)
        unsupported += int(partial)
        zid = next(iter(zids)) if len(zids) == 1 else None
        canonical = json.dumps(
            [
                location_id,
                zid,
                types,
                context.get("eventOccurredTsMs") if occurred else None,
                actor,
            ],
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        # Preserve identical legitimate rows. Positional keys do NOT deduplicate
        # overlapping offset pages; no verified server identity exists yet.
        events.append(
            AlarmHistoryEvent(
                key=f"{digest}:{offset + index}",
                location_id=location_id,
                kind=kind,
                title=title,
                occurred_at=occurred,
                retrieved_at=retrieved_at,
                zid=zid,
                actor=actor,
                impulse_types=unique_types,
            )
        )
    events.sort(
        key=lambda event: (event.occurred_at or datetime.min.replace(tzinfo=UTC), event.key),
        reverse=True,
    )
    status = AlarmHistoryStatus.PARTIAL if unsupported else AlarmHistoryStatus.READY
    if not rows:
        status = AlarmHistoryStatus.EMPTY
    elif not events:
        status = AlarmHistoryStatus.UNSUPPORTED
    next_offset = offset + len(rows) if len(rows) == limit else None
    message = {
        AlarmHistoryStatus.READY: "Ring cloud events returned; coverage is unverified.",
        AlarmHistoryStatus.EMPTY: "Ring returned no Alarm events for this page.",
        AlarmHistoryStatus.PARTIAL: "Some Alarm event fields or types are unsupported.",
        AlarmHistoryStatus.UNSUPPORTED: "Unsupported Alarm history response.",
    }[status]
    return AlarmHistoryPage(
        location_id,
        tuple(events),
        status,
        offset,
        next_offset,
        next_offset is not None or bool(unsupported),
        unsupported_rows=unsupported,
        message=message,
    )


@dataclass(slots=True)
class _Job:
    task: asyncio.Task
    waiters: int = 0


class AlarmHistoryService:
    """One active fetch, two pending/coalesced jobs; all work has a deadline.

    Owned by one client's persistent asyncio loop. Cache bytes account for the
    normalized payload, not Python allocator/RSS overhead. No durable journal.
    """

    def __init__(
        self,
        gateway: HistoryGateway,
        *,
        timeout: float = 30,
        cache_ttl: float = 300,
        max_cache_rows: int = 1000,
        max_cache_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.gateway = gateway
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_cache_rows = max_cache_rows
        self.max_cache_bytes = max_cache_bytes
        self._slots = asyncio.Semaphore(1)
        self._jobs: dict[tuple[str, int, int], _Job] = {}
        self._owned_tasks: dict[asyncio.Task, str] = {}
        self._cache: OrderedDict[tuple[str, int, int], tuple[float, AlarmHistoryPage, int]] = (
            OrderedDict()
        )
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._location_generations: dict[str, int] = {}

    @staticmethod
    def _cancel_task(task: asyncio.Task) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        current = asyncio.current_task() if running else None
        if task is current or task.done() or task.cancelling():
            return
        loop = task.get_loop()
        if running is loop:
            task.cancel()
        elif not loop.is_closed():
            with suppress(RuntimeError):  # the client loop may close concurrently
                loop.call_soon_threadsafe(AlarmHistoryService._cancel_task, task)

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            tasks = tuple(self._owned_tasks)
            self._cache.clear()
        for task in tasks:
            self._cancel_task(task)

    def _retire_task(self, task: asyncio.Task) -> None:
        with self._lock:
            self._owned_tasks.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        """Cancel owned HTTP work before the client closes its Auth transport."""
        with self._lock:
            self._closed = True
            tasks = tuple(self._owned_tasks)
        self.clear()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=1)
            for task in done:
                if not task.cancelled():
                    task.exception()
            for task in pending:
                task.cancel()

    def revoke_location(self, location_id: str) -> None:
        with self._lock:
            self._location_generations[location_id] = (
                self._location_generations.get(location_id, 0) + 1
            )
            tasks = tuple(
                task for task, location in self._owned_tasks.items() if location == location_id
            )
            for key in list(self._cache):
                if key[0] == location_id:
                    del self._cache[key]
        for task in tasks:
            self._cancel_task(task)

    async def fetch_page(
        self, location_id: str, *, offset: int = 0, limit: int = 50, force: bool = False
    ) -> AlarmHistoryPage:
        if _text(location_id, 512) != location_id:
            raise ValueError("Invalid Alarm location")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000:
            raise ValueError("Invalid history offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Invalid history limit")
        key = location_id, offset, limit
        with self._lock:
            if self._closed:
                return AlarmHistoryPage(
                    location_id,
                    status=AlarmHistoryStatus.CANCELLED,
                    offset=offset,
                    message="Alarm history request cancelled.",
                )
            cached = self._cache.get(key)
            if cached and not force and time.monotonic() - cached[0] < self.cache_ttl:
                self._cache.move_to_end(key)
                return replace(cached[1], from_cache=True)
            job = self._jobs.get(key)
            if job is None:
                if len(self._jobs) >= 3:
                    return AlarmHistoryPage(
                        location_id,
                        status=AlarmHistoryStatus.BUSY,
                        offset=offset,
                        message="Alarm history is busy. Try again.",
                    )
                task = asyncio.create_task(
                    self._fetch(
                        key, self._generation, self._location_generations.get(location_id, 0)
                    )
                )
                job = self._jobs[key] = _Job(task)
                self._owned_tasks[task] = location_id
                task.add_done_callback(self._retire_task)
            job.waiters += 1
        try:
            return await asyncio.shield(job.task)
        finally:
            with self._lock:
                job.waiters -= 1
                if not job.waiters:
                    if not job.task.done():
                        job.task.cancel()
                    if self._jobs.get(key) is job:
                        del self._jobs[key]

    async def _fetch(
        self, key: tuple[str, int, int], generation: int, location_generation: int
    ) -> AlarmHistoryPage:
        location_id, offset, limit = key
        try:
            async with asyncio.timeout(self.timeout):
                async with self._slots:
                    rows = await self.gateway.request_history(
                        location_id, limit=limit, offset=offset
                    )
                    page = parse_history(
                        location_id,
                        rows,
                        offset=offset,
                        limit=limit,
                        retrieved_at=datetime.now(UTC),
                    )
                with self._lock:
                    if generation != self._generation or location_generation != (
                        self._location_generations.get(location_id, 0)
                    ):
                        raise asyncio.CancelledError
                    if page.status in {
                        AlarmHistoryStatus.READY,
                        AlarmHistoryStatus.EMPTY,
                        AlarmHistoryStatus.PARTIAL,
                    }:
                        self._save_cache(key, page)
                    elif page.status is AlarmHistoryStatus.UNSUPPORTED:
                        self._cache.pop(key, None)
                return page
        except TimeoutError:
            status, message = AlarmHistoryStatus.TIMED_OUT, "Alarm history timed out. Try again."
        except AlarmRequestError as exc:
            status, message = {
                "permission-denied": (
                    AlarmHistoryStatus.PERMISSION_DENIED,
                    "Ring denied access to Alarm history.",
                ),
                "authentication-required": (
                    AlarmHistoryStatus.AUTHENTICATION_REQUIRED,
                    "Sign in again to read Alarm history.",
                ),
                "rate-limited": (
                    AlarmHistoryStatus.RATE_LIMITED,
                    "Ring limited Alarm history requests. Try again later.",
                ),
                "invalid-response": (
                    AlarmHistoryStatus.UNSUPPORTED,
                    "Unsupported Alarm history response.",
                ),
                "response-too-large": (
                    AlarmHistoryStatus.UNSUPPORTED,
                    "Alarm history exceeded the response limit.",
                ),
                "unsupported-encoding": (
                    AlarmHistoryStatus.UNSUPPORTED,
                    "Unsupported Alarm history response.",
                ),
                "timeout": (AlarmHistoryStatus.TIMED_OUT, "Alarm history timed out. Try again."),
                "busy": (AlarmHistoryStatus.BUSY, "Alarm history is busy. Try again."),
                "token-refresh-failed": (
                    AlarmHistoryStatus.UNAVAILABLE,
                    "Alarm authorization could not be refreshed. Try signing in again.",
                ),
            }.get(exc.code, (AlarmHistoryStatus.UNAVAILABLE, "Alarm history is unavailable."))
            if status in {
                AlarmHistoryStatus.PERMISSION_DENIED,
                AlarmHistoryStatus.AUTHENTICATION_REQUIRED,
            }:
                self.revoke_location(location_id)
            elif status is AlarmHistoryStatus.UNSUPPORTED:
                with self._lock:
                    self._cache.pop(key, None)
        except Exception:
            status, message = AlarmHistoryStatus.UNAVAILABLE, "Alarm history is unavailable."
        with self._lock:
            cached = self._cache.get(key)
            if cached and status in {
                AlarmHistoryStatus.UNAVAILABLE,
                AlarmHistoryStatus.TIMED_OUT,
                AlarmHistoryStatus.RATE_LIMITED,
            }:
                return replace(
                    cached[1],
                    status=status,
                    from_cache=True,
                    stale=True,
                    message=f"{message} Showing previously retrieved events.",
                )
        return AlarmHistoryPage(location_id, status=status, offset=offset, message=message)

    def _save_cache(self, key: tuple[str, int, int], page: AlarmHistoryPage) -> None:
        size = sum(
            len(
                json.dumps(
                    [e.key, e.location_id, e.kind, e.title, e.actor, e.zid, e.impulse_types],
                    ensure_ascii=True,
                ).encode()
            )
            + 128
            for e in page.events
        )
        self._cache[key] = time.monotonic(), page, size
        self._cache.move_to_end(key)
        while self._cache and (
            sum(len(item[1].events) for item in self._cache.values()) > self.max_cache_rows
            or sum(item[2] for item in self._cache.values()) > self.max_cache_bytes
            or len(self._cache) > 100  # also bound empty pages
        ):
            self._cache.popitem(last=False)

    async def load(
        self,
        location_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        max_pages: int = 5,
        force: bool = False,
    ) -> AlarmHistoryPage:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 5:
            raise ValueError("Invalid history page count")
        start_offset = offset
        events, fingerprints = [], set()
        last = AlarmHistoryPage(location_id, offset=offset)
        try:
            async with asyncio.timeout(self.timeout):
                for _ in range(max_pages):
                    page = await self.fetch_page(
                        location_id, offset=offset, limit=limit, force=force
                    )
                    if page.status in {
                        AlarmHistoryStatus.PERMISSION_DENIED,
                        AlarmHistoryStatus.AUTHENTICATION_REQUIRED,
                    }:
                        return replace(page, events=(), next_offset=None, offset=start_offset)
                    fingerprint = tuple(e.key.rsplit(":", 1)[0] for e in page.events)
                    if fingerprint and fingerprint in fingerprints:
                        return replace(
                            page,
                            events=tuple(events),
                            next_offset=None,
                            truncated=True,
                            status=AlarmHistoryStatus.PARTIAL,
                            message="Ring repeated a history page; pagination stopped.",
                        )
                    fingerprints.add(fingerprint)
                    events.extend(page.events)
                    last = page
                    if page.next_offset is None or page.status not in {
                        AlarmHistoryStatus.READY,
                        AlarmHistoryStatus.PARTIAL,
                    }:
                        break
                    offset = page.next_offset
        except TimeoutError:
            last = replace(
                last,
                status=AlarmHistoryStatus.TIMED_OUT,
                truncated=True,
                message="Alarm history timed out; coverage is incomplete.",
            )
        events.sort(
            key=lambda e: (e.occurred_at or datetime.min.replace(tzinfo=UTC), e.key), reverse=True
        )
        return replace(
            last,
            events=tuple(events),
            truncated=last.truncated or (last.next_offset is not None),
            offset=start_offset,
        )
