"""Ring activity classification for labels and standard symbolic icons."""

from __future__ import annotations

from typing import Any

from halo_gtk.ring_events import normalized_event_kind

_ICON_NAMES = {
    "answered_ring": "call-start-symbolic",
    "default_ring": "alarm-symbolic",
    "favorite": "starred-symbolic",
    "linked_motion": "insert-link-symbolic",
    "live_view": "camera-video-symbolic",
    "missed_ring": "call-stop-symbolic",
    "motion": "media-record-symbolic",
    "package": "package-x-generic-symbolic",
    "person": "system-users-symbolic",
    "vehicle": "media-record-symbolic",
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


def activity_icon_name(event: dict[str, Any] | None) -> str:
    """Return the standard symbolic icon name for *event*."""
    return _ICON_NAMES[activity_key(event)]


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
