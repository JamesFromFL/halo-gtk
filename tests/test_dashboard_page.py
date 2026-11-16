"""Headless Overview tests with synthetic clients and inert widget doubles."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ring_doorbell.const import PUSH_ACTION_DING, PUSH_NOTIFICATION_MOTION

from halo_gtk import dashboard_page as ui
from halo_gtk.dashboard_page import DashboardPage
from tests.test_chime_details import Widget


class _Row(Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.activatable = kwargs.get("activatable", False)

    def activate(self):
        for signal, callback in self.signals:
            if signal == "activated":
                callback(self)


class _Rows(Widget):
    def append(self, child):
        self.children.append(child)

    def get_first_child(self):
        return self.children[0] if self.children else None


class _Client:
    is_authenticated = True

    def __init__(self, name="Front door"):
        self.all_devices = (
            SimpleNamespace(id=42, name=name),
            SimpleNamespace(id=43, name="Garage"),
        )
        self.snapshot = object()
        self.calls = []
        self.alarm_callbacks = []
        self.event_callbacks = []

    def add_alarm_callback(self, callback):
        self.alarm_callbacks.append(callback)

    def remove_alarm_callback(self, callback):
        self.alarm_callbacks.remove(callback)

    def add_event_callback(self, callback):
        self.event_callbacks.append(callback)

    def remove_event_callback(self, callback):
        self.event_callbacks.remove(callback)

    def get_alarm_snapshot(self):
        self.calls.append("alarm-cache")
        return self.snapshot

    def get_device_by_id(self, device_id):
        self.calls.append(("device-cache", device_id))
        return next((device for device in self.all_devices if device.id == device_id), None)


class _Page:
    def __init__(self):
        self._client = None
        self._account_client = None
        self._callback_generation = 0
        self._alarm_callback = None
        self._event_callback = None
        self._active = False
        self._events = []
        self._events_list = _Rows()
        self._subtitle = Widget()
        self.shown_security = []
        self.grid_refreshes = []
        self.navigation = []
        self.opened_events = []
        self._on_navigate = self.navigation.append
        self._on_open_event = lambda *args: self.opened_events.append(args)
        self._camera_grid = SimpleNamespace(refresh=lambda: self.grid_refreshes.append(True))

    def _show_security(self, snapshot, signed_in):
        self.shown_security.append((snapshot, signed_in))


for _method in (
    "_bind_client",
    "refresh",
    "_on_alarm",
    "_on_event",
    "_render_events",
    "_open_event",
    "_navigate",
    "on_page_hidden",
):
    setattr(_Page, _method, getattr(DashboardPage, _method))


@pytest.fixture
def context(monkeypatch):
    state = SimpleNamespace(client=_Client(), page=_Page())
    monkeypatch.setattr(ui, "get_client", lambda: state.client)
    monkeypatch.setattr(ui.Adw, "ActionRow", _Row)
    monkeypatch.setattr(ui.Gtk, "Image", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(ui.device_names, "display_name", lambda device: device.name)
    now = datetime(2026, 11, 16, 14, 41, tzinfo=UTC)
    monkeypatch.setattr(ui, "datetime", SimpleNamespace(now=lambda: now))
    state.received_time = now.astimezone().strftime("%H:%M")
    return state


def _event(event_id=100, *, device_id=42, kind="motion"):
    return {"id": event_id, "doorbot_id": device_id, "kind": kind}


def test_refresh_reads_caches_and_binds_callbacks_once(context):
    page, client = context.page, context.client

    page.refresh()
    page.refresh()

    assert page._active
    assert len(client.alarm_callbacks) == len(client.event_callbacks) == 1
    assert client.calls == ["alarm-cache", "alarm-cache"]
    assert page.shown_security == [(client.snapshot, True)] * 2
    assert page.grid_refreshes == [True, True]
    assert page._subtitle.label.startswith("2 Ring devices")
    assert page._events == []


def test_hiding_unsubscribes_idempotently_and_showing_keeps_same_account_activity(context):
    page, client = context.page, context.client
    page.refresh()
    client.event_callbacks[0](_event())
    retained = list(page._events)

    page.on_page_hidden()
    page.on_page_hidden()

    assert not page._active
    assert client.alarm_callbacks == client.event_callbacks == []
    page.refresh()
    assert page._events == retained
    assert len(client.alarm_callbacks) == len(client.event_callbacks) == 1


@pytest.mark.parametrize("change", ["replace", "logout", "unauthenticated"])
def test_account_change_clears_old_activity_and_releases_old_callbacks(context, change):
    page, previous = context.page, context.client
    page.refresh()
    previous.event_callbacks[0](_event())
    assert page._events

    if change == "replace":
        context.client = _Client("New account camera")
    elif change == "logout":
        context.client = None
    else:
        previous.is_authenticated = False
    page.refresh()

    assert page._events == []
    assert previous.alarm_callbacks == previous.event_callbacks == []
    assert len(page._events_list.children) == 1
    assert page._events_list.children[0].title == "No new activity in this view"
    expected = (context.client.snapshot, True) if change == "replace" else (None, False)
    assert page.shown_security[-1] == expected


@pytest.mark.parametrize("retirement", ["hidden", "reshown", "replaced", "logout"])
def test_retired_callbacks_cannot_change_the_current_view(context, retirement):
    page, previous = context.page, context.client
    page.refresh()
    old_alarm = previous.alarm_callbacks[0]
    old_event = previous.event_callbacks[0]
    if retirement in {"hidden", "reshown"}:
        page.on_page_hidden()
        if retirement == "reshown":
            page.refresh()
    else:
        context.client = _Client("Another account") if retirement == "replaced" else None
        page.refresh()
    shown = list(page.shown_security)
    events = list(page._events)

    old_alarm(object())
    old_event(_event())

    assert page.shown_security == shown
    assert page._events == events


def test_account_replaced_before_refresh_rejects_current_page_callbacks(context):
    page, previous = context.page, context.client
    page.refresh()
    shown = list(page.shown_security)
    context.client = _Client()

    previous.alarm_callbacks[0](object())
    previous.event_callbacks[0](_event())

    assert page.shown_security == shown
    assert page._events == []
    assert previous.calls == ["alarm-cache"]
    assert context.client.calls == []


def test_signed_out_view_rejects_callbacks_when_both_client_references_are_none(context):
    page = context.page
    context.client = None
    page.refresh()
    shown = list(page.shown_security)

    page._on_alarm(object())
    page._on_event(_event())

    assert page.shown_security == shown == [(None, False)]
    assert page._events == []


def test_active_alarm_callback_updates_only_the_security_summary(context):
    page, client = context.page, context.client
    page.refresh()
    updated = object()

    client.alarm_callbacks[0](updated)

    assert page.shown_security[-1] == (updated, True)
    assert page.grid_refreshes == [True]
    assert client.calls == ["alarm-cache"]


@pytest.mark.parametrize(
    ("kind", "title"),
    [
        ("motion", "Motion detected"),
        ("DING", "Doorbell rang"),
        (PUSH_ACTION_DING, "Doorbell rang"),
        (PUSH_NOTIFICATION_MOTION, "Motion detected"),
    ],
)
@pytest.mark.parametrize("mapping", [True, False])
def test_event_names_and_ids_normalize_from_mapping_or_sdk_object(context, kind, title, mapping):
    page, client = context.page, context.client
    page.refresh()
    event = _event("123", device_id="42", kind=kind)
    if not mapping:
        event = SimpleNamespace(**event)

    client.event_callbacks[0](event)

    assert page._events == [(42, 123, title, "Front door", context.received_time)]
    row = page._events_list.children[0]
    assert row.title == title
    assert row.subtitle == f"Front door · Received at {context.received_time}"


@pytest.mark.parametrize(
    "event",
    [
        _event(kind="intercom_unlock"),
        _event(kind="unknown"),
        _event(device_id=None),
        _event(device_id="invalid"),
        _event(device_id=999),
    ],
)
def test_unrelated_or_unresolvable_events_do_not_create_activity(context, event):
    page, client = context.page, context.client
    page.refresh()

    client.event_callbacks[0](event)

    assert page._events == []
    assert page._events_list.children[0].title == "No new activity in this view"


def test_event_deduplication_is_scoped_to_device_and_reorders_newest_first(context):
    page, client = context.page, context.client
    page.refresh()
    for event in (_event(1), _event(1, device_id=43), _event(2), _event(1, kind="ding")):
        client.event_callbacks[0](event)

    assert [(item[0], item[1]) for item in page._events] == [(42, 1), (42, 2), (43, 1)]
    assert page._events[0][2] == "Doorbell rang"
    assert len(page._events_list.children) == 3


def test_recent_activity_remains_bounded_without_history_fetches(context):
    page, client = context.page, context.client
    page.refresh()
    for event_id in range(20):
        client.event_callbacks[0](_event(event_id))

    assert [item[1] for item in page._events] == [19, 18, 17]
    assert len(page._events_list.children) == 3
    assert client.calls == ["alarm-cache", *[("device-cache", 42)] * 20]


def test_activity_rows_bind_their_own_device_and_event_targets(context):
    page, client = context.page, context.client
    page.refresh()
    client.event_callbacks[0](_event(1))
    client.event_callbacks[0](_event(2, device_id=43))

    for row in page._events_list.children:
        row.activate()

    assert page.opened_events == [(43, 2), (42, 1)]
    assert page.navigation == []


@pytest.mark.parametrize("event_id", [None, "invalid"])
def test_unavailable_event_id_keeps_activity_but_routes_to_full_history(context, event_id):
    page, client = context.page, context.client
    page.refresh()
    client.event_callbacks[0](_event(event_id))

    page._events_list.children[0].activate()

    assert page._events[0][1] is None
    assert page.navigation == ["history"]
    assert page.opened_events == []


def test_missing_event_opener_falls_back_to_activity(context):
    page = context.page
    page._on_open_event = None

    page._open_event(42, 123)

    assert page.navigation == ["history"]


def test_empty_activity_row_opens_full_history(context):
    page = context.page
    page.refresh()

    page._events_list.children[0].activate()

    assert page.navigation == ["history"]
    assert page.opened_events == []


def test_activity_device_names_render_as_literal_text(context):
    page, client = context.page, context.client
    client.all_devices[0].name = "<b>Front & rear</b>"
    page.refresh()
    client.event_callbacks[0](_event())

    row = page._events_list.children[0]
    assert row.subtitle == f"<b>Front & rear</b> · Received at {context.received_time}"
    assert row.use_markup is False
