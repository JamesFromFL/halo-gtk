from __future__ import annotations

from halo_gtk import camera_settings


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
