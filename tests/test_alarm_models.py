"""Tests for immutable, provider-neutral Alarm snapshots."""

from dataclasses import FrozenInstanceError

import pytest

from halo_gtk.alarm import AlarmCommandResult, AlarmCommandStatus, AlarmMode
from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmCapability,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmLocationSnapshot,
    AlarmServiceStatus,
    AlarmSignal,
    TriState,
    empty_alarm_snapshot,
)


def test_public_alarm_package_exports_command_outcomes():
    result = AlarmCommandResult(
        status=AlarmCommandStatus.UNAVAILABLE,
        location_id="location-1",
        requested_mode=AlarmMode.AWAY,
    )

    assert result.status is AlarmCommandStatus.UNAVAILABLE


def test_empty_snapshot_is_stopped_and_generation_scoped():
    snapshot = empty_alarm_snapshot(generation=7)

    assert snapshot.generation == 7
    assert snapshot.revision == 0
    assert snapshot.status is AlarmServiceStatus.STOPPED
    assert snapshot.locations == ()


def test_nested_snapshots_are_immutable_and_copy_mutable_inputs():
    capabilities = {AlarmCapability.CONTACT}
    device = AlarmDeviceSnapshot(
        "location-1",
        "asset-1",
        "device-1",
        kind=AlarmDeviceKind.CONTACT_SENSOR,
        capabilities=capabilities,
    )
    devices = [device]
    asset = AlarmAssetSnapshot("location-1", "asset-1", devices=devices)
    location = AlarmLocationSnapshot("location-1", assets=[asset])
    account = AlarmAccountSnapshot(locations=[location])

    capabilities.add(AlarmCapability.TAMPER)
    devices.clear()

    assert device.capabilities == frozenset({AlarmCapability.CONTACT})
    assert asset.devices == (device,)
    assert location.devices == (device,)
    assert location.security_panel is None
    assert account.find_location("location-1") is location
    with pytest.raises(FrozenInstanceError):
        device.name = "Changed"


def test_sensor_signals_remain_independent_tri_states():
    device = AlarmDeviceSnapshot(
        "location-1",
        "asset-1",
        "listener-1",
        kind=AlarmDeviceKind.SMOKE_CO_LISTENER,
        flood=TriState.ACTIVE,
        freeze=TriState.INACTIVE,
        smoke=TriState.UNKNOWN,
        carbon_monoxide=TriState.ACTIVE,
        signal=AlarmSignal.FIRE_OR_CARBON_MONOXIDE,
    )

    assert device.flood is TriState.ACTIVE
    assert device.freeze is TriState.INACTIVE
    assert device.smoke is TriState.UNKNOWN
    assert device.carbon_monoxide is TriState.ACTIVE
    assert device.signal is AlarmSignal.FIRE_OR_CARBON_MONOXIDE


def test_snapshot_containers_reject_mismatched_child_identity():
    wrong_location = AlarmDeviceSnapshot("location-2", "asset-1", "device-1")
    with pytest.raises(ValueError, match="containing asset"):
        AlarmAssetSnapshot("location-1", "asset-1", devices=(wrong_location,))

    asset = AlarmAssetSnapshot("location-2", "asset-1")
    with pytest.raises(ValueError, match="containing location"):
        AlarmLocationSnapshot("location-1", assets=(asset,))


def test_snapshot_containers_reject_duplicate_identity():
    device = AlarmDeviceSnapshot("location-1", "asset-1", "device-1")
    with pytest.raises(ValueError, match="unique zids"):
        AlarmAssetSnapshot("location-1", "asset-1", devices=(device, device))

    location = AlarmLocationSnapshot("location-1")
    with pytest.raises(ValueError, match="unique ids"):
        AlarmAccountSnapshot(locations=(location, location))


def test_location_device_lookup_fails_closed_for_cross_asset_zid_collision():
    first = AlarmDeviceSnapshot("location-1", "asset-1", "shared-zid")
    second = AlarmDeviceSnapshot("location-1", "asset-2", "shared-zid")
    first_asset = AlarmAssetSnapshot("location-1", "asset-1", devices=(first,))
    second_asset = AlarmAssetSnapshot("location-1", "asset-2", devices=(second,))
    location = AlarmLocationSnapshot("location-1", assets=(first_asset, second_asset))

    assert location.find_device("shared-zid") is None
    assert location.find_asset("asset-1").find_device("shared-zid") is first
    assert location.find_asset("asset-2").find_device("shared-zid") is second


def test_location_security_panel_lookup_fails_closed_for_multiple_panels():
    first = AlarmDeviceSnapshot(
        "location-1",
        "asset-1",
        "panel-1",
        kind=AlarmDeviceKind.SECURITY_PANEL,
    )
    second = AlarmDeviceSnapshot(
        "location-1",
        "asset-2",
        "panel-2",
        kind=AlarmDeviceKind.SECURITY_PANEL,
    )
    location = AlarmLocationSnapshot(
        "location-1",
        assets=(
            AlarmAssetSnapshot("location-1", "asset-1", devices=(first,)),
            AlarmAssetSnapshot("location-1", "asset-2", devices=(second,)),
        ),
    )

    assert location.security_panel is None


@pytest.mark.parametrize("value", [-1, 101, True, 4.5])
def test_device_rejects_invalid_battery_level(value):
    with pytest.raises(ValueError, match="battery_level"):
        AlarmDeviceSnapshot("location-1", "asset-1", "device-1", battery_level=value)


def test_tri_state_only_treats_real_booleans_as_known():
    assert TriState.from_bool(True) is TriState.ACTIVE
    assert TriState.from_bool(False) is TriState.INACTIVE
    assert TriState.from_bool(1) is TriState.UNKNOWN
    assert TriState.from_bool(None) is TriState.UNKNOWN
