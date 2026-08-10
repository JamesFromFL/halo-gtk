from __future__ import annotations

from halo_gtk import power_status


class _Device:
    name = "Camera"
    family = "stickup_cams"

    def __init__(self, battery_life=None, attrs=None, battery_capable=True):
        self.battery_life = battery_life
        self._attrs = attrs if attrs is not None else {}
        self._battery_capable = battery_capable

    def has_capability(self, capability):
        if str(capability).lower().endswith("battery") or str(capability).lower() == "battery":
            return self._battery_capable
        return False


class _BrokenDevice:
    @property
    def battery_life(self):
        raise RuntimeError("unavailable")

    @property
    def _attrs(self):
        raise RuntimeError("unavailable")


def test_camera_power_status_uses_plugged_icon_when_no_battery_metadata():
    status = power_status.camera_power_status(_Device())

    assert status.icon_path.name in {
        "power-hardwired-dark-theme.png",
        "power-hardwired-light-theme.png",
    }
    assert status.icon_path.is_file()
    assert status.tooltip == "Plugged-In / Hardwired"


def test_camera_power_status_uses_unknown_icon_when_metadata_is_unavailable():
    status = power_status.camera_power_status(_BrokenDevice())

    assert status.icon_path.name.endswith("power-unknown.png")
    assert status.tooltip == "Unknown / Error"


def test_camera_power_status_uses_expected_battery_ranges():
    expected = {
        100: "power-battery-100-76.png",
        76: "power-battery-100-76.png",
        75: "power-battery-75-51.png",
        51: "power-battery-75-51.png",
        50: "power-battery-50-26.png",
        26: "power-battery-50-26.png",
        25: "power-battery-25-6.png",
        6: "power-battery-25-6.png",
        5: "power-battery-5-0.png",
        0: "power-battery-5-0.png",
    }

    for percent, icon_name in expected.items():
        status = power_status.camera_power_status(_Device(percent))
        assert status.icon_path.name.endswith(icon_name)
        assert status.tooltip == f"Battery: {percent}%"


def test_camera_power_status_prioritizes_external_power_over_battery_field():
    status = power_status.camera_power_status(
        _Device(
            0,
            attrs={
                "external_connection": True,
                "settings": {"power_mode": "wired"},
            },
        )
    )

    assert status.icon_path.name in {
        "power-hardwired-dark-theme.png",
        "power-hardwired-light-theme.png",
    }
    assert status.tooltip == "Plugged-In / Hardwired"


def test_camera_power_status_uses_charging_battery_icon_for_battery_on_external_power():
    status = power_status.camera_power_status(
        _Device(
            63,
            attrs={
                "external_connection": True,
                "battery_present": True,
            },
        )
    )

    assert status.icon_path.name.endswith("power-charging-battery-75-51.png")
    assert status.tooltip == "Battery charging: 63%"


def test_camera_power_status_uses_battery_unknown_icon_when_battery_has_no_percent():
    status = power_status.camera_power_status(_Device(attrs={"battery_present": True}))

    assert status.icon_path.name.endswith("power-battery-unknown.png")
    assert status.tooltip == "Battery: Unknown"


def test_camera_power_status_treats_external_power_without_battery_as_hardwired():
    status = power_status.camera_power_status(
        _Device(
            attrs={
                "external_connection": True,
                "battery_present": False,
            },
        )
    )

    assert status.icon_path.name in {
        "power-hardwired-dark-theme.png",
        "power-hardwired-light-theme.png",
    }
    assert status.tooltip == "Plugged-In / Hardwired"


def test_camera_power_status_uses_capability_to_classify_wired_devices():
    status = power_status.camera_power_status(_Device(100, battery_capable=False))

    assert status.icon_path.name in {
        "power-hardwired-dark-theme.png",
        "power-hardwired-light-theme.png",
    }
    assert status.tooltip == "Plugged-In / Hardwired"
