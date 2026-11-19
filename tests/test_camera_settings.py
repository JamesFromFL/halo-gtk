from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from halo_gtk import camera_settings, ring_client
from tests.privacy_helpers import grant


class _Device:
    id = 123
    name = "Front Door"
    family = "doorbots"
    model = "Doorbell"
    motion_detection = True
    volume = 6
    existing_doorbell_type = "Mechanical"
    existing_doorbell_type_enabled = True
    existing_doorbell_type_duration = None
    _attrs = {"settings": {"chime_settings": {"type": 0, "enable": True}}}

    def has_capability(self, capability):
        return capability == "motion_detection"

    async def async_set_motion_detection(self, _value):
        return None

    async def async_set_volume(self, _value):
        return None

    async def async_set_existing_doorbell_type(self, _value):
        return None

    async def async_set_existing_doorbell_type_enabled(self, _value):
        return None

    async def async_set_existing_doorbell_type_duration(self, _value):
        return None


class _StickupCam:
    id = 456
    name = "Garage"
    family = "stickup_cams"
    model = "Stick Up Cam"
    motion_detection = True
    _attrs = {"settings": {"motion_detection_enabled": True}}

    def has_capability(self, capability):
        return capability == "motion_detection"

    async def async_set_motion_detection(self, _value):
        return None


def _keys(device):
    return [spec.key for spec in camera_settings.camera_setting_specs(device)]


def test_camera_setting_specs_include_camera_motion_detection_only():
    assert _keys(_StickupCam()) == ["nickname", "motion_detection"]


def test_camera_setting_specs_include_supported_doorbell_settings():
    assert _keys(_Device()) == [
        "nickname",
        "motion_detection",
        "doorbell_volume",
        "chime_type",
        "chime_enabled",
    ]


def test_camera_setting_specs_include_digital_chime_duration_when_applicable():
    device = _Device()
    device.existing_doorbell_type = "Digital"
    device.existing_doorbell_type_duration = 5

    assert "chime_duration" in _keys(device)


def test_camera_setting_specs_hide_chime_settings_when_metadata_is_missing():
    device = _Device()
    device.existing_doorbell_type = None

    assert _keys(device) == ["nickname", "motion_detection", "doorbell_volume"]


def test_set_nested_attr_updates_ring_settings_cache():
    device = _Device()

    camera_settings._set_nested_attr(
        device,
        ("settings", "motion_detection_enabled"),
        False,
    )

    assert device._attrs["settings"]["motion_detection_enabled"] is False


def bound_window(monkeypatch, device):
    active = [grant(SimpleNamespace(_published_generation=1, _stopped=False), device)]
    owner = active[0]
    owner.get_device_by_id = lambda _id: device
    monkeypatch.setattr(camera_settings, "get_client", lambda: active[0])
    window = camera_settings.CameraSettingsWindow.__new__(camera_settings.CameraSettingsWindow)
    window.device = device
    window._owner = owner
    window._generation = owner._published_generation
    window._device_id, window._family = device.id, device.family
    window._loading = window._closed = False
    window._setting_serial = 0
    window._set_status = Mock()
    window._motion_switch = None
    return window, active


def test_replaced_account_retires_all_camera_controls_including_pending_writes(monkeypatch):
    old_device = _Device()
    window, active = bound_window(monkeypatch, old_device)
    old_client = active[0]
    new_device = _Device()  # Even an identical ID and family cannot retarget this window.
    new_client = grant(SimpleNamespace(_published_generation=2, _stopped=False), new_device)
    new_client.get_device_by_id = lambda _id: new_device
    captures = []
    close = Mock()
    window.close = close
    save_name = Mock()
    monkeypatch.setattr(camera_settings.device_names, "set_nickname", save_name)
    monkeypatch.setattr(camera_settings.GLib, "idle_add", lambda fn, *args: fn(*args))
    monkeypatch.setattr(
        camera_settings._SettingQueue, "enqueue", lambda _key, _client, req: captures.append(req)
    )
    for client in (new_client, old_client):
        client._async_camera_privacy_setting = AsyncMock()
    writes = []
    for device in (old_device, new_device):
        for name in (
            "async_set_volume",
            "async_set_existing_doorbell_type",
            "async_set_existing_doorbell_type_enabled",
            "async_set_existing_doorbell_type_duration",
        ):
            method = AsyncMock()
            setattr(device, name, method)
            writes.append(method)

    control = SimpleNamespace(get_value=lambda: 3, get_selected=lambda: 1, get_active=lambda: False)

    def edit():
        window._on_motion_detection_changed(control, None)
        window._on_volume_changed(control)
        window._on_chime_type_changed(control, None)
        window._on_chime_enabled_changed(control, None)
        window._on_chime_duration_changed(control)

    edit()
    assert len(captures) == 5
    active[0] = new_client
    edit()
    window._save_nickname("retired")
    assert len(captures) == 5 and window._closed
    close.assert_called_once()
    for request in captures:
        queue = camera_settings._SettingQueue(("fixture",), old_client)
        with pytest.raises(RuntimeError, match="changed"):
            asyncio.run(queue._invoke(request))
    for method in writes:
        method.assert_not_called()
    old_client._async_camera_privacy_setting.assert_not_called()
    new_client._async_camera_privacy_setting.assert_not_called()
    save_name.assert_not_called()


def test_volume_resolves_current_device_again_at_dispatch(monkeypatch):
    original, replacement = _Device(), _Device()
    original.async_set_volume = AsyncMock()
    replacement.async_set_volume = AsyncMock()
    window, active = bound_window(monkeypatch, original)
    captured = []
    monkeypatch.setattr(
        camera_settings._SettingQueue, "enqueue", lambda _key, _client, req: captured.append(req)
    )
    window._on_volume_changed(SimpleNamespace(get_value=lambda: 8))
    active[0].get_device_by_id = lambda _id: replacement
    asyncio.run(captured[0].invoke())
    original.async_set_volume.assert_not_called()
    replacement.async_set_volume.assert_awaited_once_with(8)


def test_native_motion_control_shows_observed_value_after_contrary_receipt(monkeypatch):
    from gi.repository import Gdk

    if os.environ.get("HALO_NATIVE_LAYOUT_TESTS") != "1":
        pytest.skip("Requires isolated GTK display")
    camera_settings.Adw.init()
    assert Gdk.Display.get_default() is not None
    device = _Device()
    client = grant(SimpleNamespace(_published_generation=1, _stopped=False), device)
    client.get_device_by_id = lambda _id: device
    monkeypatch.setattr(ring_client, "get_client", lambda: client)
    monkeypatch.setattr(camera_settings, "get_client", lambda: client)
    captured = []
    monkeypatch.setattr(
        camera_settings._SettingQueue, "enqueue", lambda _key, _client, req: captured.append(req)
    )
    window = camera_settings.CameraSettingsWindow(device)
    window.present()
    try:
        assert window._motion_switch.get_active()
        client._async_camera_privacy_setting = AsyncMock(return_value=True)
        window._motion_switch.set_active(False)
        assert window._motion_switch.get_active() and len(captured) == 1
        asyncio.run(captured[0].invoke())
        monkeypatch.setattr(camera_settings.GLib, "idle_add", lambda fn, *args: fn(*args))
        captured[0].finished(None, False)
        assert window._motion_switch.get_active()
        assert "still on" in window._status_row.get_subtitle()
        assert device._attrs["settings"]["motion_detection_enabled"] is True
        client.camera_privacy.invalidate()
        window._refresh_camera()
        assert not window._motion_switch.get_visible()
        assert "verify" in window._motion_row.get_subtitle()
    finally:
        window.close()
