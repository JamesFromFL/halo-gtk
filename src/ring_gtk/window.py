"""Main application window (Adw.ApplicationWindow)."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ring_gtk.ring_client import get_client  # noqa: E402


class RingWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            title="Ring",
            default_width=480,
            default_height=640,
            **kwargs,
        )
        self._build_ui()
        self._refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # Header bar
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh_btn.connect("clicked", lambda *_: self._refresh())
        header.pack_end(refresh_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu")
        menu_btn.set_menu_model(self._build_menu())
        header.pack_end(menu_btn)

        # Status banner (shown when not authenticated)
        self._banner = Adw.Banner(title="Not signed in to Ring", button_label="Sign In")
        self._banner.connect("button-clicked", self._on_sign_in)
        toolbar_view.add_top_bar(self._banner)

        # Main scroll area
        scroll = Gtk.ScrolledWindow(vexpand=True)
        toolbar_view.set_content(scroll)

        self._list_box = Gtk.ListBox(
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
            css_classes=["boxed-list"],
            selection_mode=Gtk.SelectionMode.NONE,
        )
        scroll.set_child(self._list_box)

        # Placeholder shown while loading / no devices
        self._status_page = Adw.StatusPage(
            icon_name="network-wired-symbolic",
            title="No devices",
            description="Sign in to see your Ring devices.",
        )
        self._list_box.set_placeholder(self._status_page)

    def _build_menu(self) -> Gio_Menu:  # type: ignore[name-defined]  # noqa: F821
        from gi.repository import Gio

        menu = Gio.Menu()
        menu.append("About Ring", "app.about")
        menu.append("Quit", "app.quit")
        return menu

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        client = get_client()
        if client is None or not client.is_authenticated:
            self._banner.set_revealed(True)
            return
        self._banner.set_revealed(False)
        GLib.idle_add(self._populate_devices)

    def _populate_devices(self) -> bool:
        client = get_client()
        if client is None:
            return GLib.SOURCE_REMOVE

        # Clear existing rows
        while (child := self._list_box.get_first_child()) is not None:
            self._list_box.remove(child)

        for device in client.all_devices:
            row = Adw.ActionRow(
                title=device.name,
                subtitle=device.__class__.__name__,
                icon_name="camera-video-symbolic",
            )
            self._list_box.append(row)

        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _on_sign_in(self, *_) -> None:
        from ring_gtk.auth_dialog import AuthDialog

        dialog = AuthDialog()
        dialog.present(self)
