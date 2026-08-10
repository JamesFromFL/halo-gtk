"""Camera device information content and metadata formatting."""

from __future__ import annotations

import inspect
import logging
import re
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk import device_names  # noqa: E402
from halo_gtk.network_status import camera_network_status  # noqa: E402
from halo_gtk.power_status import camera_power_status  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402

_log = logging.getLogger(__name__)

_SENSITIVE_KEY_PARTS = {
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
    "token",
    "url",
}
_PUBLIC_PROPERTIES = (
    "id",
    "device_api_id",
    "name",
    "family",
    "kind",
    "model",
    "connection_status",
    "battery_life",
    "has_subscription",
    "subscribed",
    "subscribed_motions",
    "motion_detection",
    "light",
    "lights",
    "volume",
    "existing_doorbell_type",
    "existing_doorbell_type_enabled",
)
_SCALAR_TYPES = (str, int, float, bool, type(None))
_MAX_ROWS_PER_GROUP = 160


class CameraInfoContent(Gtk.Box):
    """Scrollable-ready content for Ring camera metadata and health details."""

    def __init__(self, device: Any) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=14,
            margin_bottom=14,
            margin_start=14,
            margin_end=14,
        )
        self.device = device
        self._status_row: Adw.ActionRow | None = None
        self.refresh()
        self.refresh_health_async()

    def refresh(self) -> None:
        while (child := self.get_first_child()) is not None:
            self.remove(child)

        for section in camera_info_sections(self.device):
            group = Adw.PreferencesGroup(title=section["title"])
            for title, value in section["rows"][:_MAX_ROWS_PER_GROUP]:
                group.add(_info_row(title, value))
            omitted = len(section["rows"]) - _MAX_ROWS_PER_GROUP
            if omitted > 0:
                group.add(_info_row("More", f"{omitted} additional fields omitted"))
            self.append(group)

        status_group = Adw.PreferencesGroup(title="Health Refresh")
        self._status_row = _info_row("Status", "Loaded current device metadata")
        status_group.add(self._status_row)
        self.append(status_group)

    def refresh_health_async(self) -> None:
        if self._status_row is not None:
            self._status_row.set_subtitle("Refreshing Ring health data...")
        threading.Thread(target=self._refresh_health_worker, daemon=True).start()

    def _refresh_health_worker(self) -> None:
        error = _refresh_device_health(self.device)
        GLib.idle_add(self._finish_health_refresh, error)

    def _finish_health_refresh(self, error: str | None) -> bool:
        # The window may have closed while the worker ran — don't rebuild a
        # detached widget tree (a closed window leaves this content unrooted).
        if self.get_root() is None:
            return GLib.SOURCE_REMOVE
        self.refresh()
        if self._status_row is not None:
            self._status_row.set_subtitle(error or "Ring health data refreshed")
        return GLib.SOURCE_REMOVE


def camera_info_sections(device: Any) -> list[dict[str, Any]]:
    """Return display sections for *device* metadata and health details."""
    power = camera_power_status(device)
    network = camera_network_status(device)
    sections = [
        {
            "title": "Overview",
            "rows": [
                ("Halo Display Name", device_names.display_name(device)),
                ("Ring Device Name", device_names.ring_name(device)),
                ("Halo Nickname", device_names.get_nickname(device) or "Not Set"),
                ("Power", power.tooltip),
                ("Connection", network.tooltip),
                ("Connection Status", _safe_getattr(device, "connection_status")),
                ("Model", _safe_getattr(device, "model")),
                ("Family", _safe_getattr(device, "family")),
                ("Kind", _safe_getattr(device, "kind")),
                ("Device ID", _safe_getattr(device, "id")),
                ("API ID", _safe_getattr(device, "device_api_id")),
            ],
        },
        {"title": "Device Properties", "rows": _public_property_rows(device)},
        {
            "title": "Health Details",
            "rows": _flatten_mapping(_safe_dict_attr(device, "_health_attrs")),
        },
        {"title": "Device Metadata", "rows": _flatten_mapping(_safe_dict_attr(device, "_attrs"))},
        {"title": "Capabilities", "rows": _capability_rows(device)},
    ]
    return [section for section in sections if section["rows"]]


def _info_row(title: str, value: Any) -> Adw.ActionRow:
    # Titles can come from device-controlled metadata keys (via _flatten_mapping),
    # so escape them too — Adw row titles render Pango markup by default.
    row = Adw.ActionRow(title=GLib.markup_escape_text(str(title)))
    row.set_subtitle(GLib.markup_escape_text(_format_value(value)))
    return row


def _public_property_rows(device: Any) -> list[tuple[str, Any]]:
    rows = []
    for name in _PUBLIC_PROPERTIES:
        value = _safe_getattr(device, name)
        if callable(value) or inspect.isawaitable(value):
            continue
        rows.append((_humanize_key(name), value))
    return rows


def _capability_rows(device: Any) -> list[tuple[str, Any]]:
    checker = getattr(device, "has_capability", None)
    if not callable(checker):
        return []
    try:
        from ring_doorbell.const import RingCapability  # noqa: PLC0415
    except Exception:
        return []

    rows = []
    for capability in RingCapability:
        try:
            value = checker(capability)
        except Exception as exc:
            value = f"<error: {exc}>"
        rows.append((_humanize_key(capability.name), value))
    return rows


def _flatten_mapping(value: Any, *, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    if depth > 8:
        return [(prefix or "Value", "<max-depth>")]
    if isinstance(value, dict):
        rows: list[tuple[str, Any]] = []
        for key in sorted(value, key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten_mapping(value[key], prefix=path, depth=depth + 1))
        return rows
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]"
            rows.extend(_flatten_mapping(item, prefix=path, depth=depth + 1))
        return rows
    return [(prefix or "Value", _sanitize(value, prefix))]


def _refresh_device_health(device: Any) -> str | None:
    client = get_client()
    updater = getattr(device, "async_update_health_data", None)
    if client is None or not callable(updater):
        return "Ring health refresh is not available for this device"
    try:
        client.submit(updater()).result(timeout=20)
    except Exception as exc:
        _log.debug("Health refresh failed for %s: %s", _safe_getattr(device, "name"), exc)
        return f"Health refresh failed: {exc}"
    return None


def _safe_getattr(device: Any, name: str) -> Any:
    try:
        return getattr(device, name, None)
    except Exception as exc:
        return f"<error: {exc}>"


def _safe_dict_attr(device: Any, name: str) -> dict[str, Any]:
    try:
        value = getattr(device, name, {})
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _sanitize(value: Any, key: str) -> Any:
    if _is_sensitive_key(key):
        return "<redacted>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, _SCALAR_TYPES):
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    parts = set(re.split(r"[^a-z0-9]+", lower))
    if "latest" in parts:
        parts.discard("lat")
    return any(part in parts or lower.endswith(f".{part}") for part in _SENSITIVE_KEY_PARTS)


def _format_value(value: Any) -> str:
    if value is None:
        return "Unknown"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if text else "Unknown"


def _humanize_key(key: str) -> str:
    return str(key).replace("_", " ").replace(".", " / ").title()
