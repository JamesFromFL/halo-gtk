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


def test_ribbon_navigation_from_focused_live_keeps_real_source_in_history():
    calls = []
    page = SimpleNamespace(
        _active_page_name="focused_live",
        _focused_source_page="cameras",
        _page_history=["cameras"],
        _focused_live_page=SimpleNamespace(leave=lambda: calls.append("leave-focused")),
        _dashboard_cameras_page=SimpleNamespace(on_page_hidden=lambda: calls.append("dashboard")),
        _cameras_page=SimpleNamespace(
            on_page_hidden=lambda: calls.append("hide-monitoring"),
            reattach_live_monitoring_sessions=lambda: calls.append("reattach-monitoring"),
        ),
        _history_page=SimpleNamespace(on_page_hidden=lambda: None),
        _devices_page=SimpleNamespace(refresh=lambda: calls.append("refresh-devices")),
        _nav_rows={"devices": object()},
        _content_stack=SimpleNamespace(set_visible_child_name=lambda name: calls.append(name)),
        _select_nav_row=lambda name: calls.append(f"select-{name}"),
        _update_title=lambda name: calls.append(f"title-{name}"),
        _update_back_button=lambda: None,
    )
    page._leave_focused_live = lambda destination: RingWindow._leave_focused_live(page, destination)

    RingWindow._show_page(page, "devices")

    assert page._page_history == ["cameras"]
    assert page._focused_source_page is None
    assert calls[:2] == ["leave-focused", "hide-monitoring"]
    assert "reattach-monitoring" not in calls


def test_back_from_focused_live_reattaches_monitoring_without_hiding_it():
    calls = []
    page = SimpleNamespace(
        _active_page_name="focused_live",
        _focused_source_page="cameras",
        _page_history=["cameras"],
        _focused_live_page=SimpleNamespace(leave=lambda: calls.append("leave-focused")),
        _dashboard_cameras_page=SimpleNamespace(on_page_hidden=lambda: calls.append("dashboard")),
        _cameras_page=SimpleNamespace(
            on_page_hidden=lambda: calls.append("hide-monitoring"),
            reattach_live_monitoring_sessions=lambda: calls.append("reattach-monitoring"),
            refresh=lambda: calls.append("refresh-monitoring"),
            resume_after_focused=lambda: calls.append("resume-monitoring"),
        ),
        _history_page=SimpleNamespace(on_page_hidden=lambda: None),
        _nav_rows={"cameras": object()},
        _content_stack=SimpleNamespace(set_visible_child_name=lambda name: calls.append(name)),
        _select_nav_row=lambda name: calls.append(f"select-{name}"),
        _update_title=lambda name: calls.append(f"title-{name}"),
        _update_back_button=lambda: None,
    )
    page._leave_focused_live = lambda destination: RingWindow._leave_focused_live(page, destination)
    page._show_page = lambda name, **kwargs: RingWindow._show_page(page, name, **kwargs)

    RingWindow._on_back_clicked(page)

    assert page._page_history == []
    assert calls[:2] == ["leave-focused", "reattach-monitoring"]
    assert "hide-monitoring" not in calls
    assert "refresh-monitoring" not in calls
    assert "resume-monitoring" in calls


def test_dashboard_focus_to_monitoring_performs_initial_monitoring_refresh():
    calls = []
    page = SimpleNamespace(
        _active_page_name="focused_live",
        _focused_source_page="dashboard",
        _page_history=["dashboard"],
        _focused_live_page=SimpleNamespace(leave=lambda: calls.append("leave-focused")),
        _dashboard_cameras_page=SimpleNamespace(on_page_hidden=lambda: calls.append("dashboard")),
        _cameras_page=SimpleNamespace(
            on_page_hidden=lambda: calls.append("hide-monitoring"),
            reattach_live_monitoring_sessions=lambda: calls.append("reattach-monitoring"),
            refresh=lambda: calls.append("refresh-monitoring"),
            resume_after_focused=lambda: calls.append("resume-monitoring"),
        ),
        _history_page=SimpleNamespace(on_page_hidden=lambda: None),
        _nav_rows={"cameras": object()},
        _content_stack=SimpleNamespace(set_visible_child_name=lambda name: calls.append(name)),
        _select_nav_row=lambda name: calls.append(f"select-{name}"),
        _update_title=lambda name: calls.append(f"title-{name}"),
        _update_back_button=lambda: None,
    )
    page._leave_focused_live = lambda destination: RingWindow._leave_focused_live(page, destination)

    RingWindow._show_page(page, "cameras")

    assert "refresh-monitoring" in calls
    assert "resume-monitoring" not in calls
    assert "reattach-monitoring" not in calls
    assert "dashboard" in calls


def test_hidden_history_title_does_not_replace_active_page_title():
    titles = []
    page = SimpleNamespace(
        _active_page_name="dashboard",
        _update_title=lambda *args: titles.append(args),
    )

    RingWindow._on_history_title_change(page, None)
    page._active_page_name = "history"
    RingWindow._on_history_title_change(page, "Front Door")

    assert titles == [("history", "Front Door")]


def test_alarm_navigation_refreshes_and_closes_compact_sidebar():
    calls = []

    class SplitView:
        @staticmethod
        def get_collapsed():
            return True

        @staticmethod
        def set_show_sidebar(visible):
            calls.append(("sidebar", visible))

    page = SimpleNamespace(
        _active_page_name="dashboard",
        _page_history=[],
        _dashboard_cameras_page=SimpleNamespace(
            on_page_hidden=lambda: calls.append(("hide", "dashboard"))
        ),
        _cameras_page=SimpleNamespace(on_page_hidden=lambda: None),
        _history_page=SimpleNamespace(on_page_hidden=lambda: None),
        _focused_live_page=SimpleNamespace(leave=lambda: None),
        _alarm_page=SimpleNamespace(refresh=lambda: calls.append(("refresh", "alarm"))),
        _nav_rows={"alarm": object()},
        _content_stack=SimpleNamespace(
            set_visible_child_name=lambda name: calls.append(("page", name))
        ),
        _split_view=SplitView(),
        _select_nav_row=lambda name: calls.append(("select", name)),
        _update_title=lambda name: calls.append(("title", name)),
        _update_back_button=lambda: calls.append(("back", True)),
    )

    RingWindow._show_page(page, "alarm")

    assert page._active_page_name == "alarm"
    assert page._page_history == ["dashboard"]
    assert calls == [
        ("hide", "dashboard"),
        ("select", "alarm"),
        ("page", "alarm"),
        ("title", "alarm"),
        ("refresh", "alarm"),
        ("sidebar", False),
        ("back", True),
    ]
