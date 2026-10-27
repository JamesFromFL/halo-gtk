"""Intercom observations and explicit, single-use door-release requests.

RPC acceptance and unlock notifications never establish physical lock state.
No request is automatically repeated, reversed or caused by an incoming event.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from halo_gtk.chime import _integer, _text
from halo_gtk.intercom_rest import IntercomRestError
from halo_gtk.light_group_models import LightGroupSchemaError, validate_identifier
from halo_gtk.light_groups import light_group_monotonic_time
from halo_gtk.ring_events import normalized_event_kind

KINDS = frozenset({"intercom_handset_audio", "intercom_handset_video"})
MAX_INTERCOMS = 128
FRESH_SECONDS = 30.0
CACHE_SECONDS = 5.0
OPERATION_SECONDS = 30.0


class IntercomReadStatus(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    STALE = "stale"
    CLOSED = "closed"


class IntercomOperationStatus(StrEnum):
    PREPARING = "preparing"
    POSSIBLY_SENT = "possibly-sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class IntercomIntent:
    generation: int
    api_id: int
    revision: int
    identity_epoch: int
    created_monotonic: float = field(default_factory=light_group_monotonic_time)
    _cancelled: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _used: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def cancel(self):
        self._cancelled.set()

    @property
    def cancelled(self):
        return self._cancelled.is_set()


@dataclass(frozen=True, slots=True)
class IntercomOperation:
    operation_id: int
    status: IntercomOperationStatus
    active: bool
    possibly_sent: bool = False
    code: str | None = None


@dataclass(frozen=True, slots=True)
class IntercomObservation:
    kind: str
    received_at: float


@dataclass(frozen=True, slots=True)
class IntercomSnapshot:
    api_id: int
    name: str
    kind: str
    location_id: str
    revision: int = 1
    identity_epoch: int = 1
    connection: str = "unknown"
    battery: int | None = None
    firmware: str | None = None
    ring_to_open: bool | None = None
    stale: bool = True
    inventory_received_at: float | None = None
    inventory_monotonic: float | None = None
    denied: bool = False
    can_write: bool = False
    unavailable_reason: str | None = "refresh-required"
    operation: IntercomOperation | None = None
    acknowledgment_id: int | None = None
    observations: tuple[IntercomObservation, ...] = ()
    _hardware_id: str | None = field(default=None, repr=False)

    @property
    def model(self):
        return "Intercom Video" if self.kind == "intercom_handset_video" else "Intercom Audio"


@dataclass(frozen=True, slots=True)
class IntercomAccountSnapshot:
    generation: int
    revision: int = 0
    devices: tuple[IntercomSnapshot, ...] = ()
    status: IntercomReadStatus = IntercomReadStatus.UNKNOWN
    complete: bool = False
    has_inventory: bool = False
    code: str | None = None
    read_retry_after: float | None = None
    poll_allowed: bool = True

    def find_device(self, api_id):
        return next((device for device in self.devices if device.api_id == api_id), None)


@dataclass(frozen=True, slots=True)
class IntercomResult:
    status: IntercomOperationStatus
    api_id: int
    operation_id: int | None = None
    code: str | None = None


class IntercomSchemaError(ValueError):
    pass


def parse_intercom_inventory(payload):
    if not isinstance(payload, Mapping) or not isinstance(payload.get("other"), list):
        raise IntercomSchemaError("invalid-inventory")
    rows = payload["other"]
    if len(rows) > 512:
        raise IntercomSchemaError("inventory-limit")
    devices, seen = [], set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise IntercomSchemaError("invalid-device")
        api_id = _integer(row.get("id"), 1, 9_223_372_036_854_775_807)
        if api_id is None or api_id in seen or not isinstance(row.get("kind"), str):
            raise IntercomSchemaError("invalid-device-identity")
        seen.add(api_id)
        if row["kind"] not in KINDS:
            continue
        try:
            location = validate_identifier(row.get("location_id"))
        except LightGroupSchemaError:
            raise IntercomSchemaError("invalid-location") from None
        # Non-null deactivation is never silently treated as an active opener.
        # Preserve the prior inventory if the marker itself cannot be validated.
        if row.get("deactivated_at") is not None:
            from datetime import datetime

            try:
                stamp = datetime.fromisoformat(row["deactivated_at"].replace("Z", "+00:00"))
                if stamp.utcoffset() is None:
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                raise IntercomSchemaError("invalid-deactivation") from None
            continue
        if row.get("stolen") is True:
            continue
        alerts = row.get("alerts")
        connection = alerts.get("connection") if isinstance(alerts, Mapping) else None
        connection = connection if connection in ("online", "offline") else "unknown"
        battery = row.get("battery_life")
        if (
            isinstance(battery, str)
            and battery.isascii()
            and battery.isdecimal()
            and len(battery) <= 3
        ):
            battery = int(battery)
        settings = row.get("settings")
        settings = settings.get("intercom_settings") if isinstance(settings, Mapping) else None
        ring_to_open = settings.get("ring_to_open") if isinstance(settings, Mapping) else None
        hardware = row.get("device_id")
        if hardware is not None and (
            not isinstance(hardware, str) or not hardware or len(hardware) > 128
        ):
            raise IntercomSchemaError("invalid-hardware-identity")
        devices.append(
            IntercomSnapshot(
                api_id,
                _text(row.get("description")) or "Intercom",
                row["kind"],
                location,
                connection=connection,
                battery=_integer(battery, 0, 100),
                firmware=_text(row.get("firmware_version"), limit=80),
                ring_to_open=ring_to_open if isinstance(ring_to_open, bool) else None,
                _hardware_id=hardware,
            )
        )
        if len(devices) > MAX_INTERCOMS:
            raise IntercomSchemaError("inventory-limit")
    return tuple(sorted(devices, key=lambda device: device.api_id))


class IntercomService:
    def __init__(
        self,
        gateway,
        on_snapshot=None,
        *,
        generation,
        is_current=lambda: True,
        clock=time.time,
        elapsed_clock=light_group_monotonic_time,
    ):
        self._gateway, self._on_snapshot = gateway, on_snapshot
        self._generation, self._is_current = generation, is_current
        self._clock, self._elapsed_clock = clock, elapsed_clock
        self._snapshot = IntercomAccountSnapshot(generation)
        self._closed = False
        self._close_task = self._read_task = self._operation_task = None
        self._consumers = 0
        self._serial = self._revision = self._epoch = self._failures = 0
        self._last_read = None
        self._gates = {}
        self._events = OrderedDict()

    def _current(self):
        return not self._closed and self._is_current()

    @property
    def snapshot(self):
        devices = tuple(
            replace(
                d,
                stale=d.stale or not self._fresh(d),
                can_write=self._reason(d) is None,
                unavailable_reason=self._reason(d),
                acknowledgment_id=self._gates.get(d.api_id),
            )
            for d in self._snapshot.devices
        )
        return replace(self._snapshot, devices=devices)

    def _fresh(self, device):
        return device.inventory_monotonic is not None and (
            0 <= self._elapsed_clock() - device.inventory_monotonic <= FRESH_SECONDS
        )

    def _reason(self, device):
        if not self._current():
            return "session-changed"
        if device.denied:
            return "permission-denied"
        if not self._snapshot.poll_allowed:
            return "refresh-required"
        if self._snapshot.code and (self._snapshot.read_retry_after or 0) > self._elapsed_clock():
            return "retry-later"
        if not self._snapshot.complete or device.stale or not self._fresh(device):
            return "refresh-required"
        if device.connection == "offline":
            return "offline"
        if device.api_id in self._gates:
            return "acknowledgment-required"
        if len(self._gates) >= MAX_INTERCOMS:
            return "session-gate-limit"
        if self._operation_task is not None or (device.operation and device.operation.active):
            return "busy"
        return None

    def _publish(self):
        if self._current() and self._on_snapshot is not None:
            self._on_snapshot(self.snapshot)

    def _replace(self, device):
        if not self._current():
            return
        self._snapshot = replace(
            self._snapshot,
            devices=tuple(
                device
                if d.api_id == device.api_id and d.identity_epoch == device.identity_epoch
                else d
                for d in self._snapshot.devices
            ),
        )
        self._publish()

    async def refresh(self, *, force=False):
        if not self._current():
            return self.snapshot
        task = self._read_task
        if task is not None and task.cancelling() and not task.done():
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            if asyncio.current_task().cancelling() or not self._current():
                raise asyncio.CancelledError
        task = self._read_task
        if task is None or task.done():
            now = self._elapsed_clock()
            if now < (self._snapshot.read_retry_after or 0):
                return self.snapshot
            if not force and not self._snapshot.poll_allowed:
                return self.snapshot
            task = asyncio.create_task(self._read(force))
            self._read_task = task
        self._consumers += 1
        try:
            await asyncio.shield(task)
            return self.snapshot
        finally:
            self._consumers -= 1
            if self._consumers == 0 and not task.done():
                task.cancel()

    async def _read(self, force):
        self._last_read = self._elapsed_clock()
        self._snapshot = replace(self._snapshot, read_retry_after=self._last_read + CACHE_SECONDS)
        self._publish()
        try:
            parsed = parse_intercom_inventory(await self._gateway.read_inventory(timeout=10))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._current() and not asyncio.current_task().cancelling():
                self._failures = min(4, self._failures + 1)
                delay = min(
                    300, max(5, getattr(exc, "retry_after", None) or 15 * 2 ** (self._failures - 1))
                )
                paused = getattr(exc, "status", None) in (401, 403) or getattr(exc, "code", "") in (
                    "authentication-required",
                    "token-refresh-failed",
                )
                self._snapshot = replace(
                    self._snapshot,
                    complete=False,
                    status=IntercomReadStatus.STALE,
                    code=getattr(exc, "code", "invalid-inventory"),
                    read_retry_after=self._elapsed_clock() + delay,
                    poll_allowed=not paused,
                    devices=tuple(replace(d, stale=True) for d in self._snapshot.devices),
                )
                self._publish()
            return
        if not self._current() or asyncio.current_task().cancelling():
            return
        devices = []
        for candidate in parsed:
            old = self._snapshot.find_device(candidate.api_id)
            same = old is not None and (old.location_id, old.kind, old._hardware_id) == (
                candidate.location_id,
                candidate.kind,
                candidate._hardware_id,
            )
            if same:
                fields = ("name", "connection", "battery", "firmware", "ring_to_open")
                changed = any(getattr(old, f) != getattr(candidate, f) for f in fields)
                changed = changed or (force and old.denied)
                revision, epoch = old.revision, old.identity_epoch
                if changed:
                    self._revision += 1
                    revision = self._revision
                candidate = replace(
                    candidate,
                    revision=revision,
                    identity_epoch=epoch,
                    denied=False if force else old.denied,
                    operation=old.operation,
                    observations=old.observations,
                )
            else:
                self._epoch += 1
                self._revision += 1
                candidate = replace(candidate, identity_epoch=self._epoch, revision=self._revision)
            devices.append(
                replace(
                    candidate,
                    stale=False,
                    inventory_received_at=self._clock(),
                    inventory_monotonic=self._elapsed_clock(),
                )
            )
        self._failures = 0
        self._snapshot = replace(
            self._snapshot,
            revision=self._snapshot.revision + 1,
            devices=tuple(devices),
            complete=True,
            has_inventory=True,
            status=IntercomReadStatus.READY,
            code=None,
            poll_allowed=True,
            read_retry_after=self._elapsed_clock() + CACHE_SECONDS,
        )
        self._publish()

    def _intent_reason(self, api_id, revision, intent, *, consumed=False):
        if (
            not isinstance(intent, IntercomIntent)
            or any(
                type(v) is not int or v < 1
                for v in (api_id, revision, intent.api_id, intent.revision, intent.identity_epoch)
            )
            or (type(intent.created_monotonic) not in (int, float))
        ):
            return "invalid-intent"
        if not self._current() or intent.generation != self._generation:
            return "session-changed"
        if intent.cancelled or (intent._used.is_set() and not consumed):
            return "cancelled-or-used"
        age = self._elapsed_clock() - intent.created_monotonic
        if not math.isfinite(age) or not 0 <= age < FRESH_SECONDS:
            return "approval-expired"
        device = self._snapshot.find_device(api_id)
        if (
            device is None
            or (intent.api_id, intent.revision, intent.identity_epoch)
            != (api_id, device.revision, device.identity_epoch)
            or revision != device.revision
        ):
            return "device-changed"
        return None

    async def request_unlock(self, api_id, *, expected_revision, intent):
        reason = self._intent_reason(api_id, expected_revision, intent)
        device = self._snapshot.find_device(api_id)
        reason = reason or self._reason(device)
        if reason is not None:
            return IntercomResult(IntercomOperationStatus.REJECTED, api_id, code=reason)
        intent._used.set()
        self._serial += 1
        operation = IntercomOperation(self._serial, IntercomOperationStatus.PREPARING, True)
        deadline = min(
            intent.created_monotonic + FRESH_SECONDS, self._elapsed_clock() + OPERATION_SECONDS
        )
        # Reservation is the task reference; publish only after it is installed.
        task = asyncio.create_task(self._unlock(device, intent, operation, deadline))
        self._operation_task = task
        self._replace(replace(device, operation=operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            intent.cancel()
            raise

    async def _unlock(self, target, intent, operation, deadline):
        admitted = False
        task = asyncio.current_task()
        request_id = str(uuid.uuid4())

        def admission():
            if self._elapsed_clock() >= deadline or task.cancelling():
                raise IntercomRestError("timeout", possibly_sent=admitted)
            reason = self._intent_reason(target.api_id, target.revision, intent, consumed=True)
            current = self._snapshot.find_device(target.api_id)
            if reason is None:
                # Ignore only this operation's reservation in the normal gate.
                reason = self._reason(current)
                if reason == "busy" and self._operation_task is task:
                    reason = None
            if reason is not None:
                raise IntercomRestError(reason, possibly_sent=admitted)

        def on_admitted():
            nonlocal admitted, operation
            admission()
            admitted = True
            operation = replace(
                operation, status=IntercomOperationStatus.POSSIBLY_SENT, possibly_sent=True
            )
            self._replace_operation(target, operation)
            if self._elapsed_clock() >= deadline or task.cancelling():
                raise IntercomRestError("timeout", possibly_sent=True)

        async def cancel_unsent():
            while not admitted:
                if self._intent_reason(target.api_id, target.revision, intent, consumed=True):
                    task.cancel()
                    return
                await asyncio.sleep(0.05)

        monitor = asyncio.create_task(cancel_unsent())
        try:
            async with asyncio.timeout(max(0, deadline - self._elapsed_clock())):
                admission()
                receipt = await self._gateway.unlock(
                    target.api_id,
                    request_id,
                    timeout=10,
                    admission=admission,
                    on_admitted=on_admitted,
                )
                if self._elapsed_clock() >= deadline or task.cancelling():
                    raise IntercomRestError("timeout", possibly_sent=admitted)
                response = receipt.response_json
                result = response.get("result") if isinstance(response, dict) else None
                code = result.get("code") if isinstance(result, dict) else None
                matched = isinstance(response, dict) and (
                    "id" not in response or response["id"] == request_id
                )
                status = IntercomOperationStatus.UNKNOWN
                if receipt.accepted and receipt.body_complete and matched and type(code) is int:
                    status = (
                        IntercomOperationStatus.ACCEPTED
                        if code == 0
                        else IntercomOperationStatus.REJECTED
                    )
                operation = replace(
                    operation,
                    active=False,
                    status=status,
                    code=None
                    if status == IntercomOperationStatus.ACCEPTED
                    else "rpc-rejected"
                    if status == IntercomOperationStatus.REJECTED
                    else "unconfirmed-reply",
                )
        except (asyncio.CancelledError, TimeoutError):
            operation = replace(
                operation,
                active=False,
                status=IntercomOperationStatus.UNKNOWN
                if admitted
                else IntercomOperationStatus.REJECTED,
                code="outcome-unknown" if admitted else "cancelled-or-timeout",
            )
        except Exception as exc:
            sent = admitted or getattr(exc, "possibly_sent", False)
            rejected = getattr(exc, "status", None) in (400, 401, 403, 404, 409, 422, 429)
            operation = replace(
                operation,
                active=False,
                possibly_sent=sent,
                status=IntercomOperationStatus.REJECTED
                if rejected or not sent
                else IntercomOperationStatus.UNKNOWN,
                code=getattr(exc, "code", "request-failed"),
            )
            current = self._snapshot.find_device(target.api_id)
            if current and current.identity_epoch == target.identity_epoch:
                if getattr(exc, "status", None) == 403:
                    self._revision += 1
                    self._replace(replace(current, denied=True, revision=self._revision))
                if getattr(exc, "status", None) in (401, 429):
                    self._snapshot = replace(
                        self._snapshot,
                        poll_allowed=getattr(exc, "status", None) != 401,
                        code=operation.code,
                        read_retry_after=self._elapsed_clock()
                        + min(300, max(5, getattr(exc, "retry_after", None) or 15)),
                    )
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            if self._operation_task is task:
                self._operation_task = None
        if operation.status == IntercomOperationStatus.ACCEPTED and (
            self._elapsed_clock() >= deadline or task.cancelling()
        ):
            operation = replace(
                operation, status=IntercomOperationStatus.UNKNOWN, code="outcome-unknown"
            )
        if operation.possibly_sent and operation.status in (
            IntercomOperationStatus.ACCEPTED,
            IntercomOperationStatus.UNKNOWN,
        ):
            self._gates[target.api_id] = operation.operation_id
        self._replace_operation(target, operation)
        return IntercomResult(
            operation.status, target.api_id, operation.operation_id, operation.code
        )

    def _replace_operation(self, target, operation):
        current = self._snapshot.find_device(target.api_id)
        if current and current.identity_epoch == target.identity_epoch:
            self._replace(replace(current, operation=operation))

    async def acknowledge(self, api_id, operation_id):
        if not self._current() or self._operation_task is not None:
            return False
        if type(operation_id) is not int or self._gates.get(api_id) != operation_id:
            return False
        self._gates.pop(api_id)
        self._publish()
        return True

    def observe_event(self, event):
        if not self._current():
            return None

        def value(name):
            return event.get(name) if isinstance(event, dict) else getattr(event, name, None)

        api_id = value("doorbot_id")
        device = self._snapshot.find_device(api_id) if type(api_id) is int else None
        kind = normalized_event_kind(event)
        event_id, timestamp = value("id"), value("now")
        if (
            device is None
            or device.stale
            or not self._fresh(device)
            or value("device_kind") != device.kind
            or kind not in {"ding", "intercom_unlock"}
            or type(event_id) is not int
            or event_id <= 0
            or type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or not -5 <= self._clock() - timestamp <= 300
            or value("is_update") is True
        ):
            return None
        key = (device.api_id, device.identity_epoch, kind, event_id)
        if key in self._events:
            return None
        self._events[key] = self._elapsed_clock()
        while len(self._events) > 128:
            self._events.popitem(last=False)
        observation = IntercomObservation(kind, self._clock())
        self._replace(replace(device, observations=(observation, *device.observations)[:16]))
        return replace(device, observations=(observation,))

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        tasks = [task for task in (self._read_task, self._operation_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._gateway.close()
        self._snapshot = IntercomAccountSnapshot(self._generation, status=IntercomReadStatus.CLOSED)
        self._events.clear()
        self._gates.clear()
