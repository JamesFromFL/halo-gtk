"""Main Halo dashboard page."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk.ring_client import get_cached_account_email, get_client  # noqa: E402


class DashboardPage(Gtk.ScrolledWindow):
    """Operational first screen for the main Halo window."""

    def __init__(self, *, camera_grid: Gtk.Widget | None = None) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        self._camera_grid = camera_grid

        self._inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
        )
        self._inner.add_css_class("page-scroll-content")
        clamp = Adw.Clamp(maximum_size=1180, tightening_threshold=900)
        clamp.set_child(self._inner)
        self.set_child(clamp)

        self._account_row: Adw.ActionRow | None = None
        self._account_state_icon: Gtk.Image | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        status_group = Adw.PreferencesGroup()
        self._inner.append(status_group)

        self._account_row = Adw.ActionRow(title="Ring account")
        self._account_row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
        self._account_state_icon = Gtk.Image(icon_name="network-offline-symbolic")
        self._account_state_icon.set_tooltip_text("Not connected")
        self._account_row.add_suffix(self._account_state_icon)
        status_group.add(self._account_row)

        cameras_heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        cameras_heading.add_css_class("section-heading")
        cameras_title = Gtk.Label(label="Cameras", xalign=0)
        cameras_title.add_css_class("title-3")
        cameras_heading.append(cameras_title)
        cameras_subtitle = Gtk.Label(
            label="Live status and the most recent available snapshot",
            xalign=0,
            wrap=True,
        )
        cameras_subtitle.add_css_class("dim-label")
        cameras_heading.append(cameras_subtitle)
        self._inner.append(cameras_heading)

        if self._camera_grid is not None:
            self._camera_grid.set_vexpand(True)
            self._camera_grid.set_size_request(-1, 420)
            self._inner.append(self._camera_grid)

    def refresh(self) -> None:
        client = get_client()
        signed_in = client is not None and client.is_authenticated

        if self._account_row is not None:
            email = get_cached_account_email()
            if signed_in:
                device_count = len(client.all_devices)
                label = "device" if device_count == 1 else "devices"
                self._account_row.set_title("Ring account connected")
                detail = f"{device_count} {label} loaded"
                self._account_row.set_subtitle(f"{email} - {detail}" if email else detail)
                if self._account_state_icon is not None:
                    self._account_state_icon.set_from_icon_name("object-select-symbolic")
                    self._account_state_icon.set_tooltip_text("Connected")
            else:
                self._account_row.set_title("Ring account not connected")
                self._account_row.set_subtitle("Sign in to load Ring devices")
                if self._account_state_icon is not None:
                    self._account_state_icon.set_from_icon_name("network-offline-symbolic")
                    self._account_state_icon.set_tooltip_text("Not connected")

        if self._camera_grid is not None and hasattr(self._camera_grid, "refresh"):
            self._camera_grid.refresh()
