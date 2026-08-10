"""Window-level media lifecycle regression tests."""

from __future__ import annotations

from types import SimpleNamespace

from halo_gtk import window as window_module
from halo_gtk.window import RingWindow


def test_leaving_history_through_navigation_stops_playback():
    hidden = []
    refreshed = []
    page = SimpleNamespace(
        _active_page_name="history",
        _page_history=[],
        _history_page=SimpleNamespace(on_page_hidden=lambda: hidden.append(True)),
        _focused_live_page=SimpleNamespace(leave=lambda: None),
        _dashboard_cameras_page=SimpleNamespace(on_page_hidden=lambda: None),
        _cameras_page=SimpleNamespace(on_page_hidden=lambda: None),
        _devices_page=SimpleNamespace(refresh=lambda: refreshed.append(True)),
        _nav_rows={"devices": object()},
        _content_stack=SimpleNamespace(set_visible_child_name=lambda _name: None),
        _select_nav_row=lambda _name: None,
        _update_title=lambda _name: None,
        _update_back_button=lambda: None,
    )

    RingWindow._show_page(page, "devices")

    assert hidden == [True]
    assert refreshed == [True]


def test_opening_focused_live_from_history_stops_playback():
    hidden = []
    shown = []
    page = SimpleNamespace(
        _active_page_name="history",
        _page_history=[],
        _history_page=SimpleNamespace(on_page_hidden=lambda: hidden.append(True)),
        _focused_live_page=SimpleNamespace(
            leave=lambda: None,
            show_device=lambda device, **kwargs: shown.append((device, kwargs)),
        ),
        _dashboard_cameras_page=SimpleNamespace(deactivate_snapshot_updates=lambda: None),
        _cameras_page=SimpleNamespace(
            deactivate_snapshot_updates=lambda: None,
            _max_live_monitoring_streams=lambda: 4,
        ),
        _clear_nav_selection=lambda: None,
        _content_stack=SimpleNamespace(set_visible_child_name=lambda _name: None),
        _update_title=lambda _page, _name: None,
        _update_back_button=lambda: None,
    )
    device = SimpleNamespace(name="Camera")

    RingWindow._open_focused_live(page, device, "history")

    assert hidden == [True]
    assert shown == [(device, {"max_streams": 4, "initial_snapshot": None})]


def test_runtime_shutdown_stops_history_playback(monkeypatch):
    stopped = []
    manager = SimpleNamespace(stop_all=lambda: stopped.append("sessions"))
    monkeypatch.setattr(window_module, "get_live_session_manager", lambda: manager)
    page = SimpleNamespace(
        _focused_live_page=SimpleNamespace(leave=lambda: stopped.append("focused")),
        _dashboard_cameras_page=SimpleNamespace(
            deactivate_snapshot_updates=lambda: stopped.append("dashboard")
        ),
        _cameras_page=SimpleNamespace(
            deactivate_snapshot_updates=lambda: stopped.append("cameras"),
            stop_live_monitoring=lambda: stopped.append("monitoring"),
        ),
        _history_page=SimpleNamespace(on_page_hidden=lambda: stopped.append("history")),
    )

    RingWindow.stop_runtime_streams(page)

    assert stopped == [
        "focused",
        "dashboard",
        "cameras",
        "monitoring",
        "history",
        "sessions",
    ]
