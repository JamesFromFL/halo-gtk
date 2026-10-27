"""Bounded semantic observations of current Alarm state, never cloud history."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmInventoryStatus,
    AlarmMode,
    AlarmPhase,
    AlarmSignal,
    TriState,
)

MAX_PENDING_EVENTS = 128
_SIGNAL_LABELS = {
    AlarmSignal.BURGLAR: "Intrusion",
    AlarmSignal.FIRE: "Fire",
    AlarmSignal.CARBON_MONOXIDE: "Carbon monoxide",
    AlarmSignal.FIRE_OR_CARBON_MONOXIDE: "Fire or carbon monoxide",
    AlarmSignal.PANIC: "Panic",
}


@dataclass(frozen=True, slots=True)
class AlarmEvent:
    generation: int
    location_id: str
    kind: str
    category: str
    title: str
    detail: str = ""
    location_name: str = ""
    asset_id: str | None = None
    zid: str | None = None
    device_name: str = ""
    critical: bool = False
    observed_at: float = 0.0
    source: str = "Halo observation"


class AlarmEventBuffer:
    """Keep latest semantic keys; evict routine work before critical locations."""

    def __init__(self, capacity: int = MAX_PENDING_EVENTS) -> None:
        if not 1 <= capacity <= MAX_PENDING_EVENTS:
            raise ValueError("invalid Alarm event capacity")
        self.capacity = capacity
        self._pending: OrderedDict[tuple, AlarmEvent] = OrderedDict()
        self.suppressed = 0

    def put(self, event: AlarmEvent) -> None:
        # A critical event describes all currently reported signals at a
        # location, so a newer report can replace the older location summary.
        if event.critical:
            # FIRE -> cleared -> CO can arrive before GTK drains the queue.
            # The newer active summary supersedes that queued clear; retaining
            # it would announce a clear after the latest active alarm.
            for key, value in tuple(self._pending.items()):
                if value.location_id == event.location_id and value.kind == "alarm-cleared":
                    self._pending.pop(key)
        key = (
            (event.location_id, "critical")
            if event.critical
            else (event.location_id, event.asset_id, event.zid, event.kind)
        )
        if key in self._pending:
            self._pending[key] = event
            return
        if len(self._pending) >= self.capacity:
            victim = next((key for key, value in self._pending.items() if not value.critical), None)
            if victim is None:
                self.suppressed += 1
                return
            self._pending.pop(victim)
            self.suppressed += 1
        self._pending[key] = event

    def drain(self) -> tuple[tuple[AlarmEvent, ...], int]:
        values = tuple(self._pending.values())
        suppressed = self.suppressed
        self.clear()
        return values, suppressed

    def revoke(self, location_id: str) -> None:
        for key, value in tuple(self._pending.items()):
            if value.location_id == location_id:
                self._pending.pop(key)

    def clear(self) -> None:
        self._pending.clear()
        self.suppressed = 0


class AlarmEventReducer:
    """Suppress reconnect baselines; retain critical episodes across unknown gaps."""

    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._generation = None
        self._previous = {}
        self._critical = {}
        self._gaps = set()

    def clear(self) -> None:
        self._generation = None
        self._previous.clear()
        self._critical.clear()
        self._gaps.clear()

    def update(self, snapshot: AlarmAccountSnapshot) -> tuple[AlarmEvent, ...]:
        if snapshot.generation != self._generation:
            self.clear()
            self._generation = snapshot.generation
        ids = {location.location_id for location in snapshot.locations}
        for location_id in set(self._previous) | set(self._critical) | self._gaps:
            if location_id not in ids:
                self._previous.pop(location_id, None)
                self._critical.pop(location_id, None)
                self._gaps.discard(location_id)
        critical_events = []
        routine_events = []
        for location in snapshot.locations:
            previous = self._previous.get(location.location_id)
            ready = _ready(location)
            event = self._critical_event(snapshot.generation, location)
            if event is not None:
                critical_events.append(event)
            if not ready:
                if previous is not None and location.location_id not in self._gaps:
                    routine_events.append(
                        self._event(
                            location, "connection-lost", "health", "Alarm updates unavailable"
                        )
                    )
                    self._gaps.add(location.location_id)
                self._previous.pop(location.location_id, None)
                continue
            if location.location_id in self._gaps:
                routine_events.append(
                    self._event(location, "connection-restored", "health", "Alarm updates restored")
                )
                self._gaps.discard(location.location_id)
            if previous is not None and previous.epoch == location.epoch:
                routine_events.extend(self._changes(previous, location))
            self._previous[location.location_id] = location
        # Input locations/devices are bounded by the store. Notification
        # admission is separately bounded/coalesced by AlarmEventBuffer.
        return tuple(critical_events + routine_events)

    def _event(self, location, kind, category, title, detail="", device=None, *, critical=False):
        return AlarmEvent(
            generation=self._generation,
            location_id=location.location_id,
            kind=kind,
            category=category,
            title=title,
            detail=detail,
            location_name=location.name,
            asset_id=device.asset_id if device else None,
            zid=device.zid if device else None,
            device_name=device.name if device else "",
            critical=critical,
            observed_at=self._clock(),
        )

    def _critical_event(self, generation, location):
        active = set()
        cleared = set()
        reported = set()
        for asset in location.assets:
            if asset.stale or asset.connection is not AlarmConnectionStatus.ONLINE:
                continue
            for device in asset.devices:
                if device.stale or device.epoch != location.epoch:
                    continue
                identity = (asset.asset_id, device.zid)
                if device.signal in _SIGNAL_LABELS:
                    active.add((*identity, device.signal))
                    reported.add(identity)
                elif (
                    device.kind is AlarmDeviceKind.SECURITY_PANEL
                    and device.phase is AlarmPhase.ALARMING
                ):
                    active.add((*identity, AlarmSignal.UNKNOWN))
                    reported.add(identity)
                elif device.signal is AlarmSignal.NONE:
                    cleared.add(identity)
                    reported.add(identity)
        old = self._critical.get(location.location_id, set())
        # An absent device/unknown value/offline report cannot prove an
        # emergency cleared. Preserve its deduplication episode across a gap.
        retained = {key for key in old if key[:2] not in reported}
        combined = active | retained
        self._critical[location.location_id] = combined
        if not active - old:
            if old and not combined and all(key[:2] in cleared for key in old):
                return self._event(
                    location, "alarm-cleared", "alarm", "Ring reports alarm signals cleared"
                )
            return None
        labels = sorted({_SIGNAL_LABELS.get(key[2], "Alarm type unknown") for key in combined})
        return self._event(
            location,
            "active-alarm",
            "alarm",
            "Ring reported an active alarm",
            "; ".join(labels),
            critical=True,
        )

    def _changes(self, previous, location):
        if location.mode is not previous.mode and location.mode is not AlarmMode.UNKNOWN:
            yield self._event(
                location, "mode", "mode", f"Ring reports {location.mode.value.title()} mode"
            )
        if location.phase is not previous.phase and location.phase in {
            AlarmPhase.ENTRY_DELAY,
            AlarmPhase.EXIT_DELAY,
        }:
            yield self._event(
                location, "phase", "mode", f"Ring reports {location.phase.value.replace('-', ' ')}"
            )
        old_devices = {(d.asset_id, d.zid): d for d in previous.devices}
        fields = {
            "contact": ("sensor", "Contact"),
            "motion": ("sensor", "Motion"),
            "glass_break": ("sensor", "Glass break"),
            "tilt": ("sensor", "Tilt"),
            "flood": ("sensor", "Flood"),
            "freeze": ("sensor", "Freeze"),
            "tamper": ("health", "Tamper"),
            "battery_low": ("health", "Low battery"),
            "ac_power": ("health", "AC power"),
            "bypassed": ("health", "Bypass"),
        }
        for device in location.devices:
            old = old_devices.get((device.asset_id, device.zid))
            if old is None:
                continue
            if (
                device.connection is not old.connection
                and device.connection is not AlarmConnectionStatus.UNKNOWN
            ):
                yield self._event(
                    location,
                    "supervision",
                    "health",
                    "Sensor communication changed",
                    device.connection.value,
                    device,
                )
            if device.stale or old.stale:
                continue
            for field, (category, label) in fields.items():
                value, old_value = getattr(device, field), getattr(old, field)
                if value is TriState.UNKNOWN or old_value is TriState.UNKNOWN or value is old_value:
                    continue
                detail = "Active" if value is TriState.ACTIVE else "Inactive"
                if field == "contact":
                    detail = "Open" if value is TriState.ACTIVE else "Closed"
                yield self._event(
                    location, field, category, f"{label} state changed", detail, device
                )


def _ready(location) -> bool:
    return (
        location.connection is AlarmConnectionStatus.ONLINE
        and location.inventory is AlarmInventoryStatus.COMPLETE
    )
