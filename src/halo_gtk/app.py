"""Main Adw.Application subclass — lifecycle, D-Bus single-instance, startup."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, Gtk  # noqa: E402

from halo_gtk import APP_ID, APP_VERSION  # noqa: E402
from halo_gtk.window import RingWindow  # noqa: E402


class RingApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._setup_actions()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        style_manager = Adw.StyleManager.get_default()
        style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        # Initialise GStreamer for live camera feed playback.
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            Gst.init(None)
        except (ValueError, ImportError):
            pass  # GStreamer not available — live stream will fail gracefully
        # Initialise libnotify so notifications are available before a
        # window is shown (e.g. when running in background/systray mode).
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify  # type: ignore[attr-defined]

            Notify.init("Halo")
        except (ValueError, ImportError):
            pass  # libnotify not available — graceful degradation

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None:
            # Try to restore a prior session before creating the window so
            # the device list populates immediately without a sign-in prompt.
            self._try_restore_session()
            win = RingWindow(application=self)
        win.present()

    def _try_restore_session(self) -> None:
        from halo_gtk.ring_client import init_client_from_cache

        client = init_client_from_cache()
        if client is not None:
            client.start()

    def do_shutdown(self) -> None:
        from halo_gtk.ring_client import get_client  # avoid circular at top

        client = get_client()
        if client is not None:
            client.stop()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _setup_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

    def _on_about(self, *_) -> None:
        dialog = Adw.AboutDialog(
            application_name="Halo",
            application_icon=APP_ID,
            developer_name="JamesFromFL",
            version=APP_VERSION,
            website="https://github.com/JamesFromFL/halo-gtk",
            issue_url="https://github.com/JamesFromFL/halo-gtk/issues",
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 JamesFromFL — Unofficial GTK client for Ring home security",
        )
        dialog.add_link("Disclaimers", "about:disclaimers")
        dialog.connect("activate-link", self._on_about_link)
        dialog.present(self.get_active_window())

    def _on_about_link(self, dialog, url: str) -> bool:
        if url != "about:disclaimers":
            return False
        _DISCLAIMERS = (
            "Halo\n\n"
            "An Unofficial GTK Client for Ring Devices\n\n"
            "Project Philosophy & Origin\n\n"
            "This application was born out of a personal need to monitor my home security"
            " directly from my Arch Linux desktop. It is not an official product of Ring"
            " or Amazon, and I am not affiliated with them in any capacity.\n\n"
            "AI Disclaimer\n\n"
            "I am a Linux enthusiast using this project as a learning bridge to application"
            " development. To bring this vision to life, I have collaborated extensively"
            " with Claude AI to generate and debug the codebase. While I am meticulously"
            " testing every feature and revision to ensure it meets my personal standards"
            " for a polished experience, please be aware that this is a community-driven"
            " project provided as-is.\n\n"
            "I'm sharing this simply because I found it useful, and I hope other Linux"
            " enthusiasts do too."
        )
        alert = Adw.AlertDialog(heading="Disclaimers", body=_DISCLAIMERS)
        alert.add_response("close", "Close")
        alert.set_default_response("close")
        alert.set_close_response("close")
        alert.present(self.get_active_window())
        return True
