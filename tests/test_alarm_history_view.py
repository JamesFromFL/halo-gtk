from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace

import pytest

from halo_gtk import alarm_history, history_page
from halo_gtk.alarm.history import AlarmHistoryEvent, AlarmHistoryPage, AlarmHistoryStatus
from halo_gtk.alarm_history import AlarmHistoryView
from halo_gtk.history_page import HistoryPage


class Widget:
    def __init__(self):
        self.sensitive = True
        self.text = ""
        self.visible = False
        self.selected = 0
        self.model = None

    def set_sensitive(self, value):
        self.sensitive = value

    def set_label(self, value):
        self.text = value

    def set_title(self, value):
        self.text = value

    def set_visible(self, value):
        self.visible = value

    def set_model(self, value):
        self.model = value

    def set_selected(self, value):
        self.selected = value

    def get_selected(self):
        return self.selected


def event():
    return AlarmHistoryEvent(
        "local-key",
        "loc",
        "mode",
        "Armed Home",
        datetime(2026, 10, 26, tzinfo=UTC),
        datetime.now(UTC),
        actor="Pat",
    )


class View(SimpleNamespace):
    pass


class Client:
    def get_alarm_snapshot(self):
        return SimpleNamespace(generation=1, locations=(SimpleNamespace(location_id="loc"),))

    def remove_alarm_callback(self, callback):
        pass


def view(client=None):
    obj = View(
        _active=True,
        _client=client,
        _generation=1,
        _cancel=threading.Event(),
        _loading=False,
        _events=[],
        _page_signatures=set(),
        _location_id="loc",
        _next_offset=None,
        _refresh_button=Widget(),
        _older=Widget(),
        _message=Widget(),
        _empty=Widget(),
        _watch_id=None,
        _snapshot_pending=None,
        _snapshot_idle=False,
        _updating=False,
        _locations=(),
        _location=Widget(),
        _device=Widget(),
        _device_ids=[None],
        _account_generation=1,
        _render=lambda: None,
        _on_snapshot=lambda *_: None,
    )
    obj._finish = MethodType(AlarmHistoryView._finish, obj)
    obj.deactivate = MethodType(AlarmHistoryView.deactivate, obj)
    return obj


@pytest.mark.parametrize("reason", ["generation", "cancel", "client", "location", "hidden"])
def test_old_result_cannot_publish_after_view_or_account_change(monkeypatch, reason):
    client = Client()
    page = AlarmHistoryPage("loc", (event(),), AlarmHistoryStatus.READY)
    obj = view(client)
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    if reason == "generation":
        obj._generation = 2
    elif reason == "cancel":
        obj._cancel.set()
    elif reason == "client":
        monkeypatch.setattr(alarm_history, "get_client", lambda: object())
    elif reason == "location":
        obj._location_id = "other"
    else:
        obj._active = False
    obj._finish(page, client, 1, obj._cancel, False)
    assert not obj._events


def test_denied_result_purges_rows_and_cached_result_is_labeled(monkeypatch):
    client = Client()
    obj = view(client)
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    obj._events = [event()]
    page = AlarmHistoryPage(
        "loc",
        status=AlarmHistoryStatus.PERMISSION_DENIED,
        message="Ring denied access to Alarm history.",
    )
    obj._finish(page, client, 1, obj._cancel, True)
    assert not obj._events
    cached = AlarmHistoryPage(
        "loc", (event(),), AlarmHistoryStatus.UNAVAILABLE, from_cache=True, stale=True
    )
    obj._finish(cached, client, 1, obj._cancel, False)
    assert len(obj._events) == 1
    assert "stale" in obj._message.text


def test_queued_result_rechecks_authoritative_access_before_snapshot_idle(monkeypatch):
    client = Client()
    obj = view(client)
    obj._events = [event()]
    client.get_alarm_snapshot = lambda: SimpleNamespace(generation=1, locations=())
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    page = AlarmHistoryPage("loc", (event(),), AlarmHistoryStatus.READY)
    obj._finish(page, client, 1, obj._cancel, False)
    assert not obj._events and not obj._active and obj._cancel.is_set()
    assert obj._location.model.get_n_items() == 0


def test_actual_fetch_page_presenter_stops_exact_repetition_without_collapsing_occurrences(
    monkeypatch,
):
    client = Client()
    obj = view(client)
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    first = replace(event(), key="fingerprint:0")
    second = replace(first, key="fingerprint:1")
    page = AlarmHistoryPage("loc", (first, second), AlarmHistoryStatus.READY, next_offset=2)
    obj._finish(page, client, 1, obj._cancel, False)
    assert len(obj._events) == 2  # legitimate identical occurrences retained
    repeat = replace(
        page,
        events=(replace(first, key="fingerprint:2"), replace(second, key="fingerprint:3")),
        offset=2,
        next_offset=4,
    )
    obj._finish(repeat, client, 1, obj._cancel, True)
    assert {event.key for event in obj._events} == {first.key, second.key}
    assert len(obj._events) == 2 and obj._next_offset is None
    assert "uncertain" in obj._message.text


def test_actual_presenter_stops_page_with_no_supported_progress(monkeypatch):
    client = Client()
    obj = view(client)
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    page = AlarmHistoryPage("loc", (), AlarmHistoryStatus.UNSUPPORTED, next_offset=50)
    obj._finish(page, client, 1, obj._cancel, True)
    assert obj._next_offset is None and "uncertain" in obj._message.text


def test_mutating_offset_pages_are_sorted_together_by_ring_time(monkeypatch):
    client = Client()
    obj = view(client)
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    older = replace(event(), key="older:0")
    newer = replace(event(), key="newer:1", occurred_at=older.occurred_at + timedelta(seconds=1))
    obj._finish(
        AlarmHistoryPage("loc", (older,), AlarmHistoryStatus.READY), client, 1, obj._cancel, False
    )
    obj._finish(
        AlarmHistoryPage("loc", (newer,), AlarmHistoryStatus.READY, offset=1),
        client,
        1,
        obj._cancel,
        True,
    )
    assert obj._events == [newer, older]


def test_worker_admission_failure_retires_loading_and_restores_refresh(monkeypatch):
    obj = view(object())
    obj._loading = True
    obj._refresh_button.sensitive = False
    old_cancel = obj._cancel
    monkeypatch.setattr(alarm_history, "_WORKERS", threading.BoundedSemaphore(0))
    AlarmHistoryView._fetch(obj, append=False)
    assert old_cancel.is_set()
    assert not obj._loading and obj._refresh_button.sensitive
    assert "busy" in obj._message.text


def test_thread_start_failure_releases_worker_and_resets_controls(monkeypatch):
    client = Client()
    obj = view(client)
    workers = threading.BoundedSemaphore(1)
    monkeypatch.setattr(alarm_history, "_WORKERS", workers)
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)

    def fail(**kwargs):
        raise RuntimeError("not rendered")

    monkeypatch.setattr(alarm_history.threading, "Thread", fail)
    AlarmHistoryView._fetch(obj, append=False)
    assert workers.acquire(blocking=False)
    assert not obj._loading and obj._refresh_button.sensitive
    assert "not rendered" not in obj._message.text


def test_page_deactivation_cancels_real_worker_and_drops_queued_publication(monkeypatch):
    started = threading.Event()
    received = []
    queued, threads = [], []
    original_thread = threading.Thread

    class Client:
        def alarm_history(self, location_id, **kwargs):
            received.append(kwargs)
            started.set()
            assert kwargs["cancel_event"].wait(1)
            return AlarmHistoryPage(location_id, (event(),), AlarmHistoryStatus.READY)

        def remove_alarm_callback(self, callback):
            pass

    client = Client()
    obj = view(client)

    def thread(**kwargs):
        child = original_thread(**kwargs)
        threads.append(child)
        return child

    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    monkeypatch.setattr(alarm_history.threading, "Thread", thread)
    monkeypatch.setattr(alarm_history.GLib, "idle_add", lambda callback: queued.append(callback))
    AlarmHistoryView._fetch(obj, append=False, force=True)
    assert started.wait(1)
    obj.deactivate(clear=True)
    threads[0].join(1)
    assert not threads[0].is_alive()
    assert received[0]["force"] is True
    for callback in queued:
        callback()
    assert not obj._events and not obj._active and obj._cancel.is_set()


def test_client_generation_watch_clears_sensitive_rows(monkeypatch):
    client = SimpleNamespace(remove_alarm_callback=lambda *_: None)
    obj = view(client)
    obj._events = [event()]
    monkeypatch.setattr(alarm_history, "get_client", lambda: None)
    AlarmHistoryView._watch_client(obj)
    assert not obj._events and obj._client is None and obj._cancel.is_set()
    assert obj._locations == () and obj._device_ids == [None]
    assert obj._location.model.get_n_items() == 0
    assert obj._device.model.get_string(0) == "All devices"


def test_authoritative_snapshots_reconcile_other_locations_and_current_device_choices(monkeypatch):
    client = object()
    obj = view(client)
    old = SimpleNamespace(
        location_id="loc",
        name="Allowed",
        devices=(
            SimpleNamespace(zid="sensor", name="Retained"),
            SimpleNamespace(zid="gone", name="Removed device"),
        ),
    )
    gone = SimpleNamespace(location_id="removed", name="Removed location", devices=())
    current = SimpleNamespace(
        location_id="loc",
        name="Renamed",
        devices=(SimpleNamespace(zid="sensor", name="Renamed sensor"),),
    )
    obj._locations = old, gone
    obj._device_ids = [None, "sensor", "gone"]
    obj._device.selected = 1
    obj._account_generation = 1
    obj._snapshot_pending = SimpleNamespace(generation=1, locations=(current,))
    obj._set_locations = MethodType(AlarmHistoryView._set_locations, obj)
    obj._set_devices = MethodType(AlarmHistoryView._set_devices, obj)
    monkeypatch.setattr(alarm_history, "get_client", lambda: client)
    AlarmHistoryView._apply_snapshot(obj)
    assert obj._locations == (current,) and obj._location_id == "loc"
    assert obj._location.model.get_n_items() == 1
    assert obj._location.model.get_string(0) == "Renamed"
    assert obj._device_ids == [None, "sensor"] and obj._device.selected == 1
    assert obj._device.model.get_string(1) == "Renamed sensor"


def test_history_alarm_selection_stops_media_and_bypasses_camera_fetch():
    calls = []
    obj = SimpleNamespace(
        _history_request_generation=1,
        _history_loading=True,
        _device_options=[],
        _reset_current_event=lambda: calls.append("stop-media"),
        _mode_stack=SimpleNamespace(set_visible_child_name=lambda name: calls.append(name)),
        _alarm_history=SimpleNamespace(refresh=lambda location: calls.append(("alarm", location))),
    )
    HistoryPage.show_alarm_history(obj, "loc")
    assert calls == ["stop-media", "alarm", ("alarm", "loc")]
    assert obj._history_request_generation == 2 and not obj._history_loading


def test_history_alarm_refresh_does_not_reconcile_favorites_or_fetch_cameras(monkeypatch):
    calls = []
    obj = SimpleNamespace(
        _alarm_history_active=True,
        _alarm_history=SimpleNamespace(refresh=lambda: calls.append("alarm")),
    )
    monkeypatch.setattr(history_page, "get_client", lambda: SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(
        history_page.favorites,
        "request_reconcile",
        lambda *_args, **_kwargs: pytest.fail("camera/favorite path"),
    )
    HistoryPage.refresh(obj)
    assert calls == ["alarm"]


def test_alarm_selection_never_activates_camera_row_or_video_actions():
    HistoryPage._on_event_selected(
        SimpleNamespace(_alarm_history_active=True), None, SimpleNamespace(_event_data={"id": 1})
    )
    HistoryPage._on_screenshot(SimpleNamespace(_alarm_history_active=True))


def test_history_navigation_uses_typed_alarm_target_not_local_fingerprint():
    calls = []
    obj = SimpleNamespace(
        get_root=lambda: SimpleNamespace(
            open_alarm_location=lambda *args, **kwargs: calls.append((args, kwargs))
        )
    )
    HistoryPage._open_alarm_history_event(obj, event())
    assert calls == [(("loc",), {"asset_id": None, "zid": None, "event_id": None})]


def test_alarm_rows_use_literal_text_and_no_camera_action(monkeypatch):
    constructed = []
    rows = SimpleNamespace(get_first_child=lambda: None, append=lambda row: constructed.append(row))
    obj = SimpleNamespace(
        _rows=rows,
        _category=SimpleNamespace(get_selected=lambda: 0),
        _device=SimpleNamespace(get_selected=lambda: 0),
        _device_ids=[None],
        _events=[event()],
        _older=Widget(),
        _next_offset=None,
        _loading=False,
        _updating=False,
    )
    monkeypatch.setattr(alarm_history.Adw, "ActionRow", lambda **kw: SimpleNamespace(**kw))
    AlarmHistoryView._render(obj)
    assert len(constructed) == 1
    assert constructed[0].use_markup is False
    assert constructed[0]._alarm_history_event.event_id is None
    assert "Ring cloud history" in constructed[0].subtitle
