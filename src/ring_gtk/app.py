"""Main Adw.Application subclass — lifecycle, D-Bus single-instance, startup."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from ring_gtk import APP_ID, APP_VERSION  # noqa: E402
from ring_gtk.window import RingWindow  # noqa: E402


class RingApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        GLib.set_application_name("Ring")
        GLib.set_prgname(APP_ID)
        self._setup_actions()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Initialise libnotify so notifications are available before a
        # window is shown (e.g. when running in background/systray mode).
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify  # type: ignore[attr-defined]

            Notify.init("Ring")
        except (ValueError, ImportError):
            pass  # libnotify not available — graceful degradation

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None:
            win = RingWindow(application=self)
        win.present()

    def do_shutdown(self) -> None:
        from ring_gtk.ring_client import get_client  # avoid circular at top

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
            application_name="Ring",
            application_icon=APP_ID,
            developer_name="JamesFromFL",
            version=APP_VERSION,
            website="https://github.com/JamesFromFL/ring-gtk",
            issue_url="https://github.com/JamesFromFL/ring-gtk/issues",
            license_type=0,  # GTK_LICENSE_GPL_3_0
            copyright="© 2026 JamesFromFL",
        )
        dialog.present(self.get_active_window())
