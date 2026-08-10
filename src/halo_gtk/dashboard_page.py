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
            margin_top=24,
            margin_bottom=32,
            margin_start=24,
            margin_end=24,
        )
        self.set_child(self._inner)

        self._account_row: Adw.ActionRow | None = None
        self._device_count_row: Adw.ActionRow | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        status_group = Adw.PreferencesGroup(title="Ring Account")
        self._inner.append(status_group)

        self._account_row = Adw.ActionRow(title="Session")
        self._account_row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
        status_group.add(self._account_row)

        self._device_count_row = Adw.ActionRow(title="Ring Devices")
        self._device_count_row.add_prefix(Gtk.Image(icon_name="computer-symbolic"))
        status_group.add(self._device_count_row)

        cameras_label = Gtk.Label(
            label="Cameras",
            css_classes=["title-3"],
            halign=Gtk.Align.START,
            margin_top=6,
        )
        self._inner.append(cameras_label)

        if self._camera_grid is not None:
            self._camera_grid.set_vexpand(True)
            self._camera_grid.set_size_request(-1, 420)
            self._inner.append(self._camera_grid)

    def refresh(self) -> None:
        client = get_client()
        signed_in = client is not None and client.is_authenticated

        if self._account_row is not None:
            email = get_cached_account_email()
            if signed_in and email:
                self._account_row.set_subtitle(email)
            elif signed_in:
                self._account_row.set_subtitle("Signed in")
            else:
                self._account_row.set_subtitle("Not signed in")

        if self._device_count_row is not None:
            if not signed_in:
                self._device_count_row.set_subtitle("Sign in to load Ring devices")
            else:
                device_count = len(client.all_devices)
                label = "device" if device_count == 1 else "devices"
                self._device_count_row.set_subtitle(f"{device_count} {label} loaded")

        if self._camera_grid is not None and hasattr(self._camera_grid, "refresh"):
            self._camera_grid.refresh()
