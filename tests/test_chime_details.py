"""Headless tests of production Chime view handlers and asynchronous lifecycle."""

from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_gtk import chime_details as ui
from halo_gtk.chime import (
    ChimeAccountSnapshot,
    ChimeConnectionStatus,
    ChimeHealthSnapshot,
    ChimeOperationSnapshot,
    ChimeOperationStatus,
    ChimeReadStatus,
    ChimeSnapshot,
)


class Widget:
    def __init__(self, **kwargs):
        self.label = kwargs.get("label", "")
        self.title = kwargs.get("title", "")
        self.subtitle = kwargs.get("subtitle", "")
        self.use_markup = kwargs.get("use_markup")
        self.sensitive = True
        self.visible = True
        self.value = 0
        self.children = []
        self.signals = []

    def set_label(self, value):
        self.label = value

    def set_subtitle(self, value):
        self.subtitle = value

    def set_description(self, value):
        self.description = value

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_value(self, value):
        self.value = value

    def get_value_as_int(self):
        return int(self.value)

    def set_title_lines(self, value):
        pass

    def set_subtitle_lines(self, value):
        pass

    def set_activatable(self, value):
        self.activatable = value

    def add_prefix(self, value):
        pass

    def add_suffix(self, value):
        pass

    def add(self, value):
        self.children.append(value)

    def remove(self, value):
        self.children.remove(value)

    def connect(self, *args):
        self.signals.append(args)


def device(**kwargs):
    fields = dict(
        api_id=42,
        name="Hall <b>Chime</b>",
        kind="chime_pro_v2",
        model="Chime Pro v2",
        volume=6,
        revision=4,
        identity_epoch=2,
        stale=False,
        owned=True,
        can_write=True,
        unavailable_reason=None,
        inventory_monotonic=100,
        inventory_received_at=1_700_000_000,
        connection=ChimeConnectionStatus.ONLINE,
    )
    return ChimeSnapshot(**(fields | kwargs))


def account(*devices, generation=3, **kwargs):
    return ChimeAccountSnapshot(
        generation,
        devices=devices or (device(),),
        complete=True,
        status=ChimeReadStatus.READY,
        **kwargs,
    )


class Client:
    is_authenticated = True

    def __init__(self, snapshot=None):
        self.snapshot = snapshot or account()
        self.callbacks = []
        self.calls = []
        self.futures = []
        self.cancelled_intents = []
        self.all_devices = []

    def get_chime_snapshot(self):
        return self.snapshot

    def add_chime_callback(self, cb):
        self.callbacks.append(cb)

    def remove_chime_callback(self, cb):
        if cb in self.callbacks:
            self.callbacks.remove(cb)

    def cancel_chime_intent(self, intent):
        self.cancelled_intents.append(intent)
        intent.cancel()

    def _request(self, key, *args, **kwargs):
        self.calls.append((key, args, kwargs))
        future = Future()
        self.futures.append(future)
        return future

    def refresh_chime_details(self, api_id, *, force=False):
        return self._request("details", api_id, force=force)

    def refresh_chimes(self, *, force=False):
        return self._request("inventory", force=force)

    def request_chime_volume(self, api_id, value, **kwargs):
        return self._request("volume", api_id, value, **kwargs)

    def request_chime_test_sound(self, api_id, kind, **kwargs):
        return self._request("sound", api_id, kind, **kwargs)

    def acknowledge_chime_sound_finished(self, api_id, operation_id):
        return self._request("ack", api_id, operation_id)

    def publish(self, snapshot):
        self.snapshot = snapshot
        for callback in tuple(self.callbacks):
            callback(snapshot)


class Scheduler:
    def __init__(self):
        self.sources = {}
        self.next_id = 1

    def _add(self, kind, cb, args):
        source = self.next_id
        self.next_id += 1
        self.sources[source] = (kind, cb, args)
        return source

    def idle_add(self, cb, *args):
        return self._add("idle", cb, args)

    def timeout_add(self, ms, cb, *args):
        return self._add("watch", cb, args)

    def source_remove(self, source):
        self.sources.pop(source, None)

    def drain(self):
        while pending := [
            (key, value) for key, value in self.sources.items() if value[0] == "idle"
        ]:
            for source, (_, cb, args) in pending:
                if self.sources.pop(source, None) is not None:
                    cb(*args)

    def tick(self):
        for source, (kind, cb, args) in tuple(self.sources.items()):
            if kind == "watch" and not cb(*args):
                self.sources.pop(source, None)

    @property
    def idles(self):
        return sum(v[0] == "idle" for v in self.sources.values())


class DetailsHarness:
    def __init__(self, owner, current):
        self._api_id = 42
        self._device = owner.snapshot.find_device(42)
        self._identity_epoch = self._device.identity_epoch
        self._draft = self._device.volume
        self._draft_revision = self._device.revision
        self._updating = self._closed = self._reset_draft = False
        self.force_closed = False
        self._name = Widget()
        self._rows = {
            key: Widget()
            for key in ("model", "connection", "received", "firmware", "snooze", "health")
        }
        self._reported = Widget()
        self._volume = Widget()
        self._apply = Widget()
        self._reload_button = Widget()
        self._reason = Widget()
        self._operation = Widget()
        self._ack = Widget()
        self._message = Widget()
        self._test_buttons = {kind: Widget() for kind in ("ding", "motion")}
        self._session = ui._ChimeViewSession(
            self._snapshot_changed, self._account_retired, provider=lambda: current[0]
        )
        self._session.open(owner)

    def force_close(self):
        self.force_closed = True


for name in (
    "_clear_text",
    "retire",
    "_account_retired",
    "_selected",
    "_snapshot_changed",
    "_update_actions",
    "_draft_changed",
    "_reload",
    "_command",
    "_apply_volume",
    "_test_sound",
    "_acknowledge",
):
    setattr(DetailsHarness, name, getattr(ui.ChimeDetails, name))


@pytest.fixture
def harness(monkeypatch):
    scheduler = Scheduler()
    monkeypatch.setattr(ui.GLib, "idle_add", scheduler.idle_add)
    monkeypatch.setattr(ui.GLib, "timeout_add", scheduler.timeout_add)
    monkeypatch.setattr(ui.GLib, "source_remove", scheduler.source_remove)
    clock = [100.0]
    monkeypatch.setattr(ui, "chime_monotonic_time", lambda: clock[0])
    client = Client()
    current = [client]
    return SimpleNamespace(
        client=client,
        current=current,
        scheduler=scheduler,
        clock=clock,
        details=lambda: DetailsHarness(client, current),
    )


def test_draft_and_refresh_never_write_or_play_sound(harness):
    dialog = harness.details()
    harness.scheduler.drain()
    dialog._volume.value = 9
    dialog._draft_changed(dialog._volume)
    dialog._reload(force=True)
    assert harness.client.calls == [("details", (42,), {"force": True})]
    assert dialog._reported.subtitle == "Legacy Ring level 6 of 11"


def test_apply_is_explicit_revision_and_identity_scoped_and_double_click_is_not_queued(harness):
    dialog = harness.details()
    harness.scheduler.drain()
    dialog._volume.value = 9
    dialog._draft_changed(dialog._volume)
    assert dialog._apply.sensitive
    dialog._apply_volume()
    dialog._apply_volume()
    assert len(harness.client.calls) == 1
    key, args, kwargs = harness.client.calls[0]
    assert (key, args, kwargs["expected_revision"]) == ("volume", (42, 9), 4)
    intent = kwargs["intent_id"]
    assert (intent.generation, intent.api_id, intent.revision, intent.identity_epoch) == (
        3,
        42,
        4,
        2,
    )
    assert dialog._reported.subtitle == "Legacy Ring level 6 of 11"


def test_same_reported_volume_has_no_apply(harness):
    dialog = harness.details()
    harness.scheduler.drain()
    assert not dialog._apply.sensitive
    dialog._apply_volume()
    assert not harness.client.calls


@pytest.mark.parametrize("kind", ["ding", "motion"])
def test_deliberate_test_uses_selected_physical_chime_and_existing_kind(harness, kind):
    dialog = harness.details()
    harness.scheduler.drain()
    assert harness.client.calls == []
    dialog._test_sound(None, kind)
    assert harness.client.calls[0][0:2] == ("sound", (42, kind))
    dialog._test_sound(None, kind)
    assert len(harness.client.calls) == 1


def test_reopen_retains_sound_gate_and_never_cancels_possibly_sent_future(harness):
    dialog = harness.details()
    harness.scheduler.drain()
    dialog._test_sound(None, "ding")
    future = harness.client.futures[-1]
    operation = ChimeOperationSnapshot(
        7, "test-sound", ChimeOperationStatus.UNKNOWN, False, possibly_sent=True, test_kind="ding"
    )
    harness.client.publish(account(device(operation=operation, sound_operation_id=7)))
    harness.scheduler.drain()
    dialog.retire()
    assert not future.cancelled()
    assert harness.client.cancelled_intents[-1].cancelled
    reopened = harness.details()
    harness.scheduler.drain()
    assert reopened._ack.visible
    assert not reopened._apply.sensitive
    assert not reopened._test_buttons["ding"].sensitive
    assert "Outcome unknown" in reopened._operation.label
    reopened._acknowledge()
    assert harness.client.calls[-1][0:2] == ("ack", (42, 7))
    assert len([call for call in harness.client.calls if call[0] == "sound"]) == 1


def test_ack_cannot_run_before_sound_request_settles(harness):
    operation = ChimeOperationSnapshot(
        7, "test-sound", ChimeOperationStatus.POSSIBLY_SENT, True, possibly_sent=True
    )
    harness.client.snapshot = account(device(operation=operation, sound_operation_id=7))
    dialog = harness.details()
    harness.scheduler.drain()
    assert not dialog._ack.sensitive
    dialog._acknowledge()
    assert not harness.client.calls


def test_close_cancels_read_consumer_and_clears_private_widgets(harness):
    dialog = harness.details()
    dialog._reload(force=False)
    future = harness.client.futures[-1]
    harness.scheduler.drain()
    assert "Hall" in dialog._name.label
    dialog.retire()
    assert future.cancelled()
    assert harness.client.callbacks == []
    assert dialog._device is None and dialog._draft is None
    assert dialog._name.label == "Chime unavailable"
    assert all(row.subtitle == "Unknown" for row in dialog._rows.values())
    assert dialog._volume.value == 0 and not dialog._volume.visible
    assert not harness.scheduler.sources


def test_snapshot_flood_and_future_completion_share_one_idle(harness):
    dialog = harness.details()
    for _ in range(1000):
        harness.client.publish(harness.client.snapshot)
    dialog._reload(force=False)
    harness.client.futures[-1].set_result(harness.client.snapshot)
    assert harness.scheduler.idles == 1
    harness.scheduler.drain()
    assert not dialog._session.pending("details")


def test_account_replacement_before_queued_completion_clears_not_repaints(harness):
    dialog = harness.details()
    dialog._reload(force=False)
    harness.client.futures[-1].set_result(harness.client.snapshot)
    harness.current[0] = Client(account(generation=4))
    harness.scheduler.drain()
    assert dialog._closed and dialog.force_closed
    assert dialog._name.label == "Chime unavailable"


def test_retired_callback_cannot_clear_new_sessions_pending_idle(harness):
    dialog = harness.details()
    old = next(value for value in harness.scheduler.sources.values() if value[0] == "idle")
    session = dialog._session
    session.close()
    session.open(harness.client)
    old[1](*old[2])
    assert harness.scheduler.idles == 1
    harness.scheduler.drain()
    assert dialog._name.label == harness.client.snapshot.devices[0].name


@pytest.mark.parametrize("change", [{"identity_epoch": 3}, {"api_id": 99}])
def test_removed_or_reused_identity_retires_dialog(harness, change):
    dialog = harness.details()
    harness.scheduler.drain()
    harness.client.publish(account(device(**change)))
    harness.scheduler.drain()
    assert dialog._closed
    assert dialog._name.label == "Chime unavailable"


def test_visible_cache_watch_retires_account_without_network(harness):
    dialog = harness.details()
    harness.scheduler.drain()
    harness.current[0] = None
    harness.scheduler.tick()
    assert dialog._closed
    assert not harness.client.calls


def test_freshness_expires_controls_without_network(harness):
    dialog = harness.details()
    harness.scheduler.drain()
    assert dialog._test_buttons["ding"].sensitive
    harness.clock[0] = 131
    harness.scheduler.tick()
    harness.scheduler.drain()
    assert not dialog._test_buttons["ding"].sensitive
    assert "Reload" in dialog._reason.label
    assert "stale" in dialog._rows["connection"].subtitle
    assert not harness.client.calls


@pytest.mark.parametrize(
    "change",
    [
        {"can_write": False, "unavailable_reason": "unsupported-model", "kind": "future_chime"},
        {"can_write": False, "unavailable_reason": "permission-unverified", "owned": None},
        {"can_write": False, "unavailable_reason": "volume-unknown", "volume": None},
        {
            "can_write": False,
            "unavailable_reason": "offline",
            "connection": ChimeConnectionStatus.OFFLINE,
        },
    ],
)
def test_read_only_states_never_submit_actions(harness, change):
    harness.client.snapshot = account(device(**change))
    dialog = harness.details()
    harness.scheduler.drain()
    dialog._command(value=8)
    dialog._test_sound(None, "ding")
    assert not harness.client.calls
    assert not dialog._apply.sensitive
    assert dialog._reason.label
    if change.get("volume", 6) is None:
        assert dialog._reported.subtitle == "Unknown"
        assert not dialog._volume.visible


def test_external_revision_keeps_draft_but_requires_reload(harness):
    dialog = harness.details()
    harness.scheduler.drain()
    dialog._volume.value = 9
    dialog._draft_changed(dialog._volume)
    harness.client.publish(account(device(volume=7, revision=5)))
    harness.scheduler.drain()
    assert dialog._draft == 9 and not dialog._apply.sensitive
    dialog._apply_volume()
    assert not harness.client.calls
    dialog._reload(force=True)
    harness.client.publish(account(device(volume=8, revision=6)))
    harness.client.futures[-1].set_result(harness.client.snapshot)
    harness.scheduler.drain()
    assert dialog._draft == 8 and dialog._draft_revision == 6
    assert not dialog._apply.sensitive


def test_private_exception_text_never_reaches_ui(harness):
    dialog = harness.details()
    dialog._reload(force=True)
    harness.client.futures[-1].set_exception(RuntimeError("token=SECRET&owner=Private"))
    harness.scheduler.drain()
    assert "unavailable" in dialog._message.label
    assert "SECRET" not in dialog._message.label and "Private" not in dialog._message.label
    assert dialog._reload_button.sensitive


def test_health_has_separate_freshness_and_names_render_literal(harness):
    health = ChimeHealthSnapshot(
        stale=False,
        status=ChimeReadStatus.READY,
        observed_at=1_700_000_000,
        observed_monotonic=50,
        wifi_signal_strength=-55,
        wifi_signal_category="<b>good</b>",
    )
    harness.client.snapshot = account(device(health=health, firmware="<b>1.0</b>"))
    dialog = harness.details()
    harness.scheduler.drain()
    assert dialog._name.label == "Hall <b>Chime</b>"
    assert dialog._rows["firmware"].subtitle == "<b>1.0</b>"
    assert "stale" not in dialog._rows["health"].subtitle
    harness.clock[0] = 111
    harness.scheduler.tick()
    harness.scheduler.drain()
    assert "stale" in dialog._rows["health"].subtitle


def test_http_acceptance_message_does_not_claim_audibility_or_completion():
    value = ChimeOperationSnapshot(1, "test-sound", ChimeOperationStatus.ACCEPTED, False)
    text = ui._operation_text(value)
    assert "accepted" in text and "cannot confirm" in text and "finished" in text


@pytest.mark.parametrize("inventory_error", [True, False])
def test_typed_failed_reload_reports_failure_even_when_future_succeeds(harness, inventory_error):
    dialog = harness.details()
    dialog._reload(force=True)
    if inventory_error:
        failed = replace(
            harness.client.snapshot, status=ChimeReadStatus.STALE, code="network-unavailable"
        )
    else:
        health = ChimeHealthSnapshot(status=ChimeReadStatus.ERROR, code="invalid-response")
        failed = account(device(health=health))
    harness.client.publish(failed)
    harness.client.futures[-1].set_result(failed)
    harness.scheduler.drain()
    assert "reload failed" in dialog._message.label
    assert "network-unavailable" not in dialog._message.label


def test_unknown_volume_is_not_reported_as_zero():
    for value in (None, True, "0", -1, 12):
        assert ui._volume_text(value) == "Unknown"
    assert ui._volume_text(0) == "Legacy Ring level 0 of 11"
