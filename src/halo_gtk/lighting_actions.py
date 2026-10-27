"""Client-owned, no-queue coordination for camera and group lighting actions."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True, eq=False)
class LightingReservation:
    generation: int


class LightingActions:
    """One local lighting intent, including preflight; no network lock is held.

    The group service owns its tasks. This object owns camera tasks after their
    presentation consumer detaches, and joins them before Auth teardown.
    """

    def __init__(self, client, *, is_current):
        self._client = client
        self._is_current = is_current
        self._lock = threading.Lock()
        self._generation = None
        self._retired = threading.Event()
        self._active = None
        self._tasks = set()
        self._close_task = None

    def initialize(self, generation):
        with self._lock:
            self._generation = generation
            self._retired.clear()

    def invalidate(self, generation):
        self._retired.set()
        with self._lock:
            self._generation = generation

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
        if not self.current(token.generation):
            raise LightingActionError("session-changed")
        with self._lock:
            if self._retired.is_set() or self._active is not token:
                raise LightingActionError("lighting-superseded")

    def _current_camera(self, device):
        if getattr(device, "family", None) not in _CAMERA_FAMILIES:
            raise LightingActionError("camera-unavailable")
        if not any(candidate is device for candidate in self._client.all_devices):
            raise LightingActionError("camera-changed")
        if not callable(getattr(device, "async_set_light", None)):
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
        state = {"entered": False, "deadline": _elapsed_time() + CAMERA_ACTION_TIMEOUT}
        task = asyncio.create_task(self._run_camera(device, on, token, state))
        self._tasks.add(task)

        def finished(done):
            self._tasks.discard(done)
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
        from halo_gtk.device_commands import command_state

        try:
            async with asyncio.timeout(max(0, state["deadline"] - _elapsed_time())):
                self.admission(token)
                self._current_camera(device)
                if _elapsed_time() >= state["deadline"] or asyncio.current_task().cancelling():
                    raise TimeoutError
                # The SDK owns token/HTTP preparation. Conservatively possibly
                # sent once entered, even if it later times out during auth.
                state["entered"] = True
                await device.async_set_light(on)
                # An SDK can suppress timeout cancellation or return without
                # yielding after suspend. Neither may publish a late success.
                if _elapsed_time() >= state["deadline"] or asyncio.current_task().cancelling():
                    raise TimeoutError
                self.admission(token)
                self._current_camera(device)
                # Commit on the owner loop BEFORE releasing the reservation.
                # GTK completion is presentation-only, so it cannot revive an
                # override invalidated by a newer group command.
                command_state.record_light_success(self._client, device, on)
        except LightingActionError as exc:
            command_state.invalidate_light(self._client, device)
            raise LightingActionError(exc.code, possibly_sent=state["entered"]) from None
        except (TimeoutError, asyncio.CancelledError):
            command_state.invalidate_light(self._client, device)
            raise LightingActionError(
                "outcome-unknown" if state["entered"] else "cancelled",
                possibly_sent=state["entered"],
            ) from None
        except Exception:
            command_state.invalidate_light(self._client, device)
            raise LightingActionError(
                "camera-light-failed", possibly_sent=state["entered"]
            ) from None
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
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        number = int(value)
        return str(number) if number > 0 else None
    return None
