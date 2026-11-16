"""Opt-in native history checks; only fake clients and private temporary files."""

import os
import time

import gi
import pytest

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk import alarm_history as ui  # noqa: E402
from halo_gtk import local_history_preferences as settings  # noqa: E402
from tests.test_keypad_native import settle  # noqa: E402
from tests.test_local_history_client import active as active  # noqa: E402
from tests.test_local_history_client import page  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("HALO_NATIVE_LAYOUT_TESTS") != "1", reason="Requires isolated GTK display"
)


def finished(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        settle()
    assert predicate(), "native history worker did not publish its result"


@pytest.mark.parametrize("width", [390, 700])
def test_local_history_native_preferences_and_saved_source(active, monkeypatch, width):
    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    current = [active]
    monkeypatch.setattr(ui, "get_client", lambda: current[0])
    monkeypatch.setattr(settings, "get_client", lambda: current[0])
    active._authenticated = True
    active._ring = object()
    monkeypatch.setattr(active, "alarm_history", lambda *_a, **_k: pytest.fail("no cloud fetch"))
    window = Adw.Window(default_width=width, default_height=760)
    view = ui.AlarmHistoryView()
    window.set_content(view)
    window.present()
    dialog = settings.LocalHistoryPreferences(active)
    try:
        dialog.present(window)
        finished(lambda: not dialog._busy)
        content = dialog.get_child()
        content.allocate(min(width, 460), 600, -1, None)
        assert content.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= width
        assert dialog._retention.get_selected() == 0 and dialog._save.get_sensitive()
        dialog._retention.set_selected(1)
        dialog._save.emit("clicked")
        finished(lambda: not dialog._busy)
        assert active.local_history_preferences()["days"] == 7
        assert "Retention saved" in dialog._message.get_label()
        active._save_local_history_result(page(), 1)
        dialog._clear_response(None, "cancel")
        assert active.saved_alarm_history("home").events
        dialog._clear_response(None, "clear")
        finished(lambda: not dialog._busy)
        assert not active.saved_alarm_history("home").events
        assert "Retention is unchanged" in dialog._message.get_label()
        active._save_local_history_result(page(), 1)
        dialog.close()
        settle()
        view._source.set_selected(1)
        view.refresh("home")
        finished(lambda: not view._loading)
        view.allocate(width, 740, -1, None)
        assert view.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= width
        assert view._events and "Saved copies" in view._coverage.get_label()
        assert view._rows.get_row_at_index(0) is not None
        assert not view._older.get_visible()
        current[0] = None
        view._watch_client()
        assert not view._events and not view._active
    finally:
        if not dialog._closed:
            dialog.close()
        view.deactivate(clear=True)
        window.destroy()
        settle()
