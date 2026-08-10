"""Tests for StatusNotifier tray lifecycle handling."""

from __future__ import annotations


def test_setup_defers_registration_to_idle(monkeypatch):
    from halo_gtk import systray
    from halo_gtk.systray import SystemTray

    tray = SystemTray(app=object())
    scheduled: list = []
    monkeypatch.setattr(systray.GLib, "idle_add", lambda fn, *args: scheduled.append(fn))

    # setup() enables the tray and schedules the synchronous D-Bus work off the
    # startup critical path instead of running it inline.
    assert tray.setup() is True
    assert tray._enabled is True
    assert tray._setup_pending is True
    assert scheduled == [tray._setup_now]


def test_setup_now_schedules_retry_when_watcher_is_missing():
    from halo_gtk.systray import SystemTray

    tray = SystemTray(app=object())
    calls: list[str] = []
    tray._enabled = True
    tray._ensure_connection = lambda: True
    tray._export_objects = lambda: calls.append("export") or True
    tray._own_bus_name = lambda: calls.append("own") or True
    tray._watch_watcher_owner = lambda: calls.append("watch")
    tray._register_with_watcher = lambda: False
    tray._schedule_retry = lambda: calls.append("retry")

    tray._setup_now()

    assert tray._enabled is True
    assert calls == ["export", "own", "watch", "retry"]


def test_setup_now_cancels_retry_after_successful_registration():
    from halo_gtk.systray import SystemTray

    tray = SystemTray(app=object())
    calls: list[str] = []
    tray._enabled = True
    tray._ensure_connection = lambda: True
    tray._export_objects = lambda: calls.append("export") or True
    tray._own_bus_name = lambda: calls.append("own") or True
    tray._watch_watcher_owner = lambda: calls.append("watch")
    tray._register_with_watcher = lambda: True
    tray._cancel_retry = lambda: calls.append("cancel")

    tray._setup_now()

    assert tray._enabled is True
    assert calls == ["export", "own", "watch", "cancel"]


def test_watcher_disappear_marks_tray_unregistered_and_schedules_retry():
    from halo_gtk.systray import SystemTray

    class Params:
        def unpack(self):
            return ("org.kde.StatusNotifierWatcher", ":1.20", "")

    tray = SystemTray(app=object())
    calls: list[str] = []
    tray._enabled = True
    tray._registered_with_watcher = True
    tray._schedule_retry = lambda: calls.append("retry")

    tray._on_watcher_name_owner_changed(None, None, None, None, None, Params())

    assert tray._registered_with_watcher is False
    assert calls == ["retry"]


def test_retry_setup_calls_setup_when_enabled():
    from halo_gtk import systray
    from halo_gtk.systray import SystemTray

    tray = SystemTray(app=object())
    calls: list[str] = []
    tray._enabled = True
    tray._retry_source_id = 123
    tray.setup = lambda: calls.append("setup") or True

    assert tray._retry_setup() == systray.GLib.SOURCE_REMOVE
    assert tray._retry_source_id is None
    assert calls == ["setup"]
