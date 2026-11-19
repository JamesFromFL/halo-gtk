"""libnotify-based desktop notification helpers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

from halo_gtk import config as _config
from halo_gtk import device_names, notification_rules
from halo_gtk.alarm.events import AlarmEvent
from halo_gtk.camera_privacy import PrivacyLease, PrivacyWatch
from halo_gtk.ring_events import normalized_event_kind

if TYPE_CHECKING:
    from halo_gtk.ring_client import RingClient

_log = logging.getLogger(__name__)

_notify_available: bool | None = None
_active_notifications: list = []

# Bounded worker pool for notification enrichment so an event burst (alarm /
# rapid motion) can't spawn unbounded daemon threads doing heavy network I/O.
_NOTIFY_WORKERS = 2
_NOTIFY_QUEUE_MAX = 32
_notify_queue: queue.Queue | None = None
_notify_lock = threading.Lock()
_alarm_notifications: list = []


@dataclass(frozen=True)
class CameraNotificationTarget:
    client: RingClient
    generation: int
    preview_lease: PrivacyLease | None
    device_id: int | None
    event_id: int | None
    expires_at: float


@dataclass(frozen=True)
class AlarmNotificationTarget:
    generation: int
    location_id: str
    asset_id: str | None = None
    zid: str | None = None
    expires_at: float = 0.0


class _CameraNotifications:
    """Track visible camera alerts while their account and Ring grants are current."""

    def __init__(self):
        self.visible = []
        self.watch = None

    def add(self, notification, target):
        if self.watch is None:
            self.watch = PrivacyWatch(self.withdraw_retired)
        self.visible.append((notification, target))
        if len(self.visible) > 128:
            self.close(self.visible[0][0])

    def remove(self, notification):
        self.visible[:] = [entry for entry in self.visible if entry[0] is not notification]
        if not self.visible and self.watch is not None:
            self.watch.close()
            self.watch = None

    def close(self, notification):
        with suppress(Exception):
            notification.close()
        _drop_notification(notification)

    def withdraw_retired(self):
        for notification, target in tuple(self.visible):
            if not target.client.camera_notification_current(
                target.generation, target.preview_lease
            ):
                self.close(notification)


_camera_notifications = _CameraNotifications()


def withdraw_camera_notifications() -> None:
    """Close visible camera alerts on the GTK thread before its main loop exits."""
    for notification, _target in tuple(_camera_notifications.visible):
        _camera_notifications.close(notification)


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
    target: CameraNotificationTarget | AlarmNotificationTarget | None = None,
    urgency: str = "normal",
) -> None:
    """Show a desktop notification via libnotify."""
    if not _ensure_notify():
        if target is not None:
            _log.info("Desktop notification unavailable")
        else:
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

    if isinstance(target, AlarmNotificationTarget) or (
        target is not None and target.device_id is not None
    ):
        notif.add_action("view", "View", _on_view_action, target)

    notif.connect("closed", _on_notification_closed)
    _active_notifications.append(notif)
    if isinstance(target, CameraNotificationTarget):
        _camera_notifications.add(notif, target)
    elif isinstance(target, AlarmNotificationTarget):
        _alarm_notifications.append((notif, target))
        if len(_alarm_notifications) > 128:
            old, _old_target = _alarm_notifications[0]
            with suppress(Exception):
                old.close()
            _drop_notification(old)
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
    _camera_notifications.remove(notification)
    with suppress(ValueError):
        _active_notifications.remove(notification)
    _alarm_notifications[:] = [item for item in _alarm_notifications if item[0] is not notification]


def withdraw_alarm_notifications(*, generation=None, location_id=None) -> None:
    """GTK-thread cleanup; logout/revocation also removes already displayed text."""
    for notification, target in tuple(_alarm_notifications):
        if generation is not None and target.generation != generation:
            continue
        if location_id is not None and target.location_id != location_id:
            continue
        with suppress(Exception):
            notification.close()
        _drop_notification(notification)


def alarm_event_enabled(event: AlarmEvent, cfg: dict) -> bool:
    if not _notifications_enabled(cfg) or not cfg.get("notify_alarm", True):
        return False
    if not notification_rules.allowed(
        cfg,
        notification_rules.device_key("alarm", event.location_id),
        event.category,
        critical=event.critical,
    ):
        return False
    if not notification_rules.allowed(
        cfg,
        notification_rules.alarm_device_key(event.location_id, event.asset_id, event.zid),
        event.category,
        critical=event.critical,
    ):
        return False
    return event.category == "alarm" or (
        event.category in {"mode", "sensor", "health"}
        and bool(cfg.get(f"notify_alarm_{event.category}", False))
    )


def send_alarm_notification(event: AlarmEvent) -> None:
    """Render a typed live observation; history never calls this function."""
    from html import escape

    cfg = _config.load()
    if not alarm_event_enabled(event, cfg):
        return
    # Only explicitly opted-in detail includes household/device identifiers.
    summary = "Ring Alarm alert" if event.critical else "Ring Alarm update"
    detail = event.title
    if event.detail:
        detail += f": {event.detail}"
    if cfg.get("alarm_notification_details", False):
        names = " · ".join(name[:256] for name in (event.location_name, event.device_name) if name)
        if names:
            detail = f"{names}\n{detail}"
        custom = cfg.get("custom_alarm_message", "")
        if (
            cfg.get("custom_notification_messages", False)
            and isinstance(custom, str)
            and custom.strip()
        ):
            detail += "\n" + custom.strip()[:512]
    if event.critical:
        detail += "\nUse the Ring app or keypad for critical response."
    send_notification(
        summary,
        escape(detail[:2048]),
        "security-high-symbolic",
        target=AlarmNotificationTarget(
            event.generation, event.location_id, event.asset_id, event.zid, time.time() + 3600
        ),
        urgency="critical" if event.critical else "normal",
    )


def _on_view_action(
    notification, action: str, target: CameraNotificationTarget | AlarmNotificationTarget
) -> None:
    if isinstance(target, CameraNotificationTarget):
        _camera_notifications.close(notification)
    else:
        _drop_notification(notification)
    _open_notification_target(target)


def _open_notification_target(target: CameraNotificationTarget | AlarmNotificationTarget) -> None:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib

        GLib.idle_add(_open_notification_target_on_main, target)
    except Exception as exc:
        _log.debug("Failed to open notification target: %s", exc)


def _open_notification_target_on_main(
    target: CameraNotificationTarget | AlarmNotificationTarget,
) -> bool:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    if isinstance(
        target, CameraNotificationTarget
    ) and not target.client.camera_notification_current(target.generation, target.preview_lease):
        return False

    app = Gtk.Application.get_default()
    if app is None:
        return False

    app.activate()
    get_main = getattr(app, "get_main_window", None)
    win = get_main() if callable(get_main) else app.get_active_window()
    if win is None:
        return False

    if isinstance(target, AlarmNotificationTarget):
        from halo_gtk.ring_client import get_client

        client = get_client()
        snapshot = client.get_alarm_snapshot() if client is not None else None
        valid = (
            snapshot is not None
            and snapshot.generation == target.generation
            and time.time() < target.expires_at
            and snapshot.find_location(target.location_id) is not None
        )
        if hasattr(win, "open_alarm_location"):
            win.open_alarm_location(
                target.location_id if valid else None,
                asset_id=target.asset_id if valid else None,
                zid=target.zid if valid else None,
            )
        return False

    def navigate():
        if (
            time.time() < target.expires_at
            and hasattr(win, "open_camera_live")
            and win.open_camera_live(target.device_id)
        ):
            return
        if target.event_id is not None and hasattr(win, "open_history_event"):
            win.open_history_event(target.device_id, target.event_id)
        elif hasattr(win, "open_camera_live"):
            win.open_camera_live(target.device_id)

    target.client.apply_camera_notification(target.generation, target.preview_lease, navigate)

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


def send_ring_notification(event, target: CameraNotificationTarget) -> None:
    """Translate a ring-doorbell event into a desktop notification."""
    cfg = _config.load()
    kind = _event_kind(event)
    # The pinned Python FCM parser does not establish an Alarm event schema.
    # A prefix must not make arm/disarm/silenced messages critical, nor duplicate
    # the dedicated normalized CLAP observation stream.
    if _is_alarm_kind(kind):
        return
    if not _ring_event_enabled(event, cfg):
        return
    _enqueue_notification(event, target)


def _ring_event_enabled(event, cfg: dict) -> bool:
    kind = _event_kind(event)
    return (
        _notifications_enabled(cfg)
        and _event_type_enabled(kind, cfg)
        and notification_rules.allowed(
            cfg, notification_rules.device_key("camera", getattr(event, "doorbot_id", None)), kind
        )
    )


def _enqueue_notification(event, target: CameraNotificationTarget) -> None:
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
        work_queue.put_nowait((event, target))
    except queue.Full:
        # Never run Ring enrichment on the FCM/asyncio caller thread. A
        # saturated worker queue still gets a basic notification immediately.
        _log.warning("Notification queue full; showing one event without enrichment")
        _prepare_ring_notification(event, target, allow_enrichment=False)


def _notification_worker(work_queue: queue.Queue) -> None:
    while True:
        event, target = work_queue.get()
        try:
            _prepare_ring_notification(event, target)
        except Exception as exc:
            _log.debug("Notification worker error: %s", exc)
        finally:
            work_queue.task_done()


def _prepare_ring_notification(
    event,
    target: CameraNotificationTarget,
    *,
    allow_enrichment: bool = True,
) -> None:
    # Re-read queued work: a newly enabled snooze must stop enrichment as well.
    cfg = _config.load()
    kind = _event_kind(event)
    if _is_alarm_kind(kind):
        return
    if not _ring_event_enabled(event, cfg):
        return

    client, lease = target.client, target.preview_lease
    if not client.camera_notification_current(target.generation, lease):
        return
    context = {}
    summaries = cfg.get("notification_summaries", True)
    previews = cfg.get("notification_preview_images", True)
    if allow_enrichment and lease is not None and (summaries or previews):
        try:
            context = client.get_event_notification_context(
                event, lease, include_image=previews, include_summary=summaries
            )
        except Exception as exc:
            _log.debug("Failed to enrich Ring notification: %s", exc)

    # Decode off the GTK thread; the original grant is checked again at handoff.
    image = context.pop("image_bytes", None)
    image_pixbuf = _pixbuf_from_bytes(image) if image else None
    urgency = _notification_urgency(kind)

    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        def deliver():
            current_cfg = _config.load()
            if not _ring_event_enabled(event, current_cfg):
                return False
            text_context = context if current_cfg.get("notification_summaries", True) else {}
            summary, body, icon = _ring_notification_text(event, text_context, current_cfg)
            preview = image_pixbuf if current_cfg.get("notification_preview_images", True) else None
            client.apply_camera_notification(
                target.generation,
                lease,
                lambda: send_notification(
                    summary,
                    body,
                    icon,
                    image_pixbuf=preview,
                    target=target,
                    urgency=urgency,
                ),
            )
            return False

        GLib.idle_add(deliver)
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


def camera_notification_target(
    event, client: RingClient, generation: int
) -> CameraNotificationTarget:
    """Capture ownership before the event leaves its Ring listener thread."""
    device_id = getattr(event, "doorbot_id", None)
    event_id = getattr(event, "id", None)
    try:
        device_id = int(device_id)
        event_id = int(event_id) if event_id is not None else None
        started_at = float(getattr(event, "now", 0) or 0)
        expires_at = started_at + float(getattr(event, "expires_in", 0) or 0)
        if not isfinite(expires_at):
            raise ValueError("Non-finite event expiry")
    except (ValueError, TypeError, OverflowError):
        # Malformed events still get account-scoped, actionless basic alerts.
        device_id, event_id, expires_at = None, None, 0.0
    lease = (
        client.camera_privacy.acquire_for_id(device_id, "preview")
        if device_id is not None
        else None
    )
    return CameraNotificationTarget(client, generation, lease, device_id, event_id, expires_at)
