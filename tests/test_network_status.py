from __future__ import annotations

import pytest

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
        0: ("network-offline-symbolic", "Disconnected: 0%"),
        1: ("network-wireless-signal-none-symbolic", "Poor: 1%"),
        24: ("network-wireless-signal-none-symbolic", "Poor: 24%"),
        25: ("network-wireless-signal-weak-symbolic", "Bad: 25%"),
        49: ("network-wireless-signal-weak-symbolic", "Bad: 49%"),
        50: ("network-wireless-signal-ok-symbolic", "Moderate: 50%"),
        69: ("network-wireless-signal-ok-symbolic", "Moderate: 69%"),
        70: ("network-wireless-signal-good-symbolic", "Good: 70%"),
        84: ("network-wireless-signal-good-symbolic", "Good: 84%"),
        85: ("network-wireless-signal-excellent-symbolic", "Excellent: 85%"),
        100: ("network-wireless-signal-excellent-symbolic", "Excellent: 100%"),
    }

    for percent, (icon_name, tooltip) in expected.items():
        status = network_status.camera_network_status(_Device({"wifi_signal_percentage": percent}))
        assert status.icon_name == icon_name
        assert status.tooltip == tooltip


def test_camera_network_status_converts_rssi_to_percent():
    status = network_status.camera_network_status(_Device({"rssi": -67}))

    assert status.icon_name == "network-wireless-signal-ok-symbolic"
    assert status.tooltip == "Moderate: 66%"


def test_camera_network_status_uses_category_when_percent_is_missing():
    status = network_status.camera_network_status(_Device({"rssi_category": "good"}))

    assert status.icon_name == "network-wireless-signal-good-symbolic"
    assert status.tooltip == "Good: 77%"


def test_camera_network_status_uses_ethernet_icon_for_wired_connection():
    status = network_status.camera_network_status(
        _Device({"connection_type": "ethernet", "signal_percentage": 100})
    )

    assert status.icon_name == "network-wired-symbolic"
    assert status.tooltip == "Ethernet: 100%"


@pytest.mark.parametrize("connection", ["wlan", "wireless LAN", "Wi-Fi"])
def test_wireless_lan_is_not_classified_as_ethernet(connection):
    status = network_status.camera_network_status(
        _Device({"connection_type": connection, "signal_percentage": 100})
    )

    assert status.icon_name == "network-wireless-signal-excellent-symbolic"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "-inf", "nan", 10**1000])
def test_invalid_signal_values_fall_back_to_valid_health_data(value):
    status = network_status.camera_network_status(
        _Device({"signal_percentage": value, "rssi": -67})
    )

    assert status.tooltip == "Moderate: 66%"


def test_missing_signal_does_not_claim_an_online_camera_is_disconnected():
    status = network_status.camera_network_status(_Device())

    assert status.tooltip == "Connection: Unknown"
    assert status.icon_name == "network-wireless-symbolic"


def test_offline_status_overrides_cached_signal_strength():
    device = _Device({"connection_type": "ethernet", "signal_percentage": 100})
    device.connection_status = "offline"

    status = network_status.camera_network_status(device)

    assert status.tooltip == "Disconnected: 0%"
    assert status.icon_name == "network-offline-symbolic"
