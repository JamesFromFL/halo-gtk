"""Synthetic production Light Group view handlers; no Ring or actuator calls."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_gtk import light_group_details as ui
from halo_gtk.light_group_models import (
    LightGroupIntent,
    LightGroupLocationSnapshot,
    LightGroupMember,
    LightGroupOperationSnapshot,
    LightGroupOperationStatus,
    LightGroupPermission,
    LightGroupResult,
    LightGroupSnapshot,
    LightGroupsReadStatus,
    LightGroupsSnapshot,
)
from tests.test_chime_details import Client as ChimeClient
from tests.test_chime_details import Scheduler
from tests.test_chime_details import Widget as ChimeWidget


class Widget(ChimeWidget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.selected = 0
        self.model = None

    def set_selected(self, value):
        self.selected = value

    def get_selected(self):
        return self.selected

    def set_model(self, value):
        self.model = value


def group(**kwargs):
    values = dict(
        location_id="home",
        group_id="lights",
        name="Front <b>lights</b>",
        members=(LightGroupMember("42", "home", "Camera <b>light</b>", "floodlight"),),
        membership_complete=True,
        revision=4,
        identity_epoch=2,
        report=False,
        stale=False,
        inventory_monotonic=100,
        report_monotonic=100,
        can_write=True,
        unavailable_reason=None,
        status=LightGroupsReadStatus.READY,
        inventory_received_at=1_700_000_000,
        report_received_at=1_700_000_000,
    )
    return LightGroupSnapshot(**(values | kwargs))


def location(*groups, **kwargs):
    values = dict(
        location_id="home",
        name="Home <b>literal</b>",
        groups=groups or (group(),),
        identity_epoch=3,
        complete=True,
        has_inventory=True,
        status=LightGroupsReadStatus.READY,
        inventory_monotonic=100,
        inventory_received_at=1_700_000_000,
    )
    return LightGroupLocationSnapshot(**(values | kwargs))


def snapshot(*locations, generation=5, **kwargs):
    return LightGroupsSnapshot(
        generation,
        locations=locations or (location(),),
        complete=True,
        has_inventory=True,
        status=LightGroupsReadStatus.READY,
        **kwargs,
    )


class Client(ChimeClient):
    def __init__(self):
        super().__init__()
        self.light_snapshot = snapshot()
        self.light_callbacks = []

    def get_light_group_snapshot(self):
        return self.light_snapshot

    def add_light_group_callback(self, cb):
        self.light_callbacks.append(cb)

    def remove_light_group_callback(self, cb):
        if cb in self.light_callbacks:
            self.light_callbacks.remove(cb)

    def cancel_light_group_intent(self, intent):
        self.cancelled_intents.append(intent)
        intent.cancel()

    def refresh_light_locations(self, *, force=False):
        return self._request("locations", force=force)

    def refresh_light_groups(self, loc, *, force=False):
        return self._request("groups", loc, force=force)

    def refresh_light_group_details(self, loc, gid, *, force=False):
        return self._request("light-details", loc, gid, force=force)

    def request_light_group_lights(self, loc, gid, on, **kwargs):
        return self._request("lights", loc, gid, on, **kwargs)

    def publish_lighting(self, value):
        self.light_snapshot = value
        for callback in tuple(self.light_callbacks):
            callback(value)


class Timers(Scheduler):
    def __init__(self):
        super().__init__()
        self.delays = {}

    def timeout_add(self, ms, callback, *args):
        source = self._add("watch" if ms == 250 else "poll", callback, args)
        self.delays[source] = ms
        return source

    def fire(self, source):
        kind, callback, args = self.sources.pop(source)
        if callback(*args):
            self.sources[source] = (kind, callback, args)

    @property
    def polls(self):
        return [key for key, value in self.sources.items() if value[0] == "poll"]


class DetailsHarness:
    def __init__(self, context, loc="home", gid="lights"):
        self._location_id, self._group_id = loc, gid
        item = context.client.light_snapshot.find_group(loc, gid)
        self._identity_epoch = item.identity_epoch
        self._location_epoch = context.client.light_snapshot.find_location(loc).identity_epoch
        self._group = item
        self._closed = False
        self._poll_id = self._poll_at = None
        self.force_closed = False
        for name in (
            "_name",
            "_location",
            "_report",
            "_report_received",
            "_inventory_received",
            "_membership",
            "_permission",
            "_on",
            "_off",
            "_reload",
            "_reason",
            "_operation",
            "_message",
            "_poll_status",
            "_duration",
            "_members",
        ):
            setattr(self, name, Widget())
        self._member_rows = []
        self._session = ui._LightViewSession(
            self._snapshot_changed, self._account_retired, provider=lambda: context.current[0]
        )
        self._session.open(context.client)

    def force_close(self):
        self.force_closed = True


for name in (
    "_clear_text",
    "_stop_poll",
    "retire",
    "_account_retired",
    "_selected",
    "_snapshot_changed",
    "_render_members",
    "_update_actions",
    "_arm_poll",
    "_read",
    "_command",
):
    setattr(DetailsHarness, name, getattr(ui.LightGroupDetails, name))
DetailsHarness._poll = staticmethod(ui.LightGroupDetails._poll)


@pytest.fixture
def lighting(monkeypatch):
    timer = Timers()
    monkeypatch.setattr(ui.GLib, "idle_add", timer.idle_add)
    monkeypatch.setattr(ui.GLib, "timeout_add", timer.timeout_add)
    monkeypatch.setattr(ui.GLib, "source_remove", timer.source_remove)
    monkeypatch.setattr(ui, "_info_row", lambda text: Widget(title=text, use_markup=False))
    clock = [100.0]
    monkeypatch.setattr(ui, "light_group_monotonic_time", lambda: clock[0])
    client = Client()
    current = [client]
    context = SimpleNamespace(client=client, current=current, timer=timer, clock=clock)
    context.details = lambda loc="home", gid="lights": DetailsHarness(context, loc, gid)
    return context


def test_read_open_and_duration_edits_do_not_actuate(lighting):
    dialog = lighting.details()
    lighting.timer.drain()
    dialog._duration.set_selected(2)
    dialog._read(force=False)
    assert lighting.client.calls == [("light-details", ("home", "lights"), {"force": False})]
    assert dialog._report.subtitle == "Group reports Off"


@pytest.mark.parametrize("index,duration", [(0, 60), (1, 300), (2, 900)])
def test_explicit_on_captures_duration_revision_and_exact_identity(lighting, index, duration):
    dialog = lighting.details()
    lighting.timer.drain()
    dialog._duration.set_selected(index)
    dialog._command(True)
    dialog._command(False)
    assert len(lighting.client.calls) == 1
    kind, args, kwargs = lighting.client.calls[0]
    assert (kind, args) == ("lights", ("home", "lights", True))
    assert kwargs["duration"] == duration and kwargs["expected_revision"] == 4
    intent = kwargs["intent_id"]
    assert isinstance(intent, LightGroupIntent)
    assert (
        intent.generation,
        intent.location_id,
        intent.group_id,
        intent.revision,
        intent.identity_epoch,
    ) == (5, "home", "lights", 4, 2)
    assert dialog._report.subtitle == "Group reports Off"


def test_off_omits_duration_and_never_schedules_later_off(lighting):
    dialog = lighting.details()
    lighting.timer.drain()
    dialog._command(False)
    assert lighting.client.calls[0][2]["duration"] is None
    assert lighting.client.calls[0][1][-1] is False
    assert len([call for call in lighting.client.calls if call[0] == "lights"]) == 1


def test_unknown_permission_does_not_create_owner_only_policy(lighting):
    dialog = lighting.details()
    lighting.timer.drain()
    assert dialog._on.sensitive
    assert "not reported" in dialog._permission.label
    dialog._command(True)
    assert lighting.client.calls[-1][0] == "lights"


@pytest.mark.parametrize(
    "change",
    [
        {"permission": LightGroupPermission.DENIED, "can_write": False},
        {"membership_complete": False, "can_write": False},
        {"report": None, "can_write": False},
        {"stale": True, "can_write": False},
    ],
)
def test_unknown_incomplete_denied_and_stale_are_read_only(lighting, change):
    lighting.client.light_snapshot = snapshot(location(group(**change)))
    dialog = lighting.details()
    lighting.timer.drain()
    dialog._command(True)
    dialog._command(False)
    assert not lighting.client.calls
    assert not dialog._on.sensitive and dialog._reason.label
    if change.get("membership_complete") is False:
        assert "Unknown" in dialog._membership.subtitle


def test_state_is_strict_and_never_claims_all_member_state(lighting):
    for value in (None, 1, 0, "false"):
        assert ui._report_text(group(report=value)) == "Group report unknown"
    dialog = lighting.details()
    lighting.timer.drain()
    assert dialog._member_rows[0].title == "Camera <b>light</b>"
    assert dialog._member_rows[0].subtitle == "floodlight"
    assert dialog._member_rows[0].use_markup is False
    assert "All" not in dialog._report.subtitle


def test_poll_runs_only_selected_group_every_15_seconds_without_overlap(lighting):
    dialog = lighting.details()
    lighting.timer.drain()
    assert len(lighting.timer.polls) == 1
    assert not dialog._closed
    first = lighting.timer.polls[0]
    assert lighting.timer.delays[first] >= 15000
    lighting.clock[0] = 115
    lighting.timer.fire(first)
    assert lighting.client.calls == [("light-details", ("home", "lights"), {"force": False})]
    lighting.timer.drain()
    second = lighting.timer.polls[0]
    lighting.timer.fire(second)
    assert len(lighting.client.calls) == 1
    assert len(lighting.timer.polls) == 1


def test_poll_skips_active_command_without_read_backlog(lighting):
    operation = LightGroupOperationSnapshot(1, LightGroupOperationStatus.PREPARING, True, True, 60)
    lighting.client.light_snapshot = snapshot(location(group(operation=operation)))
    dialog = lighting.details()
    lighting.timer.drain()
    lighting.timer.fire(lighting.timer.polls[0])
    assert not lighting.client.calls
    assert len(lighting.timer.polls) == 1
    assert not dialog._closed


def test_failed_parent_inventory_is_not_successful_detail_refresh(lighting):
    dialog = lighting.details()
    dialog._read(force=True)
    failed = replace(
        lighting.client.light_snapshot,
        complete=False,
        status=LightGroupsReadStatus.ERROR,
        code="network-unavailable",
    )
    lighting.client.publish_lighting(failed)
    lighting.client.futures[-1].set_result(failed)
    lighting.timer.drain()
    assert "refresh failed" in dialog._message.label


@pytest.mark.parametrize("delay", [15, 30, 60, 120, 300])
def test_poll_honors_typed_retry_deadline_and_manual_read_cannot_bypass(lighting, delay):
    lighting.client.light_snapshot = snapshot(
        location(group(read_retry_after=100 + delay, read_failure_count=2))
    )
    dialog = lighting.details()
    lighting.timer.drain()
    timer = lighting.timer.polls[0]
    assert lighting.timer.delays[timer] >= delay * 1000
    dialog._read(force=True)
    assert not lighting.client.calls
    assert not dialog._reload.sensitive


@pytest.mark.parametrize("code", ["permission-denied", "authentication-required"])
def test_permission_auth_failures_stop_automatic_poll_until_deliberate_reload(lighting, code):
    lighting.client.light_snapshot = snapshot(
        location(group(poll_allowed=False, code=code, status=LightGroupsReadStatus.ERROR))
    )
    dialog = lighting.details()
    lighting.timer.drain()
    assert lighting.timer.polls == []
    assert "paused" in dialog._poll_status.label
    dialog._read(force=True)
    assert lighting.client.calls[-1][2] == {"force": True}


def test_hide_cancels_read_and_poll_and_clears_member_names(lighting):
    dialog = lighting.details()
    dialog._read(force=False)
    lighting.timer.drain()
    future = lighting.client.futures[-1]
    dialog.retire()
    assert future.cancelled()
    assert not lighting.client.light_callbacks
    assert not lighting.timer.sources
    assert dialog._group is None and dialog._member_rows == []
    assert dialog._name.label == "Light Group unavailable"


def test_admitted_operation_remains_service_owned_on_close(lighting):
    dialog = lighting.details()
    lighting.timer.drain()
    dialog._command(True)
    future = lighting.client.futures[-1]
    dialog.retire()
    assert not future.cancelled()
    assert lighting.client.cancelled_intents[-1].cancelled
    future.set_result(LightGroupResult(LightGroupOperationStatus.UNKNOWN, "home", "lights"))
    assert not lighting.timer.sources


def test_account_change_before_queued_delivery_clears_private_text(lighting):
    dialog = lighting.details()
    lighting.current[0] = None
    lighting.timer.drain()
    assert dialog._closed and dialog.force_closed
    assert dialog._name.label == "Light Group unavailable"


@pytest.mark.parametrize("change", ["location", "group", "removal"])
def test_current_membership_and_epoch_fences_late_result(lighting, change):
    dialog = lighting.details()
    lighting.timer.drain()
    loc = location(group(identity_epoch=9)) if change == "group" else location(identity_epoch=9)
    value = (
        snapshot(loc)
        if change != "removal"
        else LightGroupsSnapshot(5, complete=True, has_inventory=True)
    )
    lighting.client.publish_lighting(value)
    lighting.timer.drain()
    assert dialog._closed
    assert dialog._name.label == "Light Group unavailable"


def test_snapshot_flood_is_one_idle_and_uses_latest_cache(lighting):
    dialog = lighting.details()
    for _ in range(1000):
        lighting.client.publish_lighting(lighting.client.light_snapshot)
    assert lighting.timer.idles == 1
    lighting.timer.drain()
    assert dialog._name.label == "Front <b>lights</b>"


def test_freshness_expiry_disables_control_without_read(lighting):
    dialog = lighting.details()
    lighting.timer.drain()
    lighting.clock[0] = 131
    lighting.timer.tick()
    lighting.timer.drain()
    assert not dialog._on.sensitive
    assert "stale" in dialog._report.subtitle
    assert not lighting.client.calls


def test_read_cooldown_expiry_reenables_reload_without_network(lighting):
    lighting.client.light_snapshot = snapshot(location(group(read_retry_after=105)))
    dialog = lighting.details()
    lighting.timer.drain()
    assert not dialog._reload.sensitive
    assert dialog._on.sensitive
    lighting.clock[0] = 105
    lighting.timer.tick()
    lighting.timer.drain()
    assert dialog._reload.sensitive
    assert not lighting.client.calls


def test_reported_outcome_is_not_proof_of_every_lamp_or_duration(lighting):
    operation = LightGroupOperationSnapshot(1, LightGroupOperationStatus.REPORTED, False, True, 60)
    lighting.client.light_snapshot = snapshot(location(group(operation=operation)))
    dialog = lighting.details()
    lighting.timer.drain()
    assert "Requested group report observed" in dialog._operation.label
    assert "does not prove every lamp" in dialog._operation.label


def test_typed_read_failure_and_exception_feedback_is_safe(lighting):
    dialog = lighting.details()
    dialog._read(force=True)
    lighting.client.publish_lighting(
        snapshot(
            location(group(status=LightGroupsReadStatus.ERROR, code="invalid-response", stale=True))
        )
    )
    lighting.client.futures[-1].set_result(lighting.client.light_snapshot)
    lighting.timer.drain()
    assert "refresh failed" in dialog._message.label
    dialog._read(force=True)
    lighting.client.futures[-1].set_exception(RuntimeError("token=SECRET&owner=Private"))
    lighting.timer.drain()
    assert "SECRET" not in dialog._message.label
    assert "unavailable" in dialog._message.label
