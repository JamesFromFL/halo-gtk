"""Application configuration stored under the user's XDG config directory."""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any

from halo_gtk import APP_ID, storage_paths
from halo_gtk.atomic_io import atomic_write_json

_log = logging.getLogger(__name__)

CONFIG_DIR = storage_paths.config_dir()
CONFIG_FILE = CONFIG_DIR / "settings.json"
LEGACY_CONFIG_FILE = CONFIG_DIR / "config.json"

_DEFAULTS: dict[str, Any] = {
    "show_notifications": True,
    "notify_doorbell": True,
    "notify_motion": True,
    "notify_alarm": True,
    "notification_preview_images": True,
    "notification_summaries": True,
    "custom_notification_messages": False,
    "custom_doorbell_message": "",
    "custom_motion_message": "",
    "custom_alarm_message": "",
    "camera_grid_size": "medium",
    "camera_order": [],
    "live_monitoring_grid_size": "medium",
    "live_monitoring_camera_order": [],
    "live_monitoring_hidden_camera_ids": [],
    "live_monitoring_autostart": False,
    "live_monitoring_continue_on_page_exit": False,
    "live_monitoring_keep_streams_in_focus": False,
    "live_monitoring_allow_six_streams": False,
    "live_monitoring_unmute_on_start": False,
    "event_history_next_auto_play": False,
    "background_service": False,
    "autostart_login": False,
    "autostart_background": False,
    "show_tray_icon": True,
    "snapshot_dir": str(Path.home() / "Pictures" / "halo-gtk"),
    "video_dir": str(Path.home() / "Videos" / "halo-gtk"),
    "create_camera_subfolders": False,
    "open_folder_after_save": False,
}

_KEYS: dict[str, tuple[str, str]] = {
    "show_notifications": ("show-notifications", "bool"),
    "notify_doorbell": ("notify-doorbell", "bool"),
    "notify_motion": ("notify-motion", "bool"),
    "notify_alarm": ("notify-alarm", "bool"),
    "notification_preview_images": ("notification-preview-images", "bool"),
    "notification_summaries": ("notification-summaries", "bool"),
    "custom_notification_messages": ("custom-notification-messages", "bool"),
    "custom_doorbell_message": ("custom-doorbell-message", "str"),
    "custom_motion_message": ("custom-motion-message", "str"),
    "custom_alarm_message": ("custom-alarm-message", "str"),
    "camera_grid_size": ("camera-grid-size", "str"),
    "camera_order": ("camera-order", "int-list"),
    "live_monitoring_grid_size": ("live-monitoring-grid-size", "str"),
    "live_monitoring_camera_order": ("live-monitoring-camera-order", "int-list"),
    "live_monitoring_hidden_camera_ids": ("live-monitoring-hidden-camera-ids", "int-list"),
    "live_monitoring_autostart": ("live-monitoring-autostart", "bool"),
    "live_monitoring_continue_on_page_exit": (
        "live-monitoring-continue-on-page-exit",
        "bool",
    ),
    "live_monitoring_keep_streams_in_focus": (
        "live-monitoring-keep-streams-in-focus",
        "bool",
    ),
    "live_monitoring_allow_six_streams": (
        "live-monitoring-allow-six-streams",
        "bool",
    ),
    "live_monitoring_unmute_on_start": (
        "live-monitoring-unmute-on-start",
        "bool",
    ),
    "event_history_next_auto_play": ("event-history-next-auto-play", "bool"),
    "background_service": ("background-service", "bool"),
    "autostart_login": ("autostart-login", "bool"),
    "autostart_background": ("autostart-background", "bool"),
    "show_tray_icon": ("show-tray-icon", "bool"),
    "snapshot_dir": ("snapshot-dir", "str"),
    "video_dir": ("video-dir", "str"),
    "create_camera_subfolders": ("create-camera-subfolders", "bool"),
    "open_folder_after_save": ("open-folder-after-save", "bool"),
}

_GSETTINGS_ENABLED = True
_MIGRATED_GSETTINGS_TO_JSON = False
_GRID_SIZES = {"small", "medium", "large"}

# In-memory cache for the parsed settings, keyed by (path, mtime), so the many
# config.load() callers don't re-read + re-parse + re-normalise the file on every
# call (e.g. per Ring event, per history row). Invalidated on save()/reset().
_cache_lock = threading.Lock()
_cached_config: dict[str, Any] | None = None
_cached_key: tuple[str, float | None] | None = None


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int_list(value: Any) -> list[int]:
    if not isinstance(value, list | tuple):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _coerce_grid_size(value: Any, default: str) -> str:
    result = str(value)
    return result if result in _GRID_SIZES else default


def _coerce_path(value: Any, default: str) -> str:
    result = str(value).strip()
    return result or default


def _settings():
    if not _GSETTINGS_ENABLED:
        return None

    try:
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup(APP_ID, True) is None:
            return None
        return Gio.Settings.new(APP_ID)
    except Exception as exc:
        _log.debug("GSettings unavailable, using JSON config fallback: %s", exc)
        return None


def _read_json_file(path: Path | None = None) -> dict[str, Any] | None:
    path = path or CONFIG_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("Failed to load config from %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        _log.warning("Failed to load config: expected object, got %s", type(data).__name__)
        return None
    return data


def _normalise_config(config: dict[str, Any]) -> dict[str, Any]:
    data = {key: config.get(key, default) for key, default in _DEFAULTS.items()}
    data["show_notifications"] = _coerce_bool(
        data["show_notifications"], _DEFAULTS["show_notifications"]
    )
    data["notify_doorbell"] = _coerce_bool(data["notify_doorbell"], _DEFAULTS["notify_doorbell"])
    data["notify_motion"] = _coerce_bool(data["notify_motion"], _DEFAULTS["notify_motion"])
    data["notify_alarm"] = _coerce_bool(data["notify_alarm"], _DEFAULTS["notify_alarm"])
    data["notification_preview_images"] = _coerce_bool(
        data["notification_preview_images"], _DEFAULTS["notification_preview_images"]
    )
    data["notification_summaries"] = _coerce_bool(
        data["notification_summaries"], _DEFAULTS["notification_summaries"]
    )
    data["custom_notification_messages"] = _coerce_bool(
        data["custom_notification_messages"], _DEFAULTS["custom_notification_messages"]
    )
    data["custom_doorbell_message"] = str(data["custom_doorbell_message"])
    data["custom_motion_message"] = str(data["custom_motion_message"])
    data["custom_alarm_message"] = str(data["custom_alarm_message"])
    data["camera_grid_size"] = _coerce_grid_size(
        data["camera_grid_size"], _DEFAULTS["camera_grid_size"]
    )
    data["camera_order"] = _coerce_int_list(data["camera_order"])
    data["live_monitoring_grid_size"] = _coerce_grid_size(
        data["live_monitoring_grid_size"], _DEFAULTS["live_monitoring_grid_size"]
    )
    data["live_monitoring_camera_order"] = _coerce_int_list(data["live_monitoring_camera_order"])
    data["live_monitoring_hidden_camera_ids"] = _coerce_int_list(
        data["live_monitoring_hidden_camera_ids"]
    )
    data["live_monitoring_autostart"] = _coerce_bool(
        data["live_monitoring_autostart"], _DEFAULTS["live_monitoring_autostart"]
    )
    data["live_monitoring_continue_on_page_exit"] = _coerce_bool(
        data["live_monitoring_continue_on_page_exit"],
        _DEFAULTS["live_monitoring_continue_on_page_exit"],
    )
    data["live_monitoring_keep_streams_in_focus"] = _coerce_bool(
        data["live_monitoring_keep_streams_in_focus"],
        _DEFAULTS["live_monitoring_keep_streams_in_focus"],
    )
    data["live_monitoring_allow_six_streams"] = _coerce_bool(
        data["live_monitoring_allow_six_streams"], _DEFAULTS["live_monitoring_allow_six_streams"]
    )
    data["live_monitoring_unmute_on_start"] = _coerce_bool(
        data["live_monitoring_unmute_on_start"],
        _DEFAULTS["live_monitoring_unmute_on_start"],
    )
    data["event_history_next_auto_play"] = _coerce_bool(
        data["event_history_next_auto_play"], _DEFAULTS["event_history_next_auto_play"]
    )
    data["background_service"] = _coerce_bool(
        data["background_service"], _DEFAULTS["background_service"]
    )
    data["autostart_login"] = _coerce_bool(data["autostart_login"], _DEFAULTS["autostart_login"])
    data["autostart_background"] = _coerce_bool(
        data["autostart_background"], _DEFAULTS["autostart_background"]
    )
    data["show_tray_icon"] = _coerce_bool(data["show_tray_icon"], _DEFAULTS["show_tray_icon"])
    data["snapshot_dir"] = _coerce_path(data["snapshot_dir"], _DEFAULTS["snapshot_dir"])
    data["video_dir"] = _coerce_path(data["video_dir"], _DEFAULTS["video_dir"])
    data["create_camera_subfolders"] = _coerce_bool(
        data["create_camera_subfolders"], _DEFAULTS["create_camera_subfolders"]
    )
    data["open_folder_after_save"] = _coerce_bool(
        data["open_folder_after_save"], _DEFAULTS["open_folder_after_save"]
    )
    if not data["background_service"]:
        data["autostart_background"] = False
    if not data["autostart_login"]:
        data["autostart_background"] = False
    return data


def _load_json() -> dict[str, Any]:
    data = _read_json_file()
    if data is None and LEGACY_CONFIG_FILE != CONFIG_FILE:
        data = _read_json_file(LEGACY_CONFIG_FILE)
    if data is None:
        return dict(_DEFAULTS)
    return _normalise_config(data)


def _load_gsettings(settings) -> dict[str, Any]:
    config = dict(_DEFAULTS)
    for app_key, (settings_key, value_type) in _KEYS.items():
        if value_type == "bool":
            config[app_key] = settings.get_boolean(settings_key)
        elif value_type == "str":
            config[app_key] = settings.get_string(settings_key)
        elif value_type == "int-list":
            config[app_key] = list(settings.get_value(settings_key).unpack())
    return config


def _write_json_file(config: dict[str, Any]) -> None:
    payload = _normalise_config(config)
    atomic_write_json(CONFIG_FILE, payload, prefix=".settings-")


def _reset_gsettings(settings) -> None:
    for settings_key, _value_type in _KEYS.values():
        settings.reset(settings_key)


def _migrate_existing_config_to_json() -> None:
    """Persist old JSON or GSettings values into the new JSON settings file."""
    global _MIGRATED_GSETTINGS_TO_JSON

    if CONFIG_FILE.exists():
        return

    data = _read_json_file(LEGACY_CONFIG_FILE)
    if data is not None:
        _write_json_file(data)
        LEGACY_CONFIG_FILE.unlink(missing_ok=True)
        _log.info("Migrated legacy config.json to settings.json")
        return

    if _MIGRATED_GSETTINGS_TO_JSON:
        return
    _MIGRATED_GSETTINGS_TO_JSON = True

    settings = _settings()
    if settings is None:
        return
    has_gsettings_values = any(
        settings.get_user_value(settings_key) is not None
        for settings_key, _value_type in _KEYS.values()
    )
    if not has_gsettings_values:
        return

    _write_json_file(_normalise_config(_load_gsettings(settings)))
    _reset_gsettings(settings)
    _log.info("Migrated GSettings values to settings.json")


def load() -> dict:
    _migrate_existing_config_to_json()
    return _load_cached()


def _load_cached() -> dict:
    try:
        mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        mtime = None
    key = (str(CONFIG_FILE), mtime)
    global _cached_config, _cached_key
    with _cache_lock:
        if _cached_config is not None and _cached_key == key:
            return copy.deepcopy(_cached_config)
        config = _load_json()
        _cached_config = config
        _cached_key = key
        return copy.deepcopy(config)


def _invalidate_cache() -> None:
    global _cached_config, _cached_key
    with _cache_lock:
        _cached_config = None
        _cached_key = None


def save(config: dict) -> None:
    _write_json_file(config)
    _invalidate_cache()


def defaults() -> dict[str, Any]:
    """Return a normalized copy of the default application settings."""
    return _normalise_config({})


def reset() -> dict[str, Any]:
    """Reset application settings to defaults and return the resulting config."""
    settings = _settings()
    if settings is not None:
        _reset_gsettings(settings)

    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    if LEGACY_CONFIG_FILE.exists():
        LEGACY_CONFIG_FILE.unlink()
    _invalidate_cache()
    return defaults()
