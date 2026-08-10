"""Tests for the revisioned Alarm state projection."""

import pytest

import halo_gtk.alarm.store as alarm_store
from halo_gtk.alarm.models import (
    AlarmCapability,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmInventoryStatus,
    AlarmLockState,
    AlarmMode,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmSignal,
    AlarmWriteAuthorization,
    TriState,
)
from halo_gtk.alarm.protocol import (
    AssetSessionInfo,
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    HubDisconnection,
    SessionInfo,
    UnknownMessage,
)
from halo_gtk.alarm.store import AlarmStateBoundsError, AlarmStateStore


def _device(zid, device_type, **data):
    return DeviceDocument(
        zid=zid,
        data={"zid": zid, "deviceType": device_type, "name": zid, **data},
    )


def _registered_store(*assets):
    store = AlarmStateStore(generation=3, clock=lambda: 1_700_000_000.0)
    store.set_service_status(AlarmServiceStatus.SYNCING)
    store.register_location(
        "location-1",
        "Home",
        assets,
        write_authorization=AlarmWriteAuthorization.ALLOWED,
    )
    epoch = store.begin_epoch("location-1")
    sessions = tuple(
        AssetSessionInfo(
            item if isinstance(item, str) else item[0],
            AlarmConnectionStatus.ONLINE,
            "" if isinstance(item, str) else item[1],
        )
        for item in assets
    )
    store.apply_message("location-1", SessionInfo(sessions), epoch)
    return store, epoch


def test_full_lists_publish_each_asset_without_waiting_for_every_asset():
    store, epoch = _registered_store(
        ("base-station", "base_station_v1"),
        ("bridge", "beams_bridge_v1"),
    )

    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("contact-1", "sensor.contact", faulted=False),),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location is not None
    assert location.inventory is AlarmInventoryStatus.PARTIAL
    assert location.find_device("contact-1") is not None
    assert location.find_asset("bridge").stale is True

    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList("bridge", ()),
        epoch,
    )
    assert snapshot.find_location("location-1").inventory is AlarmInventoryStatus.COMPLETE


def test_cumulative_unique_device_limit_is_atomic(monkeypatch):
    monkeypatch.setattr(alarm_store, "MAX_DEVICE_DOCUMENTS", 2)
    store, epoch = _registered_store("base-station")
    store.apply_message(
        "location-1",
        DeviceInfoDocUpdate(
            "base-station",
            (_device("sensor-1", "sensor.contact", faulted=False),),
        ),
        epoch,
    )
    before = store.apply_message(
        "location-1",
        DeviceInfoDocUpdate(
            "base-station",
            (_device("sensor-2", "sensor.contact", faulted=False),),
        ),
        epoch,
    )

    with pytest.raises(AlarmStateBoundsError, match="retained-state-limit"):
        store.apply_message(
            "location-1",
            DeviceInfoDocUpdate(
                "base-station",
                (_device("sensor-3", "sensor.contact", faulted=False),),
            ),
            epoch,
        )

    assert store.snapshot is before
    assert tuple(device.zid for device in store.snapshot.find_location("location-1").devices) == (
        "sensor-1",
        "sensor-2",
    )


def test_cumulative_retained_node_limit_is_atomic(monkeypatch):
    store, epoch = _registered_store("base-station")
    before = store.apply_message(
        "location-1",
        DeviceInfoDocUpdate(
            "base-station",
            (_device("sensor-1", "sensor.contact", faulted=False),),
        ),
        epoch,
    )
    monkeypatch.setattr(alarm_store, "MAX_JSON_NODES", 6)

    with pytest.raises(AlarmStateBoundsError, match="retained-state-limit"):
        store.apply_message(
            "location-1",
            DeviceInfoDocUpdate(
                "base-station",
                (_device("sensor-2", "sensor.contact", faulted=False),),
            ),
            epoch,
        )

    assert store.snapshot is before
    assert store.snapshot.find_location("location-1").find_device("sensor-2") is None


def test_cumulative_retained_serialized_byte_limit_is_atomic(monkeypatch):
    store, epoch = _registered_store("base-station")
    before = store.apply_message(
        "location-1",
        DeviceInfoDocUpdate(
            "base-station",
            (_device("sensor-1", "sensor.contact", faulted=False),),
        ),
        epoch,
    )
    monkeypatch.setattr(alarm_store, "MAX_FRAME_BYTES", 256)

    with pytest.raises(AlarmStateBoundsError, match="retained-state-limit"):
        store.apply_message(
            "location-1",
            DeviceInfoDocUpdate(
                "base-station",
                (_device("sensor-2", "sensor.contact", name="x" * 512),),
            ),
            epoch,
        )

    assert store.snapshot is before
    assert store.snapshot.find_location("location-1").find_device("sensor-2") is None


def test_repeated_unknown_large_patch_fields_are_not_retained():
    store, epoch = _registered_store("base-station")
    before = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("sensor-1", "sensor.contact", faulted=False),),
        ),
        epoch,
    )
    large_value = "x" * (alarm_store.MAX_FRAME_BYTES - 128)

    for index in range(8):
        result = store.apply_message(
            "location-1",
            DeviceInfoDocUpdate(
                "base-station",
                (
                    DeviceDocument(
                        "sensor-1",
                        {"zid": "sensor-1", f"future-field-{index}": large_value},
                    ),
                ),
            ),
            epoch,
        )
        assert result is before

    retained = store._locations["location-1"].assets["base-station"].devices["sensor-1"].data
    assert set(retained) == {"zid", "name", "deviceType", "faulted"}


def test_full_list_replaces_asset_and_delta_shallow_merges_by_zid():
    store, epoch = _registered_store("base-station")
    initial = DeviceInfoDocList(
        "base-station",
        (
            _device("contact-1", "sensor.contact", faulted=False, batteryLevel=80),
            _device("future-1", "vendor.future-sensor", customState="ready"),
        ),
    )
    first = store.apply_message("location-1", initial, epoch)
    unchanged = store.apply_message("location-1", initial, epoch)

    assert unchanged is first
    assert unchanged.revision == first.revision

    updated = store.apply_message(
        "location-1",
        DeviceInfoDocUpdate(
            "base-station",
            (DeviceDocument("contact-1", {"zid": "contact-1", "faulted": True}),),
        ),
        epoch,
    )
    contact = updated.find_location("location-1").find_device("contact-1")
    unknown = updated.find_location("location-1").find_device("future-1")

    assert contact.contact is TriState.ACTIVE
    assert contact.battery_level == 80
    assert unknown.kind is AlarmDeviceKind.UNKNOWN
    assert unknown.raw_type == "vendor.future-sensor"

    replaced = store.apply_message(
        "location-1",
        DeviceInfoDocList("base-station", (_device("contact-1", "sensor.contact"),)),
        epoch,
    )
    assert replaced.find_location("location-1").find_device("future-1") is None


def test_sensor_capabilities_and_signals_are_normalized_independently():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "listener-1",
                    "listener.smoke-co",
                    flood={"faulted": True},
                    freeze={"faulted": False},
                    smoke={"alarmStatus": "clear"},
                    co={"alarmStatus": "active"},
                    batteryStatus="low",
                    tamperStatus="ok",
                ),
            ),
        ),
        epoch,
    )
    device = snapshot.find_location("location-1").find_device("listener-1")

    assert device.flood is TriState.ACTIVE
    assert device.freeze is TriState.INACTIVE
    assert device.smoke is TriState.INACTIVE
    assert device.carbon_monoxide is TriState.ACTIVE
    assert device.battery_low is TriState.ACTIVE
    assert device.tamper is TriState.INACTIVE
    assert AlarmCapability.SMOKE in device.capabilities
    assert AlarmCapability.CARBON_MONOXIDE in device.capabilities
    assert device.signal is AlarmSignal.CARBON_MONOXIDE


@pytest.mark.parametrize(
    ("smoke_status", "co_status", "expected_signal"),
    [
        ("inactive", "inactive", AlarmSignal.NONE),
        ("active", "inactive", AlarmSignal.FIRE),
        ("inactive", "active", AlarmSignal.CARBON_MONOXIDE),
        ("active", "active", AlarmSignal.FIRE_OR_CARBON_MONOXIDE),
    ],
)
def test_kidde_smoke_co_components_preserve_distinct_life_safety_signals(
    smoke_status,
    co_status,
    expected_signal,
):
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "kidde-1",
                    "comp.bluejay.sensor_bluejay_wsc",
                    components={
                        "alarm.smoke": {"alarmStatus": smoke_status},
                        "alarm.co": {"alarmStatus": co_status},
                    },
                ),
            ),
        ),
        epoch,
    )
    device = snapshot.find_location("location-1").find_device("kidde-1")

    assert device.kind is AlarmDeviceKind.SMOKE_CO_ALARM
    assert device.smoke is (TriState.ACTIVE if smoke_status == "active" else TriState.INACTIVE)
    assert device.carbon_monoxide is (
        TriState.ACTIVE if co_status == "active" else TriState.INACTIVE
    )
    assert device.signal is expected_signal
    assert device.capabilities >= {
        AlarmCapability.SMOKE,
        AlarmCapability.CARBON_MONOXIDE,
    }
    assert not hasattr(device, "components")
    assert not hasattr(device, "__dict__")


def test_life_safety_components_infer_future_device_kind_by_capability():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "future-alarm",
                    "vendor.future-life-safety",
                    components={
                        "alarm.smoke": {"alarmStatus": "active"},
                        "alarm.co": {"alarmStatus": "future-value"},
                    },
                ),
            ),
        ),
        epoch,
    )
    device = snapshot.find_location("location-1").find_device("future-alarm")

    assert device.kind is AlarmDeviceKind.SMOKE_CO_ALARM
    assert device.smoke is TriState.ACTIVE
    assert device.carbon_monoxide is TriState.UNKNOWN
    assert device.signal is AlarmSignal.FIRE


def test_active_inferred_life_safety_device_makes_location_read_only():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device("panel-1", "security-panel", mode="none"),
                _device(
                    "future-alarm",
                    "vendor.future-life-safety",
                    components={
                        "alarm.smoke": {"alarmStatus": "active"},
                        "alarm.co": {"alarmStatus": "inactive"},
                    },
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "life-safety-active"


def test_future_life_safety_type_and_components_survive_delta_merges():
    store, epoch = _registered_store("base-station")
    store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device("panel-1", "security-panel", mode="none"),
                _device(
                    "future-alarm",
                    "vendor.future-life-safety",
                    components={
                        "alarm.smoke": {"alarmStatus": "inactive"},
                        "alarm.co": {"alarmStatus": "inactive"},
                    },
                ),
            ),
        ),
        epoch,
    )
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocUpdate(
            "base-station",
            (
                DeviceDocument(
                    "future-alarm",
                    {
                        "zid": "future-alarm",
                        "components": {
                            "alarm.smoke": {"alarmStatus": "active"},
                            "alarm.co": {"alarmStatus": "inactive"},
                        },
                    },
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")
    device = location.find_device("future-alarm")

    assert device.raw_type == "vendor.future-life-safety"
    assert device.kind is AlarmDeviceKind.SMOKE_CO_ALARM
    assert device.smoke is TriState.ACTIVE
    assert device.carbon_monoxide is TriState.INACTIVE
    assert location.command_unavailable_reason == "life-safety-active"


def test_panel_faulted_device_list_is_normalized_without_raw_alarm_info():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmInfo={"state": "none", "faultedDevices": ["contact-1"]},
                ),
                _device("contact-1", "sensor.contact", faulted=False),
            ),
        ),
        epoch,
    )
    panel = snapshot.find_location("location-1").security_panel

    assert panel.faulted_device_ids == ("contact-1",)
    assert panel.faulted_devices_valid is True
    assert not hasattr(panel, "alarmInfo")


def test_panel_faulted_device_list_over_wire_limit_is_invalid():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmInfo={
                        "state": "none",
                        "faultedDevices": [f"sensor-{index}" for index in range(257)],
                    },
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")
    panel = location.security_panel

    assert panel.faulted_device_ids is None
    assert panel.faulted_devices_valid is False
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "faulted-device-list-invalid"


def test_panel_transition_is_normalized_and_remains_technically_ready():
    store, epoch = _registered_store(("base-station", "base_station_v1"))
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="some",
                    transitionDelayEndTimestamp=1_700_000_123_000,
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.mode is AlarmMode.HOME
    assert location.phase is AlarmPhase.EXIT_DELAY
    assert location.transition_deadline == 1_700_000_123.0
    assert location.triggered is TriState.INACTIVE
    assert location.can_set_mode is True
    assert location.command_unavailable_reason is None


@pytest.mark.parametrize(
    ("raw_state", "signal"),
    [
        ("burglar-alarm", AlarmSignal.BURGLAR),
        ("fire-alarm", AlarmSignal.FIRE),
        ("co-alarm", AlarmSignal.CARBON_MONOXIDE),
        ("user-verified-co-or-fire-alarm", AlarmSignal.FIRE_OR_CARBON_MONOXIDE),
        ("panic", AlarmSignal.PANIC),
    ],
)
def test_panel_preserves_distinct_emergency_signal(raw_state, signal):
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="all",
                    alarmInfo={"state": raw_state},
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.ALARMING
    assert location.triggered is TriState.ACTIVE
    assert location.signal is signal


def test_entry_delay_is_not_reported_as_an_active_alarm_signal():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="all",
                    alarmInfo={"state": "entry-delay"},
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.ENTRY_DELAY
    assert location.triggered is TriState.INACTIVE
    assert location.signal is AlarmSignal.NONE


def test_future_panel_alarm_state_is_unknown_triggered_and_read_only():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmInfo={"state": "future-alarm-state"},
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.UNKNOWN
    assert location.signal is AlarmSignal.UNKNOWN
    assert location.triggered is TriState.UNKNOWN
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "panel-state-unknown"


@pytest.mark.parametrize("alarm_info", [None, {}, {"state": None}, {"state": ""}])
def test_malformed_panel_alarm_info_is_unknown_and_read_only(alarm_info):
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmInfo=alarm_info,
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.UNKNOWN
    assert location.signal is AlarmSignal.UNKNOWN
    assert location.triggered is TriState.UNKNOWN
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "panel-state-unknown"


def test_uncategorized_active_panel_alarm_keeps_signal_unknown():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmStatus="active",
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.ALARMING
    assert location.signal is AlarmSignal.UNKNOWN
    assert location.triggered is TriState.ACTIVE
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "alarm-active"


@pytest.mark.parametrize(
    "alarm_info",
    [None, {}, {"state": None}, {"state": "entry-delay"}],
)
def test_explicit_active_panel_status_dominates_conflicting_alarm_info(alarm_info):
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmStatus="active",
                    alarmInfo=alarm_info,
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.ALARMING
    assert location.signal is AlarmSignal.UNKNOWN
    assert location.triggered is TriState.ACTIVE
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "alarm-active"


def test_known_alarm_info_emergency_dominates_future_panel_status():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmStatus="future-status",
                    alarmInfo={"state": "burglar-alarm"},
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.ALARMING
    assert location.signal is AlarmSignal.BURGLAR
    assert location.triggered is TriState.ACTIVE
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "alarm-active"


@pytest.mark.parametrize("alarm_status", ["future-status", None, 1, False])
def test_unrecognized_present_panel_alarm_status_is_unknown_and_read_only(alarm_status):
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "panel-1",
                    "security-panel",
                    mode="none",
                    alarmStatus=alarm_status,
                ),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.phase is AlarmPhase.UNKNOWN
    assert location.signal is AlarmSignal.UNKNOWN
    assert location.triggered is TriState.UNKNOWN
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "panel-state-unknown"


def test_cellular_session_marks_retained_inventory_stale_and_read_only():
    store, epoch = _registered_store("base-station")
    store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("panel-1", "security-panel", mode="none"),),
        ),
        epoch,
    )
    snapshot = store.apply_message(
        "location-1",
        SessionInfo(
            (
                AssetSessionInfo(
                    "base-station",
                    AlarmConnectionStatus.CELLULAR_BACKUP,
                    "base_station_v1",
                ),
            )
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.connection is AlarmConnectionStatus.CELLULAR_BACKUP
    assert location.inventory is AlarmInventoryStatus.STALE
    assert location.find_device("panel-1").stale is True
    assert location.find_device("panel-1").mode is AlarmMode.DISARMED
    assert location.mode is AlarmMode.UNKNOWN
    assert location.phase is AlarmPhase.UNKNOWN
    assert location.signal is AlarmSignal.UNKNOWN
    assert location.triggered is TriState.UNKNOWN
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "cellular-backup"


def test_panel_asset_connection_drives_location_even_when_bridge_is_online():
    store, epoch = _registered_store(
        ("base-station", "base_station_v1"),
        ("bridge", "beams_bridge_v1"),
    )
    store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("panel-1", "security-panel", mode="none"),),
        ),
        epoch,
    )
    store.apply_message("location-1", DeviceInfoDocList("bridge", ()), epoch)

    snapshot = store.apply_message(
        "location-1",
        SessionInfo(
            (
                AssetSessionInfo(
                    "base-station",
                    AlarmConnectionStatus.OFFLINE,
                    "base_station_v1",
                ),
                AssetSessionInfo(
                    "bridge",
                    AlarmConnectionStatus.ONLINE,
                    "beams_bridge_v1",
                ),
            )
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.connection is AlarmConnectionStatus.OFFLINE
    assert location.mode is AlarmMode.UNKNOWN
    assert location.triggered is TriState.UNKNOWN
    assert location.find_asset("bridge").connection is AlarmConnectionStatus.ONLINE


def test_multiple_security_panels_are_ambiguous_and_read_only():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device("panel-1", "security-panel", mode="none"),
                _device("panel-2", "security-panel", mode="none"),
            ),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.mode is AlarmMode.UNKNOWN
    assert location.triggered is TriState.UNKNOWN
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "multiple-panels"


def test_unknown_session_is_stale_and_an_omitted_kind_preserves_discovery_kind():
    store, epoch = _registered_store(("base-station", "base_station_v1"))
    store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("panel-1", "security-panel", mode="none"),),
        ),
        epoch,
    )

    snapshot = store.apply_message(
        "location-1",
        SessionInfo(
            (
                AssetSessionInfo(
                    "base-station",
                    AlarmConnectionStatus.UNKNOWN,
                    "",
                ),
            )
        ),
        epoch,
    )
    asset = snapshot.find_location("location-1").find_asset("base-station")

    assert asset.kind == "base_station_v1"
    assert asset.connection is AlarmConnectionStatus.UNKNOWN
    assert asset.stale is True


def test_full_list_cannot_promote_unknown_connectivity_to_online_or_fresh():
    store = AlarmStateStore(clock=lambda: 1_700_000_000.0)
    store.register_location(
        "location-1",
        "Home",
        (("base-station", "base_station_v1"),),
        write_authorization=AlarmWriteAuthorization.ALLOWED,
    )
    epoch = store.begin_epoch("location-1")
    store.apply_message(
        "location-1",
        SessionInfo(
            (
                AssetSessionInfo(
                    "base-station",
                    AlarmConnectionStatus.UNKNOWN,
                    "base_station_v1",
                ),
            )
        ),
        epoch,
    )

    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("panel-1", "security-panel", mode="none"),),
        ),
        epoch,
    )
    location = snapshot.find_location("location-1")
    asset = location.find_asset("base-station")

    assert asset.connection is AlarmConnectionStatus.UNKNOWN
    assert asset.inventory is AlarmInventoryStatus.STALE
    assert asset.stale is True
    assert asset.find_device("panel-1").stale is True
    assert asset.find_device("panel-1").mode is AlarmMode.DISARMED
    assert location.connection is AlarmConnectionStatus.UNKNOWN
    assert location.mode is AlarmMode.UNKNOWN
    assert location.triggered is TriState.UNKNOWN
    assert location.can_set_mode is False
    assert location.command_unavailable_reason == "panel-offline"


@pytest.mark.parametrize(
    "connection",
    [AlarmConnectionStatus.OFFLINE, AlarmConnectionStatus.CELLULAR_BACKUP],
)
def test_full_list_cannot_mark_offline_or_cellular_asset_fresh(connection):
    store, epoch = _registered_store(("base-station", "base_station_v1"))
    store.apply_message(
        "location-1",
        SessionInfo((AssetSessionInfo("base-station", connection, "base_station_v1"),)),
        epoch,
    )

    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("panel-1", "security-panel", mode="none"),),
        ),
        epoch,
    )
    asset = snapshot.find_location("location-1").find_asset("base-station")
    assert asset.inventory is AlarmInventoryStatus.STALE
    assert asset.stale is True
    assert asset.find_device("panel-1").stale is True

    store.apply_message(
        "location-1",
        SessionInfo(
            (
                AssetSessionInfo(
                    "base-station",
                    AlarmConnectionStatus.ONLINE,
                    "base_station_v1",
                ),
            )
        ),
        epoch,
    )
    assert store.snapshot.find_location("location-1").inventory is AlarmInventoryStatus.STALE

    refreshed = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("panel-1", "security-panel", mode="none"),),
        ),
        epoch,
    )
    assert refreshed.find_location("location-1").inventory is AlarmInventoryStatus.COMPLETE


def test_old_epoch_updates_are_ignored_after_reconnect():
    store, first_epoch = _registered_store("base-station")
    store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (_device("contact-1", "sensor.contact", faulted=False),),
        ),
        first_epoch,
    )
    second_epoch = store.begin_epoch("location-1")
    stale_snapshot = store.snapshot

    result = store.apply_message(
        "location-1",
        DeviceInfoDocUpdate(
            "base-station",
            (DeviceDocument("contact-1", {"zid": "contact-1", "faulted": True}),),
        ),
        first_epoch,
    )

    assert second_epoch > first_epoch
    assert result is stale_snapshot
    assert result.find_location("location-1").find_device("contact-1").stale is True


def test_hub_disconnection_marks_only_named_asset_stale():
    store, epoch = _registered_store("base-station", "bridge")
    store.apply_message("location-1", DeviceInfoDocList("base-station", ()), epoch)
    store.apply_message("location-1", DeviceInfoDocList("bridge", ()), epoch)

    snapshot = store.apply_message(
        "location-1",
        HubDisconnection("bridge"),
        epoch,
    )
    location = snapshot.find_location("location-1")

    assert location.find_asset("base-station").stale is False
    assert location.find_asset("bridge").stale is True
    assert location.inventory is AlarmInventoryStatus.PARTIAL


def test_reconciliation_adds_removes_and_forgets_locations():
    store, _epoch = _registered_store("old-asset")
    before = store.snapshot.revision

    store.register_location("location-1", "Renamed", (("new-asset", "base_station_v1"),))
    location = store.snapshot.find_location("location-1")

    assert store.snapshot.revision > before
    assert location.name == "Renamed"
    assert location.find_asset("old-asset") is None
    assert location.find_asset("new-asset") is not None

    store.remove_location("location-1")
    assert store.snapshot.locations == ()


def test_unknown_messages_and_stale_markers_are_revision_deduplicated():
    store, epoch = _registered_store("base-station")
    before = store.snapshot

    assert (
        store.apply_message(
            "location-1",
            UnknownMessage("future", "future", "future"),
            epoch,
        )
        is before
    )

    first = store.mark_location_stale("location-1", epoch)
    second = store.mark_location_stale("location-1", epoch)
    assert second is first


def test_normalization_bounds_display_strings_and_handles_boolean_lock_state():
    store, epoch = _registered_store("base-station")
    snapshot = store.apply_message(
        "location-1",
        DeviceInfoDocList(
            "base-station",
            (
                _device(
                    "lock-1",
                    "vendor.lock",
                    name="A" * 500 + "\nsecret",
                    categoryId=10,
                    locked=True,
                ),
            ),
        ),
        epoch,
    )
    lock = snapshot.find_location("location-1").find_device("lock-1")

    assert len(lock.name) == 256
    assert "\n" not in lock.name
    assert lock.kind is AlarmDeviceKind.LOCK
    assert lock.lock_state is AlarmLockState.LOCKED


def test_location_and_asset_keys_do_not_cross_contaminate():
    store = AlarmStateStore(clock=lambda: 1.0)
    store.register_location("location-a", expected_assets=("shared-asset",))
    store.register_location("location-b", expected_assets=("shared-asset",))
    epoch_a = store.begin_epoch("location-a")
    epoch_b = store.begin_epoch("location-b")

    store.apply_message(
        "location-a",
        DeviceInfoDocList(
            "shared-asset",
            (_device("shared-zid", "sensor.contact", faulted=True),),
        ),
        epoch_a,
    )
    store.apply_message(
        "location-b",
        DeviceInfoDocList(
            "shared-asset",
            (_device("shared-zid", "sensor.contact", faulted=False),),
        ),
        epoch_b,
    )

    location_a = store.snapshot.find_location("location-a")
    location_b = store.snapshot.find_location("location-b")
    assert location_a.find_device("shared-zid").faulted is TriState.ACTIVE
    assert location_b.find_device("shared-zid").faulted is TriState.INACTIVE
