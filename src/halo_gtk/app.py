"""Main Adw.Application subclass — lifecycle, D-Bus single-instance, startup."""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from halo_gtk import (  # noqa: E402
    APP_ID,
    APP_VERSION,
    autostart,  # noqa: E402
)
from halo_gtk import config as _config  # noqa: E402
from halo_gtk.settings_window import SettingsWindow  # noqa: E402
from halo_gtk.systray import SystemTray  # noqa: E402
from halo_gtk.window import RingWindow  # noqa: E402

_log = logging.getLogger(__name__)

_RESTORE_JOIN_TIMEOUT = 1


class RingApplication(Adw.Application):
    def __init__(self, *, start_background: bool = False) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._tray = SystemTray(self)
        self._background_hold = False
        self._start_background = start_background
        self._suppress_initial_activate = start_background
        self._quitting = False
        self._settings_window: SettingsWindow | None = None
        self._restore_thread: threading.Thread | None = None
        self._setup_actions()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
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
        self.reconcile_autostart()
        self.apply_background_settings()
        if self._start_background:
            self._start_session_restore()

    def do_activate(self) -> None:
        if self._suppress_initial_activate:
            self._suppress_initial_activate = False
            return
        if self._start_background:
            self._start_background = False
            self.apply_background_settings()
        win = self.get_main_window()
        if win is None:
            win = RingWindow(application=self)
        win.present()
        self._start_session_restore()

    def get_main_window(self) -> RingWindow | None:
        for win in self.get_windows():
            if isinstance(win, RingWindow):
                return win
        return None

    def _start_session_restore(self) -> None:
        """Restore cached Ring session without blocking GTK startup."""
        from halo_gtk.ring_client import get_client

        existing_client = get_client()
        if existing_client is not None and existing_client.is_authenticated:
            return
        if self._restore_thread is not None and self._restore_thread.is_alive():
            return

        self._restore_thread = threading.Thread(
            target=self._restore_session_worker,
            daemon=True,
        )
        self._restore_thread.start()

    def _restore_session_worker(self) -> None:
        from halo_gtk.ring_client import (
            get_client,
            init_client_from_cache,
            start_client,
            warm_account_email_cache,
        )

        restored = False
        try:
            warm_account_email_cache()
        except Exception as exc:
            _log.debug("Could not warm account email cache: %s", exc)
        existing_client = get_client()
        if existing_client is not None and existing_client.is_authenticated:
            restored = True
        else:
            client = init_client_from_cache()
            # Don't spin up the FCM listener if the user quit mid-restore;
            # do_shutdown stops the cached client it set.
            if client is not None and not self._quitting and start_client(client):
                restored = True
        if not self._quitting:
            GLib.idle_add(self._finish_session_restore, restored)

    def _finish_session_restore(self, restored: bool) -> bool:
        if restored and not self._quitting:
            win = self.get_main_window()
            if win is not None:
                win.refresh()
        return GLib.SOURCE_REMOVE

    def apply_background_settings(self) -> None:
        cfg = _config.load()
        should_hold = bool(
            cfg.get("background_service", False)
            or (self._start_background and cfg.get("autostart_login", False))
        )
        if should_hold and not self._background_hold:
            self.hold()
            self._background_hold = True
        elif not should_hold and self._background_hold:
            self.release()
            self._background_hold = False

        if cfg.get("show_tray_icon", True):
            self._tray.setup()
        else:
            self._tray.shutdown()

    def reconcile_autostart(self) -> None:
        cfg = _config.load()
        try:
            autostart.set_enabled(
                bool(cfg.get("autostart_login", False)),
                background=bool(cfg.get("autostart_background", False)),
            )
        except OSError as exc:
            _log.warning("Could not reconcile login autostart: %s", exc)

    def should_hide_on_close(self) -> bool:
        return bool(_config.load().get("background_service", False))

    def do_shutdown(self) -> None:
        from halo_gtk.ring_client import shutdown_client  # avoid circular at top

        self._quitting = True
        win = self.get_main_window()
        if win is not None:
            win.stop_runtime_streams()
        else:
            from halo_gtk.live_sessions import get_live_session_manager

            get_live_session_manager().stop_all()

        shutdown_client()
        restore_thread = self._restore_thread
        if (
            restore_thread is not None
            and restore_thread is not threading.current_thread()
            and restore_thread.is_alive()
        ):
            restore_thread.join(timeout=_RESTORE_JOIN_TIMEOUT)
        self._tray.shutdown()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _setup_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.request_quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect("activate", self._on_settings)
        self.add_action(settings_action)

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

    def _on_settings(self, *_) -> None:
        main_window = self.get_main_window()
        if main_window is None:
            self.activate()
            main_window = self.get_main_window()

        if self._settings_window is None:
            self._settings_window = SettingsWindow(application=self)

        if main_window is not None:
            self._settings_window.set_transient_for(main_window)
        self._settings_window.refresh()
        self._settings_window.present()

    def request_quit(self) -> None:
        self._quitting = True
        self.quit()

    def is_quitting(self) -> bool:
        return self._quitting

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
            "Project Disclaimer\n\n"
            "This is an independent community project provided as-is. Features and"
            " integrations are tested carefully, but Ring service changes may affect"
            " behavior without notice.\n\n"
            "I'm sharing this simply because I found it useful, and I hope other Linux"
            " enthusiasts do too."
        )
        alert = Adw.AlertDialog(heading="Disclaimers", body=_DISCLAIMERS)
        alert.add_response("close", "Close")
        alert.set_default_response("close")
        alert.set_close_response("close")
        alert.present(self.get_active_window())
        return True
