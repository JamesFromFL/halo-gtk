"""Client-owned, no-queue coordination for camera and group lighting actions."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, replace

from halo_gtk.camera_lighting import (
    CameraLightingView,
    CameraLightOperation,
    camera_id,
    receipt_row,
)
from halo_gtk.ring_json import RingJsonError, RingWriteReceipt

CAMERA_ACTION_TIMEOUT = 30.0
_CAMERA_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})


def _elapsed_time():
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    return time.clock_gettime(clock) if clock is not None else time.monotonic()


class LightingActionError(RuntimeError):
    def __init__(self, code, *, possibly_sent=False):
        super().__init__(code)
        self.code = code
        self.possibly_sent = possibly_sent


@dataclass(slots=True, eq=False)
class LightingReservation:
    generation: int
    sent: bool = False
    acknowledged_revision: int | None = None


class LightingActions:
    """One local lighting intent, including preflight; no network lock is held.

    The group service owns its tasks. This object owns camera tasks after their
    presentation consumer detaches, and joins them before Auth teardown.
    """

    def __init__(self, client, *, is_current):
        self._client = client
        self._is_current = is_current
        self._lock = threading.RLock()
        self._generation = None
        self._retired = threading.Event()
        self._active = None
        self._tasks = set()
        self._close_task = None
        self._revision = 0
        self._receipt = None
        self._operations = {}

    def initialize(self, generation):
        with self._lock:
            if self._generation is not None and self._generation != generation:
                self._receipt = None
                self._operations.clear()
            self._generation = generation
            self._retired.clear()

    def invalidate(self, generation):
        self._retired.set()
        with self._lock:
            self._generation = generation
            self._receipt = None
            self._operations.clear()

    def current(self, generation):
        client = self._client
        return (
            not self._retired.is_set()
            and not client._stopped
            and not client._stop_event.is_set()
            and client.is_authenticated
            and self._is_current(client, generation)
        )

    def try_acquire(self):
        generation = self._generation
        if generation is None or not self.current(generation):
            return None
        with self._lock:
            if self._retired.is_set() or self._generation != generation or self._active:
                return None
            token = LightingReservation(generation)
            self._active = token
            return token

    def release(self, token):
        with self._lock:
            if self._active is not token:
                return False
            self._active = None
            return True

    def admission(self, token):
        if not isinstance(token, LightingReservation) or not self.current(token.generation):
            raise LightingActionError("session-changed")
        with self._lock:
            if self._retired.is_set() or self._active is not token:
                raise LightingActionError("lighting-superseded")

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def mark_sent(self, token):
        try:
            self.admission(token)
        except LightingActionError:
            return False
        with self._lock:
            if self._retired.is_set() or self._active is not token:
                return False
            if not token.sent:
                token.sent = True
                self._revision += 1
            return True

    def acknowledge(self, token):
        """Invalidate reads begun before acceptance, including during the write."""
        with self._lock:
            if self._retired.is_set() or self._active is not token or not token.sent:
                return None
            if token.acknowledged_revision is None:
                self._revision += 1
                token.acknowledged_revision = self._revision
            return token.acknowledged_revision

    def observe(self, receipt):
        with self._lock:
            self._receipt = receipt
            keys = {row.camera_id for row in receipt.rows}
            self._operations = {
                key: value for key, value in self._operations.items() if key in keys
            }
            for row in receipt.rows:
                entry = self._operations.get(row.camera_id)
                if entry is None or entry[0] is not row.device:
                    continue
                operation = entry[1]
                if (
                    operation.accepted
                    and operation.revision == self._revision
                    and receipt_row(receipt, row.device, self._revision) is not None
                ):
                    self._operations[row.camera_id] = (
                        row.device,
                        replace(
                            operation,
                            phase="observed" if row.light is operation.requested else "uncertain",
                        ),
                    )

    def _set_operation(self, device, operation, token=None):
        with self._lock:
            if token is not None and (
                token.generation != self._generation or self._active is not token
            ):
                return
            key = camera_id(device)
            if key not in self._operations and len(self._operations) >= 256:
                self._operations.clear()
            self._operations[key] = (device, operation)

    def view(self, device):
        if not self.current(self._generation):
            return CameraLightingView()
        try:
            self._current_camera(device)
        except Exception:
            return CameraLightingView()
        with self._lock:
            row = receipt_row(self._receipt, device, self._revision)
            entry = self._operations.get(camera_id(device))
            operation = entry[1] if entry is not None and entry[0] is device else None
            if operation is not None and operation.revision != self._revision:
                operation = None
            return CameraLightingView(
                light=row.light if row is not None else None,
                motion_duration_seconds=row.motion_duration_seconds if row is not None else None,
                phase=operation.phase
                if operation is not None
                else "observed"
                if row is not None
                else "unknown",
                requested=operation.requested if operation is not None else None,
                accepted=operation.accepted if operation is not None else False,
            )

    def _current_camera(self, device):
        if getattr(device, "family", None) not in _CAMERA_FAMILIES:
            raise LightingActionError("camera-unavailable")
        if not any(candidate is device for candidate in self._client.all_devices):
            raise LightingActionError("camera-changed")
        if _api_id(getattr(device, "device_api_id", None)) is None:
            raise LightingActionError("camera-unavailable")
        try:
            supported = device.has_capability("light") is True
        except Exception:
            supported = False
        if not supported:
            raise LightingActionError("camera-unavailable")
        return device

    def invalidate_group_members(self, location_id, members):
        """Only exact location/API/kind matches may invalidate camera overrides."""
        from halo_gtk.device_commands import command_state

        identities = {
            (_api_id(member.api_id), member.kind)
            for member in members
            if member.location_id == location_id and _api_id(member.api_id) is not None
        }
        for device in self._client.all_devices:
            if (
                getattr(device, "family", None) in _CAMERA_FAMILIES
                and getattr(device, "location_id", None) == location_id
                and (_api_id(getattr(device, "id", None)), getattr(device, "kind", None))
                in identities
            ):
                command_state.invalidate_light(self._client, device)

    async def camera_light(self, device, on):
        if not isinstance(on, bool):
            raise LightingActionError("invalid-request")
        self._current_camera(device)
        token = self.try_acquire()
        if token is None:
            raise LightingActionError(
                "lighting-busy" if self.current(self._generation) else "session-changed"
            )
        state = {
            "entered": False,
            "accepted": False,
            "deadline": _elapsed_time() + CAMERA_ACTION_TIMEOUT,
        }
        self._set_operation(device, CameraLightOperation(on, "pending", self.revision))
        task = asyncio.create_task(self._run_camera(device, on, token, state))
        self._tasks.add(task)

        def finished(done):
            self._tasks.discard(done)
            if done.cancelled() and not state["entered"]:
                self._set_operation(
                    device, CameraLightOperation(on, "rejected", self.revision), token
                )
            # A task canceled before its first instruction never runs finally.
            self.release(token)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not state["entered"]:
                task.cancel()
            raise

    async def _run_camera(self, device, on, token, state):
        def require_budget():
            if _elapsed_time() >= state["deadline"] or asyncio.current_task().cancelling():
                raise TimeoutError

        def admission():
            require_budget()
            self.admission(token)
            self._current_camera(device)

        def on_admitted():
            admission()
            if not self.mark_sent(token):
                raise LightingActionError("session-changed")
            state["entered"] = True
            self._set_operation(
                device,
                CameraLightOperation(on, "pending", self.revision, possibly_sent=True),
                token,
            )

        try:
            async with asyncio.timeout(max(0, state["deadline"] - _elapsed_time())):
                admission()
                # Fixed route, one send. The SDK setter may retry actuator PUTs.
                transport = self._client._get_ring_json_transport()
                receipt = await transport.request_write(
                    _camera_light_url(device, on),
                    method="PUT",
                    json=None,
                    timeout=min(10.0, state["deadline"] - _elapsed_time()),
                    admission=admission,
                    on_admitted=on_admitted,
                )
                require_budget()
                admission()
                if not isinstance(receipt, RingWriteReceipt) or not receipt.accepted:
                    raise LightingActionError(
                        "camera-light-rejected", possibly_sent=state["entered"]
                    )
                state["accepted"] = True
                revision = self.acknowledge(token)
                if revision is None:
                    raise LightingActionError("session-changed", possibly_sent=True)
                operation = CameraLightOperation(on, "accepted", revision, True, True)
                self._set_operation(device, operation, token)
                try:
                    # The call begins only after acknowledgement. Its inventory
                    # lock prevents joining a GET already in flight before it.
                    await self._client._async_refresh_devices(None, -1)
                    require_budget()
                    admission()
                except Exception:
                    self._set_operation(device, replace(operation, phase="uncertain"), token)
                    return self.view(device)
                view = self.view(device)
                if view.phase != "observed":
                    self._set_operation(device, replace(operation, phase="uncertain"), token)
                return self.view(device)
        except (TimeoutError, asyncio.CancelledError) as exc:
            self._set_operation(
                device,
                CameraLightOperation(
                    on,
                    "uncertain" if state["entered"] else "rejected",
                    self.revision,
                    state["accepted"],
                    state["entered"],
                ),
                token,
            )
            raise LightingActionError(
                "outcome-unknown" if state["entered"] else "cancelled",
                possibly_sent=state["entered"],
            ) from exc
        except Exception as exc:
            sent = state["entered"] or getattr(exc, "possibly_sent", False)
            self._set_operation(
                device,
                CameraLightOperation(
                    on,
                    "uncertain" if sent else "rejected",
                    self.revision,
                    state["accepted"],
                    sent,
                ),
                token,
            )
            code = (
                exc.code
                if isinstance(exc, (LightingActionError, RingJsonError))
                else "camera-light-failed"
            )
            raise LightingActionError(code, possibly_sent=sent) from None
        finally:
            self.release(token)

    async def close(self):
        self._retired.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _api_id(value):
    if type(value) is int and 0 < value < 10**19:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        number = int(value)
        return str(number) if number > 0 else None
    return None


def _camera_light_url(device, on):
    identifier = _api_id(getattr(device, "device_api_id", None))
    if identifier is None or type(on) is not bool:
        raise LightingActionError("invalid-request")
    state = "on" if on else "off"
    return f"https://api.ring.com/clients_api/doorbots/{identifier}/floodlight_light_{state}"
