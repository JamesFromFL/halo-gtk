"""Notification timing boundaries, delivery races, and local-only preferences."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from halo_gtk import config, notifications
from halo_gtk import notification_rules as rules
from halo_gtk.alarm.events import AlarmEvent


@pytest.mark.parametrize(
    "start,end,hour,minute,expected",
    [
        (1320, 420, 21, 59, True),
        (1320, 420, 22, 0, False),
        (1320, 420, 6, 59, False),
        (1320, 420, 7, 0, True),
        (540, 1020, 12, 0, False),
        (540, 1020, 17, 0, True),
        (0, 0, 12, 0, True),
    ],
)
def test_local_quiet_hours_boundaries(start, end, hour, minute, expected):
    cfg = {"notification_rules": {"quiet_enabled": True, "quiet_start": start, "quiet_end": end}}
    assert (
        rules.allowed(cfg, None, "motion", local_time=datetime(2026, 11, 16, hour, minute))
        is expected
    )


def test_device_rules_do_not_cross_families_and_snooze_expires():
    cfg = {
        "notification_rules": {"devices": {"camera:42": {"motion": False, "snoozed_until": 1060}}}
    }
    assert not rules.allowed(cfg, "camera:42", "motion", now=1100)
    assert not rules.allowed(cfg, "camera:42", "ding", now=1000)
    assert rules.allowed(cfg, "camera:42", "ding", now=1060)
    assert rules.allowed(cfg, "intercom:42", "ding", now=1000)


def test_snooze_fails_open_for_invalid_clock_and_critical_alarm():
    cfg = {
        "notification_rules": {"snoozed_until": 10000, "devices": {"alarm:x": {"enabled": False}}}
    }
    assert rules.allowed(cfg, None, "motion", now=0)
    assert not rules.allowed(cfg, None, "motion", now=9999)
    assert rules.allowed(cfg, "alarm:x", "alarm", critical=True, now=9999)
    event = AlarmEvent(1, "x", "fire", "alarm", "Fire", critical=True)
    assert notifications.alarm_event_enabled(event, cfg)
    assert not notifications.alarm_event_enabled(event, {**cfg, "notify_alarm": False})


def test_alarm_sensor_rule_is_scoped_and_keeps_critical_alerts():
    key = rules.alarm_device_key("home", "base", "contact")
    cfg = {
        "notify_alarm_sensor": True,
        "notification_rules": {"devices": {key: {"enabled": False}}},
    }
    event = AlarmEvent(1, "home", "opened", "sensor", "Contact", asset_id="base", zid="contact")
    assert not notifications.alarm_event_enabled(event, cfg)
    assert key != rules.alarm_device_key("other", "base", "contact")
    assert key != rules.alarm_device_key("home", "other", "contact")
    assert notifications.alarm_event_enabled(
        AlarmEvent(
            1, "home", "fire", "alarm", "Fire", asset_id="base", zid="contact", critical=True
        ),
        cfg,
    )


@pytest.mark.parametrize("text", ["24:00", "01:60", "1:30", "12:2", "NaN", "０１:３０"])
def test_invalid_schedule_input(text):
    with pytest.raises(ValueError):
        rules.parse_time(text)


def test_schedule_normalization_bounds_and_independent_config_defaults():
    value = rules.normalize(
        {
            "quiet_enabled": "true",
            "quiet_start": True,
            "snoozed_until": float("nan"),
            "devices": {"bad": {}, "camera:42": {"enabled": "false"}},
        }
    )
    assert value["quiet_enabled"] is False and value["quiet_start"] == 1320
    assert value["snoozed_until"] == 0 and value["devices"] == {
        "camera:42": {"enabled": True, "motion": True, "ding": True, "snoozed_until": 0.0}
    }
    first, second = config.defaults(), config.defaults()
    first["notification_rules"]["devices"]["camera:1"] = {}
    assert second["notification_rules"]["devices"] == {}
    assert rules.parse_time(" 07:30 ") == 450
    assert rules.snooze_until(15, now=100) == 1000
    with pytest.raises(ValueError):
        rules.snooze_until(True)


def test_newly_snoozed_queued_event_never_enriches(monkeypatch):
    cfg = {"notification_rules": {"devices": {"camera:42": {"enabled": False}}}}
    monkeypatch.setattr(notifications._config, "load", lambda: cfg)
    monkeypatch.setattr(
        notifications, "_ring_notification_text", lambda *_: pytest.fail("Enriched muted event")
    )
    notifications._prepare_ring_notification(SimpleNamespace(kind="motion", doorbot_id=42), {})


def test_snooze_changed_after_enrichment_suppresses_main_thread_delivery(monkeypatch):
    from gi.repository import GLib

    cfg = {"notification_preview_images": False, "notification_summaries": False}
    callbacks = []
    monkeypatch.setattr(notifications._config, "load", lambda: cfg)
    monkeypatch.setattr(GLib, "idle_add", callbacks.append)
    monkeypatch.setattr(
        notifications, "send_notification", lambda *_a, **_k: pytest.fail("Delivered muted event")
    )
    notifications._prepare_ring_notification(SimpleNamespace(kind="motion", doorbot_id=42))
    cfg["notification_rules"] = {"devices": {"camera:42": {"enabled": False}}}
    assert len(callbacks) == 1 and callbacks[0]() is False


def test_preferences_save_failure_does_not_claim_success(monkeypatch):
    from halo_gtk.notification_preferences import NotificationPreferences

    dialog = NotificationPreferences.__new__(NotificationPreferences)
    dialog._original = rules.normalize({})
    dialog._devices = []
    dialog._reset_devices = False
    dialog._start = SimpleNamespace(get_text=lambda: "22:00")
    dialog._end = SimpleNamespace(get_text=lambda: "07:00")
    dialog._quiet = SimpleNamespace(get_active=lambda: True)
    dialog._snooze = SimpleNamespace(get_selected=lambda: 0)
    messages, closed = [], []
    dialog._message = SimpleNamespace(set_label=messages.append, set_visible=lambda _: None)
    dialog.close = lambda: closed.append(True)
    monkeypatch.setattr(config, "load", lambda: {})
    monkeypatch.setattr(config, "save", lambda _: (_ for _ in ()).throw(OSError("full")))
    dialog._save()
    assert not closed and "Could not save" in messages[0]
