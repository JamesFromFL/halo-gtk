from __future__ import annotations

from halo_gtk import camera_info


class _Device:
    id = 123
    device_api_id = "abc"
    name = "Front Door"
    family = "doorbots"
    kind = "lpd_v4"
    model = "Doorbell"
    connection_status = "online"
    battery_life = 100
    has_subscription = True
    subscribed = True
    subscribed_motions = True
    motion_detection = True
    light = None
    lights = None
    volume = 6
    existing_doorbell_type = None
    existing_doorbell_type_enabled = False
    _attrs = {
        "settings": {"power_mode": "wired"},
        "health": {"wifi_name": "ExampleWiFi", "auth_token": "secret"},
    }
    _health_attrs = {
        "rssi_category": "good",
        "latest_signal_strength": 77,
        "address": "private",
    }

    def has_capability(self, _capability):
        return False


def test_camera_info_sections_include_overview_and_health_details():
    sections = camera_info.camera_info_sections(_Device())
    titles = [section["title"] for section in sections]

    assert "Overview" in titles
    assert "Health Details" in titles

    overview = next(section for section in sections if section["title"] == "Overview")
    assert ("Power", "Plugged-In / Hardwired") in overview["rows"]
    assert ("Connection", "Good: 77%") in overview["rows"]


def test_camera_info_sections_redact_sensitive_metadata():
    sections = camera_info.camera_info_sections(_Device())
    health_rows = dict(
        next(section for section in sections if section["title"] == "Health Details")["rows"]
    )
    metadata_rows = dict(
        next(section for section in sections if section["title"] == "Device Metadata")["rows"]
    )

    assert health_rows["address"] == "<redacted>"
    assert metadata_rows["health.auth_token"] == "<redacted>"
