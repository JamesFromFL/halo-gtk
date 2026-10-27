"""Notification preferences, literal text and account-bound view-only actions."""

from types import SimpleNamespace

import gi
import pytest

gi.require_version("Notify", "0.7")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Notify  # noqa: E402

from halo_gtk import intercom_notifications as ui  # noqa: E402
from halo_gtk import notifications, ring_client  # noqa: E402
from halo_gtk.intercom import IntercomObservation  # noqa: E402
from halo_gtk.ring_events import normalized_event_kind  # noqa: E402
from tests.test_intercom_details import Client, device  # noqa: E402


class Notification:
    def __init__(self, *values):
        self.values = values
        self.actions = []
        self.closed = False

    def set_app_name(self, *_):
        pass

    def set_timeout(self, *_):
        pass

    def add_action(self, *args):
        self.actions.append(args)

    def connect(self, *_):
        pass

    def show(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def context(monkeypatch):
    cfg, created = {"show_notifications": True, "notify_doorbell": True}, []
    monkeypatch.setattr(ui.config, "load", lambda: cfg)
    monkeypatch.setattr(notifications, "_ensure_notify", lambda: True)

    def new(*args):
        notification = Notification(*args)
        created.append(notification)
        return notification

    monkeypatch.setattr(Notify.Notification, "new", new)
    monkeypatch.setattr(ui, "light_group_monotonic_time", lambda: 100)
    ui._active.clear()
    yield cfg, created
    ui._active.clear()


@pytest.mark.parametrize("kind", ["ding", "intercom_unlock"])
def test_notification_only_opens_current_intercom_and_escapes_body(context, kind):
    _, created = context
    ui.send_intercom_notification(device(), IntercomObservation(kind, 1), 7)
    (notification,) = created
    assert "Entrance &lt;b&gt;literal&lt;/b&gt;" in notification.values[1]
    assert len(notification.actions) == 1 and notification.actions[0][:2] == (
        "view",
        "View Intercom",
    )
    assert "unlock" not in [action[0] for action in notification.actions]


@pytest.mark.parametrize("disabled", ["show_notifications", "notify_doorbell"])
def test_preferences_suppress_intercom_notifications(context, disabled):
    cfg, created = context
    cfg[disabled] = False
    ui.send_intercom_notification(device(), IntercomObservation("ding", 1), 7)
    assert not created


def test_retired_generation_cleanup_does_not_close_new_account_notifications(context):
    _, created = context
    ui.send_intercom_notification(device(), IntercomObservation("ding", 1), 7)
    ui.send_intercom_notification(device(), IntercomObservation("ding", 1), 8)
    ui.withdraw_intercom_notifications(7)
    assert created[0].closed and not created[1].closed
    assert len(ui._active) == 1


@pytest.mark.parametrize("change", ["account", "identity", "expired", "valid"])
def test_notification_navigation_is_fenced_and_never_requests_release(context, monkeypatch, change):
    client, calls = Client(), []
    monkeypatch.setattr(ring_client, "get_client", lambda: client)
    app = SimpleNamespace(
        activate=lambda: calls.append("activate"),
        get_active_window=lambda: SimpleNamespace(open_intercom=lambda *args: calls.append(args)),
    )
    monkeypatch.setattr(Gtk.Application, "get_default", lambda: app)
    target = (
        8 if change == "account" else 7,
        42,
        3 if change == "identity" else 2,
        99 if change == "expired" else 160,
    )
    ui._open_target(None, "view", target)
    assert calls == (["activate", (42, 2, 7)] if change == "valid" else [])
    assert not client.calls


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("com.ring.pn.live-event.intercom", "ding"),
        ("com.ring.pn.intercom.virtual.unlock", "intercom_unlock"),
        ("com.ring.push.INTERCOM_UNLOCK_FROM_APP", "intercom_unlock"),
    ],
)
def test_intercom_push_aliases_have_stable_names(raw, expected):
    assert normalized_event_kind(raw) == expected
