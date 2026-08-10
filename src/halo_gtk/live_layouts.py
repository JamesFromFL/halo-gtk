"""Saved Live Monitoring layouts stored in the user's config directory."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from halo_gtk import storage_paths
from halo_gtk.atomic_io import atomic_write_json

CONFIG_DIR = storage_paths.config_dir()
LAYOUTS_FILE = CONFIG_DIR / "live-monitoring-layouts.json"

DEFAULT_LAYOUT_NAME = "Default"
DEFAULT_LAYOUT_SIZE = "medium"
MAX_LAYOUT_NAME_LENGTH = 50

_CUSTOM_RE = re.compile(r"^Custom (?P<number>\d+)$")
_VALID_SIZES = {"small", "medium", "large"}


def _normalise_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []

    ids: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            device_id = int(item)
        except (TypeError, ValueError):
            continue
        if device_id in seen:
            continue
        ids.append(device_id)
        seen.add(device_id)
    return ids


def clean_name(name: str, fallback: str) -> str:
    """Return a persisted layout name with whitespace trimmed and length capped."""
    cleaned = " ".join(str(name).split())[:MAX_LAYOUT_NAME_LENGTH]
    if cleaned:
        return cleaned
    return " ".join(str(fallback).split())[:MAX_LAYOUT_NAME_LENGTH] or "Custom"


def _normalise_layout(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    layout_id = str(value.get("id") or "").strip()
    if not layout_id:
        layout_id = uuid.uuid4().hex

    size = str(value.get("size") or DEFAULT_LAYOUT_SIZE)
    if size not in _VALID_SIZES:
        size = DEFAULT_LAYOUT_SIZE

    name = clean_name(str(value.get("name") or ""), "Custom")
    return {
        "id": layout_id,
        "name": name,
        "size": size,
        "order": _normalise_ids(value.get("order")),
        "visible": _normalise_ids(value.get("visible")),
    }


def load_layouts() -> list[dict[str, Any]]:
    """Load saved custom layouts from disk."""
    if not LAYOUTS_FILE.exists():
        return []

    try:
        data = json.loads(LAYOUTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    raw_layouts = data.get("layouts") if isinstance(data, dict) else None
    if not isinstance(raw_layouts, list):
        return []

    layouts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_layouts:
        layout = _normalise_layout(item)
        if layout is None or layout["id"] in seen:
            continue
        layouts.append(layout)
        seen.add(layout["id"])
    return layouts


def save_layouts(layouts: list[dict[str, Any]]) -> None:
    """Persist custom layouts, discarding malformed records."""
    normalised = [
        layout for layout in (_normalise_layout(item) for item in layouts) if layout is not None
    ]
    atomic_write_json(
        LAYOUTS_FILE,
        {"layouts": normalised},
        prefix=".live-monitoring-layouts-",
    )


def next_custom_name(layouts: list[dict[str, Any]] | None = None) -> str:
    """Return the next unsaved layout name in the Custom N sequence."""
    max_seen = 0
    for layout in layouts if layouts is not None else load_layouts():
        match = _CUSTOM_RE.match(str(layout.get("name") or ""))
        if match is not None:
            max_seen = max(max_seen, int(match.group("number")))
    return f"Custom {max_seen + 1}"


def save_new_layout(
    *,
    name: str,
    size: str,
    order: list[int],
    visible: list[int],
    fallback_name: str | None = None,
) -> dict[str, Any]:
    """Create and persist a new custom Live Monitoring layout."""
    layouts = load_layouts()
    layout = {
        "id": uuid.uuid4().hex,
        "name": clean_name(name, fallback_name or next_custom_name(layouts)),
        "size": size if size in _VALID_SIZES else DEFAULT_LAYOUT_SIZE,
        "order": _normalise_ids(order),
        "visible": _normalise_ids(visible),
    }
    layouts.append(layout)
    save_layouts(layouts)
    return layout


def update_layout(
    layout_id: str,
    *,
    name: str | None = None,
    size: str | None = None,
    order: list[int] | None = None,
    visible: list[int] | None = None,
) -> dict[str, Any] | None:
    """Update an existing layout and return it."""
    layouts = load_layouts()
    updated: dict[str, Any] | None = None
    for layout in layouts:
        if layout.get("id") != layout_id:
            continue
        if name is not None:
            layout["name"] = clean_name(name, str(layout.get("name") or "Custom"))
        if size is not None:
            layout["size"] = size if size in _VALID_SIZES else DEFAULT_LAYOUT_SIZE
        if order is not None:
            layout["order"] = _normalise_ids(order)
        if visible is not None:
            layout["visible"] = _normalise_ids(visible)
        updated = layout
        break

    if updated is not None:
        save_layouts(layouts)
    return updated


def delete_layout(layout_id: str) -> bool:
    """Delete a saved custom layout by id."""
    layouts = load_layouts()
    kept = [layout for layout in layouts if layout.get("id") != layout_id]
    if len(kept) == len(layouts):
        return False
    save_layouts(kept)
    return True
