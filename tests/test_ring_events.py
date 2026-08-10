"""Tests for Ring history and push event-name normalization."""

from types import SimpleNamespace

from ring_doorbell.const import (
    PUSH_ACTION_DING,
    PUSH_ACTION_MOTION,
    PUSH_NOTIFICATION_DING,
    PUSH_NOTIFICATION_MOTION,
)

from halo_gtk.ring_events import normalized_event_kind


def test_normalized_event_kind_maps_legacy_and_current_push_names():
    for kind in ("ding", PUSH_ACTION_DING, PUSH_NOTIFICATION_DING):
        assert normalized_event_kind(SimpleNamespace(kind=kind)) == "ding"
    for kind in ("motion", PUSH_ACTION_MOTION, PUSH_NOTIFICATION_MOTION):
        assert normalized_event_kind({"kind": kind}) == "motion"


def test_normalized_event_kind_preserves_unknown_names_case_insensitively():
    assert normalized_event_kind("Custom_Event") == "custom_event"
    assert normalized_event_kind(None) == ""
