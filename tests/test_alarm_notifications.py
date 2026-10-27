from dataclasses import replace
from types import SimpleNamespace

from halo_gtk import config, notifications
from halo_gtk.alarm.events import AlarmEvent


def event(**kwargs):
    return AlarmEvent(
        1,
        "private-id",
        "active-alarm",
        "alarm",
        "Ring reports an alarm",
        location_name="Private home",
        device_name="Bedroom",
        critical=True,
        **kwargs,
    )


def test_defaults_enable_critical_without_exposing_names_or_routine_activity():
    cfg = config.defaults()
    assert notifications.alarm_event_enabled(event(), cfg)
    for category in ("mode", "sensor", "health"):
        assert not notifications.alarm_event_enabled(
            replace(event(), category=category, critical=False), cfg
        )
    assert not cfg["alarm_notification_details"]


def test_alarm_rendering_uses_generic_text_until_details_opted_in(monkeypatch):
    calls = []
    cfg = config.defaults()
    monkeypatch.setattr(notifications._config, "load", lambda: cfg)
    monkeypatch.setattr(notifications, "send_notification", lambda *a, **k: calls.append((a, k)))
    notifications.send_alarm_notification(event())
    assert "Private" not in str(calls[0][0]) and "Bedroom" not in str(calls[0][0])
    assert calls[0][1]["urgency"] == "critical"
    assert isinstance(calls[0][1]["target"], notifications.AlarmNotificationTarget)
    cfg["alarm_notification_details"] = True
    notifications.send_alarm_notification(event())
    assert "Private home" in calls[-1][0][1]


def test_notification_body_escapes_only_its_markup_boundary(monkeypatch):
    calls = []
    cfg = {**config.defaults(), "alarm_notification_details": True}
    monkeypatch.setattr(notifications._config, "load", lambda: cfg)
    monkeypatch.setattr(notifications, "send_notification", lambda *a, **k: calls.append((a, k)))
    notifications.send_alarm_notification(replace(event(), location_name="<b>Home</b>"))
    assert "&lt;b&gt;Home&lt;/b&gt;" in calls[0][0][1]


def test_custom_alarm_text_requires_detail_opt_in_and_stays_literal(monkeypatch):
    calls = []
    cfg = {
        **config.defaults(),
        "custom_notification_messages": True,
        "custom_alarm_message": "Private <door> {camera_name}",
    }
    monkeypatch.setattr(notifications._config, "load", lambda: cfg)
    monkeypatch.setattr(notifications, "send_notification", lambda *a, **k: calls.append(a))
    notifications.send_alarm_notification(event())
    assert "Private" not in calls[-1][1]
    cfg["alarm_notification_details"] = True
    notifications.send_alarm_notification(event())
    assert "Private &lt;door&gt; {camera_name}" in calls[-1][1]
    cfg["custom_notification_messages"] = False
    notifications.send_alarm_notification(event())
    assert "{camera_name}" not in calls[-1][1]


def test_master_switches_gate_critical_and_routine(monkeypatch):
    for key in ("show_notifications", "notify_alarm"):
        cfg = {**config.defaults(), key: False}
        assert not notifications.alarm_event_enabled(event(), cfg)
    cfg = {**config.defaults(), "notify_alarm_mode": True}
    assert notifications.alarm_event_enabled(replace(event(), category="mode", critical=False), cfg)


def test_old_fcm_alarm_prefix_cannot_render_critical_or_duplicate(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications._config, "load", config.defaults)
    monkeypatch.setattr(notifications, "_enqueue_notification", lambda *a: calls.append(a))
    monkeypatch.setattr(notifications, "send_notification", lambda *a, **k: calls.append(a))
    for kind in ("alarm_mode_none", "alarm_mode_some", "alarm_silenced", "alarm_new_unknown"):
        value = SimpleNamespace(kind=kind)
        notifications.send_ring_notification(value)
        notifications._prepare_ring_notification(value)
    assert calls == []


def test_withdrawal_is_generation_and_location_scoped():
    closed = []
    first = SimpleNamespace(close=lambda: closed.append("first"))
    second = SimpleNamespace(close=lambda: closed.append("second"))
    notifications._alarm_notifications[:] = [
        (first, notifications.AlarmNotificationTarget(1, "home")),
        (second, notifications.AlarmNotificationTarget(2, "home")),
    ]
    notifications._active_notifications.extend((first, second))
    try:
        notifications.withdraw_alarm_notifications(generation=1, location_id="home")
        assert closed == ["first"]
        assert notifications._alarm_notifications == [
            (second, notifications.AlarmNotificationTarget(2, "home"))
        ]
    finally:
        notifications._drop_notification(second)
