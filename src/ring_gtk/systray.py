"""System tray / status-notifier icon via AyatanaAppIndicator3.

Falls back gracefully if the indicator library is not installed.
Requires: gir1.2-ayatanaappindicator3-0.1 (Debian/Ubuntu) or equivalent.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


class SystemTray:
    """Wraps AppIndicator3 or AyatanaAppIndicator3 for a systray icon.

    Call :meth:`setup` once after the Gtk main loop has started.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._indicator = None

    def setup(self) -> bool:
        """Attempt to create the tray icon. Returns True on success."""
        indicator_cls = self._load_indicator_cls()
        if indicator_cls is None:
            return False

        import gi

        gi.require_version("Gtk", "4.0")

        self._indicator = indicator_cls.Indicator.new(
            "ring-gtk",
            "security-high-symbolic",
            indicator_cls.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(indicator_cls.IndicatorStatus.ACTIVE)
        self._indicator.set_menu(self._build_menu())
        _log.info("Systray icon initialised")
        return True

    # ------------------------------------------------------------------

    def _build_menu(self):
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        menu = Gtk.Menu()

        show_item = Gtk.MenuItem(label="Show Ring")
        show_item.connect("activate", self._on_show)
        menu.append(show_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: self._app.quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    def _on_show(self, *_) -> None:
        self._app.activate()

    @staticmethod
    def _load_indicator_cls():
        import gi

        for ns, ver in [("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")]:
            try:
                gi.require_version(ns, ver)
                mod = __import__("gi.repository", fromlist=[ns])
                return getattr(mod, ns)
            except (ValueError, AttributeError, ImportError):
                continue
        _log.warning(
            "Neither AyatanaAppIndicator3 nor AppIndicator3 found; "
            "systray icon will not be shown."
        )
        return None
