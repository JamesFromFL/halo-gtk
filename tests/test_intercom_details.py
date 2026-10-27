"""Production GTK handlers with synthetic widgets and deterministic scheduler."""

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from halo_gtk import devices_page
from halo_gtk import intercom_details as ui
from halo_gtk.intercom import (
    IntercomAccountSnapshot,
    IntercomOperation,
    IntercomOperationStatus,
    IntercomReadStatus,
    IntercomSnapshot,
)
from tests.test_chime_details import Client as BaseClient
from tests.test_chime_details import Scheduler, Widget
from tests.test_devices_page import PageHarness as BasePage


def device(**changes):
    return IntercomSnapshot(
        **(
            {
                "api_id": 42,
                "name": "Entrance <b>literal</b>",
                "location_id": "home",
                "kind": "intercom_handset_audio",
                "revision": 4,
                "identity_epoch": 2,
                "connection": "online",
                "battery": 0,
                "ring_to_open": True,
                "stale": False,
                "can_write": True,
                "inventory_monotonic": 100,
            }
            | changes
        )
    )


def snapshot(value=None, **changes):
    return IntercomAccountSnapshot(
        **(
            {
                "generation": 7,
                "devices": (value or device(),),
                "complete": True,
                "has_inventory": True,
                "status": IntercomReadStatus.READY,
            }
            | changes
        )
    )


class Client(BaseClient):
    def __init__(self):
        super().__init__()
        self.intercom_snapshot = snapshot()
        self.intercom_callbacks = []

    def get_intercom_snapshot(self):
        return self.intercom_snapshot

    def add_intercom_callback(self, callback):
        self.intercom_callbacks.append(callback)

    def remove_intercom_callback(self, callback):
        if callback in self.intercom_callbacks:
            self.intercom_callbacks.remove(callback)

    def cancel_intercom_intent(self, intent):
        intent.cancel()

    def refresh_intercoms(self, *, force=False):
        return self._request("intercom-inventory", force=force)

    def request_intercom_unlock(self, api_id, **options):
        return self._request("unlock", api_id, **options)

    def acknowledge_intercom_request(self, api_id, operation_id):
        return self._request("ack", api_id, operation_id)


class Alert:
    def __init__(self, **kwargs):
        self.body = kwargs["body"]
        self.closed = False
        self.responses = []
        self.default = self.close = None

    def set_body_use_markup(self, value):
        self.use_markup = value

    def add_response(self, *args):
        self.responses.append(args)

    def set_response_appearance(self, *_):
        pass

    def set_default_response(self, value):
        self.default = value

    def set_close_response(self, value):
        self.close = value

    def connect(self, *_):
        pass

    def present(self, *_):
        pass

    def force_close(self):
        self.closed = True


class Details:
    def __init__(self, context):
        self._api_id, self._epoch = 42, 2
        self._closed = False
        self._device = context.client.intercom_snapshot.find_device(42)
        self._approval = self._confirmation = self._poll_id = None
        self._event_rows = []
        self._rows = {
            key: Widget()
            for key in (
                "model",
                "connection",
                "battery",
                "firmware",
                "ring_to_open",
                "received",
                "door",
            )
        }
        for name in (
            "_name",
            "_reason",
            "_operation",
            "_message",
            "_release",
            "_ack",
            "_reload_button",
            "_events",
        ):
            setattr(self, name, Widget())
        self._session = ui.IntercomViewSession(
            self._changed, self.retire, provider=lambda: context.current[0]
        )
        self._session.open(context.client)
        self.forced_closed = False

    def force_close(self):
        self.forced_closed = True


for name in (
    "_selected",
    "_cancel_confirmation",
    "retire",
    "_changed",
    "_arm_poll",
    "_reload",
    "_confirm",
    "_confirmed",
    "_acknowledge",
):
    setattr(Details, name, getattr(ui.IntercomDetails, name))
Details._poll = staticmethod(ui.IntercomDetails._poll)


@pytest.fixture
def context(monkeypatch):
    client, scheduler, now = Client(), Scheduler(), [100.0]
    result = SimpleNamespace(client=client, current=[client], scheduler=scheduler, now=now)
    monkeypatch.setattr(ui.GLib, "idle_add", scheduler.idle_add)
    monkeypatch.setattr(ui.GLib, "timeout_add", scheduler.timeout_add)
    monkeypatch.setattr(ui.GLib, "source_remove", scheduler.source_remove)
    monkeypatch.setattr(ui, "light_group_monotonic_time", lambda: now[0])
    monkeypatch.setattr(ui.Adw, "AlertDialog", Alert)
    monkeypatch.setattr(ui.Adw, "ActionRow", Widget)
    monkeypatch.setattr(ui, "get_client", lambda: result.current[0])
    monkeypatch.setattr(devices_page, "get_client", lambda: result.current[0])
    monkeypatch.setattr(devices_page.Gtk, "Image", Widget)
    return result


def shown(context):
    dialog = Details(context)
    context.scheduler.drain()
    return dialog


def test_opening_reads_and_confirmation_cancellation_never_release(context):
    dialog = shown(context)
    dialog._reload(force=True)
    dialog._confirm()
    alert, intent = dialog._confirmation, dialog._approval
    assert alert.default == alert.close == "cancel" and alert.use_markup is False
    assert "Entrance <b>literal</b>" in alert.body
    assert not [c for c in context.client.calls if c[0] == "unlock"]
    dialog._confirmed(alert, "cancel", intent)
    assert intent.cancelled and not [c for c in context.client.calls if c[0] == "unlock"]


def test_confirmed_release_captures_exact_revision_and_does_not_queue_double_click(context):
    dialog = shown(context)
    dialog._confirm()
    alert, intent = dialog._confirmation, dialog._approval
    dialog._confirmed(alert, "release", intent)
    calls = [c for c in context.client.calls if c[0] == "unlock"]
    assert len(calls) == 1 and calls[0][1] == (42,)
    assert calls[0][2] == {"expected_revision": 4, "intent_id": intent}
    dialog._confirm()
    assert dialog._confirmation is None
    dialog._confirmed(alert, "release", intent)
    assert len([c for c in context.client.calls if c[0] == "unlock"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"denied": True, "can_write": False},
        {"stale": True},
        {"acknowledgment_id": 1},
        {"can_write": False},
        {"inventory_monotonic": 60},
    ],
)
def test_unavailable_states_cannot_create_confirmation(context, change):
    context.client.intercom_snapshot = snapshot(device(**change))
    dialog = shown(context)
    dialog._confirm()
    assert dialog._confirmation is None and not context.client.calls


@pytest.mark.parametrize("action", ["expiry", "revision", "account", "identity"])
def test_delayed_confirmation_cannot_release_changed_target(context, action):
    dialog = shown(context)
    dialog._confirm()
    alert, intent = dialog._confirmation, dialog._approval
    if action == "expiry":
        context.now[0] += 31
    elif action == "revision":
        context.client.intercom_snapshot = snapshot(device(revision=5))
    elif action == "identity":
        context.client.intercom_snapshot = snapshot(device(identity_epoch=3))
    else:
        context.current[0] = None
    dialog._confirmed(alert, "release", intent)
    assert intent.cancelled and not [c for c in context.client.calls if c[0] == "unlock"]


def test_retire_cancels_unsent_approval_and_reads_but_keeps_command_owned(context):
    dialog = shown(context)
    dialog._reload(force=True)
    read = context.client.futures[-1]
    dialog._confirm()
    alert, intent = dialog._confirmation, dialog._approval
    dialog._confirmed(alert, "release", intent)
    command = context.client.futures[-1]
    dialog.retire()
    assert intent.cancelled and read.cancelled() and not command.cancelled()
    assert dialog._name.label == "Intercom unavailable" and not context.client.intercom_callbacks
    assert not context.scheduler.sources and dialog._closed


def test_render_keeps_zero_unknown_door_and_uncertain_result_distinct(context):
    operation = IntercomOperation(3, IntercomOperationStatus.UNKNOWN, False, True)
    context.client.intercom_snapshot = snapshot(device(operation=operation, acknowledgment_id=3))
    dialog = shown(context)
    assert dialog._rows["battery"].subtitle == "Reported 0%"
    assert dialog._rows["door"].subtitle == "Unknown; not reported"
    assert "Outcome unknown" in dialog._operation.label and dialog._ack.visible
    dialog._acknowledge()
    assert context.client.calls[-1] == ("ack", (42, 3), {})


def test_selected_poll_respects_pause_and_retirement(context):
    dialog = shown(context)
    assert dialog._poll_id is not None
    dialog._session.request("inventory", lambda: Future())
    context.scheduler.sources.pop(dialog._poll_id)
    ui.IntercomDetails._poll(lambda: dialog)
    assert not context.client.calls  # A pending consumer prevents overlap.
    dialog.retire()
    assert not context.scheduler.sources


class Page(BasePage):
    def __init__(self, context):
        super().__init__(context)
        self._intercom_rows = []
        for name in ("_intercom_group", "_intercom_refresh", "_intercom_message"):
            setattr(self, name, Widget())
        self._intercom_session = ui.IntercomViewSession(
            self._intercom_changed, self._intercom_retired, provider=lambda: context.current[0]
        )


for name in (
    "_fetch_intercoms",
    "_intercom_changed",
    "_intercom_retired",
    "_render_intercoms",
    "open_intercom",
    "_open_intercom_row",
):
    setattr(Page, name, getattr(devices_page.DevicesPage, name))


def test_devices_cache_only_then_visible_reads_and_hide_cleans_up(context):
    page = Page(context)
    page.refresh()
    assert not context.client.calls and not context.client.intercom_callbacks
    assert page._intercom_rows[0].title == "Entrance <b>literal</b>"
    page.on_page_shown()
    assert [c for c in context.client.calls if c[0] == "intercom-inventory"]
    page.on_page_hidden()
    assert not context.client.intercom_callbacks
    assert page._intercom_rows[0].title == "Intercom inventory not loaded"


def test_notification_page_open_rejects_reused_or_retired_identity(context, monkeypatch):
    page = Page(context)
    page._visible = True
    page._intercom_session.open(context.client)
    monkeypatch.setattr(
        devices_page, "IntercomDetails", lambda *_: pytest.fail("stale target opened")
    )
    assert not page.open_intercom(42, 9, 7)
    assert not page.open_intercom(42, 2, 8)
