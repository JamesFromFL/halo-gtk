"""Read-only presentation of cached security state for the Overview page."""

from __future__ import annotations

from dataclasses import dataclass

from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmConnectionStatus,
    AlarmInventoryStatus,
    AlarmMode,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmSignal,
    TriState,
)


@dataclass(frozen=True, slots=True)
class SecuritySummary:
    title: str
    detail: str
    severity: str = "neutral"


def security_summary(snapshot: AlarmAccountSnapshot | None, *, signed_in: bool) -> SecuritySummary:
    """Never infer safety or camera privacy from a cached alarm mode."""
    if not signed_in:
        return SecuritySummary(
            "Welcome to Halo", "Sign in to see your cameras and security devices."
        )
    if snapshot is None or not snapshot.locations:
        if snapshot is not None and snapshot.status is AlarmServiceStatus.NO_SYSTEM:
            return SecuritySummary(
                "Your cameras, together", "Open a camera to watch, or browse Activity."
            )
        return SecuritySummary(
            "Security status unavailable", "Open Security for connection details."
        )
    locations = snapshot.locations
    alarming = [
        loc
        for loc in locations
        if (
            loc.triggered is TriState.ACTIVE
            or loc.phase is AlarmPhase.ALARMING
            or loc.signal not in {AlarmSignal.NONE, AlarmSignal.UNKNOWN}
        )
    ]
    if alarming:
        names = ", ".join(loc.name or "Ring location" for loc in alarming)
        return SecuritySummary(
            "Alarm reported", f"{names} · Open Security for current details.", "error"
        )
    stale = snapshot.status not in {AlarmServiceStatus.ONLINE, AlarmServiceStatus.DEGRADED}
    stale = stale or any(
        loc.connection is not AlarmConnectionStatus.ONLINE
        or loc.inventory is not AlarmInventoryStatus.COMPLETE
        or any(asset.stale for asset in loc.assets)
        for loc in locations
    )
    if stale:
        return SecuritySummary(
            "Security status needs attention",
            "Some observations are unavailable or out of date.",
            "warning",
        )
    for phase, title, detail in (
        (AlarmPhase.ENTRY_DELAY, "Entry delay", "Check the alarm in Security."),
        (AlarmPhase.EXIT_DELAY, "Alarm arming", "Waiting for the confirmed mode."),
    ):
        transitioning = [loc for loc in locations if loc.phase is phase]
        if transitioning:
            names = ", ".join(loc.name or "Ring Alarm" for loc in transitioning)
            return SecuritySummary(title, f"{names} · {detail}", "warning")
    if len(locations) > 1:
        return SecuritySummary(
            f"{len(locations)} security locations",
            "Open Security to check each location's reported mode.",
        )
    loc = locations[0]
    name = loc.name or "Ring Alarm"
    if loc.phase is not AlarmPhase.IDLE or loc.mode is AlarmMode.UNKNOWN:
        return SecuritySummary(
            "Alarm state unknown", f"{name} · Open Security for details.", "warning"
        )
    title = {
        AlarmMode.DISARMED: "Alarm disarmed",
        AlarmMode.HOME: "Armed · Home",
        AlarmMode.AWAY: "Armed · Away",
    }[loc.mode]
    return SecuritySummary(
        title,
        f"{name} · Reported by Ring",
        "neutral" if loc.mode is AlarmMode.DISARMED else "success",
    )
