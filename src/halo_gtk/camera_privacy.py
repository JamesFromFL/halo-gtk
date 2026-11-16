"""Qualified, expiring Ring camera permissions for current media.

Only explicit booleans copied immediately after a successful inventory GET are
authoritative. SDK convenience properties and cached inventory reads are not
receipts. Historical recordings deliberately do not use this policy.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import weakref
from dataclasses import dataclass

PRIVACY_TTL = 120.0
MAX_CAMERAS = 256
CAMERA_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})
_log = logging.getLogger(__name__)


def privacy_time() -> float:
    """Count suspend time too: a sleeping desktop cannot retain fresh grants."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class CameraPrivacyReadbackError(RuntimeError):
    """The write was accepted, but effective privacy is not yet verified."""

    accepted = True


def _camera_id(device):
    value = getattr(device, "id", None)
    if type(value) not in (int, str):
        return None
    value = str(value)
    return value if value and len(value) <= 128 and value.strip() == value else None


@dataclass(frozen=True)
class RingPrivacyObservation:
    camera_id: str
    device: object
    live_allowed: bool | None
    motion_allowed: bool | None
    audio_allowed: bool | None


@dataclass(frozen=True)
class RingPrivacyReceipt:
    received_at: float
    rows: tuple[RingPrivacyObservation, ...]


def ring_privacy_receipt(devices, received_at: float) -> RingPrivacyReceipt:
    rows = {}
    duplicates = set()
    for device in devices:
        key = _camera_id(device)
        if key is None:
            continue
        if key in rows:
            duplicates.add(key)
        try:
            attrs = device._attrs
            settings = attrs.get("settings") if isinstance(attrs, dict) else None
            settings = settings if isinstance(settings, dict) else {}
            live = settings.get("live_view_disabled")
            motion = settings.get("motion_detection_enabled")
            audio = settings.get("enable_audio_recording")
        except Exception:
            live = motion = audio = None
        rows[key] = RingPrivacyObservation(
            key,
            device,
            not live if type(live) is bool else None,
            motion if type(motion) is bool else None,
            audio if type(audio) is bool else None,
        )
    return RingPrivacyReceipt(
        received_at, tuple(row for k, row in rows.items() if k not in duplicates)
    )


@dataclass(frozen=True)
class CameraPrivacyView:
    live_allowed: bool | None = None
    preview_allowed: bool | None = None
    audio_allowed: bool | None = None
    reason: str = "unknown"

    @property
    def message(self) -> str:
        return {
            "unknown": "Privacy unknown · waiting for Ring settings",
            "preview_unknown": "Previews unavailable · Ring motion setting unknown",
            "stale": "Privacy stale · refresh Ring settings",
            "live_disabled": "Live view disabled in Ring",
            "motion_disabled": "Previews hidden · motion detection disabled in Ring",
            "audio_disabled": "Audio disabled in Ring",
            "audio_unknown": "Audio unavailable · Ring setting unknown",
            "allowed": "Ring privacy verified",
        }[self.reason]


@dataclass(frozen=True)
class PrivacyLease:
    policy: object
    device: object
    purpose: str
    revision: int


@dataclass(frozen=True)
class CameraPreview:
    data: bytes
    lease: PrivacyLease


class CameraPrivacy:
    def __init__(self, *, clock=privacy_time):
        self._clock = clock
        self._lock = threading.RLock()
        self._rows = {}
        self._revisions = {}
        self._serial = 0
        self._received_at = None
        self._listeners = set()
        self._pending = {}

    def _fresh(self):
        stamp = self._received_at
        return (
            type(stamp) in (int, float)
            and math.isfinite(stamp)
            and 0 <= self._clock() - stamp <= PRIVACY_TTL
        )

    def observe(self, receipt: RingPrivacyReceipt) -> None:
        """Publish a successful GET, retaining leases only for unchanged fresh rows."""
        with self._lock:
            fresh = self._fresh()
            previous = self._rows
            rows = {row.camera_id: row for row in receipt.rows}
            if len(rows) > MAX_CAMERAS:
                rows = {}
            for key in previous.keys() | rows.keys():
                old, new = previous.get(key), rows.get(key)
                same = (
                    fresh
                    and old is not None
                    and new is not None
                    and old.device is new.device
                    and (old.live_allowed, old.motion_allowed, old.audio_allowed)
                    == (new.live_allowed, new.motion_allowed, new.audio_allowed)
                )
                if not same:
                    self._serial += 1
                    self._revisions[key] = self._serial
            self._rows = rows
            self._revisions = {key: self._revisions[key] for key in rows}
            self._received_at = receipt.received_at
        self._notify()

    def invalidate(self) -> None:
        with self._lock:
            self._serial += 1
            self._rows = {}
            self._revisions = {}
            self._received_at = None
            self._pending = {}
        self._notify()

    def begin_change(self, device):
        """Withdraw this camera while an admitted privacy-affecting write runs."""
        with self._lock:
            key = _camera_id(device)
            self._serial += 1
            token = (key, self._serial)
            self._pending[key] = token
            self._revisions[key] = self._serial
        self._notify()
        return token

    def end_change(self, token):
        with self._lock:
            key = token[0]
            if self._pending.get(key) != token:
                return
            self._pending.pop(key, None)
            self._rows.pop(key, None)
            self._revisions.pop(key, None)
        # Only a GET begun after completion may restore this camera's grant.
        self._notify()

    def view(self, device) -> CameraPrivacyView:
        with self._lock:
            row = self._rows.get(_camera_id(device))
            if _camera_id(device) in self._pending:
                return CameraPrivacyView()
            if row is None or row.device is not device:
                return CameraPrivacyView()
            if not self._fresh():
                return CameraPrivacyView(reason="stale")
            live, motion, audio = row.live_allowed, row.motion_allowed, row.audio_allowed
            preview = (
                False if live is False or motion is False else True if live and motion else None
            )
            reason = (
                "live_disabled"
                if live is False
                else "motion_disabled"
                if preview is False
                else "unknown"
                if live is None
                else "preview_unknown"
                if preview is None
                else "audio_disabled"
                if audio is False
                else "audio_unknown"
                if audio is None
                else "allowed"
            )
            return CameraPrivacyView(live, preview, audio, reason)

    def allowed(self, device, purpose: str) -> bool:
        view = self.view(device)
        if purpose == "live":
            return view.live_allowed is True
        if purpose == "preview":
            return view.preview_allowed is True
        if purpose in {"audio", "talkback"}:
            return view.live_allowed is True and view.audio_allowed is True
        return False

    def acquire(self, device, purpose: str) -> PrivacyLease | None:
        with self._lock:
            if not self.allowed(device, purpose):
                return None
            return PrivacyLease(self, device, purpose, self._revisions[_camera_id(device)])

    def valid(self, lease: PrivacyLease | None) -> bool:
        with self._lock:
            return (
                isinstance(lease, PrivacyLease)
                and lease.policy is self
                and self._revisions.get(_camera_id(lease.device)) == lease.revision
                and self.allowed(lease.device, lease.purpose)
            )

    def apply(self, lease, callback) -> bool:
        """Fence a synchronous media handoff against a concurrent receipt."""
        with self._lock:
            if not self.valid(lease):
                return False
            callback()
            return True

    def subscribe(self, callback):
        with self._lock:
            self._listeners.add(callback)

    def unsubscribe(self, callback):
        with self._lock:
            self._listeners.discard(callback)

    def _notify(self):
        with self._lock:
            callbacks = tuple(self._listeners)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                _log.exception("Camera privacy observer failed")


def current_view(device) -> CameraPrivacyView:
    from halo_gtk.ring_client import get_client

    client = get_client()
    if client is None or not hasattr(client, "camera_privacy") or not client._preview_is_current():
        return CameraPrivacyView()
    return client.camera_privacy.view(device)


def acquire_current(device, purpose):
    from halo_gtk.ring_client import get_client

    client = get_client()
    if client is None or not hasattr(client, "camera_privacy") or not client._preview_is_current():
        return None
    return client.camera_privacy.acquire(device, purpose)


def lease_is_current(lease) -> bool:
    from halo_gtk.ring_client import get_client

    client = get_client()
    return (
        client is not None
        and hasattr(client, "camera_privacy")
        and client._preview_is_current()
        and isinstance(lease, PrivacyLease)
        and lease.policy is client.camera_privacy
        and lease.policy.valid(lease)
    )


class PrivacyWatch:
    """GTK delivery for receipt changes, account changes, and clock expiry."""

    def __init__(self, callback):
        from gi.repository import GLib

        self._callback = weakref.WeakMethod(callback)
        self._policy = None
        self._pending = False
        self._source = GLib.timeout_add_seconds(1, self._tick)
        self._tick()

    def _changed(self):
        from gi.repository import GLib

        if not self._pending:
            self._pending = True
            GLib.idle_add(self._deliver)

    def _deliver(self):
        self._pending = False
        if self._source is not None:
            self._tick()
        return False

    def _tick(self):
        from halo_gtk.ring_client import get_client

        callback = self._callback()
        if callback is None:
            self.close()
            return False
        client = get_client()
        policy = getattr(client, "camera_privacy", None)
        if policy is not self._policy:
            if self._policy is not None:
                self._policy.unsubscribe(self._changed)
            self._policy = policy
            if policy is not None:
                policy.subscribe(self._changed)
        callback()
        return True

    def close(self):
        from gi.repository import GLib

        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
        if self._policy is not None:
            self._policy.unsubscribe(self._changed)
            self._policy = None
