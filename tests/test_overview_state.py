"""Synthetic coverage of Overview's reported alarm state and priority."""

from dataclasses import replace

import pytest

from halo_gtk.alarm import (
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmConnectionStatus,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmMode,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmSignal,
    TriState,
)
from halo_gtk.overview_state import SecuritySummary, security_summary


def _location(name="Home", *, phase=AlarmPhase.IDLE, mode=AlarmMode.DISARMED):
    asset = AlarmAssetSnapshot(
        location_id=name,
        asset_id="hub",
        connection=AlarmConnectionStatus.ONLINE,
        inventory=AlarmInventoryStatus.COMPLETE,
    )
    return AlarmLocationSnapshot(
        location_id=name,
        name=name,
        connection=AlarmConnectionStatus.ONLINE,
        inventory=AlarmInventoryStatus.COMPLETE,
        mode=mode,
        phase=phase,
        signal=AlarmSignal.NONE,
        triggered=TriState.INACTIVE,
        assets=(asset,),
    )


def _snapshot(*locations, status=AlarmServiceStatus.ONLINE):
    return AlarmAccountSnapshot(locations=locations, status=status)


def test_signed_out_summary_does_not_reveal_cached_security_state():
    alarm = replace(_location(), triggered=TriState.ACTIVE)

    assert security_summary(_snapshot(alarm), signed_in=False) == SecuritySummary(
        "Welcome to Halo", "Sign in to see your cameras and security devices."
    )


@pytest.mark.parametrize(
    "snapshot", [None, _snapshot(), _snapshot(status=AlarmServiceStatus.ERROR)]
)
def test_missing_security_observations_are_not_presented_as_safe(snapshot):
    assert security_summary(snapshot, signed_in=True) == SecuritySummary(
        "Security status unavailable", "Open Security for connection details."
    )


def test_camera_only_account_gets_camera_guidance():
    result = security_summary(_snapshot(status=AlarmServiceStatus.NO_SYSTEM), signed_in=True)

    assert result == SecuritySummary(
        "Your cameras, together", "Open a camera to watch, or browse Activity."
    )


@pytest.mark.parametrize(
    ("mode", "title", "severity"),
    [
        (AlarmMode.DISARMED, "Alarm disarmed", "neutral"),
        (AlarmMode.HOME, "Armed · Home", "success"),
        (AlarmMode.AWAY, "Armed · Away", "success"),
    ],
)
def test_current_mode_is_reported_without_a_sensor_safety_or_camera_privacy_claim(
    mode, title, severity
):
    result = security_summary(_snapshot(_location(mode=mode)), signed_in=True)

    assert result == SecuritySummary(title, "Home · Reported by Ring", severity)


@pytest.mark.parametrize(
    "location",
    [_location(mode=AlarmMode.UNKNOWN), _location(phase=AlarmPhase.UNKNOWN)],
)
def test_unknown_mode_or_phase_does_not_claim_the_alarm_is_disarmed(location):
    assert security_summary(_snapshot(location), signed_in=True) == SecuritySummary(
        "Alarm state unknown", "Home · Open Security for details.", "warning"
    )


@pytest.mark.parametrize("stale_source", ["service", "connection", "inventory", "asset"])
def test_stale_observations_take_priority_over_retained_transition(stale_source):
    location = _location(phase=AlarmPhase.ENTRY_DELAY)
    status = AlarmServiceStatus.ONLINE
    if stale_source == "service":
        status = AlarmServiceStatus.OFFLINE
    elif stale_source == "connection":
        location = replace(location, connection=AlarmConnectionStatus.OFFLINE)
    elif stale_source == "inventory":
        location = replace(location, inventory=AlarmInventoryStatus.PARTIAL)
    else:
        location = replace(location, assets=(replace(location.assets[0], stale=True),))

    result = security_summary(_snapshot(location, status=status), signed_in=True)

    assert result == SecuritySummary(
        "Security status needs attention",
        "Some observations are unavailable or out of date.",
        "warning",
    )


def test_multiple_idle_locations_do_not_imply_one_shared_mode():
    snapshot = _snapshot(_location(), _location("Office", mode=AlarmMode.AWAY))

    assert security_summary(snapshot, signed_in=True) == SecuritySummary(
        "2 security locations", "Open Security to check each location's reported mode."
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_entry_delay_takes_priority_over_exit_delay_across_locations(reverse):
    locations = (
        _location("Office", phase=AlarmPhase.EXIT_DELAY),
        _location("Home", phase=AlarmPhase.ENTRY_DELAY),
    )
    if reverse:
        locations = tuple(reversed(locations))

    assert security_summary(_snapshot(*locations), signed_in=True) == SecuritySummary(
        "Entry delay", "Home · Check the alarm in Security.", "warning"
    )


@pytest.mark.parametrize(
    ("phase", "title", "detail"),
    [
        (AlarmPhase.ENTRY_DELAY, "Entry delay", "Check the alarm in Security."),
        (AlarmPhase.EXIT_DELAY, "Alarm arming", "Waiting for the confirmed mode."),
    ],
)
def test_transition_summary_names_only_affected_locations(phase, title, detail):
    snapshot = _snapshot(
        _location("Home", phase=phase),
        _location("Office"),
        _location("Cabin", phase=phase),
    )

    assert security_summary(snapshot, signed_in=True) == SecuritySummary(
        title, f"Home, Cabin · {detail}", "warning"
    )


@pytest.mark.parametrize(
    "evidence",
    [
        {"triggered": TriState.ACTIVE},
        {"phase": AlarmPhase.ALARMING},
        {"signal": AlarmSignal.FIRE},
    ],
)
def test_reported_alarm_takes_priority_over_delays_and_lost_connection(evidence):
    alarm = replace(_location("Office"), **evidence)
    snapshot = _snapshot(
        _location(phase=AlarmPhase.ENTRY_DELAY), alarm, status=AlarmServiceStatus.OFFLINE
    )

    assert security_summary(snapshot, signed_in=True) == SecuritySummary(
        "Alarm reported", "Office · Open Security for current details.", "error"
    )
