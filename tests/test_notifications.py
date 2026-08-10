"""Tests for Ring desktop notification translation."""

import queue
from types import SimpleNamespace

from halo_gtk import notifications

DEFAULT_CFG = {
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
}


def test_send_ring_notification_respects_disabled_setting(monkeypatch):
    started = []
    monkeypatch.setattr(
        notifications._config,
        "load",
        lambda: {"show_notifications": False},
    )
    monkeypatch.setattr(
        notifications.threading,
        "Thread",
        lambda *args, **kwargs: SimpleNamespace(start=lambda: started.append(True)),
    )

    notifications.send_ring_notification(SimpleNamespace(kind="motion", device_name="Driveway"))

    assert started == []


def test_send_ring_notification_respects_disabled_event_type(monkeypatch):
    started = []
    cfg = {**DEFAULT_CFG, "notify_motion": False}
    monkeypatch.setattr(notifications._config, "load", lambda: cfg)
    monkeypatch.setattr(
        notifications.threading,
        "Thread",
        lambda *args, **kwargs: SimpleNamespace(start=lambda: started.append(True)),
    )

    notifications.send_ring_notification(SimpleNamespace(kind="motion", device_name="Driveway"))

    assert started == []


def test_ring_notification_text_uses_device_name_for_ding(monkeypatch):
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)

    text = notifications._ring_notification_text(
        SimpleNamespace(kind="ding", device_name="Front Door")
    )

    assert text == (
        "Doorbell: Front Door",
        "",
        "audio-input-microphone-symbolic",
    )


def test_ring_notification_text_uses_device_name_for_motion(monkeypatch):
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)

    text = notifications._ring_notification_text(
        SimpleNamespace(kind="motion", device_name="Garage")
    )

    assert text == (
        "Motion detected: Garage",
        "",
        "camera-video-symbolic",
    )


def test_ring_notification_text_uses_video_description_when_present(monkeypatch):
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)

    text = notifications._ring_notification_text(
        SimpleNamespace(kind="motion", device_name="Garage"),
        {"description": "A person walked up the driveway."},
    )

    assert text == (
        "Motion detected: Garage",
        "A person walked up the driveway.",
        "camera-video-symbolic",
    )


def test_ring_notification_text_falls_back_to_generic_device_name(monkeypatch):
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)

    text = notifications._ring_notification_text(SimpleNamespace(kind="alarm"))

    assert text == ("Alarm alert: Ring device", "alarm", "security-high-symbolic")


def test_custom_notification_message_overrides_default_body():
    text = notifications._ring_notification_text(
        SimpleNamespace(kind="ding", device_name="Front Door"),
        {"description": "Someone rang your doorbell."},
        {
            **DEFAULT_CFG,
            "custom_notification_messages": True,
            "custom_doorbell_message": "Custom doorbell body",
        },
    )

    assert text == (
        "Doorbell: Front Door",
        "Custom doorbell body",
        "audio-input-microphone-symbolic",
    )


def test_notification_urgency_marks_alarm_critical():
    assert notifications._notification_urgency("alarm") == "critical"
    assert notifications._notification_urgency("alarm_state") == "critical"
    assert notifications._notification_urgency("motion") == "normal"


def test_event_type_enabled_maps_supported_kinds():
    cfg = {**DEFAULT_CFG, "notify_doorbell": False, "notify_alarm": False}

    assert notifications._event_type_enabled("ding", cfg) is False
    assert notifications._event_type_enabled("motion", cfg) is True
    assert notifications._event_type_enabled("alarm_state", cfg) is False


def test_send_ring_notification_enqueues_when_enabled(monkeypatch):
    captured = []
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)
    monkeypatch.setattr(
        notifications, "_enqueue_notification", lambda event, cfg: captured.append(event)
    )

    notifications.send_ring_notification(SimpleNamespace(kind="motion", device_name="Driveway"))

    assert len(captured) == 1


def test_full_notification_queue_uses_basic_path_without_blocking(monkeypatch):
    work_queue = queue.Queue(maxsize=1)
    work_queue.put((object(), DEFAULT_CFG))
    calls = []
    monkeypatch.setattr(notifications, "_notify_queue", work_queue)
    monkeypatch.setattr(
        notifications,
        "_prepare_ring_notification",
        lambda event, cfg, *, allow_enrichment=True: calls.append((event, cfg, allow_enrichment)),
    )
    event = SimpleNamespace(kind="motion", device_name="Driveway")

    notifications._enqueue_notification(event, DEFAULT_CFG)

    assert calls == [(event, DEFAULT_CFG, False)]
    assert work_queue.qsize() == 1


def test_notification_target_handles_bad_metadata():
    # Non-numeric ids must not crash the worker — they yield no target.
    assert (
        notifications._notification_target(
            SimpleNamespace(doorbot_id="not-an-int", id=1, now=0, expires_in=0)
        )
        is None
    )
    assert notifications._notification_target(SimpleNamespace(doorbot_id=None)) is None

    target = notifications._notification_target(
        SimpleNamespace(doorbot_id=12, id=34, now=100.0, expires_in=30.0)
    )
    assert target is not None
    assert target.device_id == 12
    assert target.event_id == 34
    assert target.expires_at == 130.0
