"""Opt-in native preferences check; synthetic inventory and isolated display."""

import os
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib  # noqa: E402

from halo_gtk import notification_preferences as ui  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("HALO_NATIVE_LAYOUT_TESTS") != "1", reason="Requires isolated GTK display"
)


def test_native_rules_dialog_saves_schedule_device_and_snooze(monkeypatch):
    Adw.init()
    saved = []
    client = SimpleNamespace(
        is_authenticated=True,
        all_devices=[SimpleNamespace(id=42, name="Front <Door>", family="doorbots")],
        get_intercom_snapshot=lambda: SimpleNamespace(devices=()),
        get_alarm_snapshot=lambda: SimpleNamespace(locations=()),
    )
    monkeypatch.setattr(ui, "get_client", lambda: client)
    monkeypatch.setattr(ui.config, "load", lambda: {})
    monkeypatch.setattr(ui.config, "save", saved.append)
    window = Adw.Window(default_width=420, default_height=700)
    window.present()
    dialog = ui.NotificationPreferences()
    try:
        dialog.present(window)
        loop = GLib.MainLoop()
        GLib.timeout_add(100, lambda: (loop.quit(), False)[1])
        loop.run()
        assert dialog.get_width() <= 420
        dialog._quiet.set_active(True)
        dialog._start.set_text("23:30")
        dialog._end.set_text("06:15")
        key, enabled, motion, ding, snooze = dialog._devices[0]
        assert key == "camera:42"
        motion.set_active(False)
        snooze.set_selected(2)
        dialog._save()
        value = saved[0]["notification_rules"]
        assert value["quiet_start"] == 1410 and value["quiet_end"] == 375
        assert value["devices"][key]["motion"] is False
        assert value["devices"][key]["snoozed_until"] > 0
    finally:
        dialog.close()
        window.destroy()
