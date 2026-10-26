"""Shared pure Alarm policy facts; transport/admission guards stay in the service."""

from __future__ import annotations

from halo_gtk.alarm.models import (
    AlarmConnectionStatus,
    AlarmInventoryStatus,
    AlarmMode,
    AlarmPhase,
    AlarmSignal,
    AlarmWriteAuthorization,
    TriState,
)


def authorization_reason(authorization: AlarmWriteAuthorization) -> str | None:
    if authorization is AlarmWriteAuthorization.DENIED:
        return "authorization-denied"
    if authorization is not AlarmWriteAuthorization.ALLOWED:
        return "authorization-unknown"
    return None


def asset_reason(asset, *, check_stale: bool) -> str | None:
    if asset.connection is AlarmConnectionStatus.CELLULAR_BACKUP:
        return "cellular-backup"
    if asset.connection is not AlarmConnectionStatus.ONLINE:
        return "panel-offline"
    if check_stale and asset.stale:
        return "panel-stale"
    if asset.inventory is not AlarmInventoryStatus.COMPLETE:
        return "panel-inventory-incomplete"
    if any(
        not device.stale
        and (device.smoke is TriState.ACTIVE or device.carbon_monoxide is TriState.ACTIVE)
        for device in asset.devices
    ):
        return "life-safety-active"
    return None


def panel_reason(mode, phase, signal, *, faults_valid: bool, alarming_first: bool) -> str | None:
    # The service historically prioritizes active alarms over an unknown mode
    # in its displayed reason. Both layers deny either condition.
    if alarming_first and phase is AlarmPhase.ALARMING:
        return "active-alarm"
    if mode is AlarmMode.UNKNOWN:
        return "mode-unknown"
    if phase is AlarmPhase.ALARMING:
        return "active-alarm"
    if phase is AlarmPhase.UNKNOWN or signal is AlarmSignal.UNKNOWN:
        return "panel-state-unknown"
    if signal is not AlarmSignal.NONE:
        return "active-alarm"
    if not faults_valid:
        return "faulted-device-list-invalid"
    return None
