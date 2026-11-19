"""Native local notification rules, with explicit save and failure feedback."""

from __future__ import annotations

import copy
import time

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk import config, device_names, notifications  # noqa: E402
from halo_gtk import notification_rules as rules  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402


def available_devices(client) -> list[tuple[str, str, str]]:
    """Use already cached observations; opening preferences starts no requests."""
    if client is None or not client.is_authenticated:
        return []
    result = {}
    for device in client.all_devices:
        if getattr(device, "family", None) in {"doorbots", "stickup_cams", "authorized_doorbots"}:
            key = rules.device_key("camera", getattr(device, "id", None))
            if key:
                result[key] = (key, device_names.display_name(device), "camera")
    for device in client.get_intercom_snapshot().devices:
        key = rules.device_key("intercom", device.api_id)
        if key:
            result[key] = (key, device.name, "intercom")
    for location in client.get_alarm_snapshot().locations:
        key = rules.device_key("alarm", location.location_id)
        if key:
            result[key] = (key, location.name or "Alarm location", "alarm")
        for device in location.devices:
            key = rules.alarm_device_key(location.location_id, device.asset_id, device.zid)
            if key:
                result[key] = (key, f"{location.name} · {device.name}", "sensor")
    return sorted(result.values(), key=lambda row: row[1].casefold())[: rules.MAX_DEVICE_RULES]


class NotificationPreferences(Adw.PreferencesDialog):
    def __init__(self):
        super().__init__(title="Notification Rules", content_width=560, content_height=650)
        self._original = rules.normalize(config.load().get("notification_rules"))
        self._devices = []
        self._reset_devices = False
        page = Adw.PreferencesPage()
        self.add(page)
        schedule = Adw.PreferencesGroup(
            title="Quiet hours",
            description="Uses this computer's local time. Snooze and quiet hours pause routine "
            "Halo notifications; enabled critical Alarm alerts still come through. "
            "Ring's own alerts and device settings are unchanged.",
        )
        page.add(schedule)
        self._quiet = Adw.SwitchRow(
            title="Enable quiet hours", active=self._original["quiet_enabled"]
        )
        schedule.add(self._quiet)
        self._start = Adw.EntryRow(title="Start (HH:MM)")
        self._end = Adw.EntryRow(title="End (HH:MM)")
        for widget, key in ((self._start, "quiet_start"), (self._end, "quiet_end")):
            widget.set_text(rules.format_time(self._original[key]))
            widget.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
            schedule.add(widget)
        snooze = Adw.PreferencesGroup(title="Pause routine notifications")
        page.add(snooze)
        self._snooze = self._snooze_row(snooze, self._original.get("snoozed_until", 0))
        devices = Adw.PreferencesGroup(title="Device rules", description="Applies only to Halo.")
        page.add(devices)
        for key, name, family in available_devices(get_client()):
            current = self._original["devices"].get(key, {})
            row = Adw.ExpanderRow(title=name[:256], use_markup=False)
            row.set_subtitle("Alarm location" if family == "alarm" else family.capitalize())
            devices.add(row)
            enabled = Adw.SwitchRow(
                title="Routine notifications", active=current.get("enabled", True)
            )
            row.add_row(enabled)
            motion = ding = None
            if family == "camera":
                motion = Adw.SwitchRow(title="Motion", active=current.get("motion", True))
                ding = Adw.SwitchRow(title="Doorbell rings", active=current.get("ding", True))
                row.add_row(motion)
                row.add_row(ding)
            device_snooze = self._snooze_row(row, current.get("snoozed_until", 0))
            self._devices.append((key, enabled, motion, ding, device_snooze))
        if not self._devices:
            devices.add(
                Adw.ActionRow(
                    title="No devices available", subtitle="Sign in and refresh Devices first."
                )
            )
        actions = Adw.PreferencesGroup()
        page.add(actions)
        self._message = Gtk.Label(wrap=True, xalign=0, visible=False)
        actions.add(self._message)
        reset = Gtk.Button(label="Reset device rules")
        reset.connect("clicked", self._reset_device_rules)
        actions.add(reset)
        save = Gtk.Button(label="Save notification rules", css_classes=["suggested-action"])
        save.connect("clicked", self._save)
        actions.add(save)

    def _reset_device_rules(self, *_):
        self._reset_devices = True
        for _key, enabled, motion, ding, snooze in self._devices:
            enabled.set_active(True)
            if motion is not None:
                motion.set_active(True)
                ding.set_active(True)
            snooze.set_selected(1)
        self._message.set_label("Device rules will reset when you save.")
        self._message.set_visible(True)

    @staticmethod
    def _snooze_row(parent, expires):
        row = Adw.ComboRow(
            title="Snooze",
            model=Gtk.StringList.new(
                ["Keep current", "Resume now", "15 minutes", "30 minutes", "1 hour", "2 hours"]
            ),
        )
        if time.time() < expires <= time.time() + 7200:
            row.set_subtitle("Paused until " + time.strftime("%H:%M", time.localtime(expires)))
        else:
            row.set_subtitle("Not snoozed")
        if isinstance(parent, Adw.ExpanderRow):
            parent.add_row(row)
        else:
            parent.add(row)
        return row

    @staticmethod
    def _apply_snooze(value, widget):
        index = widget.get_selected()
        if 1 <= index <= len(rules.SNOOZE_MINUTES):
            value["snoozed_until"] = rules.snooze_until(rules.SNOOZE_MINUTES[index - 1])

    def _save(self, *_):
        try:
            start, end = (
                rules.parse_time(self._start.get_text()),
                rules.parse_time(self._end.get_text()),
            )
            if self._quiet.get_active() and start == end:
                raise ValueError("Choose different start and end times for quiet hours")
            cfg = config.load()
            current = rules.normalize(cfg.get("notification_rules"))
            if current != self._original:
                raise ValueError("Notification rules changed. Reopen this dialog before saving.")
            value = copy.deepcopy(current)
            if self._reset_devices:
                value["devices"] = {}
            value.update(quiet_enabled=self._quiet.get_active(), quiet_start=start, quiet_end=end)
            self._apply_snooze(value, self._snooze)
            for key, enabled, motion, ding, snooze in self._devices:
                rule = value["devices"].setdefault(key, {})
                rule["enabled"] = enabled.get_active()
                if motion is not None:
                    rule["motion"], rule["ding"] = motion.get_active(), ding.get_active()
                self._apply_snooze(rule, snooze)
            if len(value["devices"]) > rules.MAX_DEVICE_RULES:
                raise ValueError("Too many saved device rules. Reset unused rules before saving.")
            cfg["notification_rules"] = value
            config.save(cfg)
        except ValueError as error:
            self._message.set_label(str(error))
        except OSError:
            self._message.set_label(
                "Could not save notification rules. Check available space and permissions."
            )
        else:
            notifications.apply_camera_notification_preferences(cfg)
            self.close()
            return
        self._message.set_visible(True)
