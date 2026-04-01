"""libnotify-based desktop notification helpers."""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_notify_available: bool | None = None


def _ensure_notify() -> bool:
    global _notify_available
    if _notify_available is not None:
        return _notify_available
    try:
        import gi

        gi.require_version("Notify", "0.7")
        from gi.repository import Notify  # type: ignore[attr-defined]

        if not Notify.is_initted():
            Notify.init("Ring")
        _notify_available = True
    except Exception as exc:
        _log.warning("libnotify unavailable: %s", exc)
        _notify_available = False
    return _notify_available


def send_notification(summary: str, body: str = "", icon: str = "security-high") -> None:
    """Show a desktop notification via libnotify."""
    if not _ensure_notify():
        _log.info("Notification (no libnotify): %s — %s", summary, body)
        return

    from gi.repository import Notify  # type: ignore[attr-defined]

    notif = Notify.Notification.new(summary, body, icon)
    notif.set_urgency(Notify.Urgency.NORMAL)
    try:
        notif.show()
    except Exception as exc:
        _log.warning("Failed to show notification: %s", exc)


def send_ring_notification(event) -> None:
    """Translate a ring-doorbell event into a desktop notification."""
    kind = getattr(event, "kind", None)
    doorbot_name = getattr(event, "doorbot_description", None) or "Ring device"

    if kind == "ding":
        send_notification(
            f"Doorbell: {doorbot_name}",
            "Someone pressed your doorbell.",
            "audio-input-microphone-symbolic",
        )
    elif kind == "motion":
        send_notification(
            f"Motion detected: {doorbot_name}",
            "Motion was detected near your door.",
            "camera-video-symbolic",
        )
    else:
        send_notification(
            f"Ring alert: {doorbot_name}",
            str(kind),
        )
