"""Bounded Chime state and explicit, session-owned legacy operations.

An accepted HTTP response is not proof of audible playback. Volume confirmation
requires a subsequent authoritative inventory report. No actuator is retried.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum

from halo_gtk.chime_rest import ChimeRestError

MAX_CHIMES = 128
INVENTORY_FRESH_SECONDS = 30.0
INVENTORY_CACHE_SECONDS = 5.0
HEALTH_CACHE_SECONDS = 60.0
SUPPORTED_KINDS = frozenset({"chime", "chime_v2", "chime_pro", "chime_pro_v2"})
_MODELS = {
    "chime": "Chime",
    "chime_v2": "Chime v2 (legacy contract)",
    "chime_pro": "Chime Pro",
    "chime_pro_v2": "Chime Pro v2 (legacy contract)",
}


def chime_monotonic_time() -> float:
    """Elapsed time including suspend; sleeping cannot extend write freshness."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    return time.clock_gettime(clock) if clock is not None else time.monotonic()


class ChimeReadStatus(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    STALE = "stale"
    ERROR = "error"
    CLOSED = "closed"


class ChimeConnectionStatus(StrEnum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


class ChimeOperationStatus(StrEnum):
    PREPARING = "preparing"
    POSSIBLY_SENT = "possibly-sent"
    ACCEPTED = "accepted"
    REPORTED = "reported"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChimeIntent:
    """A view's cancelable approval; cancellation never pretends to undo a send."""

    generation: int
    api_id: int
    revision: int
    identity_epoch: int
    _cancelled: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


@dataclass(frozen=True, slots=True)
class ChimeHealthSnapshot:
    stale: bool = True
    status: ChimeReadStatus = ChimeReadStatus.UNKNOWN
    code: str | None = None
    observed_at: float | None = None
    observed_monotonic: float | None = None
    wifi_signal_strength: int | None = None
    wifi_signal_category: str | None = None
    firmware: str | None = None


@dataclass(frozen=True, slots=True)
class ChimeOperationSnapshot:
    operation_id: int
    kind: str
    status: ChimeOperationStatus
    active: bool
    possibly_sent: bool = False
    requested_volume: int | None = None
    test_kind: str | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class ChimeSnapshot:
    api_id: int
    name: str
    kind: str
    model: str
    revision: int = 1
    identity_epoch: int = 1
    connection: ChimeConnectionStatus = ChimeConnectionStatus.UNKNOWN
    stale: bool = True
    volume: int | None = None
    owned: bool | None = None
    can_write: bool = False
    unavailable_reason: str | None = "refresh-required"
    inventory_received_at: float | None = None
    inventory_monotonic: float | None = None
    health: ChimeHealthSnapshot | None = None
    operation: ChimeOperationSnapshot | None = None
    sound_operation_id: int | None = None
    location_id: str | None = None
    firmware: str | None = None
    snooze_seconds_left: int | None = None
    _hardware_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ChimeAccountSnapshot:
    generation: int
    revision: int = 0
    status: ChimeReadStatus = ChimeReadStatus.UNKNOWN
    devices: tuple[ChimeSnapshot, ...] = ()
    complete: bool = False
    code: str | None = None
    has_inventory: bool = False

    @property
    def chimes(self) -> tuple[ChimeSnapshot, ...]:
        return self.devices

    def find_device(self, api_id: int) -> ChimeSnapshot | None:
        return next((device for device in self.devices if device.api_id == api_id), None)


@dataclass(frozen=True, slots=True)
class ChimeResult:
    status: ChimeOperationStatus
    api_id: int
    operation_id: int | None = None
    code: str | None = None
    reported_volume: int | None = None

    @property
    def confirmed(self) -> bool:
        return self.status == ChimeOperationStatus.REPORTED


class ChimeSchemaError(ValueError):
    """A sanitized schema failure; malformed inventory never implies removal."""


def _text(value: object, *, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    result = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    return result.strip()[:limit] or None


def _integer(value: object, minimum: int, maximum: int) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum
        else None
    )


def parse_chime_inventory(payload: object) -> tuple[ChimeSnapshot, ...]:
    """Require an explicit complete list, rejecting rather than truncating it."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("chimes"), list):
        raise ChimeSchemaError("invalid-inventory")
    rows = payload["chimes"]
    if len(rows) > MAX_CHIMES:
        raise ChimeSchemaError("inventory-limit")
    devices = []
    ids = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ChimeSchemaError("invalid-device")
        api_id = _integer(row.get("id"), 1, 9_223_372_036_854_775_807)
        if api_id is None or api_id in ids:
            raise ChimeSchemaError("invalid-device-id")
        ids.add(api_id)
        kind = _text(row.get("kind"), limit=80) or "unknown"
        settings = row.get("settings")
        volume = _integer(settings.get("volume"), 0, 11) if isinstance(settings, Mapping) else None
        alerts = row.get("alerts")
        connection = alerts.get("connection") if isinstance(alerts, Mapping) else None
        connection = (
            {
                "online": ChimeConnectionStatus.ONLINE,
                "offline": ChimeConnectionStatus.OFFLINE,
            }.get(connection, ChimeConnectionStatus.UNKNOWN)
            if isinstance(connection, str)
            else ChimeConnectionStatus.UNKNOWN
        )
        snooze = row.get("do_not_disturb")
        seconds = (
            _integer(snooze.get("seconds_left"), 0, 2_147_483_647)
            if isinstance(snooze, Mapping)
            else None
        )
        location = row.get("location_id")
        if isinstance(location, int) and not isinstance(location, bool):
            location = str(location)
        devices.append(
            ChimeSnapshot(
                api_id=api_id,
                name=_text(row.get("description")) or "Chime",
                kind=kind,
                model=_MODELS.get(kind, "Unrecognized Chime model"),
                connection=connection,
                volume=volume,
                owned=row.get("owned") if isinstance(row.get("owned"), bool) else None,
                location_id=_text(location),
                firmware=_text(row.get("firmware_version"), limit=80),
                snooze_seconds_left=seconds,
                _hardware_id=_text(row.get("device_id"), limit=128),
            )
        )
    return tuple(sorted(devices, key=lambda device: device.api_id))


def parse_chime_health(
    payload: object, api_id: int, *, observed_at: float, observed_monotonic: float
) -> ChimeHealthSnapshot:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("device_health"), Mapping):
        raise ChimeSchemaError("invalid-health")
    health = payload["device_health"]
    if "id" in health and _integer(health["id"], 1, 9_223_372_036_854_775_807) != api_id:
        raise ChimeSchemaError("health-device-mismatch")
    category = health.get("latest_signal_category")
    return ChimeHealthSnapshot(
        stale=False,
        status=ChimeReadStatus.READY,
        observed_at=observed_at,
        observed_monotonic=observed_monotonic,
        wifi_signal_strength=_integer(health.get("latest_signal_strength"), -150, 0),
        wifi_signal_category=category if category in ("good", "okay", "poor") else None,
        firmware=_text(health.get("firmware"), limit=80),
    )


@dataclass(slots=True)
class _SharedRead:
    task: asyncio.Task
    consumers: int = 0


class ChimeService:
    """One event-loop owner of immutable snapshots and bounded actuator intents.

    Caller cancellation detaches a read consumer. Commands are shielded and
    remain service-owned after admission; a view cancels its ChimeIntent instead.
    The admission callback is an HTTP handoff boundary, not a wire-level fence.
    """

    def __init__(
        self,
        gateway,
        on_snapshot: Callable[[ChimeAccountSnapshot], None] | None = None,
        *,
        generation: int,
        is_current: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.time,
        elapsed_clock: Callable[[], float] = chime_monotonic_time,
    ):
        self._gateway = gateway
        self._on_snapshot = on_snapshot
        self._generation = generation
        self._is_current = is_current
        self._clock = clock
        self._elapsed_clock = elapsed_clock
        self._snapshot = ChimeAccountSnapshot(generation=generation)
        self._closed = False
        self._reads: dict[object, _SharedRead] = {}
        self._semantic_revision = 0
        self._identity_epoch = 0
        self._operation_serial = 0
        self._operations: dict[int, asyncio.Task] = {}
        self._last_inventory_attempt: float | None = None
        self._health_attempts: dict[int, float] = {}

    @property
    def snapshot(self) -> ChimeAccountSnapshot:
        return replace(
            self._snapshot,
            devices=tuple(self._derive(device) for device in self._snapshot.devices),
        )

    def _current(self) -> bool:
        return not self._closed and self._is_current()

    def _emit(self, **changes) -> None:
        if not self._current():
            return
        self._snapshot = replace(self._snapshot, revision=self._snapshot.revision + 1, **changes)
        if self._on_snapshot is not None:
            # A presentation callback cannot alter HTTP admission semantics.
            with suppress(Exception):
                self._on_snapshot(self._snapshot)

    def _reason(self, device: ChimeSnapshot, *, allow_pending: bool = False) -> str | None:
        if not self._current():
            return "closed"
        age = (
            self._elapsed_clock() - device.inventory_monotonic
            if device.inventory_monotonic is not None
            else None
        )
        if (
            not self._snapshot.complete
            or self._snapshot.status != ChimeReadStatus.READY
            or age is None
            or not 0 <= age <= INVENTORY_FRESH_SECONDS
            or device.stale
        ):
            return "refresh-required"
        if device.kind not in SUPPORTED_KINDS:
            return "unsupported-model"
        if device.owned is not True:
            return "ownership-unverified"
        if device.volume is None:
            return "unsupported-volume"
        if device.connection != ChimeConnectionStatus.ONLINE:
            return (
                "offline"
                if device.connection == ChimeConnectionStatus.OFFLINE
                else "connection-unknown"
            )
        if device.sound_operation_id is not None:
            return "sound-awaiting-confirmation"
        if not allow_pending and (
            device.api_id in self._operations
            or (device.operation is not None and device.operation.active)
        ):
            return "busy"
        return None

    def _derive(self, device: ChimeSnapshot) -> ChimeSnapshot:
        elapsed = self._elapsed_clock()
        if (
            device.inventory_monotonic is None
            or not 0 <= elapsed - device.inventory_monotonic <= INVENTORY_FRESH_SECONDS
        ):
            device = replace(device, stale=True)
        health = device.health
        if health is not None and (
            health.observed_monotonic is None
            or not 0 <= elapsed - health.observed_monotonic <= HEALTH_CACHE_SECONDS
        ):
            device = replace(device, health=replace(health, stale=True))
        reason = self._reason(device)
        return replace(device, can_write=reason is None, unavailable_reason=reason)

    def _replace_device(self, device: ChimeSnapshot) -> None:
        self._emit(
            devices=tuple(
                self._derive(device) if old.api_id == device.api_id else self._derive(old)
                for old in self._snapshot.devices
            )
        )

    async def _consume(self, key: object, factory) -> None:
        if not self._current():
            return
        shared = self._reads.get(key)
        if shared is not None and shared.task.cancelling() and not shared.task.done():
            # A rapid reopen must not inherit the final old consumer's cancel.
            # Let that bounded request retire before starting a replacement;
            # repeatedly reopening cannot grow a set of retiring HTTP jobs.
            try:
                await asyncio.shield(shared.task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            if not self._current():
                return
            shared = self._reads.get(key)
        if shared is not None and shared.task.done():
            self._reads.pop(key, None)
            shared = None
        if shared is None:
            if key != "inventory" and sum(k != "inventory" for k in self._reads) >= 4:
                raise ChimeRestError("busy")
            task = asyncio.create_task(factory())
            shared = _SharedRead(task)
            self._reads[key] = shared

            def finished(done):
                if self._reads.get(key) is shared:
                    self._reads.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Retrieve even when every consumer detached.

            task.add_done_callback(finished)
        shared.consumers += 1
        try:
            await asyncio.shield(shared.task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # Authoritative removal/identity replacement retired this read.
        finally:
            shared.consumers -= 1
            if shared.consumers == 0 and not shared.task.done():
                shared.task.cancel()

    async def refresh(self, force: bool = False) -> ChimeAccountSnapshot:
        if not self._current():
            return self.snapshot
        now = self._elapsed_clock()
        if (
            not force
            and "inventory" not in self._reads
            and self._last_inventory_attempt is not None
            and self._snapshot.status != ChimeReadStatus.UNKNOWN
            and 0 <= now - self._last_inventory_attempt < INVENTORY_CACHE_SECONDS
        ):
            return self.snapshot
        await self._consume("inventory", self._read_inventory)
        return self.snapshot

    async def _read_inventory(self) -> None:
        self._last_inventory_attempt = self._elapsed_clock()
        try:
            payload = await self._gateway.read_inventory(timeout=10.0)
            devices = parse_chime_inventory(payload)
        except (ChimeRestError, ChimeSchemaError) as exc:
            self._inventory_failed(exc.code if isinstance(exc, ChimeRestError) else str(exc))
            return
        except Exception:
            self._inventory_failed("read-failed")
            return
        if not self._current() or asyncio.current_task().cancelling():
            return
        now, elapsed = self._clock(), self._elapsed_clock()
        old_by_id = {device.api_id: device for device in self._snapshot.devices}
        updated = []
        for parsed in devices:
            old = old_by_id.get(parsed.api_id)
            same_identity = old is not None and (
                old.kind == parsed.kind and old._hardware_id == parsed._hardware_id
            )
            if same_identity:
                parsed = replace(
                    parsed,
                    revision=old.revision,
                    identity_epoch=old.identity_epoch,
                    health=old.health,
                    operation=old.operation,
                    sound_operation_id=old.sound_operation_id,
                )
                if self._semantic(parsed) != self._semantic(old):
                    self._semantic_revision += 1
                    parsed = replace(parsed, revision=self._semantic_revision)
            else:
                self._identity_epoch += 1
                self._semantic_revision += 1
                parsed = replace(
                    parsed,
                    revision=self._semantic_revision,
                    identity_epoch=self._identity_epoch,
                )
            updated.append(
                replace(
                    parsed,
                    stale=False,
                    inventory_received_at=now,
                    inventory_monotonic=elapsed,
                )
            )
        # First set authority so derived gates use the newly validated inventory.
        self._snapshot = replace(
            self._snapshot,
            complete=True,
            status=ChimeReadStatus.READY,
            code=None,
            has_inventory=True,
        )
        self._emit(devices=tuple(self._derive(device) for device in updated))
        present = {device.api_id: device.identity_epoch for device in updated}
        for key, shared in tuple(self._reads.items()):
            if isinstance(key, tuple) and present.get(key[0]) != key[1]:
                shared.task.cancel()
        for api_id, task in tuple(self._operations.items()):
            old = old_by_id.get(api_id)
            if (
                old is not None
                and present.get(api_id) != old.identity_epoch
                and old.operation is not None
                and not old.operation.possibly_sent
            ):
                task.cancel()
        self._health_attempts = {
            key: value for key, value in self._health_attempts.items() if key in present
        }

    @staticmethod
    def _semantic(device: ChimeSnapshot) -> tuple:
        return (
            device.name,
            device.kind,
            device.volume,
            device.owned,
            device.connection,
            device.location_id,
            device.firmware,
            device.snooze_seconds_left,
            device._hardware_id,
        )

    def _inventory_failed(self, code: str) -> None:
        if not self._current() or asyncio.current_task().cancelling():
            return
        self._snapshot = replace(
            self._snapshot, complete=False, status=ChimeReadStatus.ERROR, code=code
        )
        self._emit(
            devices=tuple(
                self._derive(replace(device, stale=True)) for device in self._snapshot.devices
            )
        )

    async def refresh_details(self, api_id: int, force: bool = False) -> ChimeAccountSnapshot:
        """Reload inventory (including volume) and selected health, coalesced."""
        await self.refresh(force=force)
        device = self._snapshot.find_device(api_id)
        if device is None or not self._current():
            return self.snapshot
        key = (api_id, device.identity_epoch)
        attempted = self._health_attempts.get(api_id)
        if (
            not force
            and key not in self._reads
            and attempted is not None
            and device.health is not None
            and 0 <= self._elapsed_clock() - attempted < HEALTH_CACHE_SECONDS
        ):
            return self.snapshot
        try:
            await self._consume(key, lambda: self._read_health(api_id, device.identity_epoch))
        except ChimeRestError:
            # A bounded local read admission failure is explicit in the details.
            self._health_failed(api_id, device.identity_epoch, "busy")
        return self.snapshot

    async def _read_health(self, api_id: int, identity_epoch: int) -> None:
        self._health_attempts[api_id] = self._elapsed_clock()
        try:
            payload = await self._gateway.read_health(api_id, timeout=10.0)
            health = parse_chime_health(
                payload, api_id, observed_at=self._clock(), observed_monotonic=self._elapsed_clock()
            )
        except (ChimeRestError, ChimeSchemaError) as exc:
            self._health_failed(
                api_id, identity_epoch, exc.code if isinstance(exc, ChimeRestError) else str(exc)
            )
            return
        except Exception:
            self._health_failed(api_id, identity_epoch, "read-failed")
            return
        device = self._snapshot.find_device(api_id)
        if (
            self._current()
            and not asyncio.current_task().cancelling()
            and device is not None
            and device.identity_epoch == identity_epoch
        ):
            self._replace_device(replace(device, health=health))

    def _health_failed(self, api_id: int, identity_epoch: int, code: str) -> None:
        if asyncio.current_task().cancelling():
            return
        device = self._snapshot.find_device(api_id)
        if device is not None and device.identity_epoch == identity_epoch:
            health = replace(
                device.health or ChimeHealthSnapshot(),
                stale=True,
                status=ChimeReadStatus.ERROR,
                code=code,
            )
            self._replace_device(replace(device, health=health))

    async def request_volume(
        self, api_id: int, value: int, *, expected_revision: int, intent: ChimeIntent
    ) -> ChimeResult:
        if _integer(value, 0, 11) is None:
            return ChimeResult(ChimeOperationStatus.REJECTED, api_id, code="invalid-volume")
        return await self._request(api_id, "volume", expected_revision, intent, value=value)

    async def request_test_sound(
        self, api_id: int, kind: str, *, expected_revision: int, intent: ChimeIntent
    ) -> ChimeResult:
        if kind not in ("ding", "motion"):
            return ChimeResult(ChimeOperationStatus.REJECTED, api_id, code="invalid-test-kind")
        return await self._request(api_id, "test-sound", expected_revision, intent, test_kind=kind)

    def _intent_reason(
        self, api_id: int, expected_revision: int, intent: ChimeIntent
    ) -> str | None:
        if not isinstance(intent, ChimeIntent):
            return "invalid-intent"
        if not self._current() or intent.generation != self._generation:
            return "session-changed"
        if intent.cancelled:
            return "cancelled"
        device = self._snapshot.find_device(api_id)
        if device is None:
            return "device-removed"
        if (
            intent.api_id != api_id
            or intent.revision != expected_revision
            or device.revision != expected_revision
            or intent.identity_epoch != device.identity_epoch
        ):
            return "device-changed"
        return None

    async def _request(
        self, api_id, kind, expected_revision, intent, *, value=None, test_kind=None
    ):
        reason = self._intent_reason(api_id, expected_revision, intent)
        device = self._snapshot.find_device(api_id)
        if reason is None and (api_id in self._operations or len(self._operations) >= 2):
            reason = "busy"
        if reason is None and device.sound_operation_id is not None:
            reason = "sound-awaiting-confirmation"
        if reason is not None:
            return ChimeResult(ChimeOperationStatus.REJECTED, api_id, code=reason)
        self._operation_serial += 1
        operation = ChimeOperationSnapshot(
            self._operation_serial,
            kind,
            ChimeOperationStatus.PREPARING,
            True,
            requested_volume=value,
            test_kind=test_kind,
        )
        self._replace_device(replace(device, operation=operation))
        task = asyncio.create_task(
            self._run_operation(api_id, expected_revision, intent, operation)
        )
        self._operations[api_id] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A canceled caller cannot orphan/undo a possibly-sent request.
            intent.cancel()
            raise

    def _set_operation(self, api_id, identity_epoch, operation, *, sound_gate=False):
        device = self._snapshot.find_device(api_id)
        if device is not None and device.identity_epoch == identity_epoch:
            self._replace_device(
                replace(
                    device,
                    operation=operation,
                    sound_operation_id=operation.operation_id
                    if sound_gate
                    else device.sound_operation_id,
                )
            )

    async def _run_operation(self, api_id, expected_revision, intent, operation):
        current_task = asyncio.current_task()
        admitted = False

        async def cancel_unsent():
            while not admitted:
                if intent.cancelled:
                    current_task.cancel()
                    return
                await asyncio.sleep(0.05)

        cancellation = asyncio.create_task(cancel_unsent())

        def admission():
            reason = self._intent_reason(api_id, expected_revision, intent)
            device = self._snapshot.find_device(api_id)
            if reason is None:
                # This operation's own pending state is not a Busy conflict.
                candidate = replace(device, operation=None)
                reason = self._reason(candidate, allow_pending=True)
            if reason is not None:
                raise ChimeRestError(reason)

        def on_admitted():
            nonlocal admitted, operation
            admitted = True
            operation = replace(
                operation, status=ChimeOperationStatus.POSSIBLY_SENT, possibly_sent=True
            )
            self._set_operation(
                api_id, intent.identity_epoch, operation, sound_gate=operation.kind == "test-sound"
            )

        try:
            async with asyncio.timeout(30.0 if operation.kind == "volume" else 15.0):
                device = self._snapshot.find_device(api_id)
                if self._reason(device, allow_pending=True) == "refresh-required":
                    await self.refresh(force=True)
                admission()
                if operation.kind == "volume":
                    receipt = await self._gateway.set_legacy_volume(
                        api_id,
                        operation.requested_volume,
                        timeout=10.0,
                        admission=admission,
                        on_admitted=on_admitted,
                    )
                else:
                    receipt = await self._gateway.request_test_tone(
                        api_id,
                        operation.test_kind,
                        timeout=10.0,
                        admission=admission,
                        on_admitted=on_admitted,
                    )
                current_device = self._snapshot.find_device(api_id)
                if (
                    not self._current()
                    or current_device is None
                    or current_device.identity_epoch != intent.identity_epoch
                ):
                    operation = replace(
                        operation,
                        status=ChimeOperationStatus.UNKNOWN,
                        active=False,
                        code="session-changed",
                    )
                elif not receipt.accepted:
                    operation = replace(
                        operation,
                        status=ChimeOperationStatus.REJECTED,
                        active=False,
                        code="http-rejected",
                    )
                elif operation.kind == "test-sound":
                    operation = replace(
                        operation,
                        status=ChimeOperationStatus.ACCEPTED,
                        active=False,
                        code=None if receipt.body_complete else "reply-incomplete",
                    )
                else:
                    operation = replace(operation, status=ChimeOperationStatus.ACCEPTED)
                    self._set_operation(api_id, intent.identity_epoch, operation)
                    # Do not count a GET that started before the write as readback.
                    existing = self._reads.get("inventory")
                    if existing is not None:
                        await self._consume("inventory", self._read_inventory)
                    for _ in range(2):
                        await self.refresh(force=True)
                        device = self._snapshot.find_device(api_id)
                        if (
                            not self._current()
                            or device is None
                            or device.identity_epoch != intent.identity_epoch
                        ):
                            operation = replace(
                                operation,
                                status=ChimeOperationStatus.UNKNOWN,
                                active=False,
                                code="device-changed",
                            )
                            break
                        if (
                            self._snapshot.complete
                            and not device.stale
                            and device.volume == operation.requested_volume
                        ):
                            operation = replace(
                                operation, status=ChimeOperationStatus.REPORTED, active=False
                            )
                            break
                    else:
                        operation = replace(
                            operation,
                            status=ChimeOperationStatus.UNKNOWN,
                            active=False,
                            code="readback-unconfirmed",
                        )
        except ChimeRestError as exc:
            possibly_sent = admitted or exc.possibly_sent
            rejected = exc.status is not None and 400 <= exc.status < 500
            operation = replace(
                operation,
                status=ChimeOperationStatus.REJECTED
                if rejected or not possibly_sent
                else ChimeOperationStatus.UNKNOWN,
                active=False,
                possibly_sent=possibly_sent,
                code=exc.code,
            )
        except (TimeoutError, asyncio.CancelledError):
            code = self._intent_reason(api_id, expected_revision, intent) or (
                "closed" if self._closed else "timeout"
            )
            operation = replace(
                operation,
                status=ChimeOperationStatus.UNKNOWN if admitted else ChimeOperationStatus.REJECTED,
                active=False,
                code="outcome-unknown" if admitted else code,
            )
        except Exception:
            # SDK/HTTP exception text can contain signed URLs; publish safe codes.
            operation = replace(
                operation,
                status=ChimeOperationStatus.UNKNOWN if admitted else ChimeOperationStatus.REJECTED,
                active=False,
                code="request-failed",
            )
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
            if self._operations.get(api_id) is current_task:
                self._operations.pop(api_id, None)
        self._set_operation(api_id, intent.identity_epoch, operation)
        device = self._snapshot.find_device(api_id)
        if (
            operation.kind == "volume"
            and operation.status == ChimeOperationStatus.UNKNOWN
            and device is not None
            and device.identity_epoch == intent.identity_epoch
        ):
            self._replace_device(replace(device, stale=True))
        return ChimeResult(
            operation.status,
            api_id,
            operation.operation_id,
            operation.code,
            device.volume
            if device is not None and device.identity_epoch == intent.identity_epoch
            else None,
        )

    async def acknowledge_sound_finished(self, api_id: int, operation_id: int) -> bool:
        device = self._snapshot.find_device(api_id)
        if (
            not self._current()
            or device is None
            or device.sound_operation_id != operation_id
            or api_id in self._operations
        ):
            return False
        self._replace_device(replace(device, sound_operation_id=None))
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = {shared.task for shared in self._reads.values()} | set(self._operations.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reads.clear()
        self._operations.clear()
        self._snapshot = replace(
            self._snapshot,
            status=ChimeReadStatus.CLOSED,
            complete=False,
            devices=tuple(
                replace(
                    device,
                    stale=True,
                    can_write=False,
                    unavailable_reason="closed",
                    operation=replace(
                        device.operation,
                        active=False,
                        status=ChimeOperationStatus.UNKNOWN
                        if device.operation.possibly_sent
                        else ChimeOperationStatus.REJECTED,
                        code="closed",
                    )
                    if device.operation is not None and device.operation.active
                    else device.operation,
                )
                for device in self._snapshot.devices
            ),
        )
        await self._gateway.close()
