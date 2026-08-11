"""Ring Alarm status and conservative mode controls."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk.alarm import (  # noqa: E402
    AlarmAccountSnapshot,
    AlarmCommandResult,
    AlarmCommandStatus,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmLockState,
    AlarmMode,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmSignal,
    TriState,
    empty_alarm_snapshot,
)
from halo_gtk.ring_client import get_client  # noqa: E402

_WORKING_STATUSES = frozenset(
    {
        AlarmServiceStatus.DISCOVERING,
        AlarmServiceStatus.CONNECTING,
        AlarmServiceStatus.SYNCING,
        AlarmServiceStatus.RECONNECTING,
    }
)

_ENTRY_KINDS = frozenset(
    {
        AlarmDeviceKind.CONTACT_SENSOR,
        AlarmDeviceKind.GLASS_BREAK_SENSOR,
        AlarmDeviceKind.TILT_SENSOR,
        AlarmDeviceKind.RETROFIT_ZONE,
    }
)
_MOTION_KINDS = frozenset({AlarmDeviceKind.MOTION_SENSOR})
_SAFETY_KINDS = frozenset(
    {
        AlarmDeviceKind.FLOOD_FREEZE_SENSOR,
        AlarmDeviceKind.FLOOD_SENSOR,
        AlarmDeviceKind.FREEZE_SENSOR,
        AlarmDeviceKind.SMOKE_ALARM,
        AlarmDeviceKind.CO_ALARM,
        AlarmDeviceKind.SMOKE_CO_ALARM,
        AlarmDeviceKind.SMOKE_CO_LISTENER,
        AlarmDeviceKind.TEMPERATURE_SENSOR,
        AlarmDeviceKind.PANIC_BUTTON,
    }
)
_CONNECTIVITY_ONLY_KINDS = frozenset(
    {
        AlarmDeviceKind.BASE_STATION,
        AlarmDeviceKind.KEYPAD,
        AlarmDeviceKind.RANGE_EXTENDER,
        AlarmDeviceKind.RETROFIT_BRIDGE,
        AlarmDeviceKind.HUB,
    }
)

_KIND_LABELS = {
    AlarmDeviceKind.BASE_STATION: "Base station",
    AlarmDeviceKind.SECURITY_PANEL: "Security panel",
    AlarmDeviceKind.KEYPAD: "Keypad",
    AlarmDeviceKind.CONTACT_SENSOR: "Contact sensor",
    AlarmDeviceKind.MOTION_SENSOR: "Motion sensor",
    AlarmDeviceKind.GLASS_BREAK_SENSOR: "Glass break sensor",
    AlarmDeviceKind.TILT_SENSOR: "Tilt sensor",
    AlarmDeviceKind.FLOOD_FREEZE_SENSOR: "Flood and freeze sensor",
    AlarmDeviceKind.FLOOD_SENSOR: "Flood sensor",
    AlarmDeviceKind.FREEZE_SENSOR: "Freeze sensor",
    AlarmDeviceKind.SMOKE_ALARM: "Smoke alarm",
    AlarmDeviceKind.CO_ALARM: "Carbon monoxide alarm",
    AlarmDeviceKind.SMOKE_CO_ALARM: "Smoke and carbon monoxide alarm",
    AlarmDeviceKind.SMOKE_CO_LISTENER: "Smoke and carbon monoxide listener",
    AlarmDeviceKind.TEMPERATURE_SENSOR: "Temperature sensor",
    AlarmDeviceKind.RANGE_EXTENDER: "Range extender",
    AlarmDeviceKind.RETROFIT_BRIDGE: "Retrofit bridge",
    AlarmDeviceKind.RETROFIT_ZONE: "Retrofit zone",
    AlarmDeviceKind.PANIC_BUTTON: "Panic button",
    AlarmDeviceKind.LOCK: "Lock",
    AlarmDeviceKind.SWITCH: "Switch",
    AlarmDeviceKind.VALVE: "Valve",
    AlarmDeviceKind.HUB: "Hub",
    AlarmDeviceKind.GENERIC_SENSOR: "Sensor",
    AlarmDeviceKind.UNKNOWN: "Alarm device",
}

_KIND_ICONS = {
    AlarmDeviceKind.CONTACT_SENSOR: "changes-prevent-symbolic",
    AlarmDeviceKind.RETROFIT_ZONE: "changes-prevent-symbolic",
    AlarmDeviceKind.TILT_SENSOR: "changes-prevent-symbolic",
    AlarmDeviceKind.MOTION_SENSOR: "system-run-symbolic",
    AlarmDeviceKind.FLOOD_FREEZE_SENSOR: "weather-showers-symbolic",
    AlarmDeviceKind.FLOOD_SENSOR: "weather-showers-symbolic",
    AlarmDeviceKind.FREEZE_SENSOR: "weather-snow-symbolic",
    AlarmDeviceKind.SMOKE_ALARM: "weather-fog-symbolic",
    AlarmDeviceKind.CO_ALARM: "weather-fog-symbolic",
    AlarmDeviceKind.SMOKE_CO_ALARM: "weather-fog-symbolic",
    AlarmDeviceKind.SMOKE_CO_LISTENER: "weather-fog-symbolic",
    AlarmDeviceKind.LOCK: "system-lock-screen-symbolic",
    AlarmDeviceKind.SECURITY_PANEL: "security-high-symbolic",
    AlarmDeviceKind.BASE_STATION: "preferences-system-devices-symbolic",
    AlarmDeviceKind.HUB: "preferences-system-devices-symbolic",
}

_UNAVAILABLE_REASONS = {
    "service-stopped": "Alarm control is not connected.",
    "service-failed": "The Alarm service is unavailable.",
    "location-unavailable": "This Alarm location is unavailable.",
    "connection-unavailable": "The Alarm connection is unavailable.",
    "multiple-panels": "Halo cannot safely identify one security panel.",
    "security-panel-unavailable": "The security panel is unavailable.",
    "panel-unavailable": "The security panel is unavailable.",
    "authorization-denied": "This Ring account cannot control this Alarm location.",
    "authorization-unknown": "Alarm control has not been authorized for this account.",
    "state-stale": "Alarm status is stale. Wait for it to reconnect.",
    "panel-stale": "Security panel status is stale. Wait for it to reconnect.",
    "cellular-backup": "Mode changes are unavailable while Alarm uses cellular backup.",
    "panel-not-online": "The security panel is not online.",
    "panel-offline": "The security panel is offline.",
    "panel-inventory-incomplete": "Halo is still loading the security panel.",
    "life-safety-active": "A life-safety sensor is active. Use Ring for critical response.",
    "active-alarm": "Mode changes are unavailable while an alarm is active.",
    "alarm-active": "Mode changes are unavailable while an alarm is active.",
    "mode-unknown": "The current Alarm mode is unknown.",
    "panel-state-unknown": "The security panel state is unknown.",
    "faulted-device-list-invalid": "Ring reported an invalid faulted-sensor list.",
    "faulted-device-unresolved": "Halo could not safely identify every faulted sensor.",
    "transition-in-progress": "Wait for the current Alarm transition to finish.",
}


def _mode_label(mode: AlarmMode) -> str:
    return {
        AlarmMode.DISARMED: "Disarmed",
        AlarmMode.HOME: "Home",
        AlarmMode.AWAY: "Away",
        AlarmMode.UNKNOWN: "Unknown",
    }[mode]


def _signal_label(signal: AlarmSignal) -> str:
    return {
        AlarmSignal.NONE: "Alarm",
        AlarmSignal.BURGLAR: "Burglar alarm",
        AlarmSignal.FIRE: "Fire alarm",
        AlarmSignal.CARBON_MONOXIDE: "Carbon monoxide alarm",
        AlarmSignal.FIRE_OR_CARBON_MONOXIDE: "Fire or carbon monoxide alarm",
        AlarmSignal.PANIC: "Panic alarm",
        AlarmSignal.UNKNOWN: "Alarm",
    }[signal]


def _location_has_active_signal(location: AlarmLocationSnapshot) -> bool:
    return location.signal in {
        AlarmSignal.BURGLAR,
        AlarmSignal.FIRE,
        AlarmSignal.CARBON_MONOXIDE,
        AlarmSignal.FIRE_OR_CARBON_MONOXIDE,
        AlarmSignal.PANIC,
    }


def _location_state_title(location: AlarmLocationSnapshot) -> str:
    if (
        location.triggered is TriState.ACTIVE
        or location.phase is AlarmPhase.ALARMING
        or _location_has_active_signal(location)
    ):
        return f"{_signal_label(location.signal)} active"
    if location.phase is AlarmPhase.ENTRY_DELAY:
        return "Entry delay"
    if location.phase is AlarmPhase.EXIT_DELAY:
        target = _mode_label(location.mode)
        return "Arming" if location.mode is AlarmMode.UNKNOWN else f"Arming {target}"
    if location.phase is AlarmPhase.UNKNOWN or location.mode is AlarmMode.UNKNOWN:
        return "Alarm status unknown"
    return _mode_label(location.mode)


def _connection_label(connection: AlarmConnectionStatus) -> str:
    return {
        AlarmConnectionStatus.ONLINE: "Online",
        AlarmConnectionStatus.CELLULAR_BACKUP: "Cellular backup",
        AlarmConnectionStatus.OFFLINE: "Offline",
        AlarmConnectionStatus.UNKNOWN: "Connection unknown",
    }[connection]


def _inventory_label(inventory: AlarmInventoryStatus) -> str:
    return {
        AlarmInventoryStatus.COMPLETE: "Devices loaded",
        AlarmInventoryStatus.PARTIAL: "Some devices unavailable",
        AlarmInventoryStatus.STALE: "Device status stale",
        AlarmInventoryStatus.EMPTY: "No device status",
    }[inventory]


def _device_group(device: AlarmDeviceSnapshot) -> str:
    if device.kind in _ENTRY_KINDS:
        return "Entry Sensors"
    if device.kind in _MOTION_KINDS:
        return "Motion Sensors"
    if device.kind in _SAFETY_KINDS:
        return "Safety Sensors"
    return "System Devices"


def _device_kind_label(device: AlarmDeviceSnapshot) -> str:
    return _KIND_LABELS.get(device.kind, "Alarm device")


def _active(value: TriState) -> bool:
    return value is TriState.ACTIVE


def _inactive(value: TriState) -> bool:
    return value is TriState.INACTIVE


def _device_state(device: AlarmDeviceSnapshot) -> tuple[str, str]:
    """Return a truthful primary state label and semantic severity."""
    label = "Status unknown"
    severity = "neutral"

    if _active(device.smoke):
        label, severity = "Smoke detected", "error"
    elif _active(device.carbon_monoxide):
        label, severity = "Carbon monoxide detected", "error"
    elif _active(device.flood):
        label, severity = "Water detected", "error"
    elif _active(device.freeze):
        label, severity = "Freeze detected", "error"
    elif _active(device.contact):
        label, severity = "Open", "warning"
    elif _active(device.tilt):
        label, severity = "Open or tilted", "warning"
    elif _active(device.motion):
        label, severity = "Motion detected", "warning"
    elif _active(device.glass_break):
        label, severity = "Glass break detected", "error"
    elif _active(device.tamper):
        label, severity = "Tampered", "warning"
    elif _active(device.faulted):
        label, severity = "Faulted", "warning"
    elif _active(device.bypassed):
        label, severity = "Bypassed", "warning"
    elif _active(device.battery_low):
        label, severity = "Low battery", "warning"
    elif _inactive(device.ac_power):
        label, severity = "AC power lost", "warning"
    elif _inactive(device.contact) or _inactive(device.tilt):
        label, severity = "Closed", "success"
    elif (
        _inactive(device.motion)
        or _inactive(device.glass_break)
        or (device.kind is AlarmDeviceKind.SMOKE_ALARM and _inactive(device.smoke))
        or (device.kind is AlarmDeviceKind.CO_ALARM and _inactive(device.carbon_monoxide))
        or (
            device.kind
            in {
                AlarmDeviceKind.SMOKE_CO_ALARM,
                AlarmDeviceKind.SMOKE_CO_LISTENER,
            }
            and _inactive(device.smoke)
            and _inactive(device.carbon_monoxide)
        )
    ):
        label, severity = "Clear", "success"
    elif device.kind is AlarmDeviceKind.FLOOD_SENSOR and _inactive(device.flood):
        label, severity = "Dry", "success"
    elif (device.kind is AlarmDeviceKind.FREEZE_SENSOR and _inactive(device.freeze)) or (
        device.kind is AlarmDeviceKind.FLOOD_FREEZE_SENSOR
        and _inactive(device.flood)
        and _inactive(device.freeze)
    ):
        label, severity = "Normal", "success"
    elif device.temperature_celsius is not None:
        label = f"{device.temperature_celsius:.1f} °C"
    elif device.lock_state is AlarmLockState.LOCKED:
        label, severity = "Locked", "success"
    elif device.lock_state is AlarmLockState.UNLOCKED:
        label, severity = "Unlocked", "warning"
    elif device.lock_state is AlarmLockState.JAMMED:
        label, severity = "Jammed", "error"
    elif _active(device.switch_on):
        label = "On"
    elif _inactive(device.switch_on):
        label = "Off"
    elif _active(device.valve_open):
        label, severity = "Open", "warning"
    elif _inactive(device.valve_open):
        label, severity = "Closed", "success"
    elif device.mode is not AlarmMode.UNKNOWN:
        label = _mode_label(device.mode)
    elif (
        device.kind in _CONNECTIVITY_ONLY_KINDS
        and device.connection is AlarmConnectionStatus.ONLINE
    ):
        label, severity = "Online", "success"
    elif device.connection is AlarmConnectionStatus.CELLULAR_BACKUP:
        label, severity = "Cellular backup", "warning"
    elif device.connection is AlarmConnectionStatus.OFFLINE:
        label, severity = "Offline", "warning"

    current = device.connection is AlarmConnectionStatus.ONLINE and not device.stale
    if not current and label not in {"Offline", "Connection unknown", "Status unknown"}:
        label = f"Last reported: {label}"
        severity = "warning"
    return label, severity


def _device_subtitle(device: AlarmDeviceSnapshot) -> str:
    details = [_device_kind_label(device)]
    if device.room and device.room.strip():
        details.append(device.room.strip())
    if device.temperature_celsius is not None:
        details.append(f"{device.temperature_celsius:.1f} °C")
    if device.battery_level is not None:
        details.append(f"Battery {device.battery_level}%")
    if _active(device.battery_low):
        details.append("Low battery")
    if _active(device.tamper):
        details.append("Tampered")
    if _active(device.faulted):
        details.append("Faulted")
    if _active(device.bypassed):
        details.append("Bypassed")
    if _inactive(device.ac_power):
        details.append("AC power lost")
    if device.stale:
        details.append("Stale")
    if device.connection is not AlarmConnectionStatus.ONLINE:
        details.append(_connection_label(device.connection))
    return " · ".join(dict.fromkeys(details))


def _device_needs_attention(device: AlarmDeviceSnapshot) -> bool:
    state, severity = _device_state(device)
    warning_state = any(
        _active(value)
        for value in (
            device.battery_low,
            device.tamper,
            device.bypassed,
            device.faulted,
        )
    )
    return (
        severity in {"warning", "error"}
        or state == "Status unknown"
        or warning_state
        or _inactive(device.ac_power)
        or device.stale
        or device.connection is not AlarmConnectionStatus.ONLINE
    )


class AlarmPage(Gtk.Box):
    """Ring Alarm state, sensor inventory, and revision-checked mode controls."""

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        self._alarm_client = None
        self._alarm_callback = self._on_alarm_snapshot
        self._snapshot = empty_alarm_snapshot()
        self._selected_location_id: str | None = None
        self._location_ids: tuple[str, ...] = ()
        self._updating_location_selector = False
        self._command_nonce = 0
        self._command_pending = False
        self._pending_location_id: str | None = None
        self._countdown_source_id: int | None = None
        self._closed = False
        self._build_ui()

    def _build_ui(self) -> None:
        self._toast_overlay = Adw.ToastOverlay()
        self.append(self._toast_overlay)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._toast_overlay.set_child(root)

        self._service_banner = Adw.Banner()
        self._service_banner.set_revealed(False)
        root.append(self._service_banner)

        self._page_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            hexpand=True,
            vexpand=True,
        )
        root.append(self._page_stack)

        self._status_page = Adw.StatusPage(
            icon_name="security-high-symbolic",
            title="Ring Alarm",
            description="Alarm status is starting.",
            hexpand=True,
            vexpand=True,
        )
        self._status_spinner = Gtk.Spinner(halign=Gtk.Align.CENTER)
        self._page_stack.add_named(self._status_page, "status")

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        self._page_stack.add_named(scroll, "content")

        clamp = Adw.Clamp(maximum_size=1080, tightening_threshold=720)
        scroll.set_child(clamp)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        content.add_css_class("page-scroll-content")
        clamp.set_child(content)

        location_row = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        location_label = Gtk.Label(label="Location", xalign=0)
        location_label.add_css_class("title-2")
        location_row.append(location_label)
        self._location_model = Gtk.StringList()
        self._location_selector = Gtk.DropDown(model=self._location_model)
        self._location_selector.set_hexpand(True)
        self._location_selector.set_tooltip_text("Choose Alarm location")
        self._location_selector.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Alarm location"],
        )
        self._location_selector.connect("notify::selected", self._on_location_selected)
        location_row.append(self._location_selector)
        content.append(location_row)

        overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        overview.add_css_class("alarm-overview-content")
        overview_frame = Gtk.Frame(child=overview)
        overview_frame.add_css_class("alarm-overview")
        content.append(overview_frame)

        state_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=16,
            valign=Gtk.Align.CENTER,
        )
        self._state_symbol = Gtk.Image.new_from_icon_name("security-low-symbolic")
        self._state_symbol.set_pixel_size(32)
        self._state_symbol.add_css_class("alarm-state-symbol")
        state_row.append(self._state_symbol)
        state_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        self._state_title = Gtk.Label(label="Alarm status unknown", xalign=0, wrap=True)
        self._state_title.add_css_class("title-2")
        state_copy.append(self._state_title)
        self._state_detail = Gtk.Label(label="", xalign=0, wrap=True)
        self._state_detail.add_css_class("dim-label")
        state_copy.append(self._state_detail)
        self._transition_label = Gtk.Label(label="", xalign=0, visible=False)
        self._transition_label.add_css_class("numeric")
        state_copy.append(self._transition_label)
        state_row.append(state_copy)
        overview.append(state_row)

        self._mode_buttons_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            homogeneous=False,
        )
        self._mode_buttons_box.add_css_class("linked")
        self._mode_buttons: dict[AlarmMode, Gtk.Button] = {}
        for mode in (AlarmMode.DISARMED, AlarmMode.HOME, AlarmMode.AWAY):
            button = Gtk.Button(label=_mode_label(mode), hexpand=True)
            button.add_css_class("command-button")
            button.set_tooltip_text(f"Set Alarm to {_mode_label(mode)}")
            button.connect("clicked", self._on_mode_clicked, mode)
            self._mode_buttons_box.append(button)
            self._mode_buttons[mode] = button
        overview.append(self._mode_buttons_box)

        command_status = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            valign=Gtk.Align.CENTER,
        )
        self._command_spinner = Gtk.Spinner()
        command_status.append(self._command_spinner)
        self._command_status = Gtk.Label(label="", xalign=0, wrap=True, hexpand=True)
        self._command_status.add_css_class("dim-label")
        command_status.append(self._command_status)
        overview.append(command_status)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(separator)

        sensor_heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        sensor_title = Gtk.Label(label="Alarm Devices", xalign=0)
        sensor_title.add_css_class("title-2")
        sensor_heading.append(sensor_title)
        self._sensor_summary = Gtk.Label(label="", xalign=0, wrap=True)
        self._sensor_summary.add_css_class("dim-label")
        sensor_heading.append(self._sensor_summary)
        content.append(sensor_heading)

        self._sensor_groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        content.append(self._sensor_groups)

        safety_note = Gtk.Label(
            label=(
                "Halo is not an emergency-monitoring service. Use the Ring app or keypad "
                "for critical security response."
            ),
            xalign=0,
            wrap=True,
        )
        safety_note.add_css_class("caption")
        safety_note.add_css_class("dim-label")
        content.append(safety_note)

    def refresh(self) -> None:
        """Bind the active Ring client and render its latest immutable snapshot."""
        if self._closed:
            return
        client = get_client()
        if client is None or not client.is_authenticated:
            self._bind_alarm_client(None)
            self._snapshot = empty_alarm_snapshot()
            self._render_signed_out()
            return
        if client is not self._alarm_client:
            self._bind_alarm_client(client)
        self._accept_snapshot(client.get_alarm_snapshot())

    def _bind_alarm_client(self, client) -> None:
        if client is self._alarm_client:
            return
        previous = self._alarm_client
        self._alarm_client = None
        if previous is not None:
            previous.remove_alarm_callback(self._alarm_callback)
        self._invalidate_command_state()
        self._alarm_client = client
        if client is not None:
            # Register before reading the cache so an update cannot fall between them.
            client.add_alarm_callback(self._alarm_callback)

    def _disconnect_alarm_client(self) -> None:
        client = self._alarm_client
        self._alarm_client = None
        if client is not None:
            client.remove_alarm_callback(self._alarm_callback)
        self._invalidate_command_state()

    def _invalidate_command_state(self) -> None:
        self._command_nonce += 1
        self._command_pending = False
        self._pending_location_id = None
        if hasattr(self, "_command_spinner"):
            self._command_spinner.stop()

    def _on_alarm_snapshot(self, snapshot: AlarmAccountSnapshot) -> None:
        self._accept_snapshot(snapshot)

    def _accept_snapshot(self, snapshot: AlarmAccountSnapshot) -> None:
        client = self._alarm_client
        if client is None or self._closed:
            return
        latest = client.get_alarm_snapshot()
        if snapshot.generation != latest.generation or snapshot.revision < latest.revision:
            return
        current = self._snapshot
        if snapshot.generation == current.generation and snapshot.revision < current.revision:
            return
        self._snapshot = snapshot
        self._render_snapshot(snapshot)

    def _render_signed_out(self) -> None:
        self._stop_countdown()
        self._service_banner.set_revealed(False)
        self._set_status_page(
            "Sign in to use Ring Alarm",
            "Connect a Ring account to view Alarm locations and reported sensors.",
        )

    def _render_snapshot(self, snapshot: AlarmAccountSnapshot) -> None:
        if not snapshot.locations:
            title, description = self._empty_state_copy(snapshot.status)
            self._service_banner.set_revealed(False)
            self._set_status_page(
                title,
                description,
                working=snapshot.status in _WORKING_STATUSES,
            )
            return

        self._page_stack.set_visible_child_name("content")
        self._update_service_banner(snapshot.status)
        self._rebuild_location_selector(snapshot.locations)
        location = self._selected_location()
        if location is not None:
            self._render_location(location)

    def _empty_state_copy(self, status: AlarmServiceStatus) -> tuple[str, str]:
        return {
            AlarmServiceStatus.STOPPED: (
                "Ring Alarm is starting",
                "Halo is waiting for the Alarm service to start.",
            ),
            AlarmServiceStatus.DISCOVERING: (
                "Looking for Ring Alarm",
                "Halo is checking this Ring account for Alarm locations.",
            ),
            AlarmServiceStatus.CONNECTING: (
                "Connecting to Ring Alarm",
                "Halo is opening the secure Alarm status connection.",
            ),
            AlarmServiceStatus.SYNCING: (
                "Loading Alarm devices",
                "Halo is receiving the security panel and sensor inventory.",
            ),
            AlarmServiceStatus.RECONNECTING: (
                "Reconnecting to Ring Alarm",
                "Last-known values are hidden until current status is available.",
            ),
            AlarmServiceStatus.OFFLINE: (
                "Ring Alarm is offline",
                "Halo cannot currently read this Alarm system.",
            ),
            AlarmServiceStatus.AUTHENTICATION_REQUIRED: (
                "Ring sign-in needs attention",
                "Sign in again before Halo can read Alarm status.",
            ),
            AlarmServiceStatus.ERROR: (
                "Ring Alarm is unavailable",
                "The Alarm connection failed. Camera and event features can continue to work.",
            ),
            AlarmServiceStatus.NO_SYSTEM: (
                "No Ring Alarm found",
                "This account did not report a Ring Alarm base station.",
            ),
            AlarmServiceStatus.DEGRADED: (
                "Ring Alarm data is incomplete",
                "Some Alarm locations or devices are unavailable.",
            ),
            AlarmServiceStatus.ONLINE: (
                "No Alarm locations reported",
                "Ring did not return an Alarm location for this account.",
            ),
        }[status]

    def _set_status_page(self, title: str, description: str, *, working: bool = False) -> None:
        self._stop_countdown()
        self._status_page.set_title(title)
        self._status_page.set_description(description)
        if working:
            self._status_spinner.start()
            self._status_page.set_child(self._status_spinner)
        else:
            self._status_spinner.stop()
            self._status_page.set_child(None)
        self._page_stack.set_visible_child_name("status")

    def _update_service_banner(self, status: AlarmServiceStatus) -> None:
        titles = {
            AlarmServiceStatus.DEGRADED: "Some Ring Alarm data is unavailable",
            AlarmServiceStatus.RECONNECTING: "Reconnecting to Ring Alarm",
            AlarmServiceStatus.OFFLINE: "Ring Alarm is offline",
            AlarmServiceStatus.AUTHENTICATION_REQUIRED: "Ring sign-in needs attention",
            AlarmServiceStatus.ERROR: "The Ring Alarm service is unavailable",
            AlarmServiceStatus.CONNECTING: "Connecting to Ring Alarm",
            AlarmServiceStatus.SYNCING: "Updating Ring Alarm devices",
            AlarmServiceStatus.DISCOVERING: "Looking for Ring Alarm locations",
            AlarmServiceStatus.STOPPED: "The Ring Alarm service is stopped",
        }
        title = titles.get(status)
        self._service_banner.set_revealed(title is not None)
        if title is not None:
            self._service_banner.set_title(title)

    def _rebuild_location_selector(
        self,
        locations: tuple[AlarmLocationSnapshot, ...],
    ) -> None:
        ids = tuple(location.location_id for location in locations)
        if self._selected_location_id not in ids:
            self._selected_location_id = ids[0]
        selected = ids.index(self._selected_location_id)
        names = []
        for index, location in enumerate(locations, start=1):
            names.append(location.name.strip() or f"Alarm location {index}")

        self._updating_location_selector = True
        try:
            self._location_model.splice(0, self._location_model.get_n_items(), names)
            self._location_ids = ids
            self._location_selector.set_selected(selected)
            self._location_selector.set_sensitive(len(locations) > 1)
        finally:
            self._updating_location_selector = False

    def _on_location_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_location_selector:
            return
        index = dropdown.get_selected()
        if index >= len(self._location_ids):
            return
        self._selected_location_id = self._location_ids[index]
        location = self._selected_location()
        if location is not None:
            self._render_location(location)

    def _selected_location(self) -> AlarmLocationSnapshot | None:
        if self._selected_location_id is None:
            return None
        return self._snapshot.find_location(self._selected_location_id)

    def _render_location(self, location: AlarmLocationSnapshot) -> None:
        self._state_title.set_label(_location_state_title(location))
        health = (
            f"{_connection_label(location.connection)} · {_inventory_label(location.inventory)}"
        )
        if (
            location.triggered is TriState.ACTIVE
            or location.phase is AlarmPhase.ALARMING
            or _location_has_active_signal(location)
        ):
            health += " · Use the Ring app or keypad for critical response"
        self._state_detail.set_label(health)
        self._set_state_symbol(location)
        self._update_countdown(location)
        self._refresh_mode_controls(location)
        self._rebuild_device_groups(location.devices)

    def _set_state_symbol(self, location: AlarmLocationSnapshot) -> None:
        critical = (
            location.triggered is TriState.ACTIVE
            or location.phase is AlarmPhase.ALARMING
            or _location_has_active_signal(location)
        )
        if critical:
            icon = "alarm-symbolic"
            semantic = "error"
        elif location.phase in {AlarmPhase.ENTRY_DELAY, AlarmPhase.EXIT_DELAY}:
            icon = "dialog-warning-symbolic"
            semantic = "warning"
        elif location.mode is AlarmMode.DISARMED:
            icon = "security-low-symbolic"
            semantic = "success"
        elif location.mode is AlarmMode.HOME:
            icon = "go-home-symbolic"
            semantic = "accent"
        elif location.mode is AlarmMode.AWAY:
            icon = "security-high-symbolic"
            semantic = "accent"
        else:
            icon = "dialog-question-symbolic"
            semantic = "warning"
        self._state_symbol.set_from_icon_name(icon)
        self._set_semantic_class(self._state_symbol, semantic)

    def _update_countdown(self, location: AlarmLocationSnapshot) -> None:
        self._stop_countdown()
        if location.phase not in {AlarmPhase.ENTRY_DELAY, AlarmPhase.EXIT_DELAY}:
            self._transition_label.set_visible(False)
            return
        self._transition_deadline = location.transition_deadline
        self._transition_label.set_visible(True)
        if self._update_countdown_label():
            self._countdown_source_id = GLib.timeout_add_seconds(1, self._on_countdown_tick)

    def _update_countdown_label(self) -> bool:
        deadline = getattr(self, "_transition_deadline", None)
        if deadline is None:
            self._transition_label.set_label("Transition in progress")
            return False
        remaining = max(0, math.ceil(deadline - time.time()))
        if remaining == 0:
            self._transition_label.set_label("Updating status…")
            return False
        self._transition_label.set_label(f"{remaining} seconds remaining")
        return True

    def _on_countdown_tick(self) -> bool:
        if self._update_countdown_label():
            return GLib.SOURCE_CONTINUE
        self._countdown_source_id = None
        return GLib.SOURCE_REMOVE

    def _stop_countdown(self) -> None:
        source_id = self._countdown_source_id
        self._countdown_source_id = None
        if source_id is not None:
            GLib.source_remove(source_id)

    def _refresh_mode_controls(self, location: AlarmLocationSnapshot) -> None:
        busy = self._command_pending
        selected_busy = busy and self._pending_location_id == location.location_id
        self._location_selector.set_sensitive(len(self._location_ids) > 1 and not busy)
        for mode, button in self._mode_buttons.items():
            available = location.can_set_mode and not busy and mode is not location.mode
            button.set_sensitive(available)
            button.remove_css_class("suggested-action")
            if mode is location.mode:
                button.add_css_class("suggested-action")
        if selected_busy:
            self._command_spinner.start()
            self._command_status.set_label("Waiting for Ring to confirm the mode change…")
        else:
            self._command_spinner.stop()
            if location.can_set_mode:
                self._command_status.set_label(
                    f"Current mode: {_mode_label(location.mode)}. Changes require confirmation."
                )
            else:
                self._command_status.set_label(
                    _UNAVAILABLE_REASONS.get(
                        location.command_unavailable_reason,
                        "Alarm mode control is unavailable until current status is confirmed.",
                    )
                )

    def _rebuild_device_groups(self, devices: tuple[AlarmDeviceSnapshot, ...]) -> None:
        while child := self._sensor_groups.get_first_child():
            self._sensor_groups.remove(child)

        ordered_groups = ("Entry Sensors", "Motion Sensors", "Safety Sensors", "System Devices")
        grouped = {name: [] for name in ordered_groups}
        for device in devices:
            grouped[_device_group(device)].append(device)
        attention = sum(_device_needs_attention(device) for device in devices)
        summary = f"{len(devices)} devices"
        if attention:
            summary += f" · {attention} need attention"
        self._sensor_summary.set_label(summary)

        if not devices:
            empty = Adw.StatusPage(
                icon_name="view-list-symbolic",
                title="No Alarm devices reported",
                description="Halo has not received a device inventory for this location.",
            )
            empty.set_vexpand(False)
            self._sensor_groups.append(empty)
            return

        for group_name in ordered_groups:
            group_devices = grouped[group_name]
            if not group_devices:
                continue
            group = Adw.PreferencesGroup(title=group_name)
            group_devices.sort(
                key=lambda device: (
                    not _device_needs_attention(device),
                    (device.name or _device_kind_label(device)).casefold(),
                )
            )
            for device in group_devices:
                group.add(self._build_device_row(device))
            self._sensor_groups.append(group)

    def _build_device_row(self, device: AlarmDeviceSnapshot) -> Adw.ActionRow:
        title = device.name.strip() or _device_kind_label(device)
        row = Adw.ActionRow(title=title, subtitle=_device_subtitle(device))
        icon = Gtk.Image.new_from_icon_name(
            _KIND_ICONS.get(device.kind, "dialog-question-symbolic")
        )
        icon.add_css_class("event-symbol")
        row.add_prefix(icon)
        state, severity = _device_state(device)
        state_label = Gtk.Label(label=state, xalign=1, wrap=True)
        state_label.set_max_width_chars(20)
        self._set_semantic_class(state_label, severity)
        row.add_suffix(state_label)
        return row

    @staticmethod
    def _set_semantic_class(widget: Gtk.Widget, semantic: str) -> None:
        for css_class in ("success", "warning", "error", "accent"):
            widget.remove_css_class(css_class)
        if semantic != "neutral":
            widget.add_css_class(semantic)

    def _on_mode_clicked(self, _button: Gtk.Button, target: AlarmMode) -> None:
        client = self._alarm_client
        location = self._selected_location()
        if (
            client is None
            or location is None
            or not location.can_set_mode
            or self._command_pending
            or target is location.mode
        ):
            return

        location_name = location.name.strip() or "this Alarm location"
        headings = {
            AlarmMode.DISARMED: f"Disarm {location_name}?",
            AlarmMode.HOME: f"Arm {location_name} in Home mode?",
            AlarmMode.AWAY: f"Arm {location_name} in Away mode?",
        }
        bodies = {
            AlarmMode.DISARMED: (
                "This turns off intrusion monitoring at this location. "
                "Confirm that you intend to disarm it."
            ),
            AlarmMode.HOME: (
                "Halo will request Home mode. Faulted sensors require a separate, "
                "explicit bypass confirmation."
            ),
            AlarmMode.AWAY: (
                "Halo will request Away mode. Faulted sensors require a separate, "
                "explicit bypass confirmation."
            ),
        }
        dialog = Adw.AlertDialog(heading=headings[target], body=bodies[target])
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("confirm", _mode_label(target))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        appearance = (
            Adw.ResponseAppearance.DESTRUCTIVE
            if target is AlarmMode.DISARMED
            else Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_response_appearance("confirm", appearance)
        dialog.connect(
            "response",
            self._on_mode_confirmed,
            client,
            location.location_id,
            location.revision,
            target,
        )
        dialog.present(self)

    def _on_mode_confirmed(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        client,
        location_id: str,
        revision: int,
        target: AlarmMode,
    ) -> None:
        if response != "confirm" or client is not self._alarm_client:
            return
        location = self._snapshot.find_location(location_id)
        if location is None or location.revision != revision or not location.can_set_mode:
            self._add_toast("Alarm status changed. Review it before trying again.")
            return
        self._submit_mode(client, location, target, ())

    def _submit_mode(
        self,
        client,
        location: AlarmLocationSnapshot,
        target: AlarmMode,
        bypass_ids: Iterable[str],
    ) -> None:
        if self._command_pending or client is not self._alarm_client:
            return
        bypass_ids = tuple(bypass_ids)
        self._command_nonce += 1
        nonce = self._command_nonce
        self._command_pending = True
        self._pending_location_id = location.location_id
        self._refresh_mode_controls(location)
        try:
            future = client.request_alarm_mode(
                location.location_id,
                target,
                expected_revision=location.revision,
                bypass_ids=bypass_ids,
            )
        except Exception:
            self._finish_command_exception(client, nonce)
            return
        future.add_done_callback(
            lambda completed: GLib.idle_add(
                self._finish_command_future,
                client,
                nonce,
                location.location_id,
                location.revision,
                target,
                bypass_ids,
                completed,
            )
        )

    def _finish_command_exception(self, client, nonce: int) -> None:
        if client is not self._alarm_client or nonce != self._command_nonce:
            return
        self._clear_command_pending()
        self._add_toast("Alarm control is unavailable. Review the current status and try again.")

    def _finish_command_future(
        self,
        client,
        nonce: int,
        location_id: str,
        revision: int,
        target: AlarmMode,
        submitted_bypass_ids: tuple[str, ...],
        future,
    ) -> bool:
        if self._closed or client is not self._alarm_client or nonce != self._command_nonce:
            return GLib.SOURCE_REMOVE
        self._clear_command_pending()
        try:
            result = future.result()
        except Exception:
            self._add_toast(
                "Halo could not read the command result. Check Alarm status before trying again."
            )
            self.refresh()
            return GLib.SOURCE_REMOVE

        if result.status is AlarmCommandStatus.CONFIRMED:
            self.refresh()
            self._add_toast(f"Ring confirmed {_mode_label(result.confirmed_mode)} mode.")
        elif result.status is AlarmCommandStatus.NEEDS_BYPASS:
            self._handle_bypass_required(
                client,
                result,
                revision,
                target,
                submitted_bypass_ids,
            )
        elif result.status is AlarmCommandStatus.PERMISSION_DENIED:
            self._add_toast("This Ring account is not permitted to control this Alarm location.")
            self.refresh()
        elif result.status is AlarmCommandStatus.STALE_REVISION:
            self._add_toast("Alarm status changed. Review it before trying again.")
            self.refresh()
        elif result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN:
            self.refresh()
            self._present_unknown_outcome()
        elif result.status is AlarmCommandStatus.SUPERSEDED:
            self.refresh()
        else:
            message = _UNAVAILABLE_REASONS.get(
                result.code,
                "Alarm mode control is unavailable. Review the current status.",
            )
            self._add_toast(message)
            self.refresh()
        return GLib.SOURCE_REMOVE

    def _clear_command_pending(self) -> None:
        self._command_pending = False
        self._pending_location_id = None
        self._command_spinner.stop()
        location = self._selected_location()
        if location is not None:
            self._refresh_mode_controls(location)

    def _handle_bypass_required(
        self,
        client,
        result: AlarmCommandResult,
        revision: int,
        target: AlarmMode,
        submitted_bypass_ids: tuple[str, ...],
    ) -> None:
        if submitted_bypass_ids or not result.bypass_ids or target is AlarmMode.DISARMED:
            self._add_toast("Faulted sensors changed. Review Alarm status and try again.")
            self.refresh()
            return
        location = self._snapshot.find_location(result.location_id)
        devices = self._resolve_bypass_devices(location, result.bypass_ids)
        if location is None or location.revision != revision or devices is None:
            self._add_toast("Alarm status changed. Review faulted sensors before trying again.")
            self.refresh()
            return
        self._command_pending = True
        self._pending_location_id = location.location_id
        self._refresh_mode_controls(location)
        self._present_bypass_dialog(client, location, target, result.bypass_ids, devices)

    @staticmethod
    def _resolve_bypass_devices(
        location: AlarmLocationSnapshot | None,
        bypass_ids: tuple[str, ...],
    ) -> tuple[AlarmDeviceSnapshot, ...] | None:
        if location is None:
            return None
        panel = location.security_panel
        if panel is None:
            return None
        asset = location.find_asset(panel.asset_id)
        if asset is None:
            return None
        resolved = tuple(asset.find_device(zid) for zid in bypass_ids)
        if any(device is None for device in resolved):
            return None
        return resolved  # type: ignore[return-value]

    def _present_bypass_dialog(
        self,
        client,
        location: AlarmLocationSnapshot,
        target: AlarmMode,
        bypass_ids: tuple[str, ...],
        devices: tuple[AlarmDeviceSnapshot, ...],
    ) -> None:
        dialog = Adw.AlertDialog(
            heading=(
                f"Bypass {len(devices)} faulted sensors at "
                f"{location.name.strip() or 'this location'}?"
            ),
            body=(
                f"Ring requires every listed sensor to be bypassed before "
                f"{_mode_label(target)} mode can be set."
            ),
        )
        device_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        device_list.add_css_class("boxed-list")
        for device in devices:
            name = device.name.strip() or _device_kind_label(device)
            row = Adw.ActionRow(title=name, subtitle=device.room or _device_kind_label(device))
            device_list.append(row)
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            min_content_height=min(260, 48 * len(devices)),
            max_content_height=260,
            propagate_natural_height=True,
        )
        scroller.set_child(device_list)
        dialog.set_extra_child(scroller)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("bypass", "Arm and Bypass")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("bypass", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect(
            "response",
            self._on_bypass_confirmed,
            client,
            location.location_id,
            location.revision,
            target,
            bypass_ids,
        )
        dialog.present(self)

    def _on_bypass_confirmed(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        client,
        location_id: str,
        revision: int,
        target: AlarmMode,
        bypass_ids: tuple[str, ...],
    ) -> None:
        if client is not self._alarm_client:
            return
        if response != "bypass":
            self._clear_command_pending()
            return
        location = self._snapshot.find_location(location_id)
        if (
            location is None
            or location.revision != revision
            or location.location_id != self._selected_location_id
            or not location.can_set_mode
        ):
            self._clear_command_pending()
            self._add_toast("Alarm status changed. Review faulted sensors before trying again.")
            return
        if self._resolve_bypass_devices(location, bypass_ids) is None:
            self._clear_command_pending()
            self._add_toast("Halo can no longer identify every faulted sensor.")
            return
        self._clear_command_pending()
        self._submit_mode(client, location, target, bypass_ids)

    def _present_unknown_outcome(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Alarm result could not be confirmed",
            body=(
                "The request may have reached Ring, but Halo did not receive confirmation. "
                "Check the Ring app or keypad and wait for current status before trying again."
            ),
        )
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)

    def _add_toast(self, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast.new(message))

    def close(self) -> None:
        """Release callbacks without cancelling possibly-sent Alarm commands."""
        if self._closed:
            return
        self._closed = True
        self._stop_countdown()
        self._disconnect_alarm_client()

    def do_unroot(self) -> None:
        self.close()
        Gtk.Box.do_unroot(self)
