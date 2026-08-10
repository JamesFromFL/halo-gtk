from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from halo_gtk.history_page import (
    HistoryPage,
    _devices_with_history_events,
    _event_datetime_label,
    _event_list_time_parts,
    _event_matches_device,
    _event_sort_key,
    _history_filter_devices,
    _merge_favorite_events,
    _VideoPlayer,
)


class _Device:
    def __init__(self, name: str, family: str = "other"):
        self.name = name
        self.family = family


def test_event_list_time_parts_uses_today_and_yesterday_labels():
    now = datetime.now().astimezone().replace(hour=22, minute=5, second=0, microsecond=0)
    yesterday = now - timedelta(days=1)

    assert _event_list_time_parts(now) == ("Today", "10:05 PM")
    assert _event_list_time_parts(yesterday) == ("Yesterday", "10:05 PM")


def test_event_list_time_parts_uses_weekday_month_day_for_older_events():
    old_event = datetime.now().astimezone().replace(
        hour=9,
        minute=23,
        second=0,
        microsecond=0,
    ) - timedelta(days=3)

    date_label, time_label = _event_list_time_parts(old_event)

    assert date_label not in {"Today", "Yesterday"}
    assert "," in date_label
    assert time_label == "9:23 AM"


def test_event_sort_key_handles_aware_naive_missing_and_malformed_timestamps():
    events = [
        {"id": "missing"},
        {"id": "aware", "created_at": datetime(2026, 7, 25, 12, tzinfo=UTC)},
        {"id": "malformed", "created_at": "not-a-date"},
        {"id": "naive", "created_at": datetime(2026, 7, 25, 13)},
        {"id": "iso", "created_at": "2026-07-25T14:00:00+00:00"},
    ]

    events.sort(key=_event_sort_key, reverse=True)

    assert [event["id"] for event in events] == [
        "iso",
        "naive",
        "aware",
        "missing",
        "malformed",
    ]
    assert all(_event_sort_key(event).tzinfo is UTC for event in events)


def test_event_labels_ignore_malformed_timestamps():
    assert _event_datetime_label("not-a-date") == ""
    assert _event_list_time_parts("not-a-date") == ("", "")


def test_devices_with_history_events_filters_devices_without_events():
    camera = _Device("Doorbell Camera")
    chime = _Device("Master Bathroom Chime Pro")
    floodlight = _Device("Front Yard Floodlight Cam")

    devices = _devices_with_history_events(
        [camera, chime, floodlight],
        [{"id": 1, "_device": camera}, {"id": 2, "_device": floodlight}],
    )

    assert devices == [camera, floodlight]


def test_history_filter_devices_keeps_camera_devices_and_excludes_chimes():
    doorbell = _Device("Doorbell Camera", "doorbots")
    floodlight = _Device("Front Yard Floodlight Cam", "stickup_cams")
    chime = _Device("Master Bathroom Chime Pro", "chimes")

    assert _history_filter_devices([doorbell, chime, floodlight]) == [doorbell, floodlight]


def test_event_matches_device_supports_ring_object_and_archived_device_id():
    camera = _Device("Doorbell Camera")
    camera.id = 42
    other = _Device("Other Camera")
    other.id = 99

    assert _event_matches_device({"_device": camera}, camera) is True
    assert _event_matches_device({"_device": camera}, other) is False
    assert _event_matches_device({"device_id": "42"}, camera) is True
    assert _event_matches_device({"device_id": "99"}, camera) is False


def test_merge_favorite_events_prefers_local_archive_without_duplicates():
    remote = [
        {"id": 7, "device_id": 42, "kind": "motion"},
        {"id": 7, "device_id": 42, "kind": "motion"},
        {"id": 8, "device_id": 42, "kind": "ding"},
    ]
    archived = {
        "id": "7",
        "device_id": "42",
        "kind": "motion",
        "_favorite_id": "42-7",
        "_local_video_path": "/archive/42-7.mp4",
        "_is_local_favorite": True,
    }

    merged = _merge_favorite_events(remote, [archived])

    assert merged == [archived, remote[2]]


def test_merge_favorite_events_retains_orphans_without_stable_id():
    orphan = {
        "id": None,
        "_local_video_path": "/archive/orphan.mp4",
        "_metadata_missing": True,
    }

    assert _merge_favorite_events([], [orphan]) == [orphan]


def test_page_hidden_stops_playback_and_invalidates_pending_load():
    stopped = []
    loaded = []
    player = SimpleNamespace(
        deactivate=lambda: stopped.append(True),
        load_url=lambda url: loaded.append(url),
    )
    page = SimpleNamespace(
        _history_request_generation=3,
        _playback_request_generation=7,
        _history_loading=True,
        _pending_event_id=42,
        _player=player,
        _nav_split=SimpleNamespace(set_show_content=lambda value: loaded.append(("list", value))),
    )

    HistoryPage.on_page_hidden(page)
    HistoryPage._load_url_if_current(page, "https://example.com/recording", 7)
    HistoryPage._show_fetch_error(page, "stale error", 3)

    assert stopped == [True]
    assert loaded == [("list", False)]
    assert page._history_request_generation == 4
    assert page._playback_request_generation == 8
    assert page._history_loading is False
    assert page._pending_event_id is None


def test_video_player_deactivate_stops_and_closes_fullscreen():
    calls = []
    player = SimpleNamespace(
        stop=lambda: calls.append("stop"),
        _fullscreen_window=SimpleNamespace(close=lambda: calls.append("close")),
        set_fullscreened=lambda active: calls.append(("fullscreen", active)),
    )

    _VideoPlayer.deactivate(player)

    assert calls == ["stop", "close", ("fullscreen", False)]
    assert player._fullscreen_window is None
