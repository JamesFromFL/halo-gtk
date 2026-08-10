"""Camera power status helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from halo_gtk.theme_icons import hardwired_power_icon_path, icon_path


@dataclass(frozen=True)
class PowerStatus:
    icon_path: Path
    tooltip: str


def camera_power_status(device: Any) -> PowerStatus:
    """Return the power icon and tooltip for a Ring camera device."""
    hardwired = _is_hardwired(device)
    external_power = _has_external_power(device)
    battery_present = _battery_present(device)

    if battery_present is False and (hardwired or external_power):
        return PowerStatus(hardwired_power_icon_path(), "Plugged-In / Hardwired")

    if hardwired and battery_present is not True:
        return PowerStatus(hardwired_power_icon_path(), "Plugged-In / Hardwired")

    battery = _battery_percent(device)
    if external_power and not hardwired:
        if battery is not None:
            return PowerStatus(
                _power_icon_path(_battery_icon_stem(battery, charging=True)),
                f"Battery charging: {battery}%",
            )
        return PowerStatus(_power_icon_path("power-battery-unknown"), "Battery: Unknown")

    if battery_present is True:
        if battery is None:
            return PowerStatus(_power_icon_path("power-battery-unknown"), "Battery: Unknown")
        return PowerStatus(
            _power_icon_path(_battery_icon_stem(battery)),
            f"Battery: {battery}%",
        )

    if battery is not None and not hardwired:
        return PowerStatus(
            _power_icon_path(_battery_icon_stem(battery)),
            f"Battery: {battery}%",
        )

    if _has_accessible_metadata(device):
        return PowerStatus(hardwired_power_icon_path(), "Plugged-In / Hardwired")

    return PowerStatus(_power_icon_path("power-unknown"), "Unknown / Error")


def _is_hardwired(device: Any) -> bool:
    attrs = _device_attrs(device) or {}
    settings = _dict_value(attrs, "settings")

    if str(settings.get("power_mode") or "").lower() in {"wired", "hardwired", "plugged_in"}:
        return True

    capability_checker = getattr(device, "has_capability", None)
    if callable(capability_checker):
        try:
            return capability_checker("battery") is False
        except Exception:
            return False
    return False


def _has_external_power(device: Any) -> bool:
    attrs = _device_attrs(device) or {}
    health = _dict_value(attrs, "health")
    return (
        _truthy(attrs.get("external_connection"))
        or _truthy(health.get("external_connection"))
        or _truthy(health.get("ac_power"))
    )


def _battery_percent(device: Any) -> int | None:
    try:
        value = getattr(device, "battery_life", None)
    except Exception:
        return None
    if value is None:
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def _battery_present(device: Any) -> bool | None:
    attrs = _device_attrs(device)
    if not isinstance(attrs, dict):
        return None

    for mapping in (attrs, _dict_value(attrs, "health")):
        if "battery_present" in mapping:
            return _truthy(mapping.get("battery_present"))

    batteries = attrs.get("batteries")
    if isinstance(batteries, list):
        present_values = []
        for battery in batteries:
            if isinstance(battery, dict) and "battery_present" in battery:
                present_values.append(_truthy(battery.get("battery_present")))
        if present_values:
            return any(present_values)

    return None


def _has_accessible_metadata(device: Any) -> bool:
    attrs = _device_attrs(device)
    if isinstance(attrs, dict):
        return True
    return any(hasattr(device, attr) for attr in ("family", "name", "id", "device_api_id"))


def _device_attrs(device: Any) -> dict[str, Any] | None:
    try:
        attrs = getattr(device, "_attrs", None)
    except Exception:
        return None
    return attrs if isinstance(attrs, dict) else None


def _dict_value(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() in {"true", "1", "yes"}


def _battery_icon_stem(percent: int, *, charging: bool = False) -> str:
    prefix = "power-charging-battery" if charging else "power-battery"
    if percent >= 76:
        return f"{prefix}-100-76"
    if percent >= 51:
        return f"{prefix}-75-51"
    if percent >= 26:
        return f"{prefix}-50-26"
    if percent >= 6:
        return f"{prefix}-25-6"
    return f"{prefix}-5-0"


def _power_icon_path(stem: str) -> Path:
    return icon_path("power", f"{stem}.png")
