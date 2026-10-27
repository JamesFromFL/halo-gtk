"""Unified cached device overview with explicit Chime view lifecycle."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk.chime_details import (  # noqa: E402
    ChimeDetails,
    _ChimeViewSession,
    _connection_text,
    _write_reason,
)
from halo_gtk.ring_client import get_client  # noqa: E402

_CAMERA_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})


def _device_counts(devices, snapshot):
    """Replace the SDK Chime contribution once a DTO inventory is available."""
    families = {getattr(device, "family", None) or "other" for device in devices}
    sdk_chimes = sum(getattr(device, "family", None) == "chimes" for device in devices)
    chime_count = (
        len(snapshot.devices)
        if (
            snapshot is not None
            and (snapshot.has_inventory or snapshot.complete or snapshot.devices)
        )
        else sdk_chimes
    )
    if chime_count:
        families.add("chimes")
    else:
        families.discard("chimes")
    cameras = sum(getattr(device, "family", None) in _CAMERA_FAMILIES for device in devices)
    return len(devices) - sdk_chimes + chime_count, cameras, sorted(families)


class DevicesPage(Gtk.ScrolledWindow):
    """Overview rendering is cache-only, including when the page is hidden."""

    def __init__(self, *, on_show_cameras: Callable[[], None] | None = None) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        self._on_show_cameras = on_show_cameras
        self._visible = False
        self._dialog = None
        self._chime_rows = []
        self._session = _ChimeViewSession(
            self._snapshot_changed, self._account_retired, provider=lambda: get_client()
        )
        self._inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._inner.add_css_class("page-scroll-content")
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=700)
        clamp.set_child(self._inner)
        self.set_child(clamp)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        self._account_group = Adw.PreferencesGroup(title="Ring Account")
        self._inner.append(self._account_group)
        self._summary_row = Adw.ActionRow(title="Account Devices", use_markup=False)
        self._summary_row.add_prefix(Gtk.Image(icon_name="preferences-system-devices-symbolic"))
        self._device_count_label = Gtk.Label(label="0", css_classes=["numeric", "dim-label"])
        self._summary_row.add_suffix(self._device_count_label)
        self._account_group.add(self._summary_row)
        self._camera_row = Adw.ActionRow(title="Cameras", use_markup=False)
        self._camera_row.add_prefix(Gtk.Image(icon_name="camera-video-symbolic"))
        if self._on_show_cameras is not None:
            self._camera_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            self._camera_row.set_activatable(True)
            self._camera_row.connect("activated", lambda _row: self._on_show_cameras())
        self._account_group.add(self._camera_row)
        self._chime_group = Adw.PreferencesGroup(
            title="Chimes",
            description="Standalone plug-in Ring Chimes. Legacy controls are experimental.",
        )
        self._refresh_button = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        self._refresh_button.set_tooltip_text("Refresh Chime inventory")
        self._refresh_button.connect("clicked", lambda *_: self._fetch(force=True))
        self._chime_group.set_header_suffix(self._refresh_button)
        self._inner.append(self._chime_group)
        self._chime_message = Gtk.Label(
            wrap=True, xalign=0, use_markup=False, css_classes=["dim-label"]
        )
        self._inner.append(self._chime_message)
        planned = Adw.PreferencesGroup(title="Not Available in Halo Yet")
        self._inner.append(planned)
        for title, subtitle, icon in (
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
            row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.set_sensitive(False)
            planned.add(row)

    def refresh(self) -> None:
        """Render cached snapshots; never start HTTP or a worker from here."""
        client = get_client()
        signed_in = client is not None and client.is_authenticated
        if self._session.active and (not signed_in or self._session.current() is None):
            self._session.close()
            self._close_dialog()
        snapshot = client.get_chime_snapshot() if signed_in else None
        if not signed_in:
            self._account_group.set_description("Connect your Ring account to load devices.")
            self._summary_row.set_subtitle("Sign in to load Ring devices")
            self._device_count_label.set_label("0")
            self._camera_row.set_subtitle("Sign in to load cameras")
        else:
            count, cameras, families = _device_counts(client.all_devices, snapshot)
            label = "device" if count == 1 else "devices"
            self._account_group.set_description(f"{count} {label} cached")
            self._device_count_label.set_label(str(count))
            self._summary_row.set_subtitle(
                ", ".join(f.replace("_", " ").title() for f in families) or "No devices cached"
            )
            camera_label = "camera" if cameras == 1 else "cameras"
            self._camera_row.set_subtitle(f"{cameras} {camera_label} linked to this account")
        self._render_chimes(snapshot)

    def on_page_shown(self):
        self._visible = True
        self.refresh()
        client = get_client()
        if client is not None and client.is_authenticated and self._session.open(client):
            self._fetch(force=False)

    def on_page_hidden(self):
        self._visible = False
        self._session.close()
        self._close_dialog()
        self._render_chimes(None)

    def _account_retired(self):
        self._close_dialog()
        self.refresh()

    def _close_dialog(self):
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.retire()

    def _fetch(self, *, force):
        if not self._visible or self._session.current() is None:
            return
        client = self._session.client
        self._session.request("inventory", lambda: client.refresh_chimes(force=force))
        self._refresh_button.set_sensitive(not self._session.pending("inventory"))

    def _snapshot_changed(self, _snapshot, completed):
        if not self._visible:
            return
        self.refresh()
        if "inventory" in completed:
            future = completed["inventory"]
            try:
                if future is None:
                    raise RuntimeError
                future.result()
            except Exception:
                self._chime_message.set_label(
                    "Chime refresh unavailable. Last-known status may be stale."
                )

    def _render_chimes(self, snapshot):
        for row in self._chime_rows:
            self._chime_group.remove(row)
        self._chime_rows.clear()
        self._refresh_button.set_sensitive(
            self._visible and self._session.active and not self._session.pending("inventory")
        )
        if snapshot is None or not snapshot.devices:
            title = (
                "No Chimes reported"
                if snapshot is not None and snapshot.complete
                else "Chime inventory not loaded"
            )
            row = Adw.ActionRow(title=title, use_markup=False)
            self._chime_group.add(row)
            self._chime_rows.append(row)
        else:
            for device in snapshot.devices:
                row = Adw.ActionRow(title=device.name, use_markup=False)
                row.set_title_lines(2)
                row.set_subtitle_lines(0)
                reason = _write_reason(device)
                row.set_subtitle(
                    f"{device.model} · {_connection_text(device)}"
                    + (" · Read only" if reason else "")
                )
                row.add_prefix(Gtk.Image(icon_name="audio-speakers-symbolic"))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.set_activatable(True)
                row.connect(
                    "activated",
                    self._open_chime,
                    device.api_id,
                    device.identity_epoch,
                    snapshot.generation,
                )
                self._chime_group.add(row)
                self._chime_rows.append(row)
        if snapshot is None:
            self._chime_message.set_label("Open Devices while signed in to read Chime status.")
        elif snapshot.status.value in {"stale", "error"}:
            self._chime_message.set_label(
                "Chime status is unavailable or stale. Refresh to check Ring's report."
            )
        elif not snapshot.complete:
            self._chime_message.set_label(
                "Chime inventory is incomplete; no devices are assumed removed."
            )
        else:
            self._chime_message.set_label(
                "Reported cloud status does not prove that a Chime speaker is reachable."
            )

    def _open_chime(self, _row, api_id, identity_epoch, generation):
        snapshot = self._session.current() if self._visible else None
        device = snapshot.find_device(api_id) if snapshot is not None else None
        if (
            device is None
            or snapshot.generation != generation
            or device.identity_epoch != identity_epoch
        ):
            return
        self._close_dialog()
        dialog = ChimeDetails(self._session.client, api_id)
        self._dialog = dialog
        dialog.connect("closed", self._dialog_closed)
        dialog.present(self)

    def _dialog_closed(self, dialog):
        if self._dialog is dialog:
            self._dialog = None

    def close(self):
        self.on_page_hidden()

    def do_unroot(self):
        self.close()
        Gtk.ScrolledWindow.do_unroot(self)
