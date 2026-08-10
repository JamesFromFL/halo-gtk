"""Application settings page."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from functools import partial
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from halo_gtk import (  # noqa: E402
    autostart,
    diagnostics,
    favorites,
    live_layouts,
    media_paths,
    privacy,
)
from halo_gtk import config as _config  # noqa: E402
from halo_gtk.ring_client import get_account_email, get_client, logout_client  # noqa: E402

_log = logging.getLogger(__name__)

_RING_ACCOUNT_URL = "https://account.ring.com/account/control-center/account-management"


class SettingsPage(Gtk.Box):
    """Two-pane settings page with categories on the left."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, vexpand=True)
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._refreshing = False
        self._account_email_cache: str | None = None
        self._account_email_loading = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        self._category_list = Gtk.ListBox(
            css_classes=["navigation-sidebar"],
            selection_mode=Gtk.SelectionMode.SINGLE,
            width_request=240,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self._category_list.connect("row-selected", self._on_category_selected)
        self.append(self._category_list)

        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.append(separator)

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            hexpand=True,
            vexpand=True,
        )
        self.append(self._stack)

        self._add_category("account", "Account", self._build_account_page())
        self._add_category("background", "Background Service", self._build_background_page())
        self._add_category("live-monitoring", "Live Monitoring", self._build_live_monitoring_page())
        self._add_category("playback", "Playback", self._build_playback_page())
        self._add_category("notifications", "Notifications", self._build_notifications_page())
        self._add_category("directories", "Directories", self._build_directories_page())
        self._add_category("privacy", "Privacy", self._build_privacy_page())
        self._add_category("advanced", "Advanced", self._build_advanced_page())
        self._category_list.select_row(self._rows["account"])

    def _add_category(self, name: str, label: str, page: Gtk.Widget) -> None:
        row = Gtk.ListBoxRow()
        row._settings_page_name = name  # type: ignore[attr-defined]
        row.set_child(
            Gtk.Label(
                label=label,
                halign=Gtk.Align.START,
                margin_top=10,
                margin_bottom=10,
                margin_start=12,
                margin_end=12,
            )
        )
        self._category_list.append(row)
        self._rows[name] = row
        self._stack.add_named(page, name)

    def _build_account_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Account",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        group = Adw.PreferencesGroup(title="Ring Account")
        page.add(group)

        self._login_status_row = Adw.ActionRow(title="Status")
        group.add(self._login_status_row)

        self._account_email_row = Adw.ActionRow(title="Email")
        group.add(self._account_email_row)

        self._refresh_session_button = self._add_button_row(
            group,
            "Refresh Session",
            "Refreshes the saved Ring session. You may be asked to sign in again if"
            " Ring rejects it.",
            "Refresh",
            self._on_refresh_session_clicked,
        )
        self._add_button_row(
            group,
            "Account Control Center",
            "Open Ring Account Control Center in your browser.",
            "Open",
            self._on_open_ring_account_clicked,
        )
        self._logout_button = self._add_button_row(
            group,
            "Log Out",
            "Remove the saved Ring session from this computer.",
            "Log Out",
            self._on_logout_clicked,
            destructive=True,
        )

        self._account_message_row = Adw.ActionRow(title="Last Action", visible=False)
        group.add(self._account_message_row)

        return scrolled

    def _build_playback_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Playback",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        history_group = Adw.PreferencesGroup(title="Event History")
        page.add(history_group)
        self._event_history_next_auto_play_switch = self._add_switch_row(
            history_group,
            "Next Auto Play",
            "Automatically play the next visible event when the current recording ends.",
            config_key="event_history_next_auto_play",
        )

        return scrolled

    def _build_directories_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Directories",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        locations_group = Adw.PreferencesGroup(title="Save Locations")
        page.add(locations_group)

        self._snapshot_dir_row = self._add_folder_row(
            locations_group,
            "Snapshots Folder",
            "snapshot_dir",
        )
        self._video_dir_row = self._add_folder_row(
            locations_group,
            "Videos Folder",
            "video_dir",
        )
        self._add_button_row(
            locations_group,
            "Favorites Folder",
            "Open locally archived favorite event clips.",
            "Open",
            self._on_open_favorites_folder_clicked,
        )

        options_group = Adw.PreferencesGroup(title="Options")
        page.add(options_group)
        self._camera_subfolders_switch = self._add_switch_row(
            options_group,
            "Create Camera Subfolders",
            "Save media into per-camera folders.",
            self._on_camera_subfolders_toggled,
        )
        self._open_folder_after_save_switch = self._add_switch_row(
            options_group,
            "Open Folder After Saving",
            "Open the destination folder after a snapshot or video is saved.",
            self._on_open_folder_after_save_toggled,
        )
        self._add_button_row(
            options_group,
            "Reset to Defaults",
            "Restore Halo's default snapshot and video folders.",
            "Reset",
            self._on_reset_directories_clicked,
        )

        return scrolled

    def _build_background_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Background Service",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        group = Adw.PreferencesGroup()
        page.add(group)

        self._background_switch = self._add_switch_row(
            group,
            "Enable Background Service",
            "Halo is active in the background when the window is closed.",
            self._on_background_toggled,
        )
        self._autostart_switch = self._add_switch_row(
            group,
            "Autostart on Login",
            "Halo is launched at user startup.",
            self._on_autostart_toggled,
        )
        self._autostart_background_row, self._autostart_background_switch = self._add_switch_row(
            group,
            "Autostart in Background",
            "Halo is launched in the background at user startup.",
            self._on_autostart_background_toggled,
            indent=True,
        )
        self._tray_switch = self._add_switch_row(
            group,
            "Show Tray icon",
            None,
            self._on_tray_toggled,
        )

        return scrolled

    def _build_live_monitoring_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Live Monitoring",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        behavior_group = Adw.PreferencesGroup(title="Behavior")
        page.add(behavior_group)
        self._live_monitoring_autostart_switch = self._add_switch_row(
            behavior_group,
            "Start camera live stream when Live Monitoring opened",
            "Start all visible cameras when the Live Monitoring page opens.",
            config_key="live_monitoring_autostart",
        )
        self._live_monitoring_continue_switch = self._add_switch_row(
            behavior_group,
            "Continue Live Monitoring on page exit",
            "Keep visible camera streams alive when navigating to another page.",
            config_key="live_monitoring_continue_on_page_exit",
        )
        self._live_monitoring_keep_focus_switch = self._add_switch_row(
            behavior_group,
            "Keep all Live Monitoring alive when in camera focus",
            "Keep grid streams alive when opening one camera in focused view.",
            config_key="live_monitoring_keep_streams_in_focus",
        )
        self._live_monitoring_unmute_switch = self._add_switch_row(
            behavior_group,
            "Unmute on Live Monitoring start",
            "Start Live Monitoring streams at 100% volume instead of muted.",
            config_key="live_monitoring_unmute_on_start",
        )
        self._live_monitoring_allow_six_switch = self._add_switch_row(
            behavior_group,
            "Allow Experimental 6-Camera Monitoring",
            "Allows up to 6 simultaneous streams. Ring officially supports 4 on desktop;"
            " reliability may vary.",
            self._on_live_monitoring_allow_six_toggled,
        )

        layouts_group = Adw.PreferencesGroup(title="Layouts")
        page.add(layouts_group)
        self._add_button_row(
            layouts_group,
            "Delete All Saved Custom Layouts",
            "Remove all saved custom layouts from Live Monitoring.",
            "Delete",
            self._on_delete_live_monitoring_layouts_clicked,
            destructive=True,
        )
        self._live_monitoring_message_row = Adw.ActionRow(title="Last Action", visible=False)
        layouts_group.add(self._live_monitoring_message_row)

        return scrolled

    def _build_notifications_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Notifications",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        desktop_group = Adw.PreferencesGroup(title="Desktop Notifications")
        page.add(desktop_group)

        self._notifications_switch = self._add_switch_row(
            desktop_group,
            "Desktop Notifications",
            "Show Ring event notifications.",
            self._on_notifications_toggled,
        )
        self._doorbell_notifications_row, self._doorbell_notifications_switch = (
            self._add_switch_row(
                desktop_group,
                "Doorbell Ring Notification",
                None,
                config_key="notify_doorbell",
                indent=True,
            )
        )
        self._motion_notifications_row, self._motion_notifications_switch = self._add_switch_row(
            desktop_group,
            "Motion Notifications",
            None,
            config_key="notify_motion",
            indent=True,
        )
        self._alarm_notifications_row, self._alarm_notifications_switch = self._add_switch_row(
            desktop_group,
            "Alarm Notification",
            "Urgent alert. Do not rely on Halo for emergency monitoring.",
            config_key="notify_alarm",
            indent=True,
        )

        content_group = Adw.PreferencesGroup(title="Notification Content")
        page.add(content_group)
        self._notification_preview_switch = self._add_switch_row(
            content_group,
            "Notification Preview Images",
            "Include snapshots or preview images when available.",
            config_key="notification_preview_images",
        )
        self._notification_summaries_switch = self._add_switch_row(
            content_group,
            "Ring Notification Summaries",
            "Use Ring event descriptions when available.",
            config_key="notification_summaries",
        )
        self._custom_notification_messages_switch = self._add_switch_row(
            content_group,
            "Custom Notification Messages",
            "Use custom body text for supported notification types.",
            self._on_custom_notification_messages_toggled,
        )
        self._custom_doorbell_message_row = self._add_entry_row(
            content_group,
            "Doorbell Ring Press",
            "custom_doorbell_message",
        )
        self._custom_motion_message_row = self._add_entry_row(
            content_group,
            "Motion",
            "custom_motion_message",
        )
        self._custom_alarm_message_row = self._add_entry_row(
            content_group,
            "Alarm",
            "custom_alarm_message",
        )

        return scrolled

    def _build_privacy_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Privacy",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        local_group = Adw.PreferencesGroup(title="Local Data")
        page.add(local_group)

        self._add_button_row(
            local_group,
            "Clear Cached Thumbnails/Previews",
            "Delete local cached camera preview images and generated thumbnails.",
            "Clear",
            self._on_clear_preview_cache_clicked,
            destructive=True,
        )
        self._add_button_row(
            local_group,
            "Clear Debug Log",
            "Clear Halo debug logs stored on this computer.",
            "Clear",
            self._on_clear_debug_log_clicked,
            destructive=True,
        )

        self._privacy_message_row = Adw.ActionRow(title="Last Action", visible=False)
        local_group.add(self._privacy_message_row)

        return scrolled

    def _build_advanced_page(self) -> Gtk.Widget:
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        page = Adw.PreferencesPage(
            title="Advanced",
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        scrolled.set_child(page)

        diagnostics_group = Adw.PreferencesGroup(title="Diagnostics")
        page.add(diagnostics_group)
        self._add_button_row(
            diagnostics_group,
            "Export Debug Logs",
            "Save Halo debug logs to a zip file for troubleshooting.",
            "Export",
            self._on_export_logs_clicked,
        )

        settings_group = Adw.PreferencesGroup(title="Settings")
        page.add(settings_group)
        self._add_button_row(
            settings_group,
            "Reset Settings",
            "Restore Halo settings to defaults without removing your Ring session.",
            "Reset",
            self._on_reset_settings_clicked,
            destructive=True,
        )

        self._advanced_message_row = Adw.ActionRow(title="Last Action", visible=False)
        settings_group.add(self._advanced_message_row)

        return scrolled

    def _add_switch_row(
        self,
        group,
        title: str,
        subtitle: str | None,
        callback=None,
        *,
        config_key: str | None = None,
        indent: bool = False,
    ):
        row = Adw.ActionRow(title=title, subtitle=subtitle or "")
        if indent:
            row.set_margin_start(24)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        group.add(row)
        # A pure on/off setting just needs its config key; richer toggles (master
        # switches, ones with side effects) still pass an explicit callback.
        if callback is None and config_key is not None:
            callback = partial(self._save_switch_setting, config_key=config_key)
        switch.connect("notify::active", callback)
        return (row, switch) if indent else switch

    def _add_folder_row(self, group, title: str, config_key: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        button = Gtk.Button(label="Choose", valign=Gtk.Align.CENTER)
        row.add_suffix(button)
        row.set_activatable_widget(button)
        group.add(row)
        button.connect("clicked", self._on_choose_folder_clicked, config_key)
        return row

    def _add_entry_row(self, group, title: str, config_key: str) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title)
        row.set_margin_start(24)
        group.add(row)
        row.connect("notify::text", self._on_custom_message_changed, config_key)
        return row

    def _add_button_row(
        self,
        group,
        title: str,
        subtitle: str,
        button_label: str,
        callback,
        *,
        destructive: bool = False,
    ) -> Gtk.Button:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        button = Gtk.Button(label=button_label, valign=Gtk.Align.CENTER)
        if destructive:
            button.add_css_class("destructive-action")
        row.add_suffix(button)
        row.set_activatable_widget(button)
        group.add(row)
        button.connect("clicked", callback)
        return button

    def refresh(self) -> None:
        self._refreshing = True
        cfg = _config.load()
        try:
            self._refresh_account()
            self._set_switch_active(self._background_switch, cfg.get("background_service", False))
            autostart_enabled = cfg.get("autostart_login", False) or autostart.is_enabled()
            self._set_switch_active(
                self._autostart_switch,
                autostart_enabled,
            )
            self._autostart_background_row.set_visible(autostart_enabled)
            self._set_switch_active(
                self._autostart_background_switch,
                cfg.get("autostart_background", False),
            )
            self._set_switch_active(self._tray_switch, cfg.get("show_tray_icon", True))
            self._refresh_live_monitoring(cfg)
            self._refresh_playback(cfg)
            self._refresh_directories(cfg)
            self._refresh_notifications(cfg)
        finally:
            self._refreshing = False

    def _set_switch_active(self, switch: Gtk.Switch, active: bool) -> None:
        switch.set_active(bool(active))

    def _refresh_account(self) -> None:
        client = get_client()
        logged_in = client is not None and client.is_authenticated
        self._login_status_row.set_subtitle("Logged in" if logged_in else "Not logged in")
        self._refresh_session_button.set_sensitive(logged_in)
        self._logout_button.set_sensitive(logged_in)

        if not logged_in:
            self._account_email_cache = None
            self._account_email_loading = False
            self._account_email_row.set_subtitle("Not logged in")
            return

        if self._account_email_cache is not None:
            self._account_email_row.set_subtitle(self._account_email_cache)
            return

        self._account_email_row.set_subtitle("Loading...")
        if not self._account_email_loading:
            self._account_email_loading = True
            threading.Thread(target=self._load_account_email, daemon=True).start()

    def _load_account_email(self) -> None:
        try:
            account_email = get_account_email()
        except Exception as exc:
            _log.warning("Failed to load Ring account email: %s", exc)
            account_email = None
        GLib.idle_add(self._finish_load_account_email, account_email)

    def _finish_load_account_email(self, account_email: str | None) -> bool:
        self._account_email_loading = False
        client = get_client()
        if client is None or not client.is_authenticated:
            self._account_email_cache = None
            self._account_email_row.set_subtitle("Not logged in")
            return GLib.SOURCE_REMOVE

        self._account_email_cache = account_email
        self._account_email_row.set_subtitle(account_email or "Not available")
        return GLib.SOURCE_REMOVE

    def _refresh_directories(self, cfg: dict) -> None:
        self._snapshot_dir_row.set_subtitle(self._display_path(cfg["snapshot_dir"]))
        self._video_dir_row.set_subtitle(self._display_path(cfg["video_dir"]))
        self._set_switch_active(
            self._camera_subfolders_switch,
            cfg.get("create_camera_subfolders", False),
        )
        self._set_switch_active(
            self._open_folder_after_save_switch,
            cfg.get("open_folder_after_save", False),
        )

    def _refresh_live_monitoring(self, cfg: dict) -> None:
        self._set_switch_active(
            self._live_monitoring_autostart_switch,
            cfg.get("live_monitoring_autostart", False),
        )
        self._set_switch_active(
            self._live_monitoring_continue_switch,
            cfg.get("live_monitoring_continue_on_page_exit", False),
        )
        self._set_switch_active(
            self._live_monitoring_keep_focus_switch,
            cfg.get("live_monitoring_keep_streams_in_focus", False),
        )
        self._set_switch_active(
            self._live_monitoring_unmute_switch,
            cfg.get("live_monitoring_unmute_on_start", False),
        )
        self._set_switch_active(
            self._live_monitoring_allow_six_switch,
            cfg.get("live_monitoring_allow_six_streams", False),
        )

    def _refresh_playback(self, cfg: dict) -> None:
        self._set_switch_active(
            self._event_history_next_auto_play_switch,
            cfg.get("event_history_next_auto_play", False),
        )

    def _refresh_notifications(self, cfg: dict) -> None:
        notifications_enabled = cfg.get("show_notifications", True)
        custom_messages_enabled = cfg.get("custom_notification_messages", False)

        self._set_switch_active(self._notifications_switch, notifications_enabled)
        self._doorbell_notifications_row.set_visible(notifications_enabled)
        self._motion_notifications_row.set_visible(notifications_enabled)
        self._alarm_notifications_row.set_visible(notifications_enabled)
        self._set_switch_active(
            self._doorbell_notifications_switch,
            cfg.get("notify_doorbell", True),
        )
        self._set_switch_active(
            self._motion_notifications_switch,
            cfg.get("notify_motion", True),
        )
        self._set_switch_active(
            self._alarm_notifications_switch,
            cfg.get("notify_alarm", True),
        )
        self._set_switch_active(
            self._notification_preview_switch,
            cfg.get("notification_preview_images", True),
        )
        self._set_switch_active(
            self._notification_summaries_switch,
            cfg.get("notification_summaries", True),
        )
        self._set_switch_active(
            self._custom_notification_messages_switch,
            custom_messages_enabled,
        )
        self._custom_doorbell_message_row.set_visible(custom_messages_enabled)
        self._custom_motion_message_row.set_visible(custom_messages_enabled)
        self._custom_alarm_message_row.set_visible(custom_messages_enabled)
        self._custom_doorbell_message_row.set_text(cfg.get("custom_doorbell_message", ""))
        self._custom_motion_message_row.set_text(cfg.get("custom_motion_message", ""))
        self._custom_alarm_message_row.set_text(cfg.get("custom_alarm_message", ""))

    def _display_path(self, path: str) -> str:
        text = str(path)
        home = str(GLib.get_home_dir())
        return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text

    def _on_category_selected(self, _list_box, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        name = getattr(row, "_settings_page_name", None)
        if name is not None:
            self._stack.set_visible_child_name(name)

    def _on_background_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        enabled = switch.get_active()
        cfg["background_service"] = enabled
        if not enabled:
            cfg["autostart_background"] = False
        _config.save(cfg)
        self.refresh()
        self._apply_app_settings()

    def _on_autostart_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        enabled = switch.get_active()
        cfg = _config.load()
        cfg["autostart_login"] = enabled
        if not enabled:
            cfg["autostart_background"] = False
        _config.save(cfg)
        try:
            autostart.set_enabled(enabled, background=cfg["autostart_background"])
        except Exception as exc:
            _log.warning("Failed to update autostart setting: %s", exc)
            self._set_switch_active(switch, autostart.is_enabled())
        self.refresh()
        self._apply_app_settings()

    def _on_autostart_background_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        enabled = switch.get_active()
        cfg = _config.load()
        cfg["autostart_background"] = enabled
        if enabled:
            cfg["autostart_login"] = True
            cfg["background_service"] = True
        _config.save(cfg)
        try:
            autostart.set_enabled(cfg["autostart_login"], background=enabled)
        except Exception as exc:
            _log.warning("Failed to update autostart background setting: %s", exc)
        self.refresh()
        self._apply_app_settings()

    def _on_tray_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        cfg["show_tray_icon"] = switch.get_active()
        _config.save(cfg)
        self._apply_app_settings()

    def _on_live_monitoring_allow_six_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        self._save_bool_setting("live_monitoring_allow_six_streams", switch)
        self._refresh_main_window()

    def _on_notifications_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        cfg["show_notifications"] = switch.get_active()
        _config.save(cfg)
        self.refresh()

    def _on_custom_notification_messages_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        cfg["custom_notification_messages"] = switch.get_active()
        _config.save(cfg)
        self.refresh()

    def _on_custom_message_changed(self, row: Adw.EntryRow, _pspec, config_key: str) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        cfg[config_key] = row.get_text()
        _config.save(cfg)

    def _save_switch_setting(self, switch: Gtk.Switch, _pspec, *, config_key: str) -> None:
        self._save_bool_setting(config_key, switch)

    def _save_bool_setting(self, config_key: str, switch: Gtk.Switch) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        cfg[config_key] = switch.get_active()
        _config.save(cfg)

    def _on_choose_folder_clicked(self, _button: Gtk.Button, config_key: str) -> None:
        cfg = _config.load()
        dialog = Gtk.FileChooserNative(
            title="Choose Folder",
            transient_for=self.get_root() if isinstance(self.get_root(), Gtk.Window) else None,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Select",
            cancel_label="Cancel",
        )
        current = Gio.File.new_for_path(str(cfg[config_key]))
        dialog.set_current_folder(current)
        dialog.connect("response", self._on_folder_dialog_response, config_key)
        dialog.show()

    def _on_folder_dialog_response(
        self,
        dialog: Gtk.FileChooserNative,
        response: int,
        config_key: str,
    ) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            folder = dialog.get_file()
            path = folder.get_path() if folder is not None else None
            if path:
                cfg = _config.load()
                cfg[config_key] = path
                _config.save(cfg)
                self.refresh()
        dialog.destroy()

    def _on_camera_subfolders_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        cfg["create_camera_subfolders"] = switch.get_active()
        _config.save(cfg)

    def _on_open_folder_after_save_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._refreshing:
            return
        cfg = _config.load()
        cfg["open_folder_after_save"] = switch.get_active()
        _config.save(cfg)

    def _on_open_favorites_folder_clicked(self, *_args) -> None:
        favorites.FAVORITES_DIR.mkdir(parents=True, exist_ok=True)
        media_paths.open_folder(favorites.FAVORITES_DIR)

    def _on_reset_directories_clicked(self, *_args) -> None:
        cfg = _config.load()
        cfg["snapshot_dir"] = str(media_paths.DEFAULT_SNAPSHOT_DIR)
        cfg["video_dir"] = str(media_paths.DEFAULT_VIDEO_DIR)
        cfg["create_camera_subfolders"] = False
        cfg["open_folder_after_save"] = False
        _config.save(cfg)
        self.refresh()

    def _on_delete_live_monitoring_layouts_clicked(self, *_args) -> None:
        dialog = Adw.AlertDialog(
            heading="Delete all saved custom layouts in Live Monitoring?",
            body="This removes every saved custom Live Monitoring layout.",
        )
        dialog.add_response("cancel", "No")
        dialog.add_response("delete", "Yes")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_live_monitoring_layouts_confirmed)
        dialog.present(self)

    def _on_delete_live_monitoring_layouts_confirmed(self, _dialog, response: str) -> None:
        if response != "delete":
            return
        live_layouts.save_layouts([])
        self._set_live_monitoring_message("Deleted all saved custom layouts.")
        self._refresh_main_window()

    def _set_live_monitoring_message(self, message: str) -> None:
        self._live_monitoring_message_row.set_subtitle(message)
        self._live_monitoring_message_row.set_visible(True)

    def _on_clear_preview_cache_clicked(self, *_args) -> None:
        self._confirm_privacy_action(
            "Clear cached previews?",
            "This deletes local cached camera preview images and generated thumbnails.",
            self._clear_preview_cache,
        )

    def _on_clear_debug_log_clicked(self, *_args) -> None:
        self._confirm_privacy_action(
            "Clear debug log?",
            "This clears Halo debug logs stored on this computer.",
            self._clear_debug_log,
        )

    def _confirm_privacy_action(self, heading: str, body: str, callback) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_privacy_confirmed, callback)
        dialog.present(self)

    def _on_privacy_confirmed(self, _dialog, response: str, callback) -> None:
        if response == "clear":
            callback()

    def _clear_preview_cache(self) -> None:
        try:
            removed = privacy.clear_preview_cache()
        except Exception as exc:
            _log.warning("Failed to clear cached previews: %s", exc)
            self._set_privacy_message(f"Could not clear cached previews: {exc}")
            return

        if removed:
            self._set_privacy_message("Cleared cached thumbnails and previews.")
        else:
            self._set_privacy_message("No cached thumbnails or previews found.")

    def _clear_debug_log(self) -> None:
        try:
            touched = privacy.clear_debug_logs()
        except Exception as exc:
            _log.warning("Failed to clear debug logs: %s", exc)
            self._set_privacy_message(f"Could not clear debug logs: {exc}")
            return

        if touched:
            self._set_privacy_message("Cleared debug logs.")
        else:
            self._set_privacy_message("No debug logs found.")

    def _set_privacy_message(self, message: str) -> None:
        self._privacy_message_row.set_subtitle(message)
        self._privacy_message_row.set_visible(True)

    def _on_export_logs_clicked(self, *_args) -> None:
        dialog = Gtk.FileChooserNative(
            title="Export Debug Logs",
            transient_for=self.get_root() if isinstance(self.get_root(), Gtk.Window) else None,
            action=Gtk.FileChooserAction.SAVE,
            accept_label="Export",
            cancel_label="Cancel",
        )
        dialog.set_current_name(f"halo-gtk-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip")
        dialog.connect("response", self._on_export_logs_response)
        dialog.show()

    def _on_export_logs_response(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            path = Path(file.get_path()) if file is not None and file.get_path() else None
            if path is not None:
                try:
                    archive = diagnostics.export_debug_logs(path)
                except Exception as exc:
                    _log.warning("Failed to export debug logs: %s", exc)
                    self._set_advanced_message(f"Could not export debug logs: {exc}")
                else:
                    self._set_advanced_message(
                        f"Exported debug logs to {self._display_path(str(archive))}."
                    )
        dialog.destroy()

    def _on_reset_settings_clicked(self, *_args) -> None:
        dialog = Adw.AlertDialog(
            heading="Reset settings?",
            body=(
                "This restores Halo settings to defaults. Your saved Ring session and saved"
                " media files are not removed."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reset", "Reset")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_reset_settings_confirmed)
        dialog.present(self)

    def _on_reset_settings_confirmed(self, _dialog, response: str) -> None:
        if response != "reset":
            return

        try:
            _config.reset()
        except Exception as exc:
            _log.warning("Failed to reset settings: %s", exc)
            self._set_advanced_message(f"Could not reset settings: {exc}")
            return

        try:
            autostart.set_enabled(False)
        except Exception as exc:
            _log.warning("Failed to disable autostart after settings reset: %s", exc)

        self.refresh()
        self._apply_app_settings()
        self._refresh_main_window()
        self._set_advanced_message("Settings reset to defaults.")

    def _set_advanced_message(self, message: str) -> None:
        self._advanced_message_row.set_subtitle(message)
        self._advanced_message_row.set_visible(True)

    def _on_refresh_session_clicked(self, *_args) -> None:
        client = get_client()
        if client is None or not client.is_authenticated:
            self._set_account_message("Not signed in to Ring.")
            self.refresh()
            return

        self._set_account_buttons_sensitive(False)
        self._set_account_message("Refreshing Ring session...")
        threading.Thread(target=self._refresh_session, args=(client,), daemon=True).start()

    def _refresh_session(self, client) -> None:
        from ring_doorbell import AuthenticationError

        try:
            client.refresh_session_token()
        except Exception as exc:
            if isinstance(exc, AuthenticationError):
                GLib.idle_add(
                    self._begin_rejected_session_logout,
                    client,
                )
            else:
                _log.warning("Failed to refresh Ring session: %s", exc)
                GLib.idle_add(self._finish_refresh_session, False, str(exc), False)
        else:
            GLib.idle_add(self._finish_refresh_session, True, "Ring session refreshed.", False)

    def _begin_rejected_session_logout(self, client) -> bool:
        """Stop GTK media first, then clear the rejected client off the main thread."""
        if get_client() is not client:
            return self._finish_refresh_session(
                False,
                "The Ring session changed while it was being refreshed.",
                False,
            )
        self._stop_main_window_runtime_streams()
        threading.Thread(
            target=self._rejected_session_logout_worker,
            args=(client,),
            daemon=True,
        ).start()
        return GLib.SOURCE_REMOVE

    def _rejected_session_logout_worker(self, client) -> None:
        error: str | None = None
        logged_out = False
        try:
            logged_out = logout_client(expected_client=client)
        except Exception as exc:
            _log.warning("Failed to clear rejected Ring session: %s", exc)
            error = str(exc)
        GLib.idle_add(self._finish_rejected_session_logout, logged_out, error)

    def _finish_rejected_session_logout(
        self,
        logged_out: bool,
        error: str | None,
    ) -> bool:
        if not logged_out and error is None:
            return self._finish_refresh_session(
                False,
                "The Ring session changed while it was being refreshed.",
                False,
            )
        message = "Ring rejected the saved session. Sign in again to continue."
        if error is not None:
            message = f"Ring rejected the saved session, but local sign-out failed: {error}"
        return self._finish_refresh_session(False, message, True)

    def _finish_refresh_session(
        self,
        success: bool,
        message: str,
        prompt_sign_in: bool,
    ) -> bool:
        self._set_account_message(message)
        self._set_account_buttons_sensitive(True)
        self.refresh()
        self._refresh_main_window()
        if not success and prompt_sign_in:
            self._show_sign_in_dialog()
        return GLib.SOURCE_REMOVE

    def _on_open_ring_account_clicked(self, *_args) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(_RING_ACCOUNT_URL, None)
            self._set_account_message("Opened Ring Account Control Center in your browser.")
        except Exception as exc:
            _log.warning("Failed to open Ring Account Control Center: %s", exc)
            self._set_account_message(f"Could not open browser: {exc}")

    def _on_logout_clicked(self, *_args) -> None:
        # logout_client() tears down the asyncio loop + FCM listener and can block
        # for several seconds; run it off the GTK main thread so the UI never freezes.
        self._set_account_buttons_sensitive(False)
        self._set_account_message("Logging out of Ring...")
        self._stop_main_window_runtime_streams()
        threading.Thread(target=self._logout_worker, daemon=True).start()

    def _logout_worker(self) -> None:
        error: str | None = None
        try:
            logout_client()
        except Exception as exc:
            _log.warning("Failed to log out of Ring: %s", exc)
            error = str(exc)
        GLib.idle_add(self._finish_logout, error)

    def _finish_logout(self, error: str | None) -> bool:
        self._set_account_buttons_sensitive(True)
        if error is not None:
            self._set_account_message(f"Could not log out: {error}")
            return GLib.SOURCE_REMOVE
        self._set_account_message("Logged out of Ring.")
        self._account_email_cache = None
        self._account_email_loading = False
        self.refresh()
        self._refresh_main_window()
        return GLib.SOURCE_REMOVE

    def _set_account_buttons_sensitive(self, sensitive: bool) -> None:
        self._refresh_session_button.set_sensitive(sensitive)
        self._logout_button.set_sensitive(sensitive)

    def _set_account_message(self, message: str) -> None:
        self._account_message_row.set_subtitle(message)
        self._account_message_row.set_visible(True)

    def _refresh_main_window(self) -> None:
        app = Gtk.Application.get_default()
        if app is None:
            return
        for win in app.get_windows():
            if win is self.get_root():
                continue
            if hasattr(win, "refresh"):
                win.refresh()
            if hasattr(win, "enforce_live_monitoring_limits"):
                win.enforce_live_monitoring_limits()

    def _stop_main_window_runtime_streams(self) -> None:
        """Synchronously detach GTK media before a worker tears down Ring."""
        app = Gtk.Application.get_default()
        if app is None:
            return
        for win in app.get_windows():
            stop_runtime_streams = getattr(win, "stop_runtime_streams", None)
            if callable(stop_runtime_streams):
                stop_runtime_streams()

    def _show_sign_in_dialog(self) -> None:
        from halo_gtk.auth_dialog import AuthDialog

        root = self.get_root()
        dialog = AuthDialog()
        if isinstance(root, Gtk.Window):
            dialog.present(root)
        else:
            dialog.present()

    def _apply_app_settings(self) -> None:
        app = Gtk.Application.get_default()
        if hasattr(app, "apply_background_settings"):
            app.apply_background_settings()
