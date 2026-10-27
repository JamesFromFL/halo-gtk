"""Current-account Intercom notifications; their only action opens details."""

from __future__ import annotations

from contextlib import suppress
from html import escape

from halo_gtk import config
from halo_gtk.light_groups import light_group_monotonic_time

_active = []


def withdraw_intercom_notifications(generation=None):
    for notification, owner in tuple(_active):
        if generation is not None and owner != generation:
            continue
        with suppress(Exception):
            notification.close()
        if (notification, owner) in _active:
            _active.remove((notification, owner))


def _open_target(_notification, _action, target):
    from gi.repository import Gtk

    from halo_gtk.ring_client import get_client

    generation, api_id, epoch, expires = target
    client = get_client()
    snapshot = client.get_intercom_snapshot() if client and client.is_authenticated else None
    device = snapshot.find_device(api_id) if snapshot else None
    if (
        snapshot is None
        or snapshot.generation != generation
        or device is None
        or (device.identity_epoch != epoch or light_group_monotonic_time() > expires)
    ):
        return
    app = Gtk.Application.get_default()
    if app is None:
        return
    app.activate()
    win = app.get_active_window()
    if win is not None and hasattr(win, "open_intercom"):
        win.open_intercom(api_id, epoch, generation)


def send_intercom_notification(device, observation, generation):
    from halo_gtk.notifications import _ensure_notify

    cfg = config.load()
    if not cfg.get("show_notifications", True) or not cfg.get("notify_doorbell", True):
        return
    if not _ensure_notify():
        return
    from gi.repository import Notify

    call = observation.kind == "ding"
    notification = Notify.Notification.new(
        "Intercom call" if call else "Intercom unlock event",
        escape(device.name)
        + (
            "\nRing reported a call. Answer in the Ring app."
            if call
            else "\nRing reported an unlock event. Physical door state is unknown."
        ),
        "call-start-symbolic" if call else "changes-prevent-symbolic",
    )
    notification.set_app_name("Halo")
    notification.set_timeout(30000)
    notification.add_action(
        "view",
        "View Intercom",
        _open_target,
        (generation, device.api_id, device.identity_epoch, light_group_monotonic_time() + 60),
    )
    entry = (notification, generation)
    notification.connect("closed", lambda *_: _active.remove(entry) if entry in _active else None)
    _active.append(entry)
    while len(_active) > 16:
        old, _generation = _active.pop(0)
        with suppress(Exception):
            old.close()
    try:
        notification.show()
    except Exception:
        if entry in _active:
            _active.remove(entry)
