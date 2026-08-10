"""Unified Ring devices page."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk.ring_client import get_client  # noqa: E402

_CAMERA_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})


class DevicesPage(Gtk.ScrolledWindow):
    """Account overview for cameras and planned Ring device families."""

    def __init__(self, *, on_show_cameras: Callable[[], None] | None = None) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        self._on_show_cameras = on_show_cameras
        self._inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
        )
        self._inner.add_css_class("page-scroll-content")
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=700)
        clamp.set_child(self._inner)
        self.set_child(clamp)

        self._account_group: Adw.PreferencesGroup | None = None
        self._summary_row: Adw.ActionRow | None = None
        self._device_count_label: Gtk.Label | None = None
        self._camera_row: Adw.ActionRow | None = None
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        self._account_group = Adw.PreferencesGroup(
            title="Ring Account",
            description="Connect your Ring account to load devices.",
        )
        self._inner.append(self._account_group)

        self._summary_row = Adw.ActionRow(title="Account Devices")
        self._summary_row.add_prefix(Gtk.Image(icon_name="preferences-system-devices-symbolic"))
        self._device_count_label = Gtk.Label(label="0", css_classes=["numeric", "dim-label"])
        self._summary_row.add_suffix(self._device_count_label)
        self._account_group.add(self._summary_row)

        self._camera_row = Adw.ActionRow(title="Cameras")
        self._camera_row.add_prefix(Gtk.Image(icon_name="camera-video-symbolic"))
        if self._on_show_cameras is not None:
            next_icon = Gtk.Image(icon_name="go-next-symbolic")
            next_icon.set_tooltip_text("Show cameras")
            self._camera_row.add_suffix(next_icon)
            self._camera_row.set_activatable(True)
            self._camera_row.connect("activated", lambda _row: self._on_show_cameras())
        self._account_group.add(self._camera_row)

        next_group = Adw.PreferencesGroup(title="Not Available in Halo Yet")
        self._inner.append(next_group)

        for title, subtitle, icon_name in (
            (
                "Chimes",
                "Linked device details, volume, and test sound controls are planned.",
                "audio-speakers-symbolic",
            ),
            (
                "Light Groups",
                "Grouped Ring light controls and status are planned.",
                "display-brightness-symbolic",
            ),
            (
                "Intercom",
                "Ring Intercom status and confirmed access workflows are planned.",
                "call-start-symbolic",
            ),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image(icon_name=icon_name))
            row.set_sensitive(False)
            next_group.add(row)

    def refresh(self) -> None:
        if (
            self._account_group is None
            or self._summary_row is None
            or self._device_count_label is None
            or self._camera_row is None
        ):
            return

        client = get_client()
        if client is None or not client.is_authenticated:
            self._summary_row.set_subtitle("Sign in to load Ring devices")
            self._device_count_label.set_label("0")
            self._camera_row.set_subtitle("Sign in to load cameras")
            self._account_group.set_description("Connect your Ring account to load devices.")
            return

        devices = client.all_devices
        device_count = len(devices)
        families = sorted({getattr(device, "family", None) or "other" for device in devices})
        label = "device" if device_count == 1 else "devices"
        self._account_group.set_description(f"{device_count} {label} loaded")
        self._device_count_label.set_label(str(device_count))
        if families:
            family_labels = [family.replace("_", " ").title() for family in families]
            self._summary_row.set_subtitle(", ".join(family_labels))
        else:
            self._summary_row.set_subtitle(f"{device_count} {label}")

        camera_count = sum(
            1
            for device in devices
            if (getattr(device, "family", None) or "other") in _CAMERA_FAMILIES
        )
        camera_label = "camera" if camera_count == 1 else "cameras"
        self._camera_row.set_subtitle(f"{camera_count} {camera_label} linked to this account")
