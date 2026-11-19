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

from gi.repository import Adw, GdkPixbuf, GLib, Gst, Gtk  # noqa: E402

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
        logo = window._brand.get_first_child()
        assert isinstance(logo, Gtk.Picture)
        assert logo.get_paintable() is not None
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


def test_window_starts_when_packaged_logo_decoder_fails(isolated_native_ui, monkeypatch, caplog):
    def fail_to_decode(*_args):
        raise GLib.Error("Synthetic image loader failure")

    monkeypatch.setattr(GdkPixbuf.Pixbuf, "new_from_file_at_scale", fail_to_decode)
    window = RingWindow()
    try:
        logo = window._brand.get_first_child()
        assert isinstance(logo, Gtk.Image)
        assert logo.get_icon_name() == "camera-video-symbolic"
        assert logo.get_pixel_size() == 28
        window.set_default_size(420, 920)
        window.present()
        for destination in ("cameras", "history", "devices", "alarm", "dashboard"):
            window._show_page(destination)
            _settle()
            assert window._content_stack.get_visible_child_name() == destination
        assert window.get_mapped()
        assert "Packaged app logo unavailable; using a symbolic icon" in caplog.text
    finally:
        window.stop_runtime_streams()
        window.destroy()
        _settle()
    assert get_client() is None


@pytest.mark.parametrize("dpi", [96, 115])
def test_native_compact_header_fits_all_five_views(isolated_native_ui, dpi):
    settings = Gtk.Settings.get_default()
    old_dpi = settings.get_property("gtk-xft-dpi")
    settings.set_property("gtk-xft-dpi", dpi * 1024)
    window = RingWindow()
    try:
        window.set_default_size(360, 800)
        window.present()
        for destination in ("dashboard", "cameras", "history", "devices", "alarm"):
            window._show_page(destination)
            window.allocate(360, 800, -1, None)
            _settle()
            assert window._compact
            assert window._content_header.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= 360
    finally:
        window.stop_runtime_streams()
        window.destroy()
        settings.set_property("gtk-xft-dpi", old_dpi)
        _settle()


def test_native_microphone_hold_release_focus_and_unmap(isolated_native_ui):
    from types import SimpleNamespace

    from gi.repository import Gdk

    from halo_gtk.talk_button import HoldToTalkButton

    outcomes = []
    events = []
    session = SimpleNamespace(
        start_talking=lambda **kwargs: (
            events.append("start"),
            outcomes.append(kwargs["on_state_changed"]),
        ),
        stop_talking=lambda: events.append("stop"),
    )
    button = HoldToTalkButton(lambda: session)
    window = Adw.Window(content=button)
    try:
        window.present()
        _settle()
        assert button._key_pressed(None, Gdk.KEY_space, 0, 0)
        assert button.get_label() == "Starting microphone…"
        outcomes[-1]("active")
        assert button.get_label() == "Transmitting"
        button._key_released(None, Gdk.KEY_space, 0, 0)
        outcomes[-1]("active")
        assert button.get_label() == "Hold to talk"
        button._press()
        button._window_active(SimpleNamespace(is_active=lambda: False), None)
        button._press()
        window.set_visible(False)
        assert events == ["start", "stop"] * 3
        assert button.get_label() == "Hold to talk"
    finally:
        window.destroy()
        _settle()


def test_native_closing_settings_and_main_exits_and_releases_lease(tmp_path):
    import subprocess
    import sys

    from scripts.managed_install import Manager

    manager = Manager(tmp_path / "halo-gtk" / "managed", home=tmp_path)
    name = "-".join(("a" * 12, "b" * 12, "c" * 12))
    # The managed launcher passes its shared lock to the actual GTK process
    # through exec. Give this isolated child the same descriptor lifetime.
    child = """
from gi.repository import GLib
from halo_gtk import config
from halo_gtk.app import RingApplication
from halo_gtk.settings_window import SettingsWindow

app = RingApplication()
config.load = lambda: {**config.defaults(), "background_service": False, "show_tray_icon": False}
app._start_session_restore = lambda: None
app.reconcile_autostart = lambda: None
def visit():
    app._on_settings()
    first = next(win for win in app.get_windows() if isinstance(win, SettingsWindow))
    first.close()
    assert first not in app.get_windows()
    app._on_settings()
    second = next(win for win in app.get_windows() if isinstance(win, SettingsWindow))
    assert second is not first
    second.close()
    app.get_main_window().close()
    return False
GLib.timeout_add(150, visit)
raise SystemExit(app.run(["halo-window-test"]))
"""
    with manager.lease(name, shared=True) as fd:
        proc = subprocess.Popen([sys.executable, "-c", child], pass_fds=(fd,))
    try:
        assert proc.wait(timeout=12) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    with manager.lease(name):
        pass


def test_native_zoomed_canvas_supports_discrete_pan(isolated_native_ui):
    from gi.repository import Gdk

    from halo_gtk.zoom_view import ZoomPaintableView

    canvas = ZoomPaintableView()
    surface = Gtk.DrawingArea(content_width=400, content_height=400)
    canvas.set_paintable(Gtk.WidgetPaintable(widget=surface))
    canvas.allocate(200, 200, -1, None)
    surface.allocate(400, 400, -1, None)
    canvas.set_zoom(2)
    assert canvas.get_focusable()
    assert canvas._pan_key(None, Gdk.KEY_Right, 0, 0)
    assert canvas.pan_origin()[0] < 0
    assert canvas._pan_key(None, Gdk.KEY_Left, 0, 0)
    assert canvas.pan_origin() == (0, 0)
    canvas.set_zoom(1)
    assert not canvas.get_focusable()
    assert not canvas._pan_key(None, Gdk.KEY_Right, 0, 0)
