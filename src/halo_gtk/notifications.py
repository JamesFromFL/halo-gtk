"""libnotify-based desktop notification helpers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import suppress
from dataclasses import dataclass

from halo_gtk import config as _config
from halo_gtk import device_names
from halo_gtk.ring_events import normalized_event_kind

_log = logging.getLogger(__name__)

_notify_available: bool | None = None
_active_notifications: list = []

# Bounded worker pool for notification enrichment so an event burst (alarm /
# rapid motion) can't spawn unbounded daemon threads doing heavy network I/O.
_NOTIFY_WORKERS = 2
_NOTIFY_QUEUE_MAX = 32
_notify_queue: queue.Queue | None = None
_notify_lock = threading.Lock()


@dataclass(frozen=True)
class _NotificationTarget:
    device_id: int | None
    event_id: int | None
    expires_at: float


def _ensure_notify() -> bool:
    global _notify_available
    if _notify_available is not None:
        return _notify_available
    try:
        import gi

        gi.require_version("Notify", "0.7")
        from gi.repository import Notify  # type: ignore[attr-defined]

        if not Notify.is_initted():
            Notify.init("Halo")
        _notify_available = True
    except Exception as exc:
        _log.warning("libnotify unavailable: %s", exc)
        _notify_available = False
    return _notify_available


def send_notification(
    summary: str,
    body: str = "",
    icon: str = "security-high",
    *,
    image_bytes: bytes | None = None,
    image_pixbuf=None,
    target: _NotificationTarget | None = None,
    urgency: str = "normal",
) -> None:
    """Show a desktop notification via libnotify."""
    if not _ensure_notify():
        _log.info("Notification (no libnotify): %s — %s", summary, body)
        return

    from gi.repository import Notify  # type: ignore[attr-defined]

    notif = Notify.Notification.new(summary, body, icon)
    notif.set_app_name("Halo")
    notify_urgency = Notify.Urgency.CRITICAL if urgency == "critical" else Notify.Urgency.NORMAL
    notif.set_urgency(notify_urgency)

    pixbuf = (
        image_pixbuf
        if image_pixbuf is not None
        else (_pixbuf_from_bytes(image_bytes) if image_bytes else None)
    )
    if pixbuf is not None:
        notif.set_image_from_pixbuf(pixbuf)

    if target is not None and target.device_id is not None:
        notif.add_action("view", "View", _on_view_action, target)

    notif.connect("closed", _on_notification_closed)
    _active_notifications.append(notif)
    try:
        notif.show()
    except Exception as exc:
        _log.warning("Failed to show notification: %s", exc)
        _drop_notification(notif)


def _pixbuf_from_bytes(image_bytes: bytes):
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(image_bytes)
        loader.close()
        return loader.get_pixbuf()
    except Exception as exc:
        _log.debug("Failed to decode notification image: %s", exc)
        return None


def _on_notification_closed(notification, *_) -> None:
    _drop_notification(notification)


def _drop_notification(notification) -> None:
    with suppress(ValueError):
        _active_notifications.remove(notification)


def _on_view_action(notification, action: str, target: _NotificationTarget) -> None:
    _drop_notification(notification)
    _open_notification_target(target)


def _open_notification_target(target: _NotificationTarget) -> None:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib

        GLib.idle_add(_open_notification_target_on_main, target)
    except Exception as exc:
        _log.debug("Failed to open notification target: %s", exc)


def _open_notification_target_on_main(target: _NotificationTarget) -> bool:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    app = Gtk.Application.get_default()
    if app is None:
        return False

    app.activate()
    get_main = getattr(app, "get_main_window", None)
    win = get_main() if callable(get_main) else app.get_active_window()
    if win is None:
        return False

    if (
        time.time() < target.expires_at
        and hasattr(win, "open_camera_live")
        and win.open_camera_live(target.device_id)
    ):
        return False

    if target.event_id is not None and hasattr(win, "open_history_event"):
        win.open_history_event(target.device_id, target.event_id)
    elif hasattr(win, "open_camera_live"):
        win.open_camera_live(target.device_id)

    return False


def _notifications_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg or _config.load()
    return bool(cfg.get("show_notifications", True))


def _event_device_name(event) -> str:
    fallback = (
        getattr(event, "device_name", None)
        or getattr(event, "doorbot_description", None)
        or "Ring device"
    )
    return device_names.display_name_for_id(getattr(event, "doorbot_id", None), fallback)


def send_ring_notification(event) -> None:
    """Translate a ring-doorbell event into a desktop notification."""
    cfg = _config.load()
    kind = _event_kind(event)
    if not _notifications_enabled(cfg) or not _event_type_enabled(kind, cfg):
        return
    _enqueue_notification(event, cfg)


def _enqueue_notification(event, cfg: dict) -> None:
    """Hand the event to the bounded notification worker pool (lazy-started)."""
    global _notify_queue
    with _notify_lock:
        if _notify_queue is None:
            _notify_queue = queue.Queue(maxsize=_NOTIFY_QUEUE_MAX)
            for index in range(_NOTIFY_WORKERS):
                threading.Thread(
                    target=_notification_worker,
                    args=(_notify_queue,),
                    daemon=True,
                    name=f"halo-notify-{index}",
                ).start()
        work_queue = _notify_queue
    try:
        work_queue.put_nowait((event, cfg))
    except queue.Full:
        # Never run Ring enrichment on the FCM/asyncio caller thread. A
        # saturated worker queue still gets a basic notification immediately.
        _log.warning("Notification queue full; showing one event without enrichment")
        _prepare_ring_notification(event, cfg, allow_enrichment=False)


def _notification_worker(work_queue: queue.Queue) -> None:
    while True:
        event, cfg = work_queue.get()
        try:
            _prepare_ring_notification(event, cfg)
        except Exception as exc:
            _log.debug("Notification worker error: %s", exc)
        finally:
            work_queue.task_done()


def _prepare_ring_notification(
    event,
    cfg: dict | None = None,
    *,
    allow_enrichment: bool = True,
) -> None:
    cfg = cfg or _config.load()
    kind = _event_kind(event)
    if not _notifications_enabled(cfg) or not _event_type_enabled(kind, cfg):
        return

    context = {}
    should_enrich = allow_enrichment and bool(
        cfg.get("notification_summaries", True) or cfg.get("notification_preview_images", True)
    )
    if should_enrich:
        try:
            from halo_gtk.ring_client import get_client

            client = get_client()
            if client is not None and client.is_authenticated:
                context = client.get_event_notification_context(event)
        except Exception as exc:
            _log.debug("Failed to enrich Ring notification: %s", exc)

    if not cfg.get("notification_summaries", True):
        context.pop("description", None)

    summary, body, icon = _ring_notification_text(event, context, cfg)
    target = _notification_target(event)
    image_bytes = (
        context.get("image_bytes") if cfg.get("notification_preview_images", True) else None
    )
    # Decode the preview here, on the worker thread, so showing the notification
    # never blocks the GTK main loop on image decoding.
    image_pixbuf = _pixbuf_from_bytes(image_bytes) if image_bytes else None
    urgency = _notification_urgency(kind)

    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        GLib.idle_add(
            lambda: (
                send_notification(
                    summary,
                    body,
                    icon,
                    image_pixbuf=image_pixbuf,
                    target=target,
                    urgency=urgency,
                )
                or False
            )
        )
    except Exception as exc:
        _log.debug("Failed to schedule Ring notification: %s", exc)


def _ring_notification_text(
    event,
    context: dict | None = None,
    cfg: dict | None = None,
) -> tuple[str, str, str]:
    context = context or {}
    cfg = cfg or _config.load()
    kind = _event_kind(event)
    device_name = _event_device_name(event)
    description = context.get("description")

    if kind == "ding":
        return (
            f"Doorbell: {device_name}",
            _notification_body(kind, description or "", cfg),
            "audio-input-microphone-symbolic",
        )
    if kind == "motion":
        return (
            f"Motion detected: {device_name}",
            _notification_body(kind, description or "", cfg),
            "camera-video-symbolic",
        )
    if _is_alarm_kind(kind):
        return (
            f"Alarm alert: {device_name}",
            _notification_body(kind, description or str(kind), cfg),
            "security-high-symbolic",
        )

    return (
        f"Ring alert: {device_name}",
        _notification_body(kind, description or str(kind), cfg),
        "security-high-symbolic",
    )


def _event_kind(event) -> str | None:
    return normalized_event_kind(event) or None


def _is_alarm_kind(kind: str | None) -> bool:
    return bool(kind and kind.startswith("alarm"))


def _event_type_enabled(kind: str | None, cfg: dict) -> bool:
    if kind == "ding":
        return bool(cfg.get("notify_doorbell", True))
    if kind == "motion":
        return bool(cfg.get("notify_motion", True))
    if _is_alarm_kind(kind):
        return bool(cfg.get("notify_alarm", True))
    return True


def _notification_body(kind: str | None, default_body: str, cfg: dict) -> str:
    if not cfg.get("custom_notification_messages", False):
        return default_body

    if kind == "ding":
        custom = cfg.get("custom_doorbell_message", "")
    elif kind == "motion":
        custom = cfg.get("custom_motion_message", "")
    elif _is_alarm_kind(kind):
        custom = cfg.get("custom_alarm_message", "")
    else:
        custom = ""
    return str(custom).strip() or default_body


def _notification_urgency(kind: str | None) -> str:
    return "critical" if _is_alarm_kind(kind) else "normal"


def _notification_target(event) -> _NotificationTarget | None:
    device_id = getattr(event, "doorbot_id", None)
    event_id = getattr(event, "id", None)
    if device_id is None:
        return None
    try:
        device_id_int = int(device_id)
        event_id_int = int(event_id) if event_id is not None else None
        started_at = float(getattr(event, "now", 0) or 0)
        expires_in = float(getattr(event, "expires_in", 0) or 0)
    except (TypeError, ValueError):
        return None
    return _NotificationTarget(
        device_id=device_id_int,
        event_id=event_id_int,
        expires_at=started_at + expires_in,
    )
