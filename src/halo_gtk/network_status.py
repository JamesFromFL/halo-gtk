"""Camera network status helpers."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NetworkStatus:
    icon_name: str
    tooltip: str


def camera_network_status(device: Any) -> NetworkStatus:
    """Return the network icon and tooltip for a Ring camera device."""
    connection_kind = _connection_kind(device)
    percent = _connection_percent(device)
    label = _label_for_percent(percent)

    if connection_kind == "ethernet":
        return NetworkStatus(
            "network-wired-symbolic",
            _tooltip("Ethernet", percent),
        )

    return NetworkStatus(
        _wireless_icon_name(percent),
        _tooltip(label, percent),
    )


def _connection_kind(device: Any) -> str:
    attrs = _all_metadata(device)
    for key in (
        "connection_type",
        "network_connection_type",
        "network_type",
        "connection",
        "medium",
    ):
        value = _find_value(attrs, key)
        if value is None:
            continue
        text = str(value).lower()
        if any(token in text for token in ("ethernet", "wired", "lan")):
            return "ethernet"
        if any(token in text for token in ("wifi", "wi-fi", "wireless", "wlan")):
            return "wifi"
    return "wifi"


def _connection_percent(device: Any) -> int:
    attrs = _all_metadata(device)
    for key in (
        "wifi_signal_percentage",
        "wifi_signal_percent",
        "signal_percentage",
        "signal_percent",
        "connection_percentage",
        "connection_percent",
        "latest_signal_strength",
        "latest_signal_strength_percentage",
        "rssi_percentage",
        "rssi_percent",
        "wifi_rssi_percentage",
        "wifi_rssi_percent",
    ):
        percent = _percent_from_value(_find_value(attrs, key))
        if percent is not None:
            return percent

    for key in ("rssi", "wifi_rssi", "latest_rssi", "latest_wifi_rssi"):
        percent = _percent_from_rssi(_number_from_value(_find_value(attrs, key)))
        if percent is not None:
            return percent

    category = _category_value(attrs)
    if category is not None:
        return _percent_for_category(category)

    status = _find_value(attrs, "connection_status")
    if status is not None and str(status).lower() in {"offline", "disconnected", "unreachable"}:
        return 0

    return 0


def _label_for_percent(percent: int) -> str:
    if percent <= 0:
        return "Disconnected"
    if percent <= 24:
        return "Poor"
    if percent <= 49:
        return "Bad"
    if percent <= 69:
        return "Moderate"
    if percent <= 84:
        return "Good"
    return "Excellent"


def _wireless_icon_name(percent: int) -> str:
    if percent <= 0:
        return "network-offline-symbolic"
    if percent <= 24:
        return "network-wireless-signal-none-symbolic"
    if percent <= 49:
        return "network-wireless-signal-weak-symbolic"
    if percent <= 69:
        return "network-wireless-signal-ok-symbolic"
    if percent <= 84:
        return "network-wireless-signal-good-symbolic"
    return "network-wireless-signal-excellent-symbolic"


def _tooltip(label: str, percent: int) -> str:
    return f"{label}: {percent}%"


def _percent_for_category(value: str) -> int:
    normalized = value.lower().replace("_", " ").replace("-", " ")
    if any(token in normalized for token in ("offline", "disconnected", "unreachable")):
        return 0
    if "poor" in normalized:
        return 12
    if "bad" in normalized or "weak" in normalized or "low" in normalized:
        return 37
    if "moderate" in normalized or "fair" in normalized or "ok" in normalized:
        return 60
    if "good" in normalized:
        return 77
    if "excellent" in normalized or "strong" in normalized:
        return 92
    return 0


def _category_value(value: Any) -> str | None:
    for key in (
        "rssi_category",
        "wifi_signal_category",
        "signal_category",
        "connection_category",
    ):
        found = _find_value(value, key)
        if found is not None:
            return str(found)
    return None


def _percent_from_value(value: Any) -> int | None:
    number = _number_from_value(value)
    if number is None:
        return None
    if number < 0:
        return _percent_from_rssi(number)
    return max(0, min(100, round(number)))


def _percent_from_rssi(value: float | None) -> int | None:
    if value is None:
        return None
    if value >= 0:
        return max(0, min(100, round(value)))
    if value <= -100:
        return 0
    if value >= -50:
        return 100
    return round(2 * (value + 100))


def _number_from_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _all_metadata(device: Any) -> dict[str, Any]:
    attrs = _safe_dict_attr(device, "_attrs")
    health_attrs = _safe_dict_attr(device, "_health_attrs")
    metadata = dict(attrs)
    if health_attrs:
        metadata["_health_attrs"] = health_attrs
    for attr in ("connection_status", "family", "kind", "model"):
        with suppress(Exception):
            metadata[attr] = getattr(device, attr)
    return metadata


def _safe_dict_attr(device: Any, attr_name: str) -> dict[str, Any]:
    try:
        value = getattr(device, attr_name, {})
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _find_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if str(item_key).lower() == key:
                return item_value
            found = _find_value(item_value, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, key)
            if found is not None:
                return found
    return None
