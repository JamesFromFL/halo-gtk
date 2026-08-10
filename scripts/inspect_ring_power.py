#!/usr/bin/env python3
"""Inspect sanitized Ring device metadata for future health UI work."""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from halo_gtk.network_status import camera_network_status  # noqa: E402
from halo_gtk.power_status import camera_power_status  # noqa: E402
from halo_gtk.ring_client import init_client_from_cache  # noqa: E402

_POWER_KEY_PARTS = (
    "adapter",
    "battery",
    "charge",
    "charging",
    "external",
    "hardwire",
    "health",
    "plug",
    "power",
    "solar",
    "wired",
)
_SENSITIVE_KEY_PARTS = (
    "address",
    "auth",
    "authorization",
    "coordinates",
    "jwt",
    "latitude",
    "lng",
    "location",
    "longitude",
    "mac",
    "password",
    "secret",
    "ssid",
    "token",
    "url",
    "wifi_name",
)
_PUBLIC_PROPERTY_ALLOWLIST = (
    "address",
    "alerts",
    "battery_life",
    "connection_status",
    "device_api_id",
    "existing_doorbell_type",
    "existing_doorbell_type_enabled",
    "family",
    "has_subscription",
    "id",
    "kind",
    "light",
    "lights",
    "model",
    "motion_detection",
    "name",
    "subscribed",
    "subscribed_motions",
    "volume",
)
_SCALAR_TYPES = (str, int, float, bool, type(None))
_CAMERA_FAMILIES = {"doorbots", "authorized_doorbots", "stickup_cams"}
_MAX_DEPTH = 7


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect sanitized Ring device metadata for health/status feature discovery."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="include non-camera devices",
    )
    parser.add_argument(
        "--no-health",
        action="store_true",
        help="skip Ring health data queries",
    )
    parser.add_argument(
        "--power-only",
        action="store_true",
        help="print only power/battery/charging-related fields",
    )
    args = parser.parse_args()

    client = init_client_from_cache()
    if client is None or not client.is_authenticated:
        print("No saved Ring session found. Sign in with Halo first, then rerun this script.")
        return 1

    try:
        devices = client.refresh_devices(None if args.all else _CAMERA_FAMILIES)
        payload = []
        for device in devices:
            if not args.no_health:
                _update_health(client, device)
            payload.append(_device_payload(device, power_only=args.power_only))
    finally:
        client.stop()

    print(json.dumps({"device_count": len(payload), "devices": payload}, indent=2, sort_keys=True))
    return 0


def _update_health(client, device) -> None:
    updater = getattr(device, "async_update_health_data", None)
    if updater is None:
        return
    try:
        client.submit(updater()).result(timeout=20)
    except Exception as exc:
        print(f"Health update failed for {getattr(device, 'name', 'Unknown device')}: {exc}")


def _device_payload(device, *, power_only: bool) -> dict[str, Any]:
    status = camera_power_status(device)
    network_status = camera_network_status(device)
    attrs = _safe_attrs(device, "_attrs")
    health_attrs = _safe_attrs(device, "_health_attrs")
    public_properties = _public_properties(device, power_only=power_only)

    if power_only:
        raw_attrs = dict(_interesting_paths(attrs))
        raw_health_attrs = dict(_interesting_paths(health_attrs))
    else:
        raw_attrs = _sanitize(attrs)
        raw_health_attrs = _sanitize(health_attrs)

    return {
        "device_api_id": _sanitize(_safe_getattr(device, "device_api_id")),
        "family": _sanitize(_safe_getattr(device, "family")),
        "halo_power_icon": status.icon_name,
        "halo_power_tooltip": status.tooltip,
        "halo_network_icon": network_status.icon_name,
        "halo_network_tooltip": network_status.tooltip,
        "kind": _sanitize(_safe_getattr(device, "kind")),
        "model": _sanitize(_safe_getattr(device, "model")),
        "name": _sanitize(_safe_getattr(device, "name")),
        "public_properties": public_properties,
        "raw_attrs": raw_attrs,
        "raw_health_attrs": raw_health_attrs,
        "ring_class": f"{device.__class__.__module__}.{device.__class__.__name__}",
        "supported_capabilities": _capabilities(device),
    }


def _safe_attrs(device, attr_name: str) -> dict[str, Any]:
    try:
        value = getattr(device, attr_name, {})
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _safe_getattr(device, attr_name: str) -> Any:
    try:
        return getattr(device, attr_name, None)
    except Exception as exc:
        return f"<error: {exc}>"


def _public_properties(device, *, power_only: bool) -> dict[str, Any]:
    properties = {}
    for name in _PUBLIC_PROPERTY_ALLOWLIST:
        if power_only and not _is_power_key(name):
            continue
        value = _safe_getattr(device, name)
        if callable(value) or inspect.isawaitable(value):
            continue
        properties[name] = _sanitize(value, key=name)
    return properties


def _capabilities(device) -> dict[str, Any]:
    capability_checker = getattr(device, "has_capability", None)
    if not callable(capability_checker):
        return {}

    try:
        from ring_doorbell.const import RingCapability  # noqa: PLC0415
    except Exception:
        return {}

    capabilities = {}
    for capability in RingCapability:
        try:
            capabilities[capability.name.lower()] = bool(capability_checker(capability))
        except Exception as exc:
            capabilities[capability.name.lower()] = f"<error: {exc}>"
    return capabilities


def _interesting_paths(value: Any, *, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    if depth > _MAX_DEPTH:
        return []

    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _is_sensitive_key(path):
                found.append((path, "<redacted>"))
                continue
            if isinstance(item, _SCALAR_TYPES) and _is_power_key(path):
                found.append((path, _sanitize(item)))
            elif isinstance(item, dict | list):
                found.extend(_interesting_paths(item, prefix=path, depth=depth + 1))
    elif isinstance(value, list):
        for idx, item in enumerate(value[:20]):
            path = f"{prefix}[{idx}]"
            if isinstance(item, _SCALAR_TYPES):
                if _is_power_key(prefix):
                    found.append((path, _sanitize(item)))
            elif isinstance(item, dict | list):
                found.extend(_interesting_paths(item, prefix=path, depth=depth + 1))
    return found


def _is_power_key(key: str) -> bool:
    lower = key.lower()
    return any(part in lower for part in _POWER_KEY_PARTS)


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    parts = set(re.split(r"[^a-z0-9]+", lower))
    if "latest" in parts:
        parts.discard("lat")
    return any(part in parts or lower.endswith(f".{part}") for part in _SENSITIVE_KEY_PARTS)


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if _is_sensitive_key(key):
        return "<redacted>"
    if depth > _MAX_DEPTH:
        return "<max-depth>"
    if isinstance(value, _SCALAR_TYPES):
        if isinstance(value, str):
            if len(value) > 300:
                return value[:297] + "..."
            if value.startswith(("http://", "https://")):
                return "<redacted-url>"
        return value
    if isinstance(value, dict):
        sanitized = {}
        for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0])):
            item_key_str = str(item_key)
            path = f"{key}.{item_key_str}" if key else item_key_str
            sanitized[item_key_str] = _sanitize(item_value, key=path, depth=depth + 1)
        return sanitized
    if isinstance(value, list | tuple):
        return [_sanitize(item, key=key, depth=depth + 1) for item in value[:100]]
    return f"<{type(value).__name__}>"


def _scalar(value: Any) -> str:
    if isinstance(value, _SCALAR_TYPES):
        return str(value)
    return f"<{type(value).__name__}>"


if __name__ == "__main__":
    raise SystemExit(main())
