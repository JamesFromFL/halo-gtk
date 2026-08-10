"""Immutable, provider-neutral Alarm state exposed by Halo's backend."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique


@unique
class AlarmMode(StrEnum):
    """User-facing Ring Alarm modes."""

    DISARMED = "disarmed"
    HOME = "home"
    AWAY = "away"
    UNKNOWN = "unknown"


@unique
class AlarmPhase(StrEnum):
    """Current security-panel transition or alarm phase."""

    IDLE = "idle"
    EXIT_DELAY = "exit-delay"
    ENTRY_DELAY = "entry-delay"
    ALARMING = "alarming"
    UNKNOWN = "unknown"


@unique
class AlarmSignal(StrEnum):
    """Emergency category reported by the security panel."""

    NONE = "none"
    BURGLAR = "burglar"
    FIRE = "fire"
    CARBON_MONOXIDE = "carbon-monoxide"
    FIRE_OR_CARBON_MONOXIDE = "fire-or-carbon-monoxide"
    PANIC = "panic"
    UNKNOWN = "unknown"


@unique
class AlarmServiceStatus(StrEnum):
    """Lifecycle state for the complete Alarm backend."""

    STOPPED = "stopped"
    DISCOVERING = "discovering"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    ONLINE = "online"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"
    AUTHENTICATION_REQUIRED = "authentication-required"
    ERROR = "error"
    NO_SYSTEM = "no-system"


@unique
class AlarmConnectionStatus(StrEnum):
    """Connectivity reported for an Alarm hub asset."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    CELLULAR_BACKUP = "cellular-backup"
    OFFLINE = "offline"


@unique
class AlarmWriteAuthorization(StrEnum):
    """Evidence that the active account may change an Alarm location."""

    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    DENIED = "denied"


@unique
class AlarmInventoryStatus(StrEnum):
    """Completeness of a device inventory."""

    EMPTY = "empty"
    PARTIAL = "partial"
    COMPLETE = "complete"
    STALE = "stale"


@unique
class TriState(StrEnum):
    """A boolean signal whose value may not have been reported yet."""

    UNKNOWN = "unknown"
    INACTIVE = "inactive"
    ACTIVE = "active"

    @classmethod
    def from_bool(cls, value: object) -> TriState:
        if value is True:
            return cls.ACTIVE
        if value is False:
            return cls.INACTIVE
        return cls.UNKNOWN


@unique
class AlarmDeviceKind(StrEnum):
    """Normalized device categories without discarding unknown Ring types."""

    BASE_STATION = "base-station"
    SECURITY_PANEL = "security-panel"
    KEYPAD = "keypad"
    CONTACT_SENSOR = "contact-sensor"
    MOTION_SENSOR = "motion-sensor"
    GLASS_BREAK_SENSOR = "glass-break-sensor"
    TILT_SENSOR = "tilt-sensor"
    FLOOD_FREEZE_SENSOR = "flood-freeze-sensor"
    FLOOD_SENSOR = "flood-sensor"
    FREEZE_SENSOR = "freeze-sensor"
    SMOKE_ALARM = "smoke-alarm"
    CO_ALARM = "co-alarm"
    SMOKE_CO_ALARM = "smoke-co-alarm"
    SMOKE_CO_LISTENER = "smoke-co-listener"
    TEMPERATURE_SENSOR = "temperature-sensor"
    RANGE_EXTENDER = "range-extender"
    RETROFIT_BRIDGE = "retrofit-bridge"
    RETROFIT_ZONE = "retrofit-zone"
    PANIC_BUTTON = "panic-button"
    LOCK = "lock"
    SWITCH = "switch"
    VALVE = "valve"
    HUB = "hub"
    GENERIC_SENSOR = "generic-sensor"
    UNKNOWN = "unknown"


@unique
class AlarmCapability(StrEnum):
    """Independently reported device capabilities."""

    MODE = "mode"
    BATTERY = "battery"
    AC_POWER = "ac-power"
    TAMPER = "tamper"
    BYPASS = "bypass"
    FAULT = "fault"
    CONTACT = "contact"
    MOTION = "motion"
    GLASS_BREAK = "glass-break"
    TILT = "tilt"
    FLOOD = "flood"
    FREEZE = "freeze"
    SMOKE = "smoke"
    CARBON_MONOXIDE = "carbon-monoxide"
    TEMPERATURE = "temperature"
    LOCK = "lock"
    SWITCH = "switch"
    VALVE = "valve"
    SIREN = "siren"


@unique
class AlarmLockState(StrEnum):
    """Normalized lock state."""

    UNKNOWN = "unknown"
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    JAMMED = "jammed"


def _validate_identity(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} is invalid")


def _validate_revision(value: int, field_name: str = "revision") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class AlarmDeviceSnapshot:
    """Normalized state for one device attached to an Alarm asset."""

    location_id: str
    asset_id: str
    zid: str
    name: str = ""
    room: str | None = None
    kind: AlarmDeviceKind = AlarmDeviceKind.UNKNOWN
    raw_type: str = ""
    category_id: int | None = None
    parent_zid: str | None = None
    capabilities: frozenset[AlarmCapability] = frozenset()
    connection: AlarmConnectionStatus = AlarmConnectionStatus.UNKNOWN
    stale: bool = False
    battery_level: int | None = None
    battery_low: TriState = TriState.UNKNOWN
    tamper: TriState = TriState.UNKNOWN
    bypassed: TriState = TriState.UNKNOWN
    faulted: TriState = TriState.UNKNOWN
    contact: TriState = TriState.UNKNOWN
    motion: TriState = TriState.UNKNOWN
    glass_break: TriState = TriState.UNKNOWN
    tilt: TriState = TriState.UNKNOWN
    flood: TriState = TriState.UNKNOWN
    freeze: TriState = TriState.UNKNOWN
    smoke: TriState = TriState.UNKNOWN
    carbon_monoxide: TriState = TriState.UNKNOWN
    ac_power: TriState = TriState.UNKNOWN
    switch_on: TriState = TriState.UNKNOWN
    valve_open: TriState = TriState.UNKNOWN
    lock_state: AlarmLockState = AlarmLockState.UNKNOWN
    temperature_celsius: float | None = None
    mode: AlarmMode = AlarmMode.UNKNOWN
    phase: AlarmPhase = AlarmPhase.UNKNOWN
    signal: AlarmSignal = AlarmSignal.UNKNOWN
    faulted_device_ids: tuple[str, ...] | None = None
    faulted_devices_valid: bool = True
    transition_deadline: float | None = None
    revision: int = 0
    epoch: int = 0
    updated_at: float | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.location_id, "location_id")
        _validate_identity(self.asset_id, "asset_id")
        _validate_identity(self.zid, "zid")
        _validate_revision(self.revision)
        _validate_revision(self.epoch, "epoch")
        if not isinstance(self.name, str):
            raise ValueError("name must be a string")
        if self.room is not None and not isinstance(self.room, str):
            raise ValueError("room must be a string or None")
        if self.battery_level is not None:
            if isinstance(self.battery_level, bool) or not isinstance(self.battery_level, int):
                raise ValueError("battery_level must be an integer or None")
            if not 0 <= self.battery_level <= 100:
                raise ValueError("battery_level must be between 0 and 100")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if not isinstance(self.faulted_devices_valid, bool):
            raise ValueError("faulted_devices_valid must be a boolean")
        if self.faulted_device_ids is not None:
            if isinstance(self.faulted_device_ids, str):
                raise ValueError("faulted_device_ids must contain device identifiers")
            faulted_device_ids = tuple(self.faulted_device_ids)
            for zid in faulted_device_ids:
                _validate_identity(zid, "faulted device id")
            if len(faulted_device_ids) != len(set(faulted_device_ids)):
                raise ValueError("faulted_device_ids must be unique")
            object.__setattr__(self, "faulted_device_ids", faulted_device_ids)


@dataclass(frozen=True, slots=True)
class AlarmAssetSnapshot:
    """State for one base station, bridge, or other CLAP asset."""

    location_id: str
    asset_id: str
    kind: str = ""
    connection: AlarmConnectionStatus = AlarmConnectionStatus.UNKNOWN
    inventory: AlarmInventoryStatus = AlarmInventoryStatus.EMPTY
    stale: bool = False
    devices: tuple[AlarmDeviceSnapshot, ...] = ()
    revision: int = 0
    epoch: int = 0
    updated_at: float | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.location_id, "location_id")
        _validate_identity(self.asset_id, "asset_id")
        _validate_revision(self.revision)
        _validate_revision(self.epoch, "epoch")
        if not isinstance(self.kind, str):
            raise ValueError("kind must be a string")
        object.__setattr__(self, "devices", tuple(self.devices))
        seen_zids: set[str] = set()
        for device in self.devices:
            if device.location_id != self.location_id or device.asset_id != self.asset_id:
                raise ValueError("device identity does not match its containing asset")
            if device.zid in seen_zids:
                raise ValueError("asset devices must have unique zids")
            seen_zids.add(device.zid)

    def find_device(self, zid: str) -> AlarmDeviceSnapshot | None:
        return next((device for device in self.devices if device.zid == zid), None)


@dataclass(frozen=True, slots=True)
class AlarmLocationSnapshot:
    """Aggregated panel and inventory state for one Ring location."""

    location_id: str
    name: str = ""
    write_authorization: AlarmWriteAuthorization = AlarmWriteAuthorization.UNKNOWN
    connection: AlarmConnectionStatus = AlarmConnectionStatus.UNKNOWN
    inventory: AlarmInventoryStatus = AlarmInventoryStatus.EMPTY
    mode: AlarmMode = AlarmMode.UNKNOWN
    phase: AlarmPhase = AlarmPhase.UNKNOWN
    signal: AlarmSignal = AlarmSignal.UNKNOWN
    triggered: TriState = TriState.UNKNOWN
    transition_deadline: float | None = None
    assets: tuple[AlarmAssetSnapshot, ...] = ()
    can_set_mode: bool = False
    command_unavailable_reason: str | None = None
    revision: int = 0
    epoch: int = 0
    updated_at: float | None = None

    def __post_init__(self) -> None:
        _validate_identity(self.location_id, "location_id")
        _validate_revision(self.revision)
        _validate_revision(self.epoch, "epoch")
        if not isinstance(self.name, str):
            raise ValueError("name must be a string")
        if not isinstance(self.write_authorization, AlarmWriteAuthorization):
            raise ValueError("write_authorization must be an AlarmWriteAuthorization")
        object.__setattr__(self, "assets", tuple(self.assets))
        seen_asset_ids: set[str] = set()
        for asset in self.assets:
            if asset.location_id != self.location_id:
                raise ValueError("asset identity does not match its containing location")
            if asset.asset_id in seen_asset_ids:
                raise ValueError("location assets must have unique asset ids")
            seen_asset_ids.add(asset.asset_id)

    @property
    def devices(self) -> tuple[AlarmDeviceSnapshot, ...]:
        return tuple(device for asset in self.assets for device in asset.devices)

    @property
    def security_panel(self) -> AlarmDeviceSnapshot | None:
        match = None
        for asset in self.assets:
            for device in asset.devices:
                if device.kind is not AlarmDeviceKind.SECURITY_PANEL:
                    continue
                if match is not None:
                    return None
                match = device
        return match

    def find_asset(self, asset_id: str) -> AlarmAssetSnapshot | None:
        return next((asset for asset in self.assets if asset.asset_id == asset_id), None)

    def find_device(self, zid: str) -> AlarmDeviceSnapshot | None:
        match = None
        for asset in self.assets:
            device = asset.find_device(zid)
            if device is None:
                continue
            if match is not None:
                return None
            match = device
        return match


@dataclass(frozen=True, slots=True)
class AlarmAccountSnapshot:
    """Atomic view of Alarm state for the active Ring account generation."""

    generation: int = 0
    revision: int = 0
    status: AlarmServiceStatus = AlarmServiceStatus.STOPPED
    locations: tuple[AlarmLocationSnapshot, ...] = ()
    updated_at: float | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _validate_revision(self.generation, "generation")
        _validate_revision(self.revision)
        object.__setattr__(self, "locations", tuple(self.locations))
        location_ids = [location.location_id for location in self.locations]
        if len(location_ids) != len(set(location_ids)):
            raise ValueError("account locations must have unique ids")

    def find_location(self, location_id: str) -> AlarmLocationSnapshot | None:
        return next(
            (location for location in self.locations if location.location_id == location_id),
            None,
        )


def empty_alarm_snapshot(generation: int = 0) -> AlarmAccountSnapshot:
    """Return the canonical initial snapshot for a Ring client generation."""

    return AlarmAccountSnapshot(generation=generation)
