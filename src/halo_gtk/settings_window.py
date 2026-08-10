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
            default_width=780,
            default_height=560,
            **kwargs,
        )
        self.set_size_request(620, 420)
        self.connect("close-request", self._on_close_request)
        self._build_ui()

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        self._settings_page = SettingsPage()
        toolbar_view.set_content(self._settings_page)

    def refresh(self) -> None:
        self._settings_page.refresh()

    def _on_close_request(self, *_) -> bool:
        self.set_visible(False)
        return True
