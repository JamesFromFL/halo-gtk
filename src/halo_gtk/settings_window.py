"""Dedicated application settings window."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw  # noqa: E402

from halo_gtk.settings_page import SettingsPage  # noqa: E402


class SettingsWindow(Adw.ApplicationWindow):
    """Non-modal settings window that can stay open beside the main app."""

    def __init__(self, **kwargs) -> None:
        super().__init__(
            title="Settings",
            default_width=920,
            default_height=650,
            **kwargs,
        )
        self.set_size_request(360, 480)
        self.connect("close-request", self._on_close_request)
        self._build_ui()

    def _build_ui(self) -> None:
        self._settings_page = SettingsPage()
        self.set_content(self._settings_page)

        # Start compact so the first size negotiation does not inherit the
        # combined minimum width of both split-view panes. The breakpoint then
        # expands Settings when the window has enough room.
        self._settings_page.split_view.set_collapsed(True)
        wide = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("min-width: 600px"),
        )
        wide.add_setter(self._settings_page.split_view, "collapsed", False)
        self.add_breakpoint(wide)

    def refresh(self) -> None:
        self._settings_page.refresh()

    def _on_close_request(self, *_) -> bool:
        self.set_visible(False)
        return True
