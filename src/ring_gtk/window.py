"""Main application window (Adw.ApplicationWindow)."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ring_gtk.ring_client import get_client  # noqa: E402

# Map ring-doorbell device family names to symbolic icon names.
_FAMILY_ICON: dict[str, str] = {
    "doorbots": "video-display-symbolic",
    "authorized_doorbots": "video-display-symbolic",
    "stickup_cams": "camera-video-symbolic",
    "chimes": "audio-speakers-symbolic",
    "base_stations": "network-wired-symbolic",
    "other": "security-high-symbolic",
}


class RingWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            title="Ring",
            default_width=480,
            default_height=640,
            **kwargs,
        )
        self._build_ui()
        self.refresh()

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
        refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu")
        menu_btn.set_menu_model(self._build_menu())
        header.pack_end(menu_btn)

        # Sign-in banner (shown when not authenticated)
        self._banner = Adw.Banner(title="Not signed in to Ring", button_label="Sign In")
        self._banner.connect("button-clicked", self._on_sign_in)
        toolbar_view.add_top_bar(self._banner)

        # Spinner shown while fetching devices
        self._spinner = Adw.Spinner(visible=False)

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
            icon_name="security-high-symbolic",
            title="No devices",
            description="Sign in to see your Ring devices.",
        )
        self._list_box.set_placeholder(self._status_page)

    def _build_menu(self):
        from gi.repository import Gio

        menu = Gio.Menu()
        menu.append("About Ring", "app.about")
        menu.append("Quit", "app.quit")
        return menu

    # ------------------------------------------------------------------
    # Public refresh — called after auth and by the refresh button
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        client = get_client()
        if client is None or not client.is_authenticated:
            self._banner.set_revealed(True)
            self._status_page.set_description("Sign in to see your Ring devices.")
            return

        self._banner.set_revealed(False)
        self._status_page.set_title("Loading…")
        self._status_page.set_description("")
        self._clear_list()

        # Fetch devices on a background thread; update UI on main loop.
        import threading

        threading.Thread(target=self._fetch_and_populate, daemon=True).start()

    def _fetch_and_populate(self) -> None:
        client = get_client()
        try:
            # update_data is async; run it via the client's background loop.
            client._run(client._ring.async_update_data())
            devices = client.all_devices
            GLib.idle_add(self._populate_devices, devices)
        except Exception as exc:
            GLib.idle_add(self._show_fetch_error, str(exc))

    def _populate_devices(self, devices: list) -> bool:
        self._clear_list()

        if not devices:
            self._status_page.set_title("No devices found")
            self._status_page.set_description(
                "No Ring devices are linked to your account."
            )
            return GLib.SOURCE_REMOVE

        for device in devices:
            family = getattr(device, "family", "other") or "other"
            icon = _FAMILY_ICON.get(family, "security-high-symbolic")
            kind = getattr(device, "kind", "") or ""
            row = Adw.ActionRow(
                title=device.name,
                subtitle=kind,
                icon_name=icon,
            )
            self._list_box.append(row)

        self._status_page.set_title("No devices")
        self._status_page.set_description("No Ring devices are linked to your account.")
        return GLib.SOURCE_REMOVE

    def _show_fetch_error(self, message: str) -> bool:
        self._status_page.set_title("Failed to load devices")
        self._status_page.set_description(message)
        return GLib.SOURCE_REMOVE

    def _clear_list(self) -> None:
        while (child := self._list_box.get_first_child()) is not None:
            self._list_box.remove(child)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _on_sign_in(self, *_) -> None:
        from ring_gtk.auth_dialog import AuthDialog

        dialog = AuthDialog()
        dialog.present(self)
