"""Focused tests for the Ring Alarm page contract."""

from __future__ import annotations

from types import SimpleNamespace

from halo_gtk import alarm_page as alarm_page_module
from halo_gtk.alarm import (
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmCommandResult,
    AlarmCommandStatus,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmMode,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmSignal,
    TriState,
)
from halo_gtk.alarm_page import (
    AlarmPage,
    _device_state,
    _location_state_title,
)


def _device(
    zid: str = "sensor-1",
    *,
    asset_id: str = "hub-1",
    kind: AlarmDeviceKind = AlarmDeviceKind.CONTACT_SENSOR,
    connection: AlarmConnectionStatus = AlarmConnectionStatus.ONLINE,
    stale: bool = False,
    contact: TriState = TriState.UNKNOWN,
    smoke: TriState = TriState.UNKNOWN,
    faulted: TriState = TriState.UNKNOWN,
    battery_low: TriState = TriState.UNKNOWN,
    ac_power: TriState = TriState.UNKNOWN,
) -> AlarmDeviceSnapshot:
    return AlarmDeviceSnapshot(
        location_id="home",
        asset_id=asset_id,
        zid=zid,
        name="Front Door",
        kind=kind,
        connection=connection,
        stale=stale,
        contact=contact,
        smoke=smoke,
        faulted=faulted,
        battery_low=battery_low,
        ac_power=ac_power,
    )


def _location(
    *,
    revision: int = 4,
    mode: AlarmMode = AlarmMode.DISARMED,
    phase: AlarmPhase = AlarmPhase.IDLE,
    signal: AlarmSignal = AlarmSignal.NONE,
    triggered: TriState = TriState.INACTIVE,
    devices: tuple[AlarmDeviceSnapshot, ...] = (),
    can_set_mode: bool = True,
) -> AlarmLocationSnapshot:
    panel = AlarmDeviceSnapshot(
        location_id="home",
        asset_id="hub-1",
        zid="panel-1",
        name="Security Panel",
        kind=AlarmDeviceKind.SECURITY_PANEL,
        connection=AlarmConnectionStatus.ONLINE,
        mode=mode,
        phase=phase,
        signal=signal,
        revision=revision,
    )
    asset = AlarmAssetSnapshot(
        location_id="home",
        asset_id="hub-1",
        connection=AlarmConnectionStatus.ONLINE,
        inventory=AlarmInventoryStatus.COMPLETE,
        devices=(panel, *devices),
        revision=revision,
    )
    return AlarmLocationSnapshot(
        location_id="home",
        name="Home",
        connection=AlarmConnectionStatus.ONLINE,
        inventory=AlarmInventoryStatus.COMPLETE,
        mode=mode,
        phase=phase,
        signal=signal,
        triggered=triggered,
        assets=(asset,),
        can_set_mode=can_set_mode,
        revision=revision,
    )


def _snapshot(location: AlarmLocationSnapshot, *, generation: int = 3, revision: int = 8):
    return AlarmAccountSnapshot(
        generation=generation,
        revision=revision,
        status=AlarmServiceStatus.ONLINE,
        locations=(location,),
    )


def test_sensor_state_never_presents_stale_contact_as_current():
    current = _device(contact=TriState.INACTIVE)
    stale = _device(stale=True, contact=TriState.ACTIVE)

    assert _device_state(current) == ("Closed", "success")
    assert _device_state(stale) == ("Last reported: Open", "warning")


def test_sensor_state_preserves_unknown_and_life_safety_alarm():
    unknown = _device(connection=AlarmConnectionStatus.ONLINE)
    smoke = _device(
        kind=AlarmDeviceKind.SMOKE_ALARM,
        smoke=TriState.ACTIVE,
    )

    assert _device_state(unknown) == ("Status unknown", "neutral")
    assert _device_state(smoke) == ("Smoke detected", "error")


def test_fault_and_low_battery_override_a_normally_closed_contact():
    faulted = _device(contact=TriState.INACTIVE, faulted=TriState.ACTIVE)
    low_battery = _device(contact=TriState.INACTIVE, battery_low=TriState.ACTIVE)

    assert _device_state(faulted) == ("Faulted", "warning")
    assert _device_state(low_battery) == ("Low battery", "warning")


def test_location_title_prioritizes_active_alarm_over_other_fields():
    location = _location(
        mode=AlarmMode.HOME,
        phase=AlarmPhase.ALARMING,
        signal=AlarmSignal.FIRE,
        triggered=TriState.ACTIVE,
    )

    assert _location_state_title(location) == "Fire alarm active"


def test_location_title_treats_known_emergency_signal_as_active_evidence():
    location = _location(
        mode=AlarmMode.HOME,
        phase=AlarmPhase.IDLE,
        signal=AlarmSignal.FIRE,
        triggered=TriState.UNKNOWN,
    )

    assert _location_state_title(location) == "Fire alarm active"


def test_ac_power_loss_overrides_online_system_device_state():
    keypad = _device(
        kind=AlarmDeviceKind.KEYPAD,
        ac_power=TriState.INACTIVE,
    )

    assert _device_state(keypad) == ("AC power lost", "warning")


def test_retained_ac_power_never_overrides_offline_base_station():
    base_station = _device(
        kind=AlarmDeviceKind.BASE_STATION,
        connection=AlarmConnectionStatus.OFFLINE,
        ac_power=TriState.ACTIVE,
    )

    assert _device_state(base_station) == ("Offline", "warning")


class _Client:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.added = []
        self.removed = []
        self.requests = []
        self.last_future = None

    def add_alarm_callback(self, callback):
        self.added.append(callback)

    def remove_alarm_callback(self, callback):
        self.removed.append(callback)

    def get_alarm_snapshot(self):
        return self.snapshot

    def request_alarm_mode(self, location_id, target, **kwargs):
        self.requests.append((location_id, target, kwargs))
        self.last_future = _Future()
        return self.last_future


class _Future:
    def __init__(self, result=None):
        self.callback = None
        self._result = result

    def add_done_callback(self, callback):
        self.callback = callback

    def result(self):
        return self._result


def test_client_binding_is_idempotent_and_replaces_subscription():
    invalidations = []
    callback = object()
    page = SimpleNamespace(
        _alarm_client=None,
        _alarm_callback=callback,
        _invalidate_command_state=lambda: invalidations.append(True),
    )
    first = _Client()
    second = _Client()

    AlarmPage._bind_alarm_client(page, first)
    AlarmPage._bind_alarm_client(page, first)
    AlarmPage._bind_alarm_client(page, second)
    AlarmPage._bind_alarm_client(page, None)

    assert first.added == [callback]
    assert first.removed == [callback]
    assert second.added == [callback]
    assert second.removed == [callback]
    assert invalidations == [True, True, True]


def test_snapshot_acceptance_rejects_older_cached_and_queued_revisions():
    location = _location()
    current = _snapshot(location, revision=9)
    older = _snapshot(location, revision=8)
    latest = _snapshot(location, revision=10)
    client = _Client(latest)
    rendered = []
    page = SimpleNamespace(
        _alarm_client=client,
        _closed=False,
        _snapshot=current,
        _render_snapshot=rendered.append,
    )

    AlarmPage._accept_snapshot(page, older)
    AlarmPage._accept_snapshot(page, latest)

    assert rendered == [latest]
    assert page._snapshot is latest


def test_mode_submission_forwards_exact_location_revision_and_empty_bypass():
    location = _location(revision=17)
    client = _Client()
    refreshed = []
    page = SimpleNamespace(
        _command_pending=False,
        _alarm_client=client,
        _command_nonce=2,
        _pending_location_id=None,
        _location_ids=("home",),
        _location_selector=SimpleNamespace(set_sensitive=lambda _value: None),
        _refresh_mode_controls=refreshed.append,
    )

    AlarmPage._submit_mode(page, client, location, AlarmMode.AWAY, ())

    assert client.requests == [
        (
            "home",
            AlarmMode.AWAY,
            {"expected_revision": 17, "bypass_ids": ()},
        )
    ]
    assert page._command_pending is True
    assert page._pending_location_id == "home"
    assert refreshed == [location]


def test_future_done_callback_only_marshals_result_through_glib(monkeypatch):
    location = _location(revision=17)
    client = _Client()
    queued = []
    page = SimpleNamespace(
        _command_pending=False,
        _alarm_client=client,
        _command_nonce=0,
        _pending_location_id=None,
        _location_ids=("home",),
        _location_selector=SimpleNamespace(set_sensitive=lambda _value: None),
        _refresh_mode_controls=lambda _location: None,
        _finish_command_future=lambda *args: None,
    )
    monkeypatch.setattr(
        alarm_page_module.GLib,
        "idle_add",
        lambda callback, *args: queued.append((callback, args)),
    )

    AlarmPage._submit_mode(page, client, location, AlarmMode.HOME, ())
    submitted = client.last_future
    submitted.callback(submitted)

    assert len(queued) == 1
    callback, args = queued[0]
    assert callback is page._finish_command_future
    assert args[-1] is submitted


def test_bypass_resolution_is_scoped_to_security_panel_asset():
    sensor = _device(zid="front")
    other_asset_sensor = _device(zid="garage", asset_id="bridge-1")
    location = _location(devices=(sensor,))
    bridge = AlarmAssetSnapshot(
        location_id="home",
        asset_id="bridge-1",
        connection=AlarmConnectionStatus.ONLINE,
        inventory=AlarmInventoryStatus.COMPLETE,
        devices=(other_asset_sensor,),
    )
    location = AlarmLocationSnapshot(
        location_id=location.location_id,
        name=location.name,
        connection=location.connection,
        inventory=location.inventory,
        mode=location.mode,
        phase=location.phase,
        signal=location.signal,
        triggered=location.triggered,
        assets=(*location.assets, bridge),
        can_set_mode=True,
        revision=location.revision,
    )

    assert AlarmPage._resolve_bypass_devices(location, ("front",)) == (sensor,)
    assert AlarmPage._resolve_bypass_devices(location, ("garage",)) is None


def test_bypass_confirmation_submits_exact_backend_ids_and_revision():
    sensor = _device(zid="front")
    location = _location(revision=23, devices=(sensor,))
    snapshot = _snapshot(location)
    client = _Client(snapshot)
    submitted = []
    toasts = []
    page = SimpleNamespace(
        _alarm_client=client,
        _snapshot=snapshot,
        _selected_location_id="home",
        _submit_mode=lambda *args: submitted.append(args),
        _add_toast=toasts.append,
        _resolve_bypass_devices=AlarmPage._resolve_bypass_devices,
        _clear_command_pending=lambda: None,
    )

    AlarmPage._on_bypass_confirmed(
        page,
        object(),
        "bypass",
        client,
        "home",
        23,
        AlarmMode.AWAY,
        ("front",),
    )

    assert submitted == [(client, location, AlarmMode.AWAY, ("front",))]
    assert toasts == []


def test_bypass_confirmation_refuses_changed_revision():
    sensor = _device(zid="front")
    location = _location(revision=24, devices=(sensor,))
    snapshot = _snapshot(location)
    client = _Client(snapshot)
    submitted = []
    toasts = []
    page = SimpleNamespace(
        _alarm_client=client,
        _snapshot=snapshot,
        _selected_location_id="home",
        _submit_mode=lambda *args: submitted.append(args),
        _add_toast=toasts.append,
        _resolve_bypass_devices=AlarmPage._resolve_bypass_devices,
        _clear_command_pending=lambda: None,
    )

    AlarmPage._on_bypass_confirmed(
        page,
        object(),
        "bypass",
        client,
        "home",
        23,
        AlarmMode.AWAY,
        ("front",),
    )

    assert submitted == []
    assert toasts == ["Alarm status changed. Review faulted sensors before trying again."]


def test_timeout_unknown_has_a_dedicated_non_retry_path():
    result = AlarmCommandResult(
        status=AlarmCommandStatus.TIMEOUT_UNKNOWN,
        location_id="home",
        requested_mode=AlarmMode.AWAY,
    )
    future = _Future(result)
    client = object()
    calls = []
    page = SimpleNamespace(
        _closed=False,
        _alarm_client=client,
        _command_nonce=6,
        _clear_command_pending=lambda: calls.append("clear"),
        refresh=lambda: calls.append("refresh"),
        _present_unknown_outcome=lambda: calls.append("unknown"),
        _add_toast=lambda message: calls.append(("toast", message)),
    )

    returned = AlarmPage._finish_command_future(
        page,
        client,
        6,
        "home",
        9,
        AlarmMode.AWAY,
        (),
        future,
    )

    assert returned == alarm_page_module.GLib.SOURCE_REMOVE
    assert calls == ["clear", "refresh", "unknown"]
