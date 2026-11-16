"""Typed keypad LED commands and bounded, observed proximity on the hub socket."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass, replace

from halo_gtk.alarm.models import (
    AlarmConnectionStatus,
    AlarmInventoryStatus,
    AlarmWriteAuthorization,
    TriState,
)
from halo_gtk.alarm.protocol import DeviceInfoDocList, DeviceInfoDocUpdate
from halo_gtk.alarm.provider import AlarmCommandSupersededError
from halo_gtk.alarm.store import keypad_brightness


class KeypadIntent:
    """An explicit UI operation, revocable until the single socket handoff."""

    def __init__(self, allowed):
        self._cancelled = threading.Event()
        self._allowed = allowed

    def cancel(self):
        self._cancelled.set()

    def current(self):
        return not self._cancelled.is_set() and self._allowed() is True


@dataclass(frozen=True, slots=True)
class KeypadResult:
    status: str
    code: str
    possibly_sent: bool = False


@dataclass(slots=True)
class _Observed:
    epoch: int
    brightness_at: float | None = None
    communication_at: float | None = None
    serial: int = 0
    motion_at: float | None = None
    motion_wall: float | None = None


class KeypadController:
    """Uses the Alarm service's current runtime, authorization and send fence."""

    def __init__(self, service, *, max_age=120.0, motion_seconds=20.0, timeout=10.0):
        self._service = service
        self._max_age = max_age
        self._motion_seconds = motion_seconds
        self._timeout = timeout
        self._observed = {}
        self._motion_tasks = {}
        self._pending = {}
        self._requests = set()
        self._uncertain = {}
        self._closed = False

    def _device(self, key):
        location = self._service._store.snapshot.find_location(key[0])
        asset = location.find_asset(key[1]) if location else None
        device = asset.find_device(key[2]) if asset else None
        return location, asset, device

    @staticmethod
    def _qualified(asset, device):
        return bool(
            asset
            and device
            and asset.kind == "base_station_v1"
            and asset.connection is AlarmConnectionStatus.ONLINE
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and not asset.stale
            and not device.stale
            and device.connection is AlarmConnectionStatus.ONLINE
            and device.raw_type == "security-keypad"
        )

    def observe(self, runtime, message, previous):
        if self._closed or not isinstance(message, (DeviceInfoDocList, DeviceInfoDocUpdate)):
            return
        location_id = runtime.seed.location_id
        now = self._service._monotonic_clock()
        for document in message.devices:
            key = (location_id, message.asset_id, document.zid)
            _, asset, device = self._device(key)
            if not self._qualified(asset, device) or device.epoch != runtime.epoch:
                continue
            observed = self._observed.get(key)
            if observed is None or observed.epoch != runtime.epoch:
                observed = _Observed(runtime.epoch)
                self._observed[key] = observed
            if isinstance(message, DeviceInfoDocList):
                observed.brightness_at = None
                observed.communication_at = None
            if "brightness" in document.data:
                observed.brightness_at = (
                    now if keypad_brightness(document.data["brightness"]) is not None else None
                )
                observed.serial += 1
                pending = self._pending.get(key)
                if (
                    pending
                    and pending[0]
                    and observed.serial > pending[2]
                    and device.keypad_brightness == pending[1]
                    and not pending[3].done()
                ):
                    pending[3].set_result(True)
            if "commStatus" in document.data:
                observed.communication_at = now if document.data["commStatus"] == "ok" else None
            previous_asset = previous.find_asset(message.asset_id) if previous else None
            known = previous_asset.find_device(document.zid) if previous_asset else None
            if (
                isinstance(message, DeviceInfoDocUpdate)
                and document.keypad_motion
                and self._qualified(previous_asset, known)
                and known.epoch == runtime.epoch
            ):
                observed.motion_at = now
                observed.motion_wall = self._service._wall_clock()
                old = self._motion_tasks.pop(key, None)
                if old is not None:
                    old.cancel()
                self._motion_tasks[key] = asyncio.create_task(self._expire_motion(key, observed))

    async def _expire_motion(self, key, observed):
        try:
            await asyncio.sleep(self._motion_seconds)
            if not self._closed and self._observed.get(key) is observed:
                self._service._publish()
        finally:
            if self._motion_tasks.get(key) is asyncio.current_task():
                self._motion_tasks.pop(key, None)

    def decorate(self, snapshot):
        now = self._service._monotonic_clock()
        retained = set()
        locations = []
        for location in snapshot.locations:
            assets = []
            for asset in location.assets:
                devices = []
                for device in asset.devices:
                    key = (location.location_id, asset.asset_id, device.zid)
                    observed = self._observed.get(key)
                    state, wall, motion_at = TriState.UNKNOWN, None, None
                    if (
                        not self._closed
                        and observed
                        and observed.epoch == device.epoch
                        and self._qualified(asset, device)
                    ):
                        retained.add(key)
                        if observed.motion_at is not None:
                            wall = observed.motion_wall
                            motion_at = observed.motion_at
                            state = (
                                TriState.ACTIVE
                                if 0 <= now - observed.motion_at < self._motion_seconds
                                else TriState.INACTIVE
                            )
                    devices.append(
                        replace(
                            device,
                            keypad_proximity=state,
                            keypad_proximity_at=wall,
                            keypad_proximity_monotonic=motion_at,
                        )
                        if device.raw_type == "security-keypad"
                        else device
                    )
                assets.append(replace(asset, devices=tuple(devices)))
            locations.append(replace(location, assets=tuple(assets)))
        for key in set(self._observed) - retained:
            self._observed.pop(key, None)
            self._uncertain.pop(key, None)
            task = self._motion_tasks.pop(key, None)
            if task is not None:
                task.cancel()
            pending = self._pending.get(key)
            if pending and not pending[3].done():
                pending[3].set_result(False)
        return replace(snapshot, locations=tuple(locations))

    def _check(self, key, revision, intent, runtime, observed):
        service = self._service
        location, asset, device = self._device(key)
        now = service._monotonic_clock()
        if self._closed or service._should_stop() or not intent.current():
            raise AlarmCommandSupersededError("local-permission-changed")
        if service._runtimes.get(key[0]) is not runtime or runtime.epoch != observed.epoch:
            raise AlarmCommandSupersededError("connection-changed")
        if (
            not self._qualified(asset, device)
            or device.connection is not AlarmConnectionStatus.ONLINE
        ):
            raise AlarmCommandSupersededError("keypad-unavailable")
        if location.write_authorization is not AlarmWriteAuthorization.ALLOWED:
            raise AlarmCommandSupersededError("permission-unavailable")
        if device.revision != revision or self._observed.get(key) is not observed:
            raise AlarmCommandSupersededError("keypad-changed")
        if device.keypad_brightness is None or any(
            value is None or not 0 <= now - value <= self._max_age
            for value in (
                asset.inventory_monotonic,
                observed.brightness_at,
                observed.communication_at,
            )
        ):
            raise AlarmCommandSupersededError("refresh-required")

    async def request(self, location_id, asset_id, zid, brightness, *, revision, intent):
        if type(brightness) is not int or not 0 <= brightness <= 100:
            return KeypadResult("rejected", "invalid-brightness")
        if not isinstance(intent, KeypadIntent):
            return KeypadResult("rejected", "invalid-intent")
        key = (location_id, asset_id, zid)
        if key in self._pending:
            return KeypadResult("rejected", "busy")
        runtime = self._service._runtimes.get(location_id)
        observed = self._observed.get(key)
        if runtime is None or observed is None:
            return KeypadResult("rejected", "refresh-required")
        if observed.serial <= self._uncertain.get(key, -1):
            return KeypadResult("rejected", "refresh-required")
        pending = [False, brightness, observed.serial, asyncio.get_running_loop().create_future()]
        self._pending[key] = pending
        task = asyncio.current_task()
        self._requests.add(task)
        confirmed = False

        @contextlib.contextmanager
        def admission():
            with self._service._command_admission():
                self._check(key, revision, intent, runtime, observed)
                pending[0] = True
                pending[2] = observed.serial
                yield

        try:
            self._check(key, revision, intent, runtime, observed)
            async with asyncio.timeout(self._timeout):
                await runtime.transport.send_keypad_brightness(
                    asset_id, zid, brightness, epoch=runtime.epoch, admission=admission
                )
                # Completion means the socket accepted bytes. Only a subsequent
                # brightness observation confirms the reported setting.
                if await asyncio.shield(pending[3]):
                    # A report may wake us just as logout, authorization loss,
                    # a newer report or a replacement connection retires it.
                    # Local relocking cannot recall an already submitted write.
                    with self._service._command_admission():
                        location, asset, device = self._device(key)
                        current = (
                            not self._closed
                            and not self._service._should_stop()
                            and self._service._runtimes.get(location_id) is runtime
                            and runtime.epoch == observed.epoch
                            and self._observed.get(key) is observed
                            and self._qualified(asset, device)
                            and location.write_authorization is AlarmWriteAuthorization.ALLOWED
                            and device.keypad_brightness == brightness
                        )
                    if current:
                        confirmed = True
                        self._uncertain.pop(key, None)
                        return KeypadResult("confirmed", "reported-target", True)
                return KeypadResult("unknown", "connection-changed", pending[0])
        except AlarmCommandSupersededError:
            return KeypadResult(
                "unknown" if pending[0] else "rejected", "request-superseded", pending[0]
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return KeypadResult(
                "unknown" if pending[0] else "rejected", "confirmation-unavailable", pending[0]
            )
        finally:
            self._requests.discard(task)
            self._pending.pop(key, None)
            if pending[0] and not confirmed:
                self._uncertain[key] = observed.serial
            if not pending[3].done():
                pending[3].cancel()

    async def close(self):
        self._closed = True
        tasks = tuple(self._motion_tasks.values()) + tuple(
            self._requests - {asyncio.current_task()}
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._motion_tasks.clear()
        for pending in self._pending.values():
            if not pending[3].done():
                pending[3].set_result(False)
        self._observed.clear()
        self._uncertain.clear()
