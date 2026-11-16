"""Opt-in native Beams UI checks using only synthetic state and futures."""

import os
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk import beams_details as ui  # noqa: E402
from halo_gtk.alarm.beams import BeamsLight, BeamsResult, BeamsSnapshot  # noqa: E402
from halo_gtk.alarm.models import AlarmDeviceSnapshot, TriState  # noqa: E402
from halo_gtk.alarm.provider import AlarmRefreshResult, AlarmRefreshStatus  # noqa: E402
from halo_gtk.beams_preferences import BeamsPreferences  # noqa: E402
from tests.test_keypad_native import settle  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("HALO_NATIVE_LAYOUT_TESTS") != "1", reason="Requires isolated GTK display"
)


@pytest.mark.parametrize("width", [420, 700])
def test_local_preferences_explicit_actions_outcome_and_retirement(monkeypatch, tmp_path, width):
    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    device = AlarmDeviceSnapshot(
        "sample-home", "sample-bridge", "sample-light", name="Sample garden light", revision=7
    )
    light = BeamsLight(device, "Sample home", None, TriState.INACTIVE, 999999999, 0)
    snapshot = [BeamsSnapshot(1, (light,))]
    callbacks, requests = [], []
    future = Future()

    def request(*args, **kwargs):
        requests.append((args, kwargs))
        return future

    client = SimpleNamespace(
        is_authenticated=True,
        get_beams_snapshot=lambda: snapshot[0],
        add_alarm_callback=callbacks.append,
        remove_alarm_callback=callbacks.remove,
        request_beams_light=request,
        cancel_beams_intent=lambda intent: intent.cancel(),
    )
    monkeypatch.setattr(ui, "get_client", lambda: client)
    monkeypatch.setattr(ui, "get_cached_account_email", lambda: "fixture@example.test")
    monkeypatch.setattr(ui, "preferences", BeamsPreferences(tmp_path / "beams.json"))
    window = Adw.Window(default_width=width, default_height=760)
    window.set_content(Gtk.Box())
    window.present()
    dialog = ui.BeamsDetails(client, light.identity)
    try:
        dialog.present(window)
        settle()
        content = dialog.get_child()
        content.allocate(min(width, 460), 600, -1, None)
        assert content.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= width
        assert not dialog._on.get_sensitive() and not dialog._automatic.get_sensitive()
        dialog._enabled.set_active(True)
        dialog._duration.set_selected(2)
        assert not requests and dialog._on.get_sensitive()
        assert "5 minutes" in dialog._on.get_label()
        dialog._request(None)
        settle()
        assert len(requests) == 1 and requests[0][0][-1] is None
        assert requests[0][1]["expected_revision"] == 7
        assert not dialog._on.get_sensitive() and not dialog._automatic.get_sensitive()
        future.set_result(BeamsResult("submitted", "automatic-unverified", True))
        settle()
        assert "does not report the mode" in dialog._result.get_label()
        incomplete = Future()
        incomplete.set_result(AlarmRefreshResult(AlarmRefreshStatus.INCOMPLETE))
        dialog._changed(snapshot[0], {"inventory": incomplete})
        assert "incomplete" in dialog._result.get_label()
        assert "completed" not in dialog._result.get_label()
        cooldown = Future()
        cooldown.set_result(AlarmRefreshResult(AlarmRefreshStatus.COOLDOWN))
        dialog._changed(snapshot[0], {"inventory": cooldown})
        assert "cooling down" in dialog._result.get_label()
        snapshot[0] = replace(snapshot[0], lights=(replace(light, reason="owner-unavailable"),))
        callbacks[0](None)
        settle()
        assert not dialog._on.get_sensitive()
        dialog.retire()
        settle()
        assert not requests[0][1]["intent"].current() and not callbacks
        assert dialog._session._watch_id is None
    finally:
        dialog.retire()
        window.destroy()
        settle()
