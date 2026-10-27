"""Synthetic account/location fences and bounded GTK admission; no live Ring."""

import threading
from dataclasses import replace

import pytest
from gi.repository import GLib

from halo_gtk import config, notifications, ring_client
from halo_gtk.alarm.events import MAX_PENDING_EVENTS, AlarmEvent
from halo_gtk.alarm.models import AlarmAccountSnapshot, AlarmServiceStatus, AlarmSignal, TriState
from halo_gtk.ring_client import RingClient
from tests.test_alarm_events import snapshot


@pytest.fixture
def client(monkeypatch):
    owner = RingClient(token_updater=lambda _token: None)
    owner._published_generation = 1
    owner._initialize_alarm_generation(1)
    monkeypatch.setattr(ring_client, "_client", owner)
    monkeypatch.setattr(ring_client, "_active_client_generation", 1)
    monkeypatch.setattr(config, "load", config.defaults)
    return owner


def online(**kwargs):
    return replace(snapshot(**kwargs), status=AlarmServiceStatus.ONLINE)


def capture_idle(monkeypatch):
    scheduled = []
    monkeypatch.setattr(GLib, "idle_add", lambda *args: scheduled.append(args))
    return scheduled


@pytest.mark.parametrize("method", ["_on_alarm_snapshot", "_dispatch_alarm_snapshot"])
def test_retirement_after_global_admission_cannot_repopulate_or_drain(client, monkeypatch, method):
    scheduled = capture_idle(monkeypatch)
    checked, release = threading.Event(), threading.Event()
    original = ring_client._is_active_client_generation
    errors = []

    def checked_guard(owner, generation):
        result = original(owner, generation)
        checked.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(ring_client, "_is_active_client_generation", checked_guard)
    value = online(signal=AlarmSignal.FIRE)

    def invoke():
        try:
            if method == "_on_alarm_snapshot":
                client._on_alarm_snapshot(value)
            else:
                client._dispatch_alarm_snapshot(value, 1)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        assert checked.wait(2)
        # Publishing/retirement lock order is session -> snapshot. If the
        # producer checks session ownership under the snapshot lock this
        # regression deadlocks instead of completing.
        with ring_client._session_lock:
            monkeypatch.setattr(ring_client, "_client", None)
            monkeypatch.setattr(ring_client, "_active_client_generation", 2)
            client._initialize_alarm_generation(2)
            client._alarm_event_buffer.put(AlarmEvent(2, "new", "mode", "mode", "New account"))
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert not errors
    current = client.get_alarm_snapshot()
    assert current.generation == 2
    assert current.locations == ()
    assert [event.generation for event in client._alarm_event_buffer.drain()[0]] == [2]
    assert scheduled == []


def test_revocation_between_drain_and_render_discards_drained_event(client, monkeypatch):
    capture_idle(monkeypatch)
    shown, rendered = [], []
    monkeypatch.setattr(notifications, "send_alarm_notification", shown.append)
    client.add_alarm_callback(rendered.append)
    value = online(signal=AlarmSignal.FIRE)
    client._on_alarm_snapshot(value)
    original = ring_client._is_active_client_generation
    calls = 0

    def revoke_before_render(owner, generation):
        nonlocal calls
        calls += 1
        if calls == 2:
            client._on_alarm_snapshot(
                AlarmAccountSnapshot(generation=1, status=AlarmServiceStatus.NO_SYSTEM)
            )
        return original(owner, generation)

    monkeypatch.setattr(ring_client, "_is_active_client_generation", revoke_before_render)
    client._dispatch_alarm_snapshot(value, 1)
    assert shown == []
    assert len(rendered) == 1 and rendered[0].locations == ()


def test_retirement_between_drain_and_render_discards_event_and_callbacks(client, monkeypatch):
    capture_idle(monkeypatch)
    shown, rendered = [], []
    monkeypatch.setattr(notifications, "send_alarm_notification", shown.append)
    client.add_alarm_callback(rendered.append)
    value = online(signal=AlarmSignal.FIRE)
    client._on_alarm_snapshot(value)
    calls = 0

    def retire_before_render(_owner, _generation):
        nonlocal calls
        calls += 1
        if calls == 2:
            client._initialize_alarm_generation(2)
        # The local fence must also reject a global check whose result became
        # stale immediately before taking the snapshot lock.
        return True

    monkeypatch.setattr(ring_client, "_is_active_client_generation", retire_before_render)
    client._dispatch_alarm_snapshot(value, 1)
    assert shown == []
    assert rendered == []


def test_location_revocation_burst_schedules_only_one_idle(client, monkeypatch):
    scheduled = capture_idle(monkeypatch)
    withdrawn = []
    monkeypatch.setattr(
        notifications, "withdraw_alarm_notifications", lambda **kw: withdrawn.append(kw)
    )
    present = online()
    absent = AlarmAccountSnapshot(generation=1, status=AlarmServiceStatus.NO_SYSTEM)
    for _ in range(100):
        client._on_alarm_snapshot(present)
        client._on_alarm_snapshot(absent)
    assert len(scheduled) == 1
    assert client._alarm_pending_revocations == {"home"}
    assert client._alarm_event_buffer.drain() == ((), 0)
    callback, *args = scheduled[0]
    callback(*args)
    assert withdrawn == [{"generation": 1, "location_id": "home"}]
    assert not client._alarm_pending_revocations


def test_distinct_revocation_overflow_is_bounded_and_withdraws_generation_once(client, monkeypatch):
    scheduled = capture_idle(monkeypatch)
    withdrawn = []
    monkeypatch.setattr(
        notifications, "withdraw_alarm_notifications", lambda **kw: withdrawn.append(kw)
    )
    base = online()
    absent = AlarmAccountSnapshot(generation=1, status=AlarmServiceStatus.NO_SYSTEM)
    for index in range(MAX_PENDING_EVENTS + 100):
        location = replace(base.locations[0], location_id=f"location-{index}", assets=())
        client._on_alarm_snapshot(replace(base, locations=(location,)))
        client._on_alarm_snapshot(absent)
        assert len(client._alarm_pending_revocations) <= MAX_PENDING_EVENTS
    assert len(scheduled) == 1
    assert client._alarm_withdraw_all
    callback, *args = scheduled[0]
    callback(*args)
    assert withdrawn == [{"generation": 1}]
    assert not client._alarm_withdraw_all


def test_regranted_location_withdraws_old_display_before_current_critical(client, monkeypatch):
    scheduled = capture_idle(monkeypatch)
    actions = []
    monkeypatch.setattr(
        notifications, "withdraw_alarm_notifications", lambda **kw: actions.append(("withdraw", kw))
    )
    monkeypatch.setattr(
        notifications, "send_alarm_notification", lambda event: actions.append(("show", event))
    )
    value = online(signal=AlarmSignal.FIRE)
    client._on_alarm_snapshot(value)
    client._on_alarm_snapshot(
        AlarmAccountSnapshot(generation=1, status=AlarmServiceStatus.NO_SYSTEM)
    )
    client._on_alarm_snapshot(value)
    callback, *args = scheduled[0]
    callback(*args)
    assert [kind for kind, _ in actions] == ["withdraw", "show"]
    assert actions[1][1].critical


def test_disabled_routine_burst_does_not_count_as_dropped_notifications(client, monkeypatch):
    scheduled = capture_idle(monkeypatch)
    shown = []
    monkeypatch.setattr(notifications, "send_alarm_notification", shown.append)
    for index in range(1000):
        contact = TriState.ACTIVE if index % 2 else TriState.INACTIVE
        client._on_alarm_snapshot(online(contact=contact))
    client._on_alarm_snapshot(online(signal=AlarmSignal.FIRE))
    assert len(scheduled) == 1
    assert client._alarm_event_buffer.suppressed == 0
    assert len(client._alarm_event_buffer._pending) == 1
    callback, *args = scheduled[0]
    callback(*args)
    assert len(shown) == 1 and shown[0].critical
    assert client._alarm_notification_gap_count == 0


def test_master_opt_out_still_advances_reducer_without_admitting_notifications(client, monkeypatch):
    capture_idle(monkeypatch)
    cfg = {**config.defaults(), "notify_alarm": False}
    monkeypatch.setattr(config, "load", lambda: cfg)
    client._on_alarm_snapshot(online(signal=AlarmSignal.FIRE))
    assert client._alarm_event_buffer.drain() == ((), 0)
    cfg["notify_alarm"] = True
    # Toggling settings must not manufacture a new observed alarm episode.
    client._on_alarm_snapshot(online(signal=AlarmSignal.FIRE))
    assert client._alarm_event_buffer.drain() == ((), 0)
