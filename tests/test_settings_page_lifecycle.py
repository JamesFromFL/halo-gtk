"""Focused lifecycle tests for Ring account actions in Settings."""

from ring_doorbell import AuthenticationError

from halo_gtk import settings_page
from halo_gtk.settings_page import SettingsPage


class _ImmediateThread:
    def __init__(self, *, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def test_explicit_logout_stops_runtime_before_client_teardown(monkeypatch):
    order = []
    page = SettingsPage.__new__(SettingsPage)
    page._set_account_buttons_sensitive = lambda _sensitive: None
    page._set_account_message = lambda _message: None
    page._stop_main_window_runtime_streams = lambda: order.append("stop-runtime")
    page._logout_worker = lambda: order.append("logout-client")
    monkeypatch.setattr(settings_page.threading, "Thread", _ImmediateThread)

    page._on_logout_clicked()

    assert order == ["stop-runtime", "logout-client"]


def test_rejected_refresh_stops_runtime_before_expected_client_logout(monkeypatch):
    order = []

    class RejectedClient:
        def refresh_session_token(self):
            order.append("refresh")
            raise AuthenticationError("expired")

    client = RejectedClient()
    page = SettingsPage.__new__(SettingsPage)
    page._stop_main_window_runtime_streams = lambda: order.append("stop-runtime")
    page._finish_rejected_session_logout = lambda logged_out, error: (
        order.append(("finish", logged_out, error)) or False
    )

    def logout_client(*, expected_client=None):
        assert expected_client is client
        order.append("logout-client")
        return True

    monkeypatch.setattr(settings_page, "get_client", lambda: client)
    monkeypatch.setattr(settings_page, "logout_client", logout_client)
    monkeypatch.setattr(settings_page.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        settings_page.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )

    page._refresh_session(client)

    assert order == [
        "refresh",
        "stop-runtime",
        "logout-client",
        ("finish", True, None),
    ]


def test_rejected_refresh_does_not_logout_a_replacement_client(monkeypatch):
    old_client = object()
    replacement = object()
    page = SettingsPage.__new__(SettingsPage)
    finishes = []
    page._finish_refresh_session = lambda success, message, prompt: (
        finishes.append((success, message, prompt)) or False
    )
    page._stop_main_window_runtime_streams = lambda: (_ for _ in ()).throw(
        AssertionError("replacement session runtime must remain active")
    )
    monkeypatch.setattr(settings_page, "get_client", lambda: replacement)

    assert page._begin_rejected_session_logout(old_client) is False
    assert finishes == [
        (
            False,
            "The Ring session changed while it was being refreshed.",
            False,
        )
    ]


def test_grid_density_selection_is_persisted_and_refreshes_the_main_window(monkeypatch):
    page = SettingsPage.__new__(SettingsPage)
    page._refreshing = False
    refreshed = []
    saved = []
    page._refresh_main_window = lambda: refreshed.append(True)
    monkeypatch.setattr(settings_page._config, "load", lambda: {"show_notifications": True})
    monkeypatch.setattr(settings_page._config, "save", lambda cfg: saved.append(cfg))

    class Combo:
        @staticmethod
        def get_selected():
            return 1

    page._on_grid_density_selected(Combo(), None)

    assert saved == [
        {
            "show_notifications": True,
            "camera_grid_density_preset": "dense",
        }
    ]
    assert refreshed == [True]


def test_refreshing_six_stream_switch_does_not_refresh_main_window():
    page = SettingsPage.__new__(SettingsPage)
    page._refreshing = True
    refreshed = []
    page._save_bool_setting = lambda *_args: refreshed.append("saved")
    page._refresh_main_window = lambda: refreshed.append("refreshed")

    page._on_live_monitoring_allow_six_toggled(object(), None)

    assert refreshed == []
