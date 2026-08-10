"""Halo-local display names for Ring devices."""

from __future__ import annotations

import json
import threading
from typing import Any

from halo_gtk import storage_paths
from halo_gtk.atomic_io import atomic_write_json

CONFIG_DIR = storage_paths.config_dir()
NICKNAMES_FILE = CONFIG_DIR / "device-nicknames.json"

# Cache the parsed nicknames (keyed by path+mtime) so display_name/get_nickname —
# called dozens of times per camera grid and history list — don't re-read the
# file on every call. Invalidated on write.
_cache_lock = threading.Lock()
_cached_nicknames: dict[str, str] | None = None
_cached_key: tuple[str, float | None] | None = None


def device_key(device: Any) -> str | None:
    """Return a stable key for a Ring device."""
    for attr in ("id", "device_api_id", "doorbot_id"):
        try:
            value = getattr(device, attr, None)
        except Exception:
            value = None
        if value is not None:
            return str(value)
    if isinstance(device, dict):
        for key in ("device_id", "doorbot_id", "id"):
            value = device.get(key)
            if value is not None:
                return str(value)
    return None


def ring_name(device: Any, fallback: str = "Ring device") -> str:
    """Return the Ring-provided device name."""
    try:
        value = getattr(device, "name", None)
    except Exception:
        value = None
    if value is None and isinstance(device, dict):
        value = device.get("camera_name") or device.get("device_name")
    text = str(value or "").strip()
    return text or fallback


def display_name(device: Any, fallback: str = "Ring device") -> str:
    """Return the Halo display name for *device*, preferring a local nickname."""
    key = device_key(device)
    if key is not None:
        nickname = get_nickname(key)
        if nickname:
            return nickname
    return ring_name(device, fallback)


def display_name_for_id(device_id: Any, fallback: str = "Ring device") -> str:
    """Return the Halo display name for *device_id*, falling back to *fallback*."""
    if device_id is not None:
        nickname = get_nickname(str(device_id))
        if nickname:
            return nickname
    return str(fallback or "Ring device")


def get_nickname(device_or_id: Any) -> str:
    """Return the saved nickname for *device_or_id*, or an empty string."""
    key = device_or_id if isinstance(device_or_id, str) else device_key(device_or_id)
    if key is None:
        return ""
    value = _read_nicknames().get(str(key), "")
    return str(value).strip()


def set_nickname(device_or_id: Any, nickname: str) -> None:
    """Save or clear the Halo-local nickname for *device_or_id*."""
    key = device_or_id if isinstance(device_or_id, str) else device_key(device_or_id)
    if key is None:
        raise ValueError("Cannot save a nickname for a device without an id")

    cleaned = " ".join(str(nickname).strip().split())
    data = _read_nicknames()
    if cleaned:
        data[str(key)] = cleaned
    else:
        data.pop(str(key), None)
    _write_nicknames(data)


def _read_nicknames() -> dict[str, str]:
    try:
        mtime = NICKNAMES_FILE.stat().st_mtime
    except OSError:
        mtime = None
    key = (str(NICKNAMES_FILE), mtime)
    global _cached_nicknames, _cached_key
    with _cache_lock:
        if _cached_nicknames is not None and _cached_key == key:
            return dict(_cached_nicknames)
        data = _read_nicknames_from_disk()
        _cached_nicknames = data
        _cached_key = key
        return dict(_cached_nicknames)


def _invalidate_cache() -> None:
    global _cached_nicknames, _cached_key
    with _cache_lock:
        _cached_nicknames = None
        _cached_key = None


def _read_nicknames_from_disk() -> dict[str, str]:
    if not NICKNAMES_FILE.is_file():
        return {}
    try:
        data = json.loads(NICKNAMES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    nicknames = data.get("nicknames", data)
    if not isinstance(nicknames, dict):
        return {}
    return {str(key): str(value).strip() for key, value in nicknames.items() if str(value).strip()}


def _write_nicknames(nicknames: dict[str, str]) -> None:
    payload = {"version": 1, "nicknames": dict(sorted(nicknames.items()))}
    atomic_write_json(NICKNAMES_FILE, payload, prefix=".device-nicknames-")
    _invalidate_cache()
