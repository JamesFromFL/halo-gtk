"""Ring activity classification for event labels and packaged icons."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from halo_gtk.ring_events import normalized_event_kind
from halo_gtk.theme_icons import icon_path

_ICON_FILES = {
    "answered_ring": ("events", "event-answered-ring.png"),
    "default_ring": ("defaults", "default-ring.png"),
    "favorite": ("defaults", "favorite.png"),
    "linked_motion": ("events", "event-linked.png"),
    "live_view": ("events", "event-live-view.png"),
    "missed_ring": ("events", "event-missed-ring.png"),
    "motion": ("events", "event-motion-detected.png"),
    "package": ("events", "event-package.png"),
    "person": ("events", "event-person-detected.png"),
    "vehicle": ("events", "event-vehicle-detected.png"),
}

_LABELS = {
    "answered_ring": "Answered Ring",
    "default_ring": "Ring",
    "favorite": "Favorite",
    "linked_motion": "Linked Motion",
    "live_view": "Live View",
    "missed_ring": "Missed Ring",
    "motion": "Motion Detected",
    "package": "Package Detected",
    "person": "Person Detected",
    "vehicle": "Vehicle Detected",
}


def activity_key(event: dict[str, Any] | None) -> str:
    """Return the canonical activity key for a Ring history event."""
    if not event:
        return "motion"
    if event.get("_is_local_favorite") or event.get("kind") == "favorite":
        return "favorite"

    kind = normalized_event_kind(event)
    if kind == "on_demand":
        return "live_view"
    if kind == "on_demand_link":
        return "linked_motion"
    if kind == "ding":
        answered = event.get("answered")
        if answered is True:
            return "answered_ring"
        if answered is False:
            return "missed_ring"
        return "default_ring"
    if kind == "motion":
        return _motion_activity_key(event)
    return "motion"


def activity_label(event: dict[str, Any] | None) -> str:
    """Return the user-facing activity label for *event*."""
    if event and event.get("_metadata_missing"):
        return "Missing Metadata"
    return _LABELS[activity_key(event)]


def activity_icon_path(event: dict[str, Any] | None) -> Path:
    """Return the packaged PNG icon path for *event*."""
    category, filename = _ICON_FILES[activity_key(event)]
    return icon_path(category, filename)


def _motion_activity_key(event: dict[str, Any]) -> str:
    detection_types = _detection_types(event)
    cv_properties = event.get("cv_properties")
    primary = ""
    person_detected = False
    if isinstance(cv_properties, dict):
        primary = str(cv_properties.get("detection_type") or "").lower()
        person_detected = cv_properties.get("person_detected") is True

    if "package_delivery" in detection_types or primary == "package_delivery":
        return "package"
    if "vehicle" in detection_types or primary == "vehicle":
        return "vehicle"
    if person_detected or "human" in detection_types or primary == "human":
        return "person"
    return "motion"


def _detection_types(event: dict[str, Any]) -> set[str]:
    cv_properties = event.get("cv_properties")
    if not isinstance(cv_properties, dict):
        return set()

    types = set()
    for key in ("detection_type", "detection_types", "detection_details", "tags"):
        _collect_detection_values(cv_properties.get(key), types)
    return types


def _collect_detection_values(value: Any, values: set[str]) -> None:
    if isinstance(value, str):
        values.add(value.lower())
        return
    if isinstance(value, dict):
        if detection_type := value.get("detection_type"):
            _collect_detection_values(detection_type, values)
        return
    if isinstance(value, list):
        for item in value:
            _collect_detection_values(item, values)
