"""Qualified individual Beams lighting, with one-shot writes and observed state."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace

from halo_gtk.alarm.models import AlarmConnectionStatus, AlarmInventoryStatus, TriState
from halo_gtk.alarm.protocol import DeviceInfoDocList, DeviceInfoDocUpdate
from halo_gtk.alarm.provider import AlarmCommandSupersededError

BEAMS_TYPES = frozenset({"switch.beams", "switch.multilevel.beams", "switch.transformer.beams"})
ON_DURATIONS = (30, 60, 300, 900)
MAX_LIGHTS = 1024


class BeamsIntent:
    def __init__(self, allowed):
        self._allowed = allowed
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def current(self):
        return not self._cancelled.is_set() and self._allowed() is True


@dataclass(frozen=True, slots=True)
class BeamsResult:
    status: str
    code: str
    possibly_sent: bool = False


@dataclass(frozen=True, slots=True)
class BeamsLight:
    device: object
    location_name: str
    reason: str | None
    reported_on: TriState
    valid_until: float
    lighting_revision: int

    @property
    def identity(self):
        return (self.device.location_id, self.device.asset_id, self.device.zid)


@dataclass(frozen=True, slots=True)
class BeamsSnapshot:
    generation: int = 0
    lights: tuple[BeamsLight, ...] = ()
    truncated: bool = False

    def find(self, identity):
        return next((light for light in self.lights if light.identity == identity), None)

    def current(self, now, lighting_revision):
        return replace(
            self,
            lights=tuple(
                replace(
                    light, reason=light.reason or "refresh-required", reported_on=TriState.UNKNOWN
                )
                if now < 0
                or now > light.valid_until
                or light.lighting_revision != lighting_revision
                else light
                for light in self.lights
            ),
        )


@dataclass(slots=True)
class _Observed:
    epoch: int
    on_at: float | None = None
    communication_at: float | None = None
    serial: int = 0
    lighting_revision: int = -1


class BeamsController:
    def __init__(self, service, lighting, *, timeout=10.0, max_age=120.0):
        self._service, self._lighting = service, lighting
        self._timeout = min(10.0, timeout)
        self._max_age = min(120.0, max_age)
        self._observed, self._owners, self._uncertain = {}, {}, {}
        self._owners_at = None
        self._pending = None
        self._requests = set()
        self._closed = False
        self.snapshot = BeamsSnapshot(service._generation)

    def update_owners(self, records):
        """Only a fresh authenticated devices GET supplies bridge-specific authority."""
        self._owners, self._owners_at = {}, None
        if not isinstance(records, list) or len(records) > 1024:
            self._service._publish()
            return
        owners, seen, duplicate = {}, set(), set()
        for row in records:
            if not isinstance(row, Mapping):
                self._service._publish()
                return
            api_id, location = row.get("id"), row.get("location_id")
            if type(api_id) is not int or not 0 < api_id < 2**53:
                continue
            if api_id in seen:
                duplicate.add(api_id)
            seen.add(api_id)
            if not isinstance(location, str) or not location or len(location) > 512:
                continue
            if any(ord(char) < 32 for char in location):
                continue
            owners[(location, api_id)] = row.get("role") == "owner"
        self._owners = {key: value for key, value in owners.items() if key[1] not in duplicate}
        self._owners_at = self._service._monotonic_clock()
        self._service._publish()

    def _device(self, key):
        location = self._service._store.snapshot.find_location(key[0])
        asset = location.find_asset(key[1]) if location else None
        return location, asset, asset.find_device(key[2]) if asset else None

    @staticmethod
    def _qualified(asset, device):
        return bool(
            asset
            and device
            and asset.kind == "beams_bridge_v1"
            and asset.connection is AlarmConnectionStatus.ONLINE
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and not asset.stale
            and not device.stale
            and device.connection is AlarmConnectionStatus.ONLINE
            and device.raw_type in BEAMS_TYPES
        )

    def _owner(self, key, runtime):
        now = self._service._monotonic_clock()
        if self._owners_at is None or not 0 <= now - self._owners_at <= self._max_age:
            return False
        assets = runtime.transport.assets
        asset = next((item for item in assets if item.asset_id == key[1]), None)
        api_id = getattr(asset, "doorbot_id", None)
        return bool(
            asset
            and asset.kind == "beams_bridge_v1"
            and type(api_id) is int
            and 0 < api_id < 2**53
            and sum(getattr(item, "doorbot_id", None) == api_id for item in assets) == 1
            and self._owners.get((key[0], api_id)) is True
        )

    def _reason(self, key, *, allow_pending=False):
        _, asset, device = self._device(key)
        runtime = self._service._runtimes.get(key[0])
        observed = self._observed.get(key)
        now = self._service._monotonic_clock()
        if self._closed or self._service._should_stop():
            return "session-unavailable"
        if (
            not device
            or device.raw_type not in BEAMS_TYPES
            or not asset
            or asset.kind != "beams_bridge_v1"
        ):
            return "model-unsupported"
        if not self._qualified(asset, device) or runtime is None or runtime.epoch != device.epoch:
            return "light-unavailable"
        if not self._owner(key, runtime):
            return "owner-unavailable"
        if observed is None or observed.epoch != runtime.epoch or self._lighting is None:
            return "refresh-required"
        if device.switch_on is TriState.UNKNOWN or any(
            value is None or not 0 <= now - value <= self._max_age
            for value in (asset.inventory_monotonic, observed.on_at, observed.communication_at)
        ):
            return "refresh-required"
        if not allow_pending and observed.lighting_revision != self._lighting.revision:
            return "refresh-required"
        if observed.serial <= self._uncertain.get(key, -1):
            return "refresh-required"
        return None

    def observe(self, runtime, message):
        if self._closed or not isinstance(message, (DeviceInfoDocList, DeviceInfoDocUpdate)):
            return
        now = self._service._monotonic_clock()
        for document in message.devices:
            key = (runtime.seed.location_id, message.asset_id, document.zid)
            _, asset, device = self._device(key)
            if not self._qualified(asset, device) or device.epoch != runtime.epoch:
                continue
            observed = self._observed.get(key)
            if observed is None or observed.epoch != runtime.epoch:
                if len(self._observed) >= MAX_LIGHTS:
                    continue
                observed = _Observed(runtime.epoch)
                self._observed[key] = observed
            if isinstance(message, DeviceInfoDocList):
                observed.on_at = observed.communication_at = None
            if "commStatus" in document.data:
                observed.communication_at = now if document.data["commStatus"] == "ok" else None
            if "on" in document.data:
                observed.on_at = now if type(document.data["on"]) is bool else None
                observed.serial += 1
                observed.lighting_revision = getattr(self._lighting, "revision", -1)
                pending = self._pending
                if (
                    pending
                    and pending[0] == key
                    and pending[1]
                    and observed.serial > pending[2]
                    and not pending[3].done()
                ):
                    pending[3].set_result(True)

    def publish(self, snapshot):
        retained, lights, count = set(), [], 0
        for location in snapshot.locations:
            for asset in location.assets:
                if not asset.kind.startswith("beams_bridge"):
                    continue
                for device in asset.devices:
                    if ".beams" not in device.raw_type:
                        continue
                    count += 1
                    if len(lights) >= MAX_LIGHTS:
                        continue
                    key = (location.location_id, asset.asset_id, device.zid)
                    observed = self._observed.get(key)
                    if (
                        observed
                        and observed.epoch == device.epoch
                        and self._qualified(asset, device)
                    ):
                        retained.add(key)
                    else:
                        observed = None
                    times = (
                        asset.inventory_monotonic,
                        self._owners_at,
                        observed.on_at if observed else None,
                        observed.communication_at if observed else None,
                    )
                    until = min(times) + self._max_age if all(x is not None for x in times) else -1
                    revision = observed.lighting_revision if observed else -1
                    on = device.switch_on if observed else TriState.UNKNOWN
                    lights.append(
                        BeamsLight(device, location.name, self._reason(key), on, until, revision)
                    )
        for key in set(self._observed) - retained:
            self._observed.pop(key, None)
            if self._pending and self._pending[0] == key and not self._pending[3].done():
                self._pending[3].set_result(False)
        for key in set(self._uncertain) - retained:
            self._uncertain.pop(key, None)
        self.snapshot = BeamsSnapshot(snapshot.generation, tuple(lights), count > MAX_LIGHTS)

    async def request(self, location_id, asset_id, zid, duration, *, revision, intent, reservation):
        if duration is not None and (type(duration) is not int or duration not in ON_DURATIONS):
            return BeamsResult("rejected", "invalid-duration")
        if not isinstance(intent, BeamsIntent) or self._lighting is None:
            return BeamsResult("rejected", "invalid-intent")
        if self._pending is not None:
            return BeamsResult("rejected", "lighting-busy")
        key = (location_id, asset_id, zid)
        runtime = self._service._runtimes.get(location_id)
        observed = self._observed.get(key)
        if runtime is None or observed is None:
            return BeamsResult("rejected", "refresh-required")
        pending = [key, False, observed.serial, asyncio.get_running_loop().create_future()]
        self._pending = pending
        task = asyncio.current_task()
        self._requests.add(task)
        reported = False

        @contextlib.contextmanager
        def admission():
            with self._service._command_admission():
                self._lighting.admission(reservation)
                _, _, device = self._device(key)
                if (
                    self._reason(key)
                    or not intent.current()
                    or device.revision != revision
                    or self._service._runtimes.get(location_id) is not runtime
                    or runtime.epoch != observed.epoch
                    or self._observed.get(key) is not observed
                ):
                    raise AlarmCommandSupersededError("light-state-changed")
                if not self._lighting.mark_sent(reservation):
                    raise AlarmCommandSupersededError("lighting-retired")
                pending[1], pending[2] = True, observed.serial
                yield

        try:
            async with asyncio.timeout(self._timeout):
                await runtime.transport.send_beams_light(
                    asset_id, zid, duration, epoch=runtime.epoch, admission=admission
                )
                if await asyncio.shield(pending[3]):
                    with self._service._command_admission():
                        self._lighting.admission(reservation)
                        _, _, device = self._device(key)
                        current = (
                            self._service._runtimes.get(location_id) is runtime
                            and runtime.epoch == observed.epoch
                            and self._observed.get(key) is observed
                            and not self._reason(key)
                        )
                        if current and device.switch_on is not TriState.UNKNOWN:
                            reported = True
                            self._uncertain.pop(key, None)
                            # No exposed field proves automatic mode. A lamp
                            # observation must never be presented as that proof.
                            if duration is None:
                                return BeamsResult("submitted", "automatic-unverified", True)
                            if device.switch_on is TriState.ACTIVE:
                                return BeamsResult("observed", "reported-on", True)
                            return BeamsResult("unknown", "reported-off", True)
                return BeamsResult("unknown", "connection-changed", pending[1])
        except asyncio.CancelledError:
            raise
        except Exception:
            return BeamsResult(
                "unknown" if pending[1] else "rejected",
                "outcome-unknown" if pending[1] else "state-changed",
                pending[1],
            )
        finally:
            self._requests.discard(task)
            self._pending = None
            if (
                pending[1]
                and not reported
                and self._observed.get(key) is observed
                and self._service._runtimes.get(location_id) is runtime
                and runtime.epoch == observed.epoch
            ):
                self._uncertain[key] = observed.serial
            if not pending[3].done():
                pending[3].cancel()
            self._service._publish()

    async def close(self):
        self._closed = True
        tasks = tuple(self._requests - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._observed.clear()
        self._owners.clear()
        self._uncertain.clear()
