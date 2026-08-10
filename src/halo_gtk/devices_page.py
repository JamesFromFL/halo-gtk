"""Unified Ring devices page."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk.ring_client import get_client  # noqa: E402


class DevicesPage(Gtk.ScrolledWindow):
    """First-class destination for non-camera Ring devices."""

    def __init__(self) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        self._inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=24,
            margin_bottom=32,
            margin_start=24,
            margin_end=24,
        )
        self.set_child(self._inner)

        self._summary_row: Adw.ActionRow | None = None
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        heading = Gtk.Label(
            label="Devices",
            css_classes=["title-1"],
            halign=Gtk.Align.START,
        )
        self._inner.append(heading)

        overview = Adw.PreferencesGroup(title="Ring Devices")
        self._inner.append(overview)

        self._summary_row = Adw.ActionRow(title="Account Devices")
        self._summary_row.add_prefix(Gtk.Image(icon_name="computer-symbolic"))
        overview.add(self._summary_row)

        next_group = Adw.PreferencesGroup(title="Planned Device Controls")
        self._inner.append(next_group)

        for title, subtitle, icon_name in (
            (
                "Chimes",
                "Linked device details, volume, and test sound controls.",
                "audio-speakers-symbolic",
            ),
            (
                "Light Groups",
                "Grouped Ring light controls and status.",
                "display-brightness-symbolic",
            ),
            (
                "Intercom",
                "Ring Intercom status and confirmed access workflows.",
                "phone-symbolic",
            ),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image(icon_name=icon_name))
            row.set_sensitive(False)
            next_group.add(row)

    def refresh(self) -> None:
        if self._summary_row is None:
            return

        client = get_client()
        if client is None or not client.is_authenticated:
            self._summary_row.set_subtitle("Sign in to load Ring devices")
            return

        devices = client.all_devices
        device_count = len(devices)
        families = sorted({getattr(device, "family", None) or "other" for device in devices})
        label = "device" if device_count == 1 else "devices"
        if families:
            self._summary_row.set_subtitle(f"{device_count} {label}: {', '.join(families)}")
        else:
            self._summary_row.set_subtitle(f"{device_count} {label}")
