"""Provider-neutral backend primitives for Ring Alarm integration."""

from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmCapability,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmLockState,
    AlarmMode,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmSignal,
    AlarmWriteAuthorization,
    TriState,
    empty_alarm_snapshot,
)
from halo_gtk.alarm.provider import AlarmCommandResult, AlarmCommandStatus

__all__ = [
    "AlarmAccountSnapshot",
    "AlarmAssetSnapshot",
    "AlarmCapability",
    "AlarmCommandResult",
    "AlarmCommandStatus",
    "AlarmConnectionStatus",
    "AlarmDeviceKind",
    "AlarmDeviceSnapshot",
    "AlarmInventoryStatus",
    "AlarmLocationSnapshot",
    "AlarmLockState",
    "AlarmMode",
    "AlarmPhase",
    "AlarmSignal",
    "AlarmServiceStatus",
    "AlarmWriteAuthorization",
    "TriState",
    "empty_alarm_snapshot",
]
