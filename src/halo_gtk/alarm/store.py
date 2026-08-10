"""Deterministic in-memory projection of validated Alarm protocol messages."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmCapability,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmLockState,
    AlarmMode,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmSignal,
    AlarmWriteAuthorization,
    TriState,
    empty_alarm_snapshot,
)
from halo_gtk.alarm.protocol import (
    MAX_DEVICE_DOCUMENTS,
    MAX_FRAME_BYTES,
    MAX_JSON_NODES,
    ClapMessage,
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    HubDisconnection,
    SessionInfo,
    UnknownMessage,
)

ExpectedAsset: TypeAlias = str | tuple[str, str]


class AlarmStateBoundsError(RuntimeError):
    """A sanitized signal that retained per-asset state exceeded safe bounds."""

    def __init__(self, code: str = "retained-state-limit") -> None:
        super().__init__(code)
        self.code = code


_ALARMING_STATES = {
    "burglar-alarm",
    "fire-alarm",
    "co-alarm",
    "panic",
    "user-verified-burglar-alarm",
    "user-verified-co-or-fire-alarm",
    "burglar-accelerated-alarm",
    "fire-accelerated-alarm",
}
_SAFE_ALARM_STATES = {"idle", "none"}

_KIND_BY_TYPE = {
    "hub.redsky": AlarmDeviceKind.BASE_STATION,
    "hub.kili": AlarmDeviceKind.BASE_STATION,
    "security-panel": AlarmDeviceKind.SECURITY_PANEL,
    "security-keypad": AlarmDeviceKind.KEYPAD,
    "sensor.contact": AlarmDeviceKind.CONTACT_SENSOR,
    "sensor.motion": AlarmDeviceKind.MOTION_SENSOR,
    "motion-sensor.beams": AlarmDeviceKind.MOTION_SENSOR,
    "sensor.glassbreak": AlarmDeviceKind.GLASS_BREAK_SENSOR,
    "sensor.tilt": AlarmDeviceKind.TILT_SENSOR,
    "sensor.flood-freeze": AlarmDeviceKind.FLOOD_FREEZE_SENSOR,
    "sensor.water": AlarmDeviceKind.FLOOD_SENSOR,
    "sensor.freeze": AlarmDeviceKind.FREEZE_SENSOR,
    "alarm.smoke": AlarmDeviceKind.SMOKE_ALARM,
    "alarm.co": AlarmDeviceKind.CO_ALARM,
    "comp.bluejay.sensor_bluejay_wsc": AlarmDeviceKind.SMOKE_CO_ALARM,
    "listener.smoke-co": AlarmDeviceKind.SMOKE_CO_LISTENER,
    "sensor.temperature": AlarmDeviceKind.TEMPERATURE_SENSOR,
    "range-extender.zwave": AlarmDeviceKind.RANGE_EXTENDER,
    "bridge.flatline": AlarmDeviceKind.RETROFIT_BRIDGE,
    "sensor.zone": AlarmDeviceKind.RETROFIT_ZONE,
    "security-panic": AlarmDeviceKind.PANIC_BUTTON,
    "valve.water": AlarmDeviceKind.VALVE,
}

_SWITCH_TYPES = {
    "switch",
    "switch.multilevel",
    "switch.multilevel.bulb",
    "switch.beams",
    "switch.multilevel.beams",
    "switch.transformer.beams",
    "group.light-group.beams",
}

_TYPE_CAPABILITIES = {
    "security-panel": {AlarmCapability.MODE, AlarmCapability.SIREN},
    "sensor.contact": {AlarmCapability.CONTACT, AlarmCapability.FAULT, AlarmCapability.BYPASS},
    "sensor.motion": {AlarmCapability.MOTION, AlarmCapability.FAULT, AlarmCapability.BYPASS},
    "motion-sensor.beams": {AlarmCapability.MOTION},
    "sensor.glassbreak": {
        AlarmCapability.GLASS_BREAK,
        AlarmCapability.FAULT,
        AlarmCapability.BYPASS,
    },
    "sensor.tilt": {AlarmCapability.TILT, AlarmCapability.FAULT, AlarmCapability.BYPASS},
    "sensor.flood-freeze": {AlarmCapability.FLOOD, AlarmCapability.FREEZE},
    "sensor.water": {AlarmCapability.FLOOD},
    "sensor.freeze": {AlarmCapability.FREEZE},
    "alarm.smoke": {AlarmCapability.SMOKE},
    "alarm.co": {AlarmCapability.CARBON_MONOXIDE},
    "comp.bluejay.sensor_bluejay_wsc": {
        AlarmCapability.SMOKE,
        AlarmCapability.CARBON_MONOXIDE,
    },
    "listener.smoke-co": {AlarmCapability.SMOKE, AlarmCapability.CARBON_MONOXIDE},
    "sensor.temperature": {AlarmCapability.TEMPERATURE},
    "sensor.zone": {AlarmCapability.CONTACT, AlarmCapability.FAULT, AlarmCapability.BYPASS},
    "valve.water": {AlarmCapability.VALVE},
}


@dataclass(slots=True)
class _DeviceState:
    data: dict[str, Any]
    revision: int
    epoch: int
    updated_at: float


@dataclass(slots=True)
class _AssetState:
    asset_id: str
    kind: str = ""
    connection: AlarmConnectionStatus = AlarmConnectionStatus.UNKNOWN
    inventory: AlarmInventoryStatus = AlarmInventoryStatus.EMPTY
    stale: bool = False
    devices: dict[str, _DeviceState] = field(default_factory=dict)
    revision: int = 1
    epoch: int = 0
    updated_at: float | None = None


@dataclass(slots=True)
class _LocationState:
    location_id: str
    name: str = ""
    write_authorization: AlarmWriteAuthorization = AlarmWriteAuthorization.UNKNOWN
    assets: dict[str, _AssetState] = field(default_factory=dict)
    revision: int = 1
    epoch: int = 0
    updated_at: float | None = None


_RETAINED_TEXT_FIELDS = {
    "name": 256,
    "roomName": 256,
    "deviceType": 128,
    "parentZid": 512,
}
_RETAINED_SCALAR_FIELDS = {
    "categoryId",
    "batteryLevel",
    "batteryStatus",
    "acStatus",
    "tamperStatus",
    "bypassed",
    "bypassStatus",
    "faulted",
    "motionStatus",
    "celsius",
    "locked",
    "on",
    "valveState",
    "siren",
    "mode",
    "transitionDelayEndTimestamp",
    "alarmStatus",
}
_RETAINED_NESTED_FIELDS = {
    "flood": ("faulted",),
    "freeze": ("faulted",),
    "smoke": ("alarmStatus",),
    "co": ("alarmStatus",),
}


def _retained_device_patch(document: DeviceDocument) -> dict[str, Any]:
    data = document.data
    retained: dict[str, Any] = {"zid": document.zid}
    for field_name, limit in _RETAINED_TEXT_FIELDS.items():
        if field_name in data:
            retained[field_name] = _bounded_text(data.get(field_name), limit)
    for field_name in _RETAINED_SCALAR_FIELDS:
        if field_name in data:
            retained[field_name] = _retained_scalar(data.get(field_name))
    for field_name, nested_fields in _RETAINED_NESTED_FIELDS.items():
        if field_name in data:
            retained[field_name] = _retained_mapping(data.get(field_name), nested_fields)
    if "components" in data:
        retained["components"] = _retained_components(data.get("components"))
    if "alarmInfo" in data:
        retained["alarmInfo"] = _retained_alarm_info(data.get("alarmInfo"))
    return retained


def _retained_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -(2**53 - 1) <= value <= 2**53 - 1 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if (
        isinstance(value, str)
        and len(value) <= 512
        and not any(ord(character) < 32 for character in value)
    ):
        return value
    return None


def _retained_mapping(
    value: object,
    fields: tuple[str, ...],
) -> dict[str, Any] | None:
    mapping = _mapping(value)
    if mapping is None:
        return None
    return {
        field_name: _retained_scalar(mapping.get(field_name))
        for field_name in fields
        if field_name in mapping
    }


def _retained_components(value: object) -> dict[str, Any] | None:
    components = _mapping(value)
    if components is None:
        return None
    return {
        component_name: _retained_mapping(components.get(component_name), ("alarmStatus",))
        for component_name in ("alarm.smoke", "alarm.co")
        if component_name in components
    }


def _retained_alarm_info(value: object) -> dict[str, Any] | None:
    alarm_info = _mapping(value)
    if alarm_info is None:
        return None
    retained: dict[str, Any] = {}
    if "state" in alarm_info:
        retained["state"] = _retained_scalar(alarm_info.get("state"))
    if "faultedDevices" in alarm_info:
        retained["faultedDevices"] = _retained_faulted_device_ids(alarm_info.get("faultedDevices"))
    return retained


def _retained_faulted_device_ids(value: object) -> list[str] | None:
    if not isinstance(value, list | tuple) or len(value) > 256:
        return None
    identifiers: list[str] = []
    for identifier in value:
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or len(identifier) > 512
            or any(ord(character) < 32 for character in identifier)
            or identifier in identifiers
        ):
            return None
        identifiers.append(identifier)
    return identifiers


def _validate_retained_candidate(devices: Mapping[str, _DeviceState]) -> None:
    if len(devices) > MAX_DEVICE_DOCUMENTS:
        raise AlarmStateBoundsError()
    payload = [device.data for device in devices.values()]
    nodes = 0
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise AlarmStateBoundsError()
        if isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise AlarmStateBoundsError("retained-state-invalid") from None
    if len(encoded) > MAX_FRAME_BYTES:
        raise AlarmStateBoundsError()


def _validate_device_batch(documents: tuple[DeviceDocument, ...]) -> None:
    if len(documents) > MAX_DEVICE_DOCUMENTS:
        raise AlarmStateBoundsError()
    zids = tuple(document.zid for document in documents)
    if len(zids) != len(set(zids)):
        raise AlarmStateBoundsError("duplicate-device-identifiers")


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _tri_from_status(value: object, *, active: set[str], inactive: set[str]) -> TriState:
    if isinstance(value, str):
        if value in active:
            return TriState.ACTIVE
        if value in inactive:
            return TriState.INACTIVE
    return TriState.UNKNOWN


def _nested_tri(
    data: Mapping[str, Any],
    key: str,
    nested_key: str,
    *,
    active: set[object],
    inactive: set[object],
) -> TriState:
    nested = _mapping(data.get(key))
    if nested is None or nested_key not in nested:
        return TriState.UNKNOWN
    value = nested[nested_key]
    if any(value == candidate for candidate in active):
        return TriState.ACTIVE
    if any(value == candidate for candidate in inactive):
        return TriState.INACTIVE
    return TriState.UNKNOWN


def _combined_tri(*states: TriState) -> TriState:
    if TriState.ACTIVE in states:
        return TriState.ACTIVE
    if TriState.INACTIVE in states:
        return TriState.INACTIVE
    return TriState.UNKNOWN


def _component_alarm_tri(data: Mapping[str, Any], component_name: str) -> TriState:
    components = _mapping(data.get("components"))
    component = _mapping(components.get(component_name)) if components is not None else None
    return _tri_from_status(
        component.get("alarmStatus") if component is not None else None,
        active={"active"},
        inactive={"inactive", "clear"},
    )


def _life_safety_tri(
    data: Mapping[str, Any],
    raw_type: str,
    *,
    field: str,
    direct_type: str,
) -> TriState:
    states = [
        _nested_tri(
            data,
            field,
            "alarmStatus",
            active={"active"},
            inactive={"inactive", "clear"},
        ),
        _component_alarm_tri(data, f"alarm.{field}"),
    ]
    if raw_type == direct_type:
        states.append(
            _tri_from_status(
                data.get("alarmStatus"),
                active={"active"},
                inactive={"inactive", "clear"},
            )
        )
    return _combined_tri(*states)


def _mode(value: object) -> AlarmMode:
    if not isinstance(value, str):
        return AlarmMode.UNKNOWN
    return {
        "none": AlarmMode.DISARMED,
        "some": AlarmMode.HOME,
        "all": AlarmMode.AWAY,
    }.get(value, AlarmMode.UNKNOWN)


def _phase(data: Mapping[str, Any]) -> AlarmPhase:
    alarm_info = _mapping(data.get("alarmInfo"))
    has_alarm_info = "alarmInfo" in data
    alarm_state = alarm_info.get("state") if alarm_info is not None else None
    if data.get("alarmStatus") == "active" or (
        isinstance(alarm_state, str) and alarm_state in _ALARMING_STATES
    ):
        return AlarmPhase.ALARMING
    if alarm_state == "entry-delay":
        return AlarmPhase.ENTRY_DELAY
    if "alarmStatus" in data or (has_alarm_info and alarm_info is None):
        return AlarmPhase.UNKNOWN
    if alarm_info is not None and alarm_state not in _SAFE_ALARM_STATES:
        return AlarmPhase.UNKNOWN
    deadline = data.get("transitionDelayEndTimestamp")
    if isinstance(deadline, int | float) and not isinstance(deadline, bool) and deadline > 0:
        return AlarmPhase.EXIT_DELAY
    if data.get("deviceType") == "security-panel":
        return AlarmPhase.IDLE
    return AlarmPhase.UNKNOWN


def _deadline(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    result = float(value)
    return result / 1000 if result >= 100_000_000_000 else result


def _signal(data: Mapping[str, Any]) -> AlarmSignal:
    alarm_info = _mapping(data.get("alarmInfo"))
    has_alarm_info = "alarmInfo" in data
    state = alarm_info.get("state") if alarm_info is not None else None
    if isinstance(state, str):
        if state in {
            "burglar-alarm",
            "user-verified-burglar-alarm",
            "burglar-accelerated-alarm",
        }:
            return AlarmSignal.BURGLAR
        if state in {"fire-alarm", "fire-accelerated-alarm"}:
            return AlarmSignal.FIRE
        if state == "co-alarm":
            return AlarmSignal.CARBON_MONOXIDE
        if state == "user-verified-co-or-fire-alarm":
            return AlarmSignal.FIRE_OR_CARBON_MONOXIDE
        if state == "panic":
            return AlarmSignal.PANIC
    if data.get("alarmStatus") == "active":
        return AlarmSignal.UNKNOWN
    if state == "entry-delay":
        return (
            AlarmSignal.NONE if data.get("deviceType") == "security-panel" else AlarmSignal.UNKNOWN
        )
    if "alarmStatus" in data or (has_alarm_info and alarm_info is None):
        return AlarmSignal.UNKNOWN
    if isinstance(state, str):
        if state not in _SAFE_ALARM_STATES:
            return AlarmSignal.UNKNOWN
    elif alarm_info is not None:
        return AlarmSignal.UNKNOWN
    if data.get("deviceType") == "security-panel":
        return AlarmSignal.NONE
    return AlarmSignal.UNKNOWN


def _life_safety_signal(
    capabilities: frozenset[AlarmCapability],
    smoke: TriState,
    carbon_monoxide: TriState,
) -> AlarmSignal:
    supports_smoke = AlarmCapability.SMOKE in capabilities
    supports_co = AlarmCapability.CARBON_MONOXIDE in capabilities
    if not supports_smoke and not supports_co:
        return AlarmSignal.UNKNOWN
    if smoke is TriState.ACTIVE and carbon_monoxide is TriState.ACTIVE:
        return AlarmSignal.FIRE_OR_CARBON_MONOXIDE
    if smoke is TriState.ACTIVE:
        return AlarmSignal.FIRE
    if carbon_monoxide is TriState.ACTIVE:
        return AlarmSignal.CARBON_MONOXIDE
    supported_states = (
        *((smoke,) if supports_smoke else ()),
        *((carbon_monoxide,) if supports_co else ()),
    )
    if supported_states and all(state is TriState.INACTIVE for state in supported_states):
        return AlarmSignal.NONE
    return AlarmSignal.UNKNOWN


def _battery_level(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
        return None
    return round(value)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _category(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _kind(
    data: Mapping[str, Any],
    capabilities: frozenset[AlarmCapability],
) -> AlarmDeviceKind:
    raw_type = data.get("deviceType")
    if isinstance(raw_type, str):
        if raw_type in _SWITCH_TYPES:
            return AlarmDeviceKind.SWITCH
        if raw_type in _KIND_BY_TYPE:
            return _KIND_BY_TYPE[raw_type]
        has_smoke = AlarmCapability.SMOKE in capabilities
        has_co = AlarmCapability.CARBON_MONOXIDE in capabilities
        if has_smoke and has_co:
            return AlarmDeviceKind.SMOKE_CO_ALARM
        if has_smoke:
            return AlarmDeviceKind.SMOKE_ALARM
        if has_co:
            return AlarmDeviceKind.CO_ALARM
        if raw_type.startswith("sensor."):
            return AlarmDeviceKind.GENERIC_SENSOR
        if raw_type.startswith(("hub.", "bridge.", "adapter.")):
            return AlarmDeviceKind.HUB
    if data.get("categoryId") == 10:
        return AlarmDeviceKind.LOCK
    return AlarmDeviceKind.UNKNOWN


def _capabilities(data: Mapping[str, Any], raw_type: str) -> frozenset[AlarmCapability]:
    capabilities = set(_TYPE_CAPABILITIES.get(raw_type, ()))
    field_capabilities = {
        "batteryLevel": AlarmCapability.BATTERY,
        "batteryStatus": AlarmCapability.BATTERY,
        "acStatus": AlarmCapability.AC_POWER,
        "tamperStatus": AlarmCapability.TAMPER,
        "bypassed": AlarmCapability.BYPASS,
        "bypassStatus": AlarmCapability.BYPASS,
        "faulted": AlarmCapability.FAULT,
        "motionStatus": AlarmCapability.MOTION,
        "flood": AlarmCapability.FLOOD,
        "freeze": AlarmCapability.FREEZE,
        "smoke": AlarmCapability.SMOKE,
        "co": AlarmCapability.CARBON_MONOXIDE,
        "celsius": AlarmCapability.TEMPERATURE,
        "locked": AlarmCapability.LOCK,
        "on": AlarmCapability.SWITCH,
        "valveState": AlarmCapability.VALVE,
        "siren": AlarmCapability.SIREN,
        "mode": AlarmCapability.MODE,
    }
    for key, capability in field_capabilities.items():
        if key in data:
            capabilities.add(capability)
    components = _mapping(data.get("components"))
    if components is not None:
        if _mapping(components.get("alarm.smoke")) is not None:
            capabilities.add(AlarmCapability.SMOKE)
        if _mapping(components.get("alarm.co")) is not None:
            capabilities.add(AlarmCapability.CARBON_MONOXIDE)
    return frozenset(capabilities)


def _faulted_device_ids(
    data: Mapping[str, Any],
) -> tuple[tuple[str, ...] | None, bool]:
    alarm_info_value = data.get("alarmInfo")
    alarm_info = _mapping(alarm_info_value)
    if alarm_info is None:
        return (None, "alarmInfo" not in data)
    if "faultedDevices" not in alarm_info:
        return None, True
    value = alarm_info.get("faultedDevices")
    if not isinstance(value, list | tuple) or len(value) > 256:
        return None, False
    identifiers: list[str] = []
    for identifier in value:
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or len(identifier) > 512
            or any(ord(character) < 32 for character in identifier)
            or identifier in identifiers
        ):
            return None, False
        identifiers.append(identifier)
    return tuple(sorted(identifiers)), True


def _bypassed(data: Mapping[str, Any]) -> TriState:
    if "bypassed" in data:
        return TriState.from_bool(data["bypassed"])
    return _tri_from_status(
        data.get("bypassStatus"),
        active={"bypassed", "active"},
        inactive={"not-bypassed", "inactive", "none"},
    )


def _lock_state(value: object) -> AlarmLockState:
    if value is True:
        return AlarmLockState.LOCKED
    if value is False:
        return AlarmLockState.UNLOCKED
    if not isinstance(value, str):
        return AlarmLockState.UNKNOWN
    return {
        "locked": AlarmLockState.LOCKED,
        "unlocked": AlarmLockState.UNLOCKED,
        "jammed": AlarmLockState.JAMMED,
    }.get(value, AlarmLockState.UNKNOWN)


def _normalize_device(
    location_id: str,
    asset: _AssetState,
    zid: str,
    state: _DeviceState,
) -> AlarmDeviceSnapshot:
    data = state.data
    raw_type_value = data.get("deviceType")
    raw_type = _bounded_text(raw_type_value, 128)
    capabilities = _capabilities(data, raw_type)
    kind = _kind(data, capabilities)
    faulted = TriState.from_bool(data.get("faulted"))
    motion = _tri_from_status(
        data.get("motionStatus"),
        active={"faulted", "active", "motion"},
        inactive={"clear", "inactive"},
    )
    if motion is TriState.UNKNOWN and kind is AlarmDeviceKind.MOTION_SENSOR:
        motion = faulted

    battery_status = data.get("batteryStatus")
    battery_low = _tri_from_status(
        battery_status,
        active={"low"},
        inactive={"full", "charged", "ok", "charging"},
    )
    name = _bounded_text(data.get("name"), 256)
    room = _bounded_text(data.get("roomName"), 256) or None
    parent_zid = _bounded_text(data.get("parentZid"), 512) or None
    phase = _phase(data)
    smoke = _life_safety_tri(
        data,
        raw_type,
        field="smoke",
        direct_type="alarm.smoke",
    )
    carbon_monoxide = _life_safety_tri(
        data,
        raw_type,
        field="co",
        direct_type="alarm.co",
    )
    faulted_device_ids, faulted_devices_valid = _faulted_device_ids(data)
    signal = (
        _signal(data)
        if kind is AlarmDeviceKind.SECURITY_PANEL
        else _life_safety_signal(capabilities, smoke, carbon_monoxide)
    )
    return AlarmDeviceSnapshot(
        location_id=location_id,
        asset_id=asset.asset_id,
        zid=zid,
        name=name,
        room=room,
        kind=kind,
        raw_type=raw_type,
        category_id=_category(data.get("categoryId")),
        parent_zid=parent_zid,
        capabilities=capabilities,
        connection=asset.connection,
        stale=asset.stale,
        battery_level=_battery_level(data.get("batteryLevel")),
        battery_low=battery_low,
        tamper=_tri_from_status(
            data.get("tamperStatus"),
            active={"tamper"},
            inactive={"ok"},
        ),
        bypassed=_bypassed(data),
        faulted=faulted,
        contact=(
            faulted
            if kind in {AlarmDeviceKind.CONTACT_SENSOR, AlarmDeviceKind.RETROFIT_ZONE}
            else TriState.UNKNOWN
        ),
        motion=motion,
        glass_break=faulted if kind is AlarmDeviceKind.GLASS_BREAK_SENSOR else TriState.UNKNOWN,
        tilt=faulted if kind is AlarmDeviceKind.TILT_SENSOR else TriState.UNKNOWN,
        flood=_nested_tri(
            data,
            "flood",
            "faulted",
            active={True},
            inactive={False},
        ),
        freeze=_nested_tri(
            data,
            "freeze",
            "faulted",
            active={True},
            inactive={False},
        ),
        smoke=smoke,
        carbon_monoxide=carbon_monoxide,
        ac_power=_tri_from_status(
            data.get("acStatus"),
            active={"ok"},
            inactive={"error"},
        ),
        switch_on=TriState.from_bool(data.get("on")),
        valve_open=_tri_from_status(
            data.get("valveState"),
            active={"open"},
            inactive={"closed"},
        ),
        lock_state=_lock_state(data.get("locked")),
        temperature_celsius=_number(data.get("celsius")),
        mode=_mode(data.get("mode")),
        phase=phase,
        signal=signal,
        faulted_device_ids=faulted_device_ids,
        faulted_devices_valid=faulted_devices_valid,
        transition_deadline=_deadline(data.get("transitionDelayEndTimestamp")),
        revision=state.revision,
        epoch=state.epoch,
        updated_at=state.updated_at,
    )


class AlarmStateStore:
    """Apply ordered messages and publish immutable, revisioned snapshots."""

    def __init__(
        self,
        generation: int = 0,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._generation = generation
        self._revision = 0
        self._status = AlarmServiceStatus.STOPPED
        self._error_code: str | None = None
        self._locations: dict[str, _LocationState] = {}
        self._snapshot = empty_alarm_snapshot(generation)

    @property
    def snapshot(self) -> AlarmAccountSnapshot:
        return self._snapshot

    def set_service_status(
        self,
        status: AlarmServiceStatus,
        *,
        error_code: str | None = None,
    ) -> AlarmAccountSnapshot:
        if not isinstance(status, AlarmServiceStatus):
            raise ValueError("status must be an AlarmServiceStatus")
        if error_code is not None and (not isinstance(error_code, str) or len(error_code) > 128):
            raise ValueError("error_code must be a bounded string or None")
        if self._status is status and self._error_code == error_code:
            return self._snapshot
        self._status = status
        self._error_code = error_code
        self._publish(self._clock())
        return self._snapshot

    def register_location(
        self,
        location_id: str,
        name: str = "",
        expected_assets: Iterable[ExpectedAsset] = (),
        *,
        write_authorization: AlarmWriteAuthorization = AlarmWriteAuthorization.UNKNOWN,
    ) -> AlarmAccountSnapshot:
        _validate_store_id(location_id, "location_id")
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        if not isinstance(write_authorization, AlarmWriteAuthorization):
            raise ValueError("write_authorization must be an AlarmWriteAuthorization")
        name = _bounded_text(name, 256)
        normalized_assets = _normalize_expected_assets(expected_assets)
        now = self._clock()
        location = self._locations.get(location_id)
        if location is None:
            location = _LocationState(
                location_id=location_id,
                name=name,
                write_authorization=write_authorization,
                updated_at=now,
            )
            self._locations[location_id] = location
            new_location = True
            changed = True
        else:
            new_location = False
            changed = (
                location.name != name or location.write_authorization is not write_authorization
            )
            location.name = name
            location.write_authorization = write_authorization

        expected_ids = set(normalized_assets)
        removed_ids = set(location.assets) - expected_ids
        if removed_ids:
            changed = True
            for asset_id in removed_ids:
                del location.assets[asset_id]
        for asset_id, kind in normalized_assets.items():
            asset = location.assets.get(asset_id)
            if asset is None:
                location.assets[asset_id] = _AssetState(
                    asset_id=asset_id,
                    kind=kind,
                    epoch=location.epoch,
                    updated_at=now,
                )
                changed = True
            elif asset.kind != kind:
                asset.kind = kind
                asset.revision += 1
                asset.updated_at = now
                changed = True

        if changed:
            if not new_location:
                location.revision += 1
            location.updated_at = now
            self._publish(now)
        return self._snapshot

    def remove_location(self, location_id: str) -> AlarmAccountSnapshot:
        """Forget a removed or permission-lost location and all household state."""

        _validate_store_id(location_id, "location_id")
        if self._locations.pop(location_id, None) is not None:
            self._publish(self._clock())
        return self._snapshot

    def begin_epoch(self, location_id: str) -> int:
        _validate_store_id(location_id, "location_id")
        location = self._locations.get(location_id)
        if location is None:
            self.register_location(location_id)
            location = self._locations[location_id]
        now = self._clock()
        location.epoch += 1
        location.revision += 1
        location.updated_at = now
        for asset in location.assets.values():
            asset.epoch = location.epoch
            asset.stale = True
            asset.inventory = AlarmInventoryStatus.STALE
            asset.revision += 1
            asset.updated_at = now
            self._touch_devices(asset, now, epoch=location.epoch)
        self._publish(now)
        return location.epoch

    def apply_message(
        self,
        location_id: str,
        message: ClapMessage,
        epoch: int,
    ) -> AlarmAccountSnapshot:
        _validate_store_id(location_id, "location_id")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("epoch must be a non-negative integer")
        location = self._locations.get(location_id)
        if location is None or epoch != location.epoch or isinstance(message, UnknownMessage):
            return self._snapshot

        now = self._clock()
        changed = False
        if isinstance(message, DeviceInfoDocList):
            changed = self._replace_asset_devices(location, message.asset_id, message.devices, now)
        elif isinstance(message, DeviceInfoDocUpdate):
            changed = self._merge_asset_devices(location, message.asset_id, message.devices, now)
        elif isinstance(message, SessionInfo):
            changed = self._apply_sessions(location, message, now)
        elif isinstance(message, HubDisconnection):
            changed = self._apply_disconnection(location, message.asset_id, now)

        if changed:
            location.revision += 1
            location.updated_at = now
            self._publish(now)
        return self._snapshot

    def mark_location_stale(
        self,
        location_id: str,
        epoch: int,
        *,
        connection: AlarmConnectionStatus = AlarmConnectionStatus.OFFLINE,
    ) -> AlarmAccountSnapshot:
        location = self._locations.get(location_id)
        if location is None or epoch != location.epoch:
            return self._snapshot
        now = self._clock()
        changed = False
        for asset in location.assets.values():
            changed |= self._mark_asset(asset, now, connection)
        if changed:
            location.revision += 1
            location.updated_at = now
            self._publish(now)
        return self._snapshot

    def mark_asset_stale(
        self,
        location_id: str,
        asset_id: str,
        epoch: int,
        *,
        connection: AlarmConnectionStatus = AlarmConnectionStatus.OFFLINE,
    ) -> AlarmAccountSnapshot:
        location = self._locations.get(location_id)
        if location is None or epoch != location.epoch:
            return self._snapshot
        asset = location.assets.get(asset_id)
        if asset is None:
            return self._snapshot
        now = self._clock()
        if self._mark_asset(asset, now, connection):
            location.revision += 1
            location.updated_at = now
            self._publish(now)
        return self._snapshot

    def _ensure_asset(self, location: _LocationState, asset_id: str, now: float) -> _AssetState:
        _validate_store_id(asset_id, "asset_id")
        asset = location.assets.get(asset_id)
        if asset is None:
            asset = _AssetState(asset_id=asset_id, epoch=location.epoch, updated_at=now)
            location.assets[asset_id] = asset
        return asset

    def _replace_asset_devices(
        self,
        location: _LocationState,
        asset_id: str,
        documents: tuple[DeviceDocument, ...],
        now: float,
    ) -> bool:
        _validate_device_batch(documents)
        _validate_store_id(asset_id, "asset_id")
        existing_asset = location.assets.get(asset_id)
        asset = existing_asset or _AssetState(
            asset_id=asset_id,
            epoch=location.epoch,
            updated_at=now,
        )
        target_connection = asset.connection
        target_stale = target_connection is not AlarmConnectionStatus.ONLINE
        target_inventory = (
            AlarmInventoryStatus.STALE if target_stale else AlarmInventoryStatus.COMPLETE
        )
        device_metadata_changed = asset.stale is not target_stale or asset.epoch != location.epoch
        replacement: dict[str, _DeviceState] = {}
        device_changed = False
        for document in documents:
            data = _retained_device_patch(document)
            previous = asset.devices.get(document.zid)
            unchanged = (
                previous is not None and previous.data == data and not device_metadata_changed
            )
            if unchanged:
                replacement[document.zid] = previous
            else:
                replacement[document.zid] = _DeviceState(
                    data=data,
                    revision=(previous.revision + 1 if previous else 1),
                    epoch=location.epoch,
                    updated_at=now,
                )
                device_changed = True
        if set(replacement) != set(asset.devices):
            device_changed = True
        metadata_changed = (
            asset.inventory is not target_inventory
            or asset.stale is not target_stale
            or asset.epoch != location.epoch
        )
        if not device_changed and not metadata_changed:
            return False
        _validate_retained_candidate(replacement)
        if existing_asset is None:
            location.assets[asset_id] = asset
        asset.devices = replacement
        asset.inventory = target_inventory
        asset.stale = target_stale
        asset.epoch = location.epoch
        asset.revision += 1
        asset.updated_at = now
        return True

    def _merge_asset_devices(
        self,
        location: _LocationState,
        asset_id: str,
        documents: tuple[DeviceDocument, ...],
        now: float,
    ) -> bool:
        if not documents:
            return False
        _validate_device_batch(documents)
        _validate_store_id(asset_id, "asset_id")
        existing_asset = location.assets.get(asset_id)
        asset = existing_asset or _AssetState(
            asset_id=asset_id,
            epoch=location.epoch,
            updated_at=now,
        )
        replacement = dict(asset.devices)
        changed = False
        for document in documents:
            patch = _retained_device_patch(document)
            previous = asset.devices.get(document.zid)
            merged = dict(previous.data) if previous else {}
            merged.update(patch)
            if previous is None or previous.data != merged or previous.epoch != location.epoch:
                replacement[document.zid] = _DeviceState(
                    data=merged,
                    revision=(previous.revision + 1 if previous else 1),
                    epoch=location.epoch,
                    updated_at=now,
                )
                changed = True

        target_stale = asset.stale or asset.connection is not AlarmConnectionStatus.ONLINE
        target_inventory = AlarmInventoryStatus.STALE if target_stale else asset.inventory
        if not target_stale and target_inventory is not AlarmInventoryStatus.COMPLETE:
            target_inventory = AlarmInventoryStatus.PARTIAL
        metadata_changed = (
            asset.inventory is not target_inventory
            or asset.stale is not target_stale
            or asset.epoch != location.epoch
        )
        if not changed and not metadata_changed:
            return False
        _validate_retained_candidate(replacement)
        if existing_asset is None:
            location.assets[asset_id] = asset
        asset.devices = replacement
        asset.inventory = target_inventory
        asset.stale = target_stale
        asset.epoch = location.epoch
        asset.revision += 1
        asset.updated_at = now
        return True

    def _apply_sessions(self, location: _LocationState, message: SessionInfo, now: float) -> bool:
        changed = False
        for session in message.sessions:
            existing = location.assets.get(session.asset_id)
            asset = self._ensure_asset(location, session.asset_id, now)
            kind = _bounded_text(session.kind, 128) or asset.kind
            metadata_changed = asset.connection is not session.connection
            asset_changed = existing is None or metadata_changed or asset.kind != kind
            asset.connection = session.connection
            asset.kind = kind
            if session.connection in {
                AlarmConnectionStatus.CELLULAR_BACKUP,
                AlarmConnectionStatus.OFFLINE,
                AlarmConnectionStatus.UNKNOWN,
            }:
                if not asset.stale or asset.inventory is not AlarmInventoryStatus.STALE:
                    asset_changed = True
                asset.stale = True
                asset.inventory = AlarmInventoryStatus.STALE
            if asset_changed:
                asset.revision += 1
                asset.updated_at = now
                if metadata_changed:
                    self._touch_devices(asset, now)
                changed = True
        return changed

    def _apply_disconnection(
        self,
        location: _LocationState,
        asset_id: str | None,
        now: float,
    ) -> bool:
        assets = (
            (location.assets[asset_id],)
            if asset_id is not None and asset_id in location.assets
            else tuple(location.assets.values())
        )
        changed = False
        for asset in assets:
            changed |= self._mark_asset(asset, now, AlarmConnectionStatus.OFFLINE)
        return changed

    @staticmethod
    def _mark_asset(
        asset: _AssetState,
        now: float,
        connection: AlarmConnectionStatus,
    ) -> bool:
        if (
            asset.stale
            and asset.inventory is AlarmInventoryStatus.STALE
            and asset.connection is connection
        ):
            return False
        asset.stale = True
        asset.inventory = AlarmInventoryStatus.STALE
        asset.connection = connection
        asset.revision += 1
        asset.updated_at = now
        AlarmStateStore._touch_devices(asset, now)
        return True

    @staticmethod
    def _touch_devices(asset: _AssetState, now: float, *, epoch: int | None = None) -> None:
        for device in asset.devices.values():
            device.revision += 1
            device.updated_at = now
            if epoch is not None:
                device.epoch = epoch

    def _publish(self, now: float) -> None:
        self._revision += 1
        locations = tuple(
            self._location_snapshot(location)
            for location in sorted(
                self._locations.values(),
                key=lambda item: item.location_id,
            )
        )
        self._snapshot = AlarmAccountSnapshot(
            generation=self._generation,
            revision=self._revision,
            status=self._status,
            locations=locations,
            updated_at=now,
            error_code=self._error_code,
        )

    def _location_snapshot(self, location: _LocationState) -> AlarmLocationSnapshot:
        assets = tuple(
            self._asset_snapshot(location.location_id, asset)
            for asset in sorted(location.assets.values(), key=lambda item: item.asset_id)
        )
        inventory = _location_inventory(assets)
        panels = tuple(
            device
            for asset in assets
            for device in asset.devices
            if device.kind is AlarmDeviceKind.SECURITY_PANEL
        )
        panel = panels[0] if len(panels) == 1 else None
        panel_asset = (
            next(asset for asset in assets if asset.asset_id == panel.asset_id)
            if panel is not None
            else None
        )
        connection = (
            panel_asset.connection if panel_asset is not None else _location_connection(assets)
        )
        panel_is_fresh = (
            panel is not None
            and panel_asset is not None
            and not panel.stale
            and not panel_asset.stale
            and panel_asset.connection is AlarmConnectionStatus.ONLINE
            and panel_asset.inventory is AlarmInventoryStatus.COMPLETE
        )
        mode = panel.mode if panel_is_fresh else AlarmMode.UNKNOWN
        phase = panel.phase if panel_is_fresh else AlarmPhase.UNKNOWN
        signal = panel.signal if panel_is_fresh else AlarmSignal.UNKNOWN
        triggered = (
            TriState.UNKNOWN
            if panel is None or phase is AlarmPhase.UNKNOWN
            else TriState.ACTIVE
            if phase is AlarmPhase.ALARMING
            else TriState.INACTIVE
        )
        can_set_mode, reason = _command_availability(
            assets,
            location.write_authorization,
            panel,
            multiple_panels=len(panels) > 1,
        )
        return AlarmLocationSnapshot(
            location_id=location.location_id,
            name=location.name,
            write_authorization=location.write_authorization,
            connection=connection,
            inventory=inventory,
            mode=mode,
            phase=phase,
            signal=signal,
            triggered=triggered,
            transition_deadline=panel.transition_deadline if panel_is_fresh else None,
            assets=assets,
            can_set_mode=can_set_mode,
            command_unavailable_reason=reason,
            revision=location.revision,
            epoch=location.epoch,
            updated_at=location.updated_at,
        )

    @staticmethod
    def _asset_snapshot(location_id: str, asset: _AssetState) -> AlarmAssetSnapshot:
        devices = tuple(
            _normalize_device(location_id, asset, zid, state)
            for zid, state in sorted(asset.devices.items())
        )
        return AlarmAssetSnapshot(
            location_id=location_id,
            asset_id=asset.asset_id,
            kind=asset.kind,
            connection=asset.connection,
            inventory=asset.inventory,
            stale=asset.stale,
            devices=devices,
            revision=asset.revision,
            epoch=asset.epoch,
            updated_at=asset.updated_at,
        )


def _validate_store_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} is invalid")


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(character if ord(character) >= 32 else " " for character in value)
    return cleaned[:limit]


def _normalize_expected_assets(assets: Iterable[ExpectedAsset]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in assets:
        if isinstance(item, str):
            asset_id, kind = item, ""
        elif isinstance(item, tuple) and len(item) == 2:
            asset_id, kind = item
            if not isinstance(kind, str):
                raise ValueError("asset kind must be a string")
        else:
            raise ValueError("expected assets must be ids or (id, kind) tuples")
        _validate_store_id(asset_id, "asset_id")
        kind = _bounded_text(kind, 128)
        if asset_id in result:
            raise ValueError("expected asset ids must be unique")
        result[asset_id] = kind
    return result


def _location_connection(assets: tuple[AlarmAssetSnapshot, ...]) -> AlarmConnectionStatus:
    connections = {asset.connection for asset in assets}
    if AlarmConnectionStatus.ONLINE in connections:
        return AlarmConnectionStatus.ONLINE
    if AlarmConnectionStatus.CELLULAR_BACKUP in connections:
        return AlarmConnectionStatus.CELLULAR_BACKUP
    if assets and connections == {AlarmConnectionStatus.OFFLINE}:
        return AlarmConnectionStatus.OFFLINE
    return AlarmConnectionStatus.UNKNOWN


def _location_inventory(assets: tuple[AlarmAssetSnapshot, ...]) -> AlarmInventoryStatus:
    if not assets:
        return AlarmInventoryStatus.EMPTY
    if all(asset.stale for asset in assets):
        return AlarmInventoryStatus.STALE
    if all(asset.inventory is AlarmInventoryStatus.COMPLETE for asset in assets):
        return AlarmInventoryStatus.COMPLETE
    if any(asset.stale for asset in assets):
        return AlarmInventoryStatus.PARTIAL
    if any(
        asset.devices
        or asset.inventory in {AlarmInventoryStatus.PARTIAL, AlarmInventoryStatus.COMPLETE}
        for asset in assets
    ):
        return AlarmInventoryStatus.PARTIAL
    return AlarmInventoryStatus.EMPTY


def _command_availability(
    assets: tuple[AlarmAssetSnapshot, ...],
    write_authorization: AlarmWriteAuthorization,
    panel: AlarmDeviceSnapshot | None,
    *,
    multiple_panels: bool = False,
) -> tuple[bool, str | None]:
    if multiple_panels:
        return False, "multiple-panels"
    if panel is None:
        return False, "panel-unavailable"
    if write_authorization is AlarmWriteAuthorization.DENIED:
        return False, "authorization-denied"
    if write_authorization is not AlarmWriteAuthorization.ALLOWED:
        return False, "authorization-unknown"
    panel_asset = next(asset for asset in assets if asset.asset_id == panel.asset_id)
    if panel_asset.connection is AlarmConnectionStatus.CELLULAR_BACKUP:
        return False, "cellular-backup"
    if panel_asset.connection is not AlarmConnectionStatus.ONLINE:
        return False, "panel-offline"
    if panel_asset.stale:
        return False, "panel-stale"
    if panel_asset.inventory is not AlarmInventoryStatus.COMPLETE:
        return False, "panel-inventory-incomplete"
    if any(
        not device.stale
        and (device.smoke is TriState.ACTIVE or device.carbon_monoxide is TriState.ACTIVE)
        for device in panel_asset.devices
    ):
        return False, "life-safety-active"
    if panel.mode is AlarmMode.UNKNOWN:
        return False, "mode-unknown"
    if panel.phase is AlarmPhase.ALARMING:
        return False, "alarm-active"
    if panel.phase is AlarmPhase.UNKNOWN or panel.signal is AlarmSignal.UNKNOWN:
        return False, "panel-state-unknown"
    if panel.signal is not AlarmSignal.NONE:
        return False, "alarm-active"
    if not panel.faulted_devices_valid:
        return False, "faulted-device-list-invalid"
    return True, None
