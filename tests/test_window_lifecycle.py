"""Window-level media lifecycle regression tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_primary_navigation_from_focused_live_keeps_real_source_in_history():
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


def test_security_navigation_retires_overview_and_refreshes_security():
    calls = []

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
        _dashboard_page=SimpleNamespace(
            on_page_hidden=lambda: calls.append(("hide", "overview-summary"))
        ),
        _select_nav_row=lambda name: calls.append(("select", name)),
        _update_title=lambda name: calls.append(("title", name)),
        _update_back_button=lambda: calls.append(("back", True)),
    )

    RingWindow._show_page(page, "alarm")

    assert page._active_page_name == "alarm"
    assert page._page_history == ["dashboard"]
    assert calls == [
        ("hide", "dashboard"),
        ("hide", "overview-summary"),
        ("select", "alarm"),
        ("page", "alarm"),
        ("title", "alarm"),
        ("refresh", "alarm"),
        ("back", True),
    ]


def test_devices_navigation_uses_visible_entry_and_retires_on_exit():
    calls = []
    page = SimpleNamespace(
        _active_page_name="devices",
        _page_history=[],
        _devices_page=SimpleNamespace(
            refresh=lambda: calls.append("cached"),
            on_page_shown=lambda: calls.append("shown"),
            on_page_hidden=lambda: calls.append("hidden"),
        ),
        _alarm_page=SimpleNamespace(refresh=lambda: calls.append("alarm")),
        _nav_rows={"devices": object(), "alarm": object()},
        _content_stack=SimpleNamespace(set_visible_child_name=lambda _: None),
        _select_nav_row=lambda _: None,
        _update_title=lambda _: None,
        _update_back_button=lambda: None,
    )
    RingWindow._show_page(page, "devices")
    RingWindow._show_page(page, "alarm")
    assert calls == ["shown", "hidden", "alarm"]


def test_window_teardown_retires_chime_dialogs_and_alarm_view(monkeypatch):
    calls = []
    monkeypatch.setattr(
        window_module, "get_live_session_manager", lambda: SimpleNamespace(stop_all=lambda: None)
    )
    page = SimpleNamespace(
        _focused_live_page=SimpleNamespace(leave=lambda: None),
        _dashboard_cameras_page=SimpleNamespace(deactivate_snapshot_updates=lambda: None),
        _cameras_page=SimpleNamespace(
            deactivate_snapshot_updates=lambda: None, stop_live_monitoring=lambda: None
        ),
        _history_page=SimpleNamespace(on_page_hidden=lambda: None),
        _devices_page=SimpleNamespace(on_page_hidden=lambda: calls.append("devices")),
        _alarm_page=SimpleNamespace(on_page_hidden=lambda: calls.append("alarm")),
    )
    RingWindow.stop_runtime_streams(page)
    assert calls == ["devices", "alarm"]


def test_compact_navigation_is_hidden_while_focused():
    states = []
    page = SimpleNamespace(
        _compact=True,
        _active_page_name="focused_live",
        _header_stack=SimpleNamespace(set_visible_child_name=lambda name: states.append(name)),
        _bottom_navigation=SimpleNamespace(set_reveal=lambda value: states.append(value)),
        _brand=SimpleNamespace(set_visible=lambda value: states.append(value)),
    )
    RingWindow._sync_navigation(page)
    assert states == ["title", False, False]
    states.clear()
    page._active_page_name = "cameras"
    RingWindow._sync_navigation(page)
    assert states == ["title", True, False]
    states.clear()
    page._compact = False
    RingWindow._sync_navigation(page)
    assert states == ["navigation", False, True]


def test_view_switcher_transition_invokes_lifecycle_only_for_new_page():
    shown = []
    page = SimpleNamespace(_active_page_name="dashboard", _show_page=shown.append)
    RingWindow._on_view_changed(
        page, SimpleNamespace(get_visible_child_name=lambda: "dashboard"), None
    )
    RingWindow._on_view_changed(
        page, SimpleNamespace(get_visible_child_name=lambda: "history"), None
    )
    assert shown == ["history"]


def test_activity_deep_link_retires_device_dialogs():
    calls = []
    page = SimpleNamespace(
        _active_page_name="devices",
        _page_history=[],
        _devices_page=SimpleNamespace(on_page_hidden=lambda: calls.append("hide-devices")),
        _select_nav_row=lambda name: calls.append(name),
        _content_stack=SimpleNamespace(set_visible_child_name=lambda name: None),
        _update_title=lambda name: None,
        _update_back_button=lambda: None,
    )
    RingWindow._enter_history_page(page, lambda: calls.append("load-events"))
    assert calls == ["hide-devices", "history", "load-events"]
    assert page._page_history == ["devices"]


def test_appearance_reset_updates_theme_and_menu_without_saving(monkeypatch):
    calls = []
    monkeypatch.setattr(window_module.config, "load", lambda: {"appearance": "system"})
    monkeypatch.setattr(window_module.config, "save", lambda _: pytest.fail("Unexpected save"))
    choice = SimpleNamespace(get_active=lambda: True)
    page = SimpleNamespace(_apply_appearance=lambda value: calls.append(("theme", value)))

    def select(active):
        calls.append(("selected", active))
        RingWindow._on_appearance_changed(page, choice, "system")

    page._appearance_choices = {"system": SimpleNamespace(set_active=select)}
    RingWindow._sync_appearance(page)
    assert calls == [("selected", True), ("theme", "system")]
    assert not page._syncing_appearance


def test_selecting_saved_appearance_still_applies_it(monkeypatch):
    applied = []
    monkeypatch.setattr(window_module.config, "load", lambda: {"appearance": "system"})
    monkeypatch.setattr(window_module.config, "save", lambda _: pytest.fail("Unexpected save"))
    page = SimpleNamespace(_apply_appearance=applied.append)
    RingWindow._on_appearance_changed(page, SimpleNamespace(get_active=lambda: True), "system")
    assert applied == ["system"]


def test_failed_appearance_save_restores_prior_choice_and_theme(monkeypatch):
    applied = []
    selected = []
    notices = []
    monkeypatch.setattr(window_module.config, "load", lambda: {"appearance": "light"})

    def fail_save(_):
        raise OSError("disk full")

    monkeypatch.setattr(window_module.config, "save", fail_save)
    page = SimpleNamespace(_apply_appearance=applied.append, show_toast=notices.append)

    def restore(active):
        selected.append(active)
        RingWindow._on_appearance_changed(page, SimpleNamespace(get_active=lambda: active), "light")

    page._appearance_choices = {"light": SimpleNamespace(set_active=restore)}
    RingWindow._on_appearance_changed(page, SimpleNamespace(get_active=lambda: True), "dark")
    assert selected == [True]
    assert applied == ["light"]
    assert notices == ["Could not save the appearance setting."]


def test_reopening_hidden_overview_resumes_observation_once():
    refreshed = []
    page = SimpleNamespace(
        _resume_after_hide=True,
        _active_page_name="dashboard",
        refresh=lambda: refreshed.append(True),
    )
    RingWindow._on_map(page)
    RingWindow._on_map(page)
    assert refreshed == [True]
    assert not page._resume_after_hide


def test_reopening_hidden_focused_player_returns_to_source_before_refresh():
    calls = []
    page = SimpleNamespace(
        _resume_after_hide=True,
        _active_page_name="focused_live",
        _on_back_clicked=lambda: calls.append("back"),
        refresh=lambda: calls.append("refresh"),
    )
    RingWindow._on_map(page)
    assert calls == ["back"]


@pytest.mark.parametrize("source", ["devices", "alarm"])
def test_camera_notification_retires_device_or_security_page(source):
    hidden = []
    page = SimpleNamespace(
        _active_page_name=source,
        _page_history=[],
        _devices_page=SimpleNamespace(on_page_hidden=lambda: hidden.append("devices")),
        _alarm_page=SimpleNamespace(on_page_hidden=lambda: hidden.append("alarm")),
        _focused_live_page=SimpleNamespace(show_device=lambda *_args, **_kwargs: None),
        _cameras_page=SimpleNamespace(_max_live_monitoring_streams=lambda: 4),
        _clear_nav_selection=lambda: None,
        _update_title=lambda *_: None,
        _update_back_button=lambda: None,
    )
    RingWindow._open_focused_live(page, SimpleNamespace(name="Camera"), source)
    assert hidden == [source]
    assert page._page_history == [source]


def test_hidden_refresh_waits_for_map_and_restores_only_visible_activity(monkeypatch):
    calls = []
    mapped = False
    page = SimpleNamespace(
        get_mapped=lambda: mapped,
        _resume_after_hide=False,
        _active_page_name="history",
        _banner=SimpleNamespace(set_revealed=lambda _: None),
        _sync_appearance=lambda: None,
        _update_events_banner=lambda _: None,
        _cameras_page=SimpleNamespace(
            enforce_live_monitoring_limits=lambda: calls.append("limits")
        ),
        _history_page=SimpleNamespace(refresh=lambda: calls.append("history")),
    )
    monkeypatch.setattr(window_module, "get_client", lambda: SimpleNamespace(is_authenticated=True))
    page.refresh = lambda: RingWindow.refresh(page)
    page.refresh()
    page.refresh()
    assert page._resume_after_hide and calls == []
    mapped = True
    RingWindow._on_map(page)
    RingWindow._on_map(page)
    assert calls == ["limits", "history"]


def test_close_before_restore_does_not_reopen_camera_work(monkeypatch):
    from halo_gtk.app import RingApplication

    calls = []
    mapped = True

    def visible(value):
        nonlocal mapped
        mapped = value

    page = SimpleNamespace(
        _active_page_name="cameras",
        _resume_after_hide=False,
        get_root=lambda: page,
        get_mapped=lambda: mapped,
        get_application=lambda: app,
        set_visible=visible,
        stop_runtime_streams=lambda: calls.append("stop"),
        _sync_appearance=lambda: None,
        _update_events_banner=lambda _: None,
        _banner=SimpleNamespace(set_revealed=lambda _: None),
        _cameras_page=SimpleNamespace(refresh=lambda: calls.append("camera work")),
    )
    app = SimpleNamespace(
        should_hide_on_close=lambda: True, get_main_window=lambda: page, _quitting=False
    )
    monkeypatch.setattr(window_module, "get_client", lambda: SimpleNamespace(is_authenticated=True))
    page.refresh = lambda: RingWindow.refresh(page)
    assert RingWindow._on_close_request(page)
    RingApplication._finish_session_restore(app, True)
    page.refresh()
    assert calls == ["stop"]
    visible(True)
    RingWindow._on_map(page)
    assert calls == ["stop", "camera work"]
