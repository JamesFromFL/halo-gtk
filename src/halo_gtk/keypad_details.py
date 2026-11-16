"""Keypad details: reported settings, explicit Apply and a local control lock."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk.alarm.keypads import KeypadIntent  # noqa: E402
from halo_gtk.alarm.models import (  # noqa: E402
    AlarmConnectionStatus,
    AlarmInventoryStatus,
    AlarmWriteAuthorization,
    TriState,
    alarm_monotonic_time,
)
from halo_gtk.keypad_preferences import KeypadPreference, preferences  # noqa: E402
from halo_gtk.ring_client import get_cached_account_email, get_client  # noqa: E402


class KeypadDetailsDialog(Adw.Dialog):
    def __init__(self, client, device):
        super().__init__(title=device.name or "Keypad", content_width=460, content_height=640)
        self._client = client
        self._identity = (device.location_id, device.asset_id, device.zid)
        self._generation = client.get_alarm_snapshot().generation
        self._account = (get_cached_account_email() or "").strip().casefold()
        self._closed = False
        self._updating = False
        self._dirty = False
        self._pending = False
        self._intent = None
        self._preference = KeypadPreference()
        self._device = None
        self._refresh_future = None
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{edge}")(18)
        reported = Adw.PreferencesGroup(title="Reported by keypad")
        self._brightness = Adw.ActionRow(title="LED brightness")
        self._proximity = Adw.ActionRow(title="Proximity")
        self._proximity.set_subtitle("Unknown — no live keypad proximity event observed")
        reported.add(self._brightness)
        reported.add(self._proximity)
        content.append(reported)
        permission = Adw.PreferencesGroup(
            title="Controls in Halo",
            description=(
                "The lock only prevents Halo brightness changes. "
                "Ring and other apps can still change this keypad."
            ),
        )
        self._enabled = Adw.SwitchRow(title="Enable brightness controls")
        self._locked = Adw.SwitchRow(title="Lock brightness changes")
        permission.add(self._enabled)
        permission.add(self._locked)
        content.append(permission)
        content.append(Gtk.Label(label="Requested brightness (%)", xalign=0))
        self._scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._scale.set_digits(0)
        self._scale.set_draw_value(True)
        self._scale.set_tooltip_text("Requested LED brightness percentage")
        self._scale.connect("value-changed", self._edited)
        content.append(self._scale)
        actions = Gtk.Box(spacing=12, homogeneous=True)
        self._refresh = Gtk.Button(label="Refresh status")
        self._refresh.connect("clicked", self._refresh_clicked)
        self._apply = Gtk.Button(label="Apply brightness")
        self._apply.add_css_class("suggested-action")
        self._apply.connect("clicked", self._apply_clicked)
        actions.append(self._refresh)
        actions.append(self._apply)
        content.append(actions)
        self._status = Gtk.Label(xalign=0, wrap=True)
        self._status.add_css_class("dim-label")
        content.append(self._status)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(content)
        toolbar.set_content(scroller)
        self.set_child(toolbar)
        self._enabled.connect("notify::active", self._permission_changed)
        self._locked.connect("notify::active", self._permission_changed)
        self.connect("closed", self._on_closed)
        client.add_alarm_callback(self._snapshot_changed)
        self._timer = GLib.timeout_add_seconds(2, self._tick)
        self._render()

    def _current(self):
        return (
            not self._closed
            and get_client() is self._client
            and self._client.get_alarm_snapshot().generation == self._generation
        )

    def _load_preference(self):
        try:
            return preferences.get(self._account, *self._identity)
        except ValueError:
            return KeypadPreference()

    def _render(self):
        if not self._current():
            self.close()
            return
        snapshot = self._client.get_alarm_snapshot()
        location = snapshot.find_location(self._identity[0])
        asset = location.find_asset(self._identity[1]) if location else None
        device = asset.find_device(self._identity[2]) if asset else None
        self._device = device
        value = device.keypad_brightness if device else None
        self._brightness.set_subtitle("Unknown" if value is None else f"{value}%")
        proximity = device.keypad_proximity if device else TriState.UNKNOWN
        if (
            proximity is TriState.ACTIVE
            and device.keypad_proximity_monotonic is not None
            and alarm_monotonic_time() - device.keypad_proximity_monotonic >= 20
        ):
            proximity = TriState.INACTIVE
        self._proximity.set_subtitle(
            {
                TriState.ACTIVE: "Detected within the last 20 seconds",
                TriState.INACTIVE: "No detection in the last 20 seconds",
            }.get(proximity, "Unknown — no current live proximity observation")
        )
        self._preference = self._load_preference()
        self._updating = True
        self._enabled.set_active(self._preference.enabled)
        self._locked.set_active(self._preference.locked)
        if not self._dirty and value is not None:
            self._scale.set_value(value)
        self._updating = False
        self._enabled.set_sensitive(bool(self._account))
        self._locked.set_sensitive(bool(self._account))
        fresh = bool(
            device
            and device.raw_type == "security-keypad"
            and value is not None
            and not device.stale
            and device.connection is AlarmConnectionStatus.ONLINE
            and asset.kind == "base_station_v1"
            and not asset.stale
            and asset.connection is AlarmConnectionStatus.ONLINE
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and asset.inventory_monotonic is not None
            and 0 <= alarm_monotonic_time() - asset.inventory_monotonic <= 120
        )
        authorized = bool(
            location and location.write_authorization is AlarmWriteAuthorization.ALLOWED
        )
        if value is not None and not fresh:
            self._brightness.set_subtitle(f"{value}% · previously reported")
        allowed = fresh and authorized and self._preference.allowed and not self._pending
        self._scale.set_sensitive(allowed)
        self._apply.set_sensitive(allowed and self._dirty)
        self._refresh.set_sensitive(not self._pending and self._refresh_future is None)
        if not self._pending and not self._status.get_label():
            self._status.set_label(
                "This account has no current permission to change the keypad."
                if not authorized
                else "Unlock local controls and choose a value, then Apply."
                if fresh
                else "Refresh status to obtain current, writable keypad settings."
            )

    def _edited(self, *_args):
        if not self._updating:
            self._dirty = True
            self._render()

    def _permission_changed(self, *_args):
        if self._updating or not self._current():
            return
        if self._intent is not None:
            self._intent.cancel()
        try:
            self._preference = preferences.set(
                self._account,
                *self._identity,
                enabled=self._enabled.get_active(),
                locked=self._locked.get_active(),
            )
            self._status.set_label("Local control permission saved. No device command was sent.")
        except (OSError, ValueError):
            self._status.set_label("Could not save local permission. No device command was sent.")
        self._render()

    def _apply_clicked(self, *_args):
        if not self._current() or not self._apply.get_sensitive() or self._device is None:
            return
        preference = self._preference
        self._intent = KeypadIntent(
            lambda: (
                preferences.get(self._account, *self._identity) == preference and preference.allowed
            )
        )
        self._pending = True
        self._status.set_label("Waiting for the keypad to report the requested brightness…")
        try:
            future = self._client.request_keypad_brightness(
                *self._identity,
                int(self._scale.get_value()),
                expected_revision=self._device.revision,
                intent=self._intent,
            )
            future.add_done_callback(lambda result: GLib.idle_add(self._finished, result))
        except Exception:
            self._pending = False
            self._status.set_label(
                "Could not start the request. Refresh status before trying again."
            )
        self._render()

    def _finished(self, future):
        if not self._current():
            return False
        self._pending = False
        try:
            result = future.result()
            if result.status == "confirmed":
                self._dirty = False
                message = "The keypad reported the requested brightness."
            elif result.possibly_sent:
                message = "Outcome unknown. Refresh status before another request."
            else:
                message = "Request was not sent. Check local permissions and refresh status."
        except Exception:
            message = "Could not verify the result. Refresh status before another request."
        self._status.set_label(message)
        self._render()
        return False

    def _refresh_clicked(self, *_args):
        if not self._current() or self._refresh_future is not None:
            return
        self._refresh_future = self._client.refresh_alarm(self._identity[0])
        self._refresh_future.add_done_callback(
            lambda result: GLib.idle_add(self._refreshed, result)
        )
        self._status.set_label("Refreshing reported keypad status…")
        self._render()

    def _refreshed(self, future):
        if not self._current():
            return False
        self._refresh_future = None
        try:
            result = future.result()
            message = (
                "Status refreshed."
                if result.status == "refreshed"
                else "Status refresh incomplete."
            )
        except Exception:
            message = "Could not refresh keypad status."
        self._status.set_label(message)
        self._render()
        return False

    def _snapshot_changed(self, _snapshot):
        self._render()

    def _tick(self):
        if self._closed:
            return False
        self._render()
        return not self._closed

    def _on_closed(self, *_args):
        if self._closed:
            return
        self._closed = True
        if self._intent is not None:
            self._intent.cancel()
        if self._refresh_future is not None:
            self._refresh_future.cancel()
        self._client.remove_alarm_callback(self._snapshot_changed)
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
