"""Tests for shared Ring device command handling."""

from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace

from halo_gtk import device_commands
from halo_gtk.cameras_page import CamerasPage, CameraTile
from halo_gtk.focused_live_page import FocusedLivePage


class _Client:
    def __init__(self, future):
        self.future = future

    def submit(self, _coroutine):
        if isinstance(self.future, Exception):
            raise self.future
        return self.future


async def _command():
    return None


class _Button:
    def __init__(self, *, active=False):
        self.active = active
        self.sensitive = False
        self.visible = True
        self.block_calls = 0
        self.icon_name = None
        self.tooltip = None

    def set_sensitive(self, value):
        self.sensitive = value

    def set_active(self, value):
        self.active = value

    def set_visible(self, value):
        self.visible = value

    def set_icon_name(self, value):
        self.icon_name = value

    def set_tooltip_text(self, value):
        self.tooltip = value

    def handler_block_by_func(self, _callback):
        self.block_calls += 1

    def handler_unblock_by_func(self, _callback):
        self.block_calls -= 1


class _Device:
    def __init__(self, device_id=12, *, light=False, siren=0):
        self.id = device_id
        self.name = "Front Door"
        self.light = light
        self.siren = siren

    def has_capability(self, capability):
        return capability in {"light", "siren"}


class _StateClient:
    pass


def test_observed_command_reports_async_failure_on_main_thread(monkeypatch):
    future = concurrent.futures.Future()
    callbacks = []
    monkeypatch.setattr(
        device_commands.GLib,
        "idle_add",
        lambda callback, error: callbacks.append((callback, error)),
    )
    coroutine = _command()

    returned = device_commands.submit_observed(_Client(future), coroutine, callbacks.append)
    future.set_exception(RuntimeError("rejected"))

    assert returned is future
    callback, error = callbacks.pop()
    assert callback == callbacks.append
    assert str(error) == "rejected"
    coroutine.close()


def test_observed_command_reports_success(monkeypatch):
    future = concurrent.futures.Future()
    completed = []
    monkeypatch.setattr(
        device_commands.GLib,
        "idle_add",
        lambda callback, error: callback(error),
    )
    coroutine = _command()

    device_commands.submit_observed(_Client(future), coroutine, completed.append)
    future.set_result(None)

    assert completed == [None]
    coroutine.close()


def test_observed_command_closes_coroutine_when_submit_rejects(monkeypatch):
    completed = []
    monkeypatch.setattr(
        device_commands.GLib,
        "idle_add",
        lambda callback, error: callback(error),
    )
    coroutine = _command()

    returned = device_commands.submit_observed(
        _Client(RuntimeError("loop stopped")),
        coroutine,
        completed.append,
    )

    assert returned is None
    assert str(completed[0]) == "loop stopped"
    assert coroutine.cr_frame is None


def test_capability_and_reported_state_parsing_are_defensive():
    device = SimpleNamespace(
        has_capability=lambda capability: capability == "light",
        lights=" ON ",
        siren="active",
    )

    assert device_commands.has_capability(device, "light") is True
    assert device_commands.has_capability(device, "siren") is False
    assert device_commands.reported_light_enabled(device) is True
    assert device_commands.reported_siren_active(device) is True

    broken = SimpleNamespace(has_capability=lambda _capability: 1 / 0)
    assert device_commands.has_capability(broken, "light") is False
    assert device_commands.reported_light_enabled(broken) is False
    assert device_commands.reported_siren_active(broken) is False


def test_successful_command_state_is_scoped_to_client_and_stable_device_id():
    now = [100.0]
    state = device_commands.DeviceCommandState(clock=lambda: now[0])
    first_client = _StateClient()
    replacement_client = _StateClient()
    device = _Device(light=False, siren=0)

    state.record_light_success(first_client, device, True)
    state.record_siren_success(first_client, device, 30)

    # A refreshed SDK object for the same camera retains this session's state.
    refreshed_device = _Device(device.id, light=False, siren=0)
    assert state.light_enabled(first_client, refreshed_device) is True
    assert state.siren_active(first_client, refreshed_device) is True

    # A replacement authenticated client must not inherit the previous session.
    assert state.light_enabled(replacement_client, refreshed_device) is False
    assert state.siren_active(replacement_client, refreshed_device) is False

    now[0] = 130.0
    assert state.siren_active(first_client, refreshed_device) is False

    # Optimistic state eventually yields to refreshed Ring state from another app.
    now[0] = 161.0
    refreshed_device.siren = 30
    assert state.siren_active(first_client, refreshed_device) is True


def test_light_override_reconciles_with_ring_state_or_expires():
    now = [100.0]
    state = device_commands.DeviceCommandState(clock=lambda: now[0])
    client = _StateClient()
    device = _Device(light=False)

    state.record_light_success(client, device, True)
    assert state.light_enabled(client, device) is True

    device.light = True
    assert state.light_enabled(client, device) is True
    device.light = False
    assert state.light_enabled(client, device) is False

    state.record_light_success(client, device, True)
    now[0] = 131.0
    assert state.light_enabled(client, device) is False


def test_successful_siren_stop_overrides_stale_sdk_attributes():
    state = device_commands.DeviceCommandState(clock=lambda: 100.0)
    client = _StateClient()
    device = _Device(siren=30)

    state.record_siren_success(client, device, 30)
    assert state.siren_active(client, device) is True

    state.record_siren_success(client, device, 0)
    assert state.siren_active(client, device) is False


def test_camera_grid_rolls_back_rejected_light_command():
    button = _Button(active=True)
    tile = SimpleNamespace(
        _light_btn=button,
        _on_light_toggled=lambda *_args: None,
        _refresh_light_icon=lambda: None,
    )
    device = SimpleNamespace(id=12, name="Front Door")
    errors = []
    page = CamerasPage.__new__(CamerasPage)
    page._cards = {12: tile}
    page._present_device_command_error = lambda heading, failed_device, error: errors.append(
        (heading, failed_device, error)
    )

    page._finish_tile_light_command(device, True, RuntimeError("rejected"))

    assert button.active is False
    assert button.sensitive is True
    assert button.block_calls == 0
    assert errors[0][0] == "Could not turn on light"
    assert errors[0][1] is device


def test_focused_view_surfaces_rejected_siren_stop():
    button = _Button()
    device = SimpleNamespace(name="Backyard")
    errors = []
    page = FocusedLivePage.__new__(FocusedLivePage)
    page._device = device
    page._siren_btn = button
    page._present_device_command_error = lambda heading, failed_device, error: errors.append(
        (heading, failed_device, error)
    )

    page._finish_siren_command(device, 0, RuntimeError("offline"))

    assert button.sensitive is True
    assert errors[0][0] == "Could not stop siren"


def test_focused_siren_second_click_stops_after_success(monkeypatch):
    from halo_gtk import focused_live_page

    state = device_commands.DeviceCommandState(clock=lambda: 100.0)
    client = _StateClient()
    device = _Device(siren=0)
    page = FocusedLivePage.__new__(FocusedLivePage)
    page._device = device
    page._siren_btn = _Button()
    page._present_device_command_error = lambda *_args: None
    monkeypatch.setattr(device_commands, "command_state", state)
    monkeypatch.setattr(focused_live_page, "_ring_client", lambda: client)

    page._finish_siren_command(device, 30, None, client=client)
    calls = []
    page._set_siren = lambda duration, *, client=None: calls.append((duration, client))

    page._on_siren_clicked()

    assert calls == [(0, client)]


def test_intercom_support_gates_mic_sensitivity_without_gating_volume():
    tile = CameraTile.__new__(CameraTile)
    tile._mic_btn = _Button()
    tile._volume_btn = _Button()
    tile._volume_scale = _Button()
    tile._intercom_supported = False

    tile._set_stream_controls_sensitive(True)

    assert tile._mic_btn.sensitive is False
    assert tile._volume_btn.sensitive is True
    assert tile._volume_scale.sensitive is True

    page = FocusedLivePage.__new__(FocusedLivePage)
    page._play_btn = _Button()
    page._stop_btn = _Button()
    page._mic_btn = _Button()
    page._volume_btn = _Button()
    page._volume_scale = _Button()
    page._intercom_supported = False

    page._set_controls_sensitive(True)

    assert page._mic_btn.sensitive is False
    assert page._volume_btn.sensitive is True
    assert page._volume_scale.sensitive is True


def test_focused_capability_refresh_uses_intercom_helper(monkeypatch):
    from halo_gtk import focused_live_page

    page = FocusedLivePage.__new__(FocusedLivePage)
    page._device = _Device()
    page._mic_btn = _Button(active=True)
    page._light_btn = _Button()
    page._siren_btn = _Button()
    page._siren_icon = object()
    page._on_mic_toggled = lambda *_args: None
    page._on_light_toggled = lambda *_args: None
    page._refresh_light_icon = lambda: None
    page._load_icon = lambda *_args: None
    monkeypatch.setattr(focused_live_page, "_ring_client", lambda: None)
    monkeypatch.setattr(focused_live_page, "supports_ring_camera_intercom", lambda _device: False)

    page._refresh_device_capability_controls()

    assert page._intercom_supported is False
    assert page._mic_btn.visible is False
    assert page._mic_btn.active is False

    monkeypatch.setattr(focused_live_page, "supports_ring_camera_intercom", lambda _device: True)
    page._refresh_device_capability_controls()

    assert page._intercom_supported is True
    assert page._mic_btn.visible is True
