"""Immutable, actual-GET camera light observations and presentation."""

from __future__ import annotations

import math
from dataclasses import dataclass

from halo_gtk.camera_privacy import CAMERA_FAMILIES, MAX_CAMERAS, privacy_time

LIGHTING_TTL = 90.0


def motion_duration(value):
    return value if type(value) is int and 30 <= value <= 900 else None


def camera_id(device):
    value = getattr(device, "id", None)
    if type(value) not in (int, str):
        return None
    value = str(value)
    return value if value and len(value) <= 128 and value.strip() == value else None


@dataclass(frozen=True)
class CameraLightingObservation:
    camera_id: str
    device: object
    light: bool | None
    motion_duration_seconds: int | None


@dataclass(frozen=True)
class CameraLightingReceipt:
    received_at: float
    revision: int
    rows: tuple[CameraLightingObservation, ...]


def camera_lighting_receipt(devices, *, received_at, revision):
    """The mutation revision must be sampled before the actual inventory GET."""
    rows, duplicates = {}, set()
    for device in devices:
        try:
            if getattr(device, "family", None) not in CAMERA_FAMILIES:
                continue
            key = camera_id(device)
            if key is None:
                continue
            if key in rows:
                duplicates.add(key)
            if len(rows) >= MAX_CAMERAS and key not in rows:
                return CameraLightingReceipt(received_at, revision, ())
            light = duration = None
            attrs = getattr(device, "_attrs", None)
            if isinstance(attrs, dict):
                status = attrs.get("led_status")
                if type(status) is str and status in {"on", "off"}:
                    light = status == "on"
                settings = attrs.get("settings")
                if isinstance(settings, dict):
                    floodlight = settings.get("floodlight_settings")
                    if isinstance(floodlight, dict):
                        duration = motion_duration(floodlight.get("duration"))
                    alternate = settings.get("light_settings")
                    if ("light_settings" in settings and not isinstance(alternate, dict)) or (
                        isinstance(alternate, dict)
                        and "duration" in alternate
                        and motion_duration(alternate["duration"]) != duration
                    ):
                        duration = None
            rows[key] = CameraLightingObservation(key, device, light, duration)
        except Exception:
            continue
    return CameraLightingReceipt(
        received_at, revision, tuple(row for key, row in rows.items() if key not in duplicates)
    )


@dataclass(frozen=True)
class CameraLightOperation:
    requested: bool
    phase: str
    revision: int
    accepted: bool = False
    possibly_sent: bool = False


@dataclass(frozen=True)
class CameraLightingView:
    light: bool | None = None
    motion_duration_seconds: int | None = None
    phase: str = "unknown"
    requested: bool | None = None
    accepted: bool = False

    @property
    def message(self):
        target = "On" if self.requested else "Off"
        observed = "On" if self.light else "Off"
        if self.phase == "pending":
            text = f"Light {target} requested · pending"
        elif self.phase == "accepted":
            text = f"Ring accepted {target} · checking light"
        elif self.phase == "uncertain":
            text = (
                f"Ring accepted {target} · light unconfirmed"
                if self.accepted
                else "Light request outcome unknown"
            )
        elif self.phase == "rejected":
            text = "Light request not accepted"
        else:
            text = (
                f"Light {observed} · reported by Ring"
                if self.light is not None
                else "Light state unknown"
            )
        if self.light is not None and self.phase in {"uncertain", "rejected"}:
            text += f" · Ring reports {observed}"
        timer = self.motion_duration_seconds
        return text + (
            f" · motion timer {timer}s" if timer is not None else " · motion timer unknown"
        )

    @property
    def button_active(self):
        # An unconfirmed requested value remains visibly labelled as a request,
        # allowing an explicit Off request even when the observed state is unknown.
        if self.phase in {"pending", "accepted", "uncertain"} and self.requested is not None:
            return self.requested
        return self.light is True


def receipt_row(receipt, device, revision, *, now=None):
    if not isinstance(receipt, CameraLightingReceipt) or receipt.revision != revision:
        return None
    stamp = receipt.received_at
    now = privacy_time() if now is None else now
    if (
        type(stamp) not in (int, float)
        or not math.isfinite(stamp)
        or not 0 <= now - stamp <= LIGHTING_TTL
    ):
        return None
    matches = [row for row in receipt.rows if row.camera_id == camera_id(device)]
    return matches[0] if len(matches) == 1 and matches[0].device is device else None


def current_view(device):
    from halo_gtk.ring_client import get_client

    client = get_client()
    return view_for(client, device)


def view_for(client, device):
    actions = getattr(client, "_lighting_actions", None)
    return actions.view(device) if actions is not None else CameraLightingView()
