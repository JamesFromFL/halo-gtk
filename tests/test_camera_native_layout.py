"""Native layout regressions; opt in on an isolated GTK display.

Run with HALO_NATIVE_LAYOUT_TESTS=1 and an isolated GDK_BACKEND/BROADWAY_DISPLAY.
These checks construct a signed-out window and never create a Ring client.
"""

import os

import gi
import pytest

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, GLib, Gst, Gtk  # noqa: E402

from halo_gtk import config  # noqa: E402
from halo_gtk.cameras_page import CamerasPage  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402
from halo_gtk.window import RingWindow  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("HALO_NATIVE_LAYOUT_TESTS") != "1",
    reason="Requires an explicitly enabled, isolated GTK display",
)


@pytest.fixture
def isolated_native_ui(monkeypatch):
    Adw.init()
    Gst.init(None)
    assert get_client() is None
    monkeypatch.setattr(config, "load", lambda: config.defaults())
    monkeypatch.setattr(config, "save", lambda *_: None)
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)


def _settle():
    loop = GLib.MainLoop()
    GLib.timeout_add(50, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()


@pytest.mark.parametrize("width", [1440, 1050, 420])
def test_camera_inspector_adapts_before_content_allocation(isolated_native_ui, width):
    window = RingWindow()
    try:
        window.set_default_size(width, 920)
        window._show_page("cameras")
        window.present()
        for _ in range(3):
            window.allocate(width, 920, -1, None)
            window.get_content().allocate(width, 920, -1, None)
            _settle()
        page = window._cameras_page
        inspector = page.inspector_split_view
        assert inspector.get_collapsed() is (width <= 1100)
        assert window._compact is (width <= 980)
        assert page.get_width() == width
        assert page.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= width

        # A hidden inspector stays available as an overlay on narrow windows.
        page._inspector_toggle.set_active(True)
        assert inspector.get_show_sidebar()
        assert page.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= width
    finally:
        window.stop_runtime_streams()
        window.destroy()
        _settle()
    assert get_client() is None


def test_embedded_signed_out_message_fits_its_minimum_height(isolated_native_ui):
    page = CamerasPage(lambda *_: None, embedded=True)
    page._show_signed_out_state()
    status = page._status_page
    minimum = page.measure(Gtk.Orientation.VERTICAL, 380).minimum
    natural = page.measure(Gtk.Orientation.VERTICAL, 380).natural

    assert minimum == natural
    page.allocate(380, minimum, -1, None)
    for label in (status._title, status._description):
        assert label.get_visible()
        assert (
            label.get_height() >= label.measure(Gtk.Orientation.VERTICAL, label.get_width()).minimum
        )
        assert label.get_height() > 0
    assert not isinstance(page._scroll, Gtk.ScrolledWindow)
