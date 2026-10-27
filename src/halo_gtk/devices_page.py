"""Unified cached device overview with explicit Chime view lifecycle."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from halo_gtk.chime_details import (  # noqa: E402
    ChimeDetails,
    _ChimeViewSession,
    _connection_text,
    _write_reason,
)
from halo_gtk.intercom_details import IntercomDetails, IntercomViewSession  # noqa: E402
from halo_gtk.intercom_details import _fresh as _intercom_fresh  # noqa: E402
from halo_gtk.intercom_details import _retry as _intercom_retry  # noqa: E402
from halo_gtk.light_group_details import (  # noqa: E402
    LightGroupDetails,
    _LightViewSession,
    _report_text,
    _retry_delay,
)
from halo_gtk.ring_client import get_client  # noqa: E402

_CAMERA_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})


def _location_factory():
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item):
        item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=24))

    def bind(_factory, item):
        name = item.get_item().get_string()
        item.get_child().set_label(name)
        item.get_child().set_tooltip_text(name)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def _device_counts(devices, snapshot, intercom_snapshot=None):
    """Replace the SDK Chime contribution once a DTO inventory is available."""
    devices = [device for device in devices if getattr(device, "family", None) != "groups"]
    intercom_count = 0
    if intercom_snapshot is not None and intercom_snapshot.has_inventory:
        from halo_gtk.intercom import KINDS

        devices = [
            device
            for device in devices
            if not (
                getattr(device, "family", None) == "other"
                and getattr(device, "kind", None) in KINDS
            )
        ]
        intercom_count = len(intercom_snapshot.devices)
    families = {getattr(device, "family", None) or "other" for device in devices}
    if intercom_count:
        families.add("intercoms")
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
    return len(devices) - sdk_chimes + chime_count + intercom_count, cameras, sorted(families)


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
        self._lighting_session = _LightViewSession(
            self._lighting_changed, self._lighting_retired, provider=lambda: get_client()
        )
        self._intercom_session = IntercomViewSession(
            self._intercom_changed, self._intercom_retired, provider=lambda: get_client()
        )
        self._intercom_rows = []
        self._light_location_ids = ()
        self._light_selected_id = None
        self._light_location_epoch = None
        self._light_updating = False
        self._light_rows = []
        self._light_needs_groups = False
        self._light_force_groups = False
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
        self._build_lighting()
        self._intercom_group = Adw.PreferencesGroup(
            title="Intercoms",
            description="Reported status and entrance release with confirmation. "
            "Calling remains in the Ring app.",
        )
        self._intercom_refresh = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        self._intercom_refresh.connect("clicked", lambda *_: self._fetch_intercoms(force=True))
        self._intercom_group.set_header_suffix(self._intercom_refresh)
        self._inner.append(self._intercom_group)
        self._intercom_message = Gtk.Label(
            wrap=True, xalign=0, use_markup=False, css_classes=["dim-label"]
        )
        self._inner.append(self._intercom_message)

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
            intercom_snapshot = (
                client.get_intercom_snapshot() if hasattr(client, "get_intercom_snapshot") else None
            )
            count, cameras, families = _device_counts(
                client.all_devices, snapshot, intercom_snapshot
            )
            label = "device" if count == 1 else "devices"
            self._account_group.set_description(f"{count} {label} cached")
            self._device_count_label.set_label(str(count))
            self._summary_row.set_subtitle(
                ", ".join(f.replace("_", " ").title() for f in families) or "No devices cached"
            )
            camera_label = "camera" if cameras == 1 else "cameras"
            self._camera_row.set_subtitle(f"{cameras} {camera_label} linked to this account")
        self._render_chimes(snapshot)
        lighting = getattr(self, "_lighting_session", None)
        if lighting is not None:
            if lighting.active and (not signed_in or lighting.current() is None):
                lighting.close()
                self._close_dialog()
            lighting_snapshot = client.get_light_group_snapshot() if signed_in else None
            self._render_lighting(lighting_snapshot)
        intercoms = getattr(self, "_intercom_session", None)
        if intercoms is not None:
            if intercoms.active and (not signed_in or intercoms.current() is None):
                intercoms.close()
                self._close_dialog()
            self._render_intercoms(client.get_intercom_snapshot() if signed_in else None)

    def on_page_shown(self):
        self._visible = True
        self.refresh()
        client = get_client()
        if client is not None and client.is_authenticated and self._session.open(client):
            self._fetch(force=False)
        lighting = getattr(self, "_lighting_session", None)
        if (
            lighting is not None
            and client is not None
            and client.is_authenticated
            and lighting.open(client)
        ):
            self._refresh_lighting(force=False)
        intercoms = getattr(self, "_intercom_session", None)
        if (
            intercoms is not None
            and client is not None
            and client.is_authenticated
            and intercoms.open(client)
        ):
            self._fetch_intercoms(force=False)

    def on_page_hidden(self):
        self._visible = False
        self._session.close()
        self._close_dialog()
        self._render_chimes(None)
        lighting = getattr(self, "_lighting_session", None)
        if lighting is not None:
            lighting.close()
            self._render_lighting(None)
        intercoms = getattr(self, "_intercom_session", None)
        if intercoms is not None:
            intercoms.close()
            self._render_intercoms(None)

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

    def _build_lighting(self):
        self._light_group = Adw.PreferencesGroup(
            title="Light Groups",
            description="Logical lighting groups, separate from physical device counts.",
        )
        self._light_refresh = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        self._light_refresh.set_tooltip_text(
            "Refresh lighting locations and the selected group list"
        )
        self._light_refresh.connect("clicked", lambda *_: self._refresh_lighting(force=True))
        self._light_group.set_header_suffix(self._light_refresh)
        location_row = Adw.ActionRow(title="Location", use_markup=False)
        self._light_location = Gtk.DropDown(model=Gtk.StringList.new([]), valign=Gtk.Align.CENTER)
        self._light_location.set_factory(_location_factory())
        self._light_location.set_hexpand(True)
        self._light_location.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Light Groups location"]
        )
        self._light_location.connect("notify::selected", self._light_location_changed)
        location_row.add_suffix(self._light_location)
        self._light_group.add(location_row)
        self._inner.append(self._light_group)
        self._light_message = Gtk.Label(
            wrap=True, xalign=0, use_markup=False, css_classes=["dim-label"]
        )
        self._inner.append(self._light_message)

    def _refresh_lighting(self, *, force):
        session = self._lighting_session
        snapshot = session.current()
        if not self._visible or snapshot is None or _retry_delay(snapshot) > 0:
            return
        selected = snapshot.find_location(self._light_selected_id)
        if selected is not None and _retry_delay(selected) > 0:
            return
        self._light_needs_groups = True
        self._light_force_groups = force
        session.request("locations", lambda: session.client.refresh_light_locations(force=force))

    def _lighting_retired(self):
        self._close_dialog()
        self._light_selected_id = None
        self._light_location_epoch = None
        self._render_lighting(None)
        self.refresh()

    def _lighting_changed(self, snapshot, completed):
        if not self._visible:
            return
        if self._lighting_session.current() is None:
            self._lighting_retired()
            return
        self._render_lighting(snapshot)
        if self._light_needs_groups and not self._lighting_session.pending("locations"):
            self._light_needs_groups = False
            location_id = self._light_selected_id
            if location_id is not None:
                force, self._light_force_groups = self._light_force_groups, False
                self._lighting_session.request(
                    "groups",
                    lambda: self._lighting_session.client.refresh_light_groups(
                        location_id, force=force
                    ),
                )
        for future in completed.values():
            try:
                if future is None:
                    raise RuntimeError
                future.result()
            except Exception:
                self._light_message.set_label(
                    "Lighting refresh unavailable; last-known evidence may be stale."
                )

    def _render_lighting(self, snapshot):
        for row in self._light_rows:
            self._light_group.remove(row)
        self._light_rows.clear()
        locations = snapshot.locations if snapshot is not None else ()
        selected = next(
            (loc for loc in locations if loc.location_id == self._light_selected_id), None
        )
        if selected is None and locations:
            selected = locations[0]
        selected_id = selected.location_id if selected is not None else None
        selected_epoch = selected.identity_epoch if selected is not None else None
        if (selected_id, selected_epoch) != (self._light_selected_id, self._light_location_epoch):
            self._close_dialog()
            self._lighting_session.cancel_read("groups")
            self._light_selected_id, self._light_location_epoch = selected_id, selected_epoch
            self._light_needs_groups = selected_id is not None
        self._light_updating = True
        self._light_location_ids = tuple(loc.location_id for loc in locations)
        self._light_location.set_model(Gtk.StringList.new([loc.name for loc in locations]))
        self._light_location.set_selected(
            self._light_location_ids.index(selected_id)
            if selected_id is not None
            else Gtk.INVALID_LIST_POSITION
        )
        self._light_location.set_sensitive(bool(locations) and self._visible)
        self._light_updating = False
        self._light_refresh.set_sensitive(
            self._visible
            and self._lighting_session.active
            and not self._lighting_session.pending("locations")
            and snapshot is not None
            and _retry_delay(snapshot) == 0
            and (selected is None or _retry_delay(selected) == 0)
        )
        if selected is None:
            title = (
                "No lighting locations reported"
                if snapshot is not None and snapshot.complete
                else "Lighting locations not loaded"
            )
            self._add_light_row(Adw.ActionRow(title=title, use_markup=False))
            self._light_message.set_label(
                "Choose a Ring location to load its existing logical groups."
            )
            return
        for group in selected.groups:
            count = group.member_count
            membership = "Member count unknown" if count is None else f"{count} reported members"
            row = Adw.ActionRow(
                title=group.name, subtitle=f"{_report_text(group)} · {membership}", use_markup=False
            )
            row.set_title_lines(2)
            row.set_subtitle_lines(0)
            row.add_prefix(Gtk.Image(icon_name="display-brightness-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.set_activatable(True)
            row.connect(
                "activated",
                self._open_light_group,
                snapshot.generation,
                selected.location_id,
                selected.identity_epoch,
                group.group_id,
                group.identity_epoch,
            )
            self._add_light_row(row)
        if not selected.groups:
            title = (
                "No Light Groups reported at this location"
                if selected.complete
                else "Group list unavailable or not loaded"
            )
            self._add_light_row(Adw.ActionRow(title=title, use_markup=False))
        self._light_message.set_label(
            f"{len(selected.groups)} cached logical groups here. "
            "Other locations are loaded separately."
            if selected.has_inventory
            else "This location's group inventory has not been loaded completely."
        )
        if (
            not snapshot.complete
            or not selected.complete
            or snapshot.status.value in {"stale", "error"}
            or selected.status.value in {"stale", "error"}
        ):
            self._light_message.set_label(
                "Lighting inventory is unavailable or stale. "
                "Cached groups are not current evidence."
            )

    def _add_light_row(self, row):
        self._light_group.add(row)
        self._light_rows.append(row)

    def _light_location_changed(self, dropdown, _param):
        if self._light_updating:
            return
        index = dropdown.get_selected()
        if index >= len(self._light_location_ids):
            return
        location_id = self._light_location_ids[index]
        if location_id != self._light_selected_id:
            self._close_dialog()
            self._lighting_session.cancel_read("groups")
            self._light_selected_id = location_id
            snapshot = self._lighting_session.current()
            location = snapshot.find_location(location_id) if snapshot else None
            self._light_location_epoch = location.identity_epoch if location else None
            self._light_needs_groups = True
            self._lighting_changed(snapshot, {}) if snapshot is not None else None

    def _open_light_group(
        self, _row, generation, location_id, location_epoch, group_id, identity_epoch
    ):
        snapshot = self._lighting_session.current() if self._visible else None
        location = snapshot.find_location(location_id) if snapshot else None
        group = snapshot.find_group(location_id, group_id) if snapshot else None
        if (
            group is None
            or location is None
            or snapshot.generation != generation
            or location.identity_epoch != location_epoch
            or group.identity_epoch != identity_epoch
            or location_id != self._light_selected_id
        ):
            return
        self._close_dialog()
        dialog = LightGroupDetails(self._lighting_session.client, location_id, group_id)
        self._dialog = dialog
        dialog.connect("closed", self._dialog_closed)
        dialog.present(self)

    def _fetch_intercoms(self, *, force):
        snapshot = self._intercom_session.current()
        if not self._visible or snapshot is None or _intercom_retry(snapshot) > 0:
            return
        client = self._intercom_session.client
        self._intercom_session.request("inventory", lambda: client.refresh_intercoms(force=force))

    def _intercom_changed(self, snapshot, completed):
        if self._visible:
            self._render_intercoms(snapshot)

    def _intercom_retired(self):
        self._close_dialog()
        self._render_intercoms(None)

    def _render_intercoms(self, snapshot):
        for row in self._intercom_rows:
            self._intercom_group.remove(row)
        self._intercom_rows.clear()
        self._intercom_refresh.set_sensitive(
            self._visible
            and snapshot is not None
            and self._intercom_session.active
            and not self._intercom_session.pending("inventory")
            and _intercom_retry(snapshot) == 0
        )
        for device in snapshot.devices if snapshot else ():
            row = Adw.ActionRow(title=device.name, use_markup=False)
            row.set_title_lines(2)
            row.set_subtitle_lines(0)
            row.set_subtitle(
                f"{device.model} · Reported {device.connection}"
                + (" · stale" if not _intercom_fresh(device) else "")
            )
            row.add_prefix(Gtk.Image(icon_name="call-start-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.set_activatable(True)
            row.connect(
                "activated",
                self._open_intercom_row,
                device.api_id,
                device.identity_epoch,
                snapshot.generation,
            )
            self._intercom_group.add(row)
            self._intercom_rows.append(row)
        if not self._intercom_rows:
            row = Adw.ActionRow(
                title="No Intercoms reported"
                if snapshot and snapshot.complete
                else "Intercom inventory not loaded",
                use_markup=False,
            )
            self._intercom_group.add(row)
            self._intercom_rows.append(row)
        self._intercom_message.set_label(
            "Reported evidence is stale or unavailable. Reload to check."
            if snapshot and not snapshot.complete
            else "No physical lock or door-position state is inferred."
        )

    def _open_intercom_row(self, _row, api_id, epoch, generation):
        return self.open_intercom(api_id, epoch, generation)

    def open_intercom(self, api_id, epoch, generation):
        snapshot = self._intercom_session.current() if self._visible else None
        device = snapshot.find_device(api_id) if snapshot else None
        if device is None or device.identity_epoch != epoch or snapshot.generation != generation:
            return False
        self._close_dialog()
        dialog = IntercomDetails(self._intercom_session.client, api_id)
        self._dialog = dialog
        dialog.connect("closed", self._dialog_closed)
        dialog.present(self)
        return True

    def close(self):
        self.on_page_hidden()

    def do_unroot(self):
        self.close()
        Gtk.ScrolledWindow.do_unroot(self)
