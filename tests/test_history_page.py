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


def test_selecting_an_event_stops_the_previous_recording_before_requesting_url(monkeypatch):
    from halo_gtk import history_page

    calls = []
    event = {"id": 7, "device_id": 42}
    page = SimpleNamespace(
        _current_event=None,
        _playback_request_generation=3,
        _player=SimpleNamespace(stop=lambda: calls.append("stop")),
        _update_favorite_button=lambda: None,
        _update_event_details=lambda _event: calls.append("details"),
        _nav_split=SimpleNamespace(set_show_content=lambda _value: None),
        _on_title_change=None,
        _load_and_play=lambda *_args: None,
    )
    monkeypatch.setattr(
        history_page.threading,
        "Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: calls.append("fetch")),
    )

    HistoryPage._on_event_selected(page, None, SimpleNamespace(_event_data=event))

    assert calls == ["stop", "details", "fetch"]
    assert page._current_event is event
    assert page._playback_request_generation == 4


def test_refresh_invalidates_pending_recording_requests(monkeypatch):
    from halo_gtk import history_page

    calls = []
    page = SimpleNamespace(
        _reset_current_event=lambda: calls.append("reset-playback"),
        _nav_split=SimpleNamespace(set_show_content=lambda _value: None),
        _list_placeholder=SimpleNamespace(
            set_title=lambda _value: None, set_description=lambda _value: None
        ),
        _clear_event_list=lambda: None,
        _reset_history_pagination=lambda: None,
        _start_history_fetch=lambda **_kwargs: calls.append("fetch"),
        _after_favorite_change=lambda: None,
    )
    monkeypatch.setattr(history_page, "get_client", lambda: SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(history_page.favorites, "request_reconcile", lambda *_args, **_kwargs: None)

    HistoryPage.refresh(page)

    assert calls == ["reset-playback", "fetch"]


def test_delayed_delete_preserves_other_cameras_and_newer_playback():
    deleted = {"id": 7, "device_id": 42}
    other_camera = {"id": 7, "device_id": 99}
    newly_selected = {"id": 8, "device_id": 42}
    calls = []
    page = SimpleNamespace(
        _events=[deleted, other_camera, newly_selected],
        _current_event=newly_selected,
        _reset_current_event=lambda: calls.append("reset"),
        _fill_event_rows=lambda: calls.append("rows"),
    )

    HistoryPage._after_delete(page, deleted)

    assert page._events == [other_camera, newly_selected]
    assert page._current_event is newly_selected
    assert calls == ["rows"]


def test_delayed_delete_rebuild_keeps_navigation_without_restarting_current_clip(monkeypatch):
    from halo_gtk import history_page

    deleted = {"id": 7, "device_id": 42}
    playing = {"id": 8, "device_id": 42}
    following = {"id": 9, "device_id": 42}
    activated = []

    class EventList:
        def __init__(self):
            self.rows = []
            self.selected = None

        def append(self, row):
            self.rows.append(row)

        def clear(self):
            self.rows.clear()
            self.selected = None

        def select_row(self, row):
            self.selected = row

        def get_selected_row(self):
            return self.selected

        def get_row_at_index(self, index):
            return self.rows[index] if 0 <= index < len(self.rows) else None

    event_list = EventList()

    def make_row(event):
        row = SimpleNamespace(_event_data=event)
        row.get_index = lambda: event_list.rows.index(row)
        return row

    page = SimpleNamespace(
        _events=[deleted, playing, following],
        _current_event=dict(playing),  # Match identity even if the event dict was replaced.
        _pending_event_id=None,
        _history_exhausted=True,
        _event_list=event_list,
        _clear_event_list=event_list.clear,
        _make_event_row=make_row,
        _on_event_selected=lambda _list, row: activated.append(row._event_data),
        _reset_current_event=lambda: activated.append("unexpected playback reset"),
    )
    page._visible_events = lambda: page._events
    page._fill_event_rows = lambda: HistoryPage._fill_event_rows(page)
    page._select_adjacent_event = lambda offset: HistoryPage._select_adjacent_event(page, offset)
    monkeypatch.setattr(history_page._cfg, "load", lambda: {"event_history_next_auto_play": True})

    HistoryPage._after_delete(page, deleted)

    assert event_list.get_selected_row()._event_data is playing
    assert activated == []
    HistoryPage._on_playback_finished(page)
    assert event_list.get_selected_row()._event_data is following
    HistoryPage._select_adjacent_event(page, -1)
    assert event_list.get_selected_row()._event_data is playing
    assert activated == [following, playing]


def test_delete_invalidates_pending_playback_for_a_matching_event():
    deleted = {"id": 7, "device_id": 42}
    calls = []
    page = SimpleNamespace(
        _events=[dict(deleted)],
        _current_event=dict(deleted),
        _reset_current_event=lambda: calls.append("reset"),
        _fill_event_rows=lambda: calls.append("rows"),
    )

    HistoryPage._after_delete(page, deleted)

    assert page._events == []
    assert calls == ["reset", "rows"]


def test_queued_end_of_stream_does_not_restart_autoplay_after_stop(monkeypatch):
    from halo_gtk import history_page

    queued = []
    player = SimpleNamespace(
        _playback_generation=3,
        _handle_eos=lambda generation: _VideoPlayer._handle_eos(player, generation),
    )
    monkeypatch.setattr(
        history_page.GLib, "idle_add", lambda callback, *args: queued.append((callback, args))
    )

    _VideoPlayer._on_eos(player)
    player._playback_generation += 1  # stop() or loading another recording
    callback, args = queued[0]

    # No scrubber, polling or completion callback should be touched for the old clip.
    assert callback(*args) is False
