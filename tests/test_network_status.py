from __future__ import annotations

from halo_gtk import network_status


class _Device:
    name = "Camera"
    family = "stickup_cams"
    connection_status = "online"

    def __init__(self, attrs=None, health_attrs=None):
        self._attrs = attrs if attrs is not None else {}
        self._health_attrs = health_attrs if health_attrs is not None else {}


def test_camera_network_status_uses_percent_ranges():
    expected = {
        0: ("network-wireless-disconnected-0", "Disconnected: 0%"),
        1: ("network-wireless-poor-1-24", "Poor: 1%"),
        24: ("network-wireless-poor-1-24", "Poor: 24%"),
        25: ("network-wireless-bad-25-49", "Bad: 25%"),
        49: ("network-wireless-bad-25-49", "Bad: 49%"),
        50: ("network-wireless-moderate-50-69", "Moderate: 50%"),
        69: ("network-wireless-moderate-50-69", "Moderate: 69%"),
        70: ("network-wireless-good-70-84", "Good: 70%"),
        84: ("network-wireless-good-70-84", "Good: 84%"),
        85: ("network-wireless-excellent-84-100", "Excellent: 85%"),
        100: ("network-wireless-excellent-84-100", "Excellent: 100%"),
    }

    for percent, (icon_stem, tooltip) in expected.items():
        status = network_status.camera_network_status(_Device({"wifi_signal_percentage": percent}))
        assert status.icon_path.name.startswith(icon_stem)
        assert status.tooltip == tooltip


def test_camera_network_status_converts_rssi_to_percent():
    status = network_status.camera_network_status(_Device({"rssi": -67}))

    assert status.icon_path.name.startswith("network-wireless-moderate-50-69")
    assert status.tooltip == "Moderate: 66%"


def test_camera_network_status_uses_category_when_percent_is_missing():
    status = network_status.camera_network_status(_Device({"rssi_category": "good"}))

    assert status.icon_path.name.startswith("network-wireless-good-70-84")
    assert status.tooltip == "Good: 77%"


def test_camera_network_status_uses_ethernet_icon_for_wired_connection():
    status = network_status.camera_network_status(
        _Device({"connection_type": "ethernet", "signal_percentage": 100})
    )

    assert status.icon_path.name.startswith("network-ethernet")
    assert status.tooltip == "Ethernet: 100%"
