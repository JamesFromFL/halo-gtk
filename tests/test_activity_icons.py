from __future__ import annotations

from halo_gtk import activity_icons


def test_motion_package_detection_wins_over_person_detection():
    event = {
        "kind": "motion",
        "cv_properties": {
            "detection_type": "package_delivery",
            "detection_types": [
                {"detection_type": "human"},
                {"detection_type": "package_delivery"},
            ],
        },
    }

    assert activity_icons.activity_key(event) == "package"
    assert activity_icons.activity_label(event) == "Package Detected"


def test_motion_detection_types_map_to_specific_activity_icons():
    assert (
        activity_icons.activity_key(
            {"kind": "motion", "cv_properties": {"detection_type": "human"}}
        )
        == "person"
    )
    assert (
        activity_icons.activity_key(
            {"kind": "motion", "cv_properties": {"detection_type": "vehicle"}}
        )
        == "vehicle"
    )
    assert (
        activity_icons.activity_key(
            {"kind": "motion", "cv_properties": {"detection_type": "other_motion"}}
        )
        == "motion"
    )


def test_ring_and_live_event_kinds_map_to_activity_icons():
    assert activity_icons.activity_key({"kind": "ding", "answered": True}) == "answered_ring"
    assert activity_icons.activity_key({"kind": "ding", "answered": False}) == "missed_ring"
    assert activity_icons.activity_key({"kind": "ding"}) == "default_ring"
    assert activity_icons.activity_key({"kind": "on_demand"}) == "live_view"
    assert activity_icons.activity_key({"kind": "on_demand_link"}) == "linked_motion"


def test_activity_icon_path_uses_packaged_assets():
    path = activity_icons.activity_icon_path({"kind": "on_demand"})

    assert path.name == "event-live-view.png"
    assert path.is_file()


def test_local_favorites_and_missing_metadata_have_favorite_icon_and_missing_label():
    event = {"kind": "motion", "_is_local_favorite": True}
    missing = {"kind": "favorite", "_is_local_favorite": True, "_metadata_missing": True}

    assert activity_icons.activity_key(event) == "favorite"
    assert activity_icons.activity_label(event) == "Favorite"
    assert activity_icons.activity_key(missing) == "favorite"
    assert activity_icons.activity_label(missing) == "Missing Metadata"
