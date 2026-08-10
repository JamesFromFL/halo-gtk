"""Tests for config load/save round-trip."""

from __future__ import annotations


def _redirect_config_store(config, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "LEGACY_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "_MIGRATED_GSETTINGS_TO_JSON", False)


def test_load_defaults(tmp_path, monkeypatch):
    from halo_gtk import config

    monkeypatch.setattr(config, "_GSETTINGS_ENABLED", False)
    _redirect_config_store(config, monkeypatch, tmp_path)

    cfg = config.load()
    assert cfg["show_notifications"] is True
    assert cfg["notify_doorbell"] is True
    assert cfg["notify_motion"] is True
    assert cfg["notify_alarm"] is True
    assert cfg["notification_preview_images"] is True
    assert cfg["notification_summaries"] is True
    assert cfg["custom_notification_messages"] is False
    assert cfg["custom_doorbell_message"] == ""
    assert cfg["custom_motion_message"] == ""
    assert cfg["custom_alarm_message"] == ""
    assert cfg["camera_grid_size"] == "medium"
    assert cfg["camera_grid_density_preset"] == "balanced"
    assert cfg["camera_order"] == []
    assert cfg["live_monitoring_grid_size"] == "medium"
    assert cfg["live_monitoring_camera_order"] == []
    assert cfg["live_monitoring_hidden_camera_ids"] == []
    assert cfg["live_monitoring_autostart"] is False
    assert cfg["live_monitoring_continue_on_page_exit"] is False
    assert cfg["live_monitoring_keep_streams_in_focus"] is False
    assert cfg["live_monitoring_allow_six_streams"] is False
    assert cfg["live_monitoring_unmute_on_start"] is False
    assert cfg["event_history_next_auto_play"] is False
    assert cfg["background_service"] is False
    assert cfg["autostart_login"] is False
    assert cfg["autostart_background"] is False
    assert cfg["show_tray_icon"] is True
    assert cfg["snapshot_dir"].endswith("/Pictures/halo-gtk")
    assert cfg["video_dir"].endswith("/Videos/halo-gtk")
    assert cfg["create_camera_subfolders"] is False
    assert cfg["open_folder_after_save"] is False


def test_load_caches_until_invalidated(tmp_path, monkeypatch):
    from halo_gtk import config

    monkeypatch.setattr(config, "_GSETTINGS_ENABLED", False)
    _redirect_config_store(config, monkeypatch, tmp_path)
    config._invalidate_cache()

    reads = 0
    real_load_json = config._load_json

    def counting_load_json():
        nonlocal reads
        reads += 1
        return real_load_json()

    monkeypatch.setattr(config, "_load_json", counting_load_json)

    config.load()
    config.load()
    assert reads == 1  # second load served from the in-memory cache

    config.save({"show_notifications": False})  # invalidates the cache
    loaded = config.load()
    assert loaded["show_notifications"] is False
    assert reads == 2  # re-read once after the save


def test_save_and_reload(tmp_path, monkeypatch):
    from halo_gtk import config

    monkeypatch.setattr(config, "_GSETTINGS_ENABLED", False)
    _redirect_config_store(config, monkeypatch, tmp_path)

    config.save(
        {
            "show_notifications": False,
            "notify_doorbell": False,
            "notify_motion": False,
            "notify_alarm": False,
            "notification_preview_images": False,
            "notification_summaries": False,
            "custom_notification_messages": True,
            "custom_doorbell_message": "Doorbell custom",
            "custom_motion_message": "Motion custom",
            "custom_alarm_message": "Alarm custom",
            "camera_grid_size": "large",
            "camera_grid_density_preset": "dense",
            "camera_order": [20, 10],
            "live_monitoring_grid_size": "small",
            "live_monitoring_camera_order": [40, 50],
            "live_monitoring_hidden_camera_ids": [30],
            "live_monitoring_autostart": True,
            "live_monitoring_continue_on_page_exit": True,
            "live_monitoring_keep_streams_in_focus": True,
            "live_monitoring_allow_six_streams": True,
            "live_monitoring_unmute_on_start": True,
            "event_history_next_auto_play": True,
            "background_service": True,
            "autostart_login": True,
            "autostart_background": True,
            "show_tray_icon": False,
            "snapshot_dir": str(tmp_path / "snaps"),
            "video_dir": str(tmp_path / "videos"),
            "create_camera_subfolders": True,
            "open_folder_after_save": True,
        }
    )
    loaded = config.load()

    assert loaded["show_notifications"] is False
    assert loaded["notify_doorbell"] is False
    assert loaded["notify_motion"] is False
    assert loaded["notify_alarm"] is False
    assert loaded["notification_preview_images"] is False
    assert loaded["notification_summaries"] is False
    assert loaded["custom_notification_messages"] is True
    assert loaded["custom_doorbell_message"] == "Doorbell custom"
    assert loaded["custom_motion_message"] == "Motion custom"
    assert loaded["custom_alarm_message"] == "Alarm custom"
    assert loaded["camera_grid_size"] == "large"
    assert loaded["camera_grid_density_preset"] == "dense"
    assert loaded["camera_order"] == [20, 10]
    assert loaded["live_monitoring_grid_size"] == "small"
    assert loaded["live_monitoring_camera_order"] == [40, 50]
    assert loaded["live_monitoring_hidden_camera_ids"] == [30]
    assert loaded["live_monitoring_autostart"] is True
    assert loaded["live_monitoring_continue_on_page_exit"] is True
    assert loaded["live_monitoring_keep_streams_in_focus"] is True
    assert loaded["live_monitoring_allow_six_streams"] is True
    assert loaded["live_monitoring_unmute_on_start"] is True
    assert loaded["event_history_next_auto_play"] is True
    assert loaded["background_service"] is True
    assert loaded["autostart_login"] is True
    assert loaded["autostart_background"] is True
    assert loaded["show_tray_icon"] is False
    assert loaded["snapshot_dir"] == str(tmp_path / "snaps")
    assert loaded["video_dir"] == str(tmp_path / "videos")
    assert loaded["create_camera_subfolders"] is True
    assert loaded["open_folder_after_save"] is True
    assert config.CONFIG_FILE.exists()


class _FakeVariant:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class _FakeSettingsSchema:
    def __init__(self, keys):
        self._keys = tuple(keys)

    def list_keys(self):
        return self._keys


class _FakeSettings:
    def __init__(self, *, unavailable_keys=()):
        self._values = {
            "show-notifications": False,
            "notify-doorbell": False,
            "notify-motion": False,
            "notify-alarm": False,
            "notification-preview-images": False,
            "notification-summaries": False,
            "custom-notification-messages": True,
            "custom-doorbell-message": "Doorbell custom",
            "custom-motion-message": "Motion custom",
            "custom-alarm-message": "Alarm custom",
            "camera-grid-size": "small",
            "camera-grid-density-preset": "dense",
            "camera-order": [3, 1, 2],
            "live-monitoring-grid-size": "large",
            "live-monitoring-camera-order": [4, 5],
            "live-monitoring-hidden-camera-ids": [2],
            "live-monitoring-autostart": True,
            "live-monitoring-continue-on-page-exit": True,
            "live-monitoring-keep-streams-in-focus": True,
            "live-monitoring-allow-six-streams": True,
            "live-monitoring-unmute-on-start": True,
            "event-history-next-auto-play": True,
            "background-service": True,
            "autostart-login": True,
            "autostart-background": True,
            "show-tray-icon": False,
            "snapshot-dir": "/tmp/snaps",
            "video-dir": "/tmp/videos",
            "create-camera-subfolders": True,
            "open-folder-after-save": True,
        }
        for key in unavailable_keys:
            self._values.pop(key)
        self.settings_schema = _FakeSettingsSchema(self._values)
        self._user_values = set(self._values)
        self.reset_keys = []

    def get_boolean(self, key):
        return self._values[key]

    def get_string(self, key):
        return self._values[key]

    def get_value(self, key):
        return _FakeVariant(self._values[key])

    def get_user_value(self, key):
        return object() if key in self._user_values else None

    def reset(self, key):
        self._values[key] = config_default_for_settings_key(key)
        self._user_values.discard(key)
        self.reset_keys.append(key)


def config_default_for_settings_key(key):
    defaults = {
        "show-notifications": True,
        "notify-doorbell": True,
        "notify-motion": True,
        "notify-alarm": True,
        "notification-preview-images": True,
        "notification-summaries": True,
        "custom-notification-messages": False,
        "custom-doorbell-message": "",
        "custom-motion-message": "",
        "custom-alarm-message": "",
        "camera-grid-size": "medium",
        "camera-grid-density-preset": "balanced",
        "camera-order": [],
        "live-monitoring-grid-size": "medium",
        "live-monitoring-camera-order": [],
        "live-monitoring-hidden-camera-ids": [],
        "live-monitoring-autostart": False,
        "live-monitoring-continue-on-page-exit": False,
        "live-monitoring-keep-streams-in-focus": False,
        "live-monitoring-allow-six-streams": False,
        "live-monitoring-unmute-on-start": False,
        "event-history-next-auto-play": False,
        "background-service": False,
        "autostart-login": False,
        "autostart-background": False,
        "show-tray-icon": True,
        "snapshot-dir": "",
        "video-dir": "",
        "create-camera-subfolders": False,
        "open-folder-after-save": False,
    }
    return defaults[key]


def test_load_gsettings_maps_schema_keys():
    from halo_gtk import config

    loaded = config._load_gsettings(_FakeSettings())

    assert loaded["show_notifications"] is False
    assert loaded["notify_doorbell"] is False
    assert loaded["notify_motion"] is False
    assert loaded["notify_alarm"] is False
    assert loaded["notification_preview_images"] is False
    assert loaded["notification_summaries"] is False
    assert loaded["custom_notification_messages"] is True
    assert loaded["custom_doorbell_message"] == "Doorbell custom"
    assert loaded["custom_motion_message"] == "Motion custom"
    assert loaded["custom_alarm_message"] == "Alarm custom"
    assert loaded["camera_grid_size"] == "small"
    assert loaded["camera_grid_density_preset"] == "dense"
    assert loaded["camera_order"] == [3, 1, 2]
    assert loaded["live_monitoring_grid_size"] == "large"
    assert loaded["live_monitoring_camera_order"] == [4, 5]
    assert loaded["live_monitoring_hidden_camera_ids"] == [2]
    assert loaded["live_monitoring_autostart"] is True
    assert loaded["live_monitoring_continue_on_page_exit"] is True
    assert loaded["live_monitoring_keep_streams_in_focus"] is True
    assert loaded["live_monitoring_allow_six_streams"] is True
    assert loaded["live_monitoring_unmute_on_start"] is True
    assert loaded["event_history_next_auto_play"] is True
    assert loaded["background_service"] is True
    assert loaded["autostart_login"] is True
    assert loaded["autostart_background"] is True
    assert loaded["show_tray_icon"] is False
    assert loaded["snapshot_dir"] == "/tmp/snaps"
    assert loaded["video_dir"] == "/tmp/videos"
    assert loaded["create_camera_subfolders"] is True
    assert loaded["open_folder_after_save"] is True


def test_gsettings_migration_skips_keys_missing_from_installed_schema(tmp_path, monkeypatch):
    from halo_gtk import config

    _redirect_config_store(config, monkeypatch, tmp_path)
    fake_settings = _FakeSettings(unavailable_keys={"camera-grid-density-preset"})
    monkeypatch.setattr(config, "_settings", lambda: fake_settings)

    loaded = config.load()

    assert loaded["camera_grid_density_preset"] == "balanced"
    assert "camera-grid-density-preset" not in fake_settings.reset_keys
    assert config.CONFIG_FILE.exists()


def test_normalise_disables_background_autostart_without_background_service():
    from halo_gtk import config

    data = config._normalise_config(
        {
            "background_service": False,
            "autostart_login": True,
            "autostart_background": True,
        }
    )

    assert data["autostart_background"] is False


def test_normalise_disables_background_autostart_without_autostart():
    from halo_gtk import config

    data = config._normalise_config(
        {
            "background_service": True,
            "autostart_login": False,
            "autostart_background": True,
        }
    )

    assert data["autostart_background"] is False


def test_normalise_tolerates_malformed_json_values():
    from halo_gtk import config

    data = config._normalise_config(
        {
            "show_notifications": "false",
            "camera_grid_size": "huge",
            "camera_grid_density_preset": "contradictory",
            "camera_order": ["10", "bad", None, 20],
            "live_monitoring_grid_size": "medium",
            "live_monitoring_camera_order": "1,2,3",
            "live_monitoring_hidden_camera_ids": [1.0, "2", "bad"],
            "snapshot_dir": "   ",
            "video_dir": "",
        }
    )

    assert data["show_notifications"] is False
    assert data["camera_grid_size"] == "medium"
    assert data["camera_grid_density_preset"] == "balanced"
    assert data["camera_order"] == [10, 20]
    assert data["live_monitoring_grid_size"] == "medium"
    assert data["live_monitoring_camera_order"] == []
    assert data["live_monitoring_hidden_camera_ids"] == [1, 2]
    assert data["snapshot_dir"].endswith("/Pictures/halo-gtk")
    assert data["video_dir"].endswith("/Videos/halo-gtk")


def test_reset_json_config_removes_file_and_returns_defaults(tmp_path, monkeypatch):
    from halo_gtk import config

    monkeypatch.setattr(config, "_GSETTINGS_ENABLED", False)
    _redirect_config_store(config, monkeypatch, tmp_path)
    config.CONFIG_FILE.write_text('{"show_notifications": false}')
    config.LEGACY_CONFIG_FILE.write_text('{"notify_motion": false}')

    reset = config.reset()

    assert not config.CONFIG_FILE.exists()
    assert not config.LEGACY_CONFIG_FILE.exists()
    assert reset["show_notifications"] is True


def test_legacy_json_config_migrates_to_settings_json(tmp_path, monkeypatch):
    from halo_gtk import config

    monkeypatch.setattr(config, "_GSETTINGS_ENABLED", False)
    _redirect_config_store(config, monkeypatch, tmp_path)
    config.LEGACY_CONFIG_FILE.write_text('{"show_notifications": false}')

    loaded = config.load()

    assert loaded["show_notifications"] is False
    assert config.CONFIG_FILE.exists()
    assert not config.LEGACY_CONFIG_FILE.exists()


def test_gsettings_config_migrates_to_settings_json(tmp_path, monkeypatch):
    from halo_gtk import config

    _redirect_config_store(config, monkeypatch, tmp_path)
    fake_settings = _FakeSettings()
    monkeypatch.setattr(config, "_settings", lambda: fake_settings)

    loaded = config.load()

    assert loaded["show_notifications"] is False
    assert loaded["camera_grid_size"] == "small"
    assert config.CONFIG_FILE.exists()
    assert fake_settings.reset_keys == [
        settings_key for settings_key, _type in config._KEYS.values()
    ]
