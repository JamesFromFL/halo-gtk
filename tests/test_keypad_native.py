"""Native keypad UI with synthetic state only; opt in on an isolated display."""

import os
from concurrent.futures import Future
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk import keypad_details as ui  # noqa: E402
from halo_gtk.alarm.models import (  # noqa: E402
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmWriteAuthorization,
    alarm_monotonic_time,
)
from halo_gtk.keypad_preferences import KeypadPreferences  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("HALO_NATIVE_LAYOUT_TESTS") != "1", reason="Requires isolated GTK display"
)


def settle():
    loop = GLib.MainLoop()
    GLib.timeout_add(80, lambda: (loop.quit(), False)[1])
    loop.run()


@pytest.mark.parametrize("width", [420, 700])
def test_keypad_local_lock_apply_and_dialog_retirement(monkeypatch, tmp_path, width):
    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    device = AlarmDeviceSnapshot(
        "fixture-home",
        "fixture-hub",
        "fixture-keypad",
        name="Sample entry keypad",
        kind=AlarmDeviceKind.KEYPAD,
        raw_type="security-keypad",
        connection=AlarmConnectionStatus.ONLINE,
        keypad_brightness=50,
        revision=4,
    )
    snapshot = AlarmAccountSnapshot(
        generation=1,
        locations=(
            AlarmLocationSnapshot(
                "fixture-home",
                write_authorization=AlarmWriteAuthorization.ALLOWED,
                assets=(
                    AlarmAssetSnapshot(
                        "fixture-home",
                        "fixture-hub",
                        kind="base_station_v1",
                        connection=AlarmConnectionStatus.ONLINE,
                        inventory=AlarmInventoryStatus.COMPLETE,
                        inventory_monotonic=alarm_monotonic_time(),
                        devices=(device,),
                    ),
                ),
            ),
        ),
    )
    callbacks, sent = [], []
    future = Future()

    def request(*args, **kwargs):
        sent.append((args, kwargs))
        return future

    client = SimpleNamespace(
        get_alarm_snapshot=lambda: snapshot,
        add_alarm_callback=callbacks.append,
        remove_alarm_callback=callbacks.remove,
        request_keypad_brightness=request,
    )
    monkeypatch.setattr(ui, "get_client", lambda: client)
    monkeypatch.setattr(ui, "get_cached_account_email", lambda: "fixture@example.test")
    monkeypatch.setattr(ui, "preferences", KeypadPreferences(tmp_path / "keypads.json"))
    window = Adw.Window(default_width=width, default_height=760)
    window.set_content(Gtk.Box())
    window.present()
    dialog = ui.KeypadDetailsDialog(client, device)
    try:
        dialog.present(window)
        settle()
        content = dialog.get_child()
        content.allocate(min(width, 460), 560, -1, None)
        assert content.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= width
        assert not dialog._apply.get_sensitive() and dialog._locked.get_active()
        dialog._enabled.set_active(True)
        dialog._locked.set_active(False)
        dialog._scale.set_value(25)
        assert not sent and dialog._apply.get_sensitive()
        dialog._apply_clicked()
        assert len(sent) == 1 and sent[0][0][-1] == 25
        assert sent[0][1]["expected_revision"] == 4
        assert not dialog._apply.get_sensitive()
        dialog.close()
        settle()
        assert not sent[0][1]["intent"].current()
        assert not callbacks and dialog._timer == 0
    finally:
        dialog.close()
        window.destroy()
        settle()
