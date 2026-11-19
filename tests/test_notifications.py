"""Tests for Ring desktop notification translation."""

import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from halo_gtk import notifications, ring_client
from halo_gtk.camera_privacy import CameraPrivacy, ring_privacy_receipt

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


@pytest.fixture
def camera_event(monkeypatch):
    device = SimpleNamespace(
        id=12,
        _attrs={"settings": {"live_view_disabled": False, "motion_detection_enabled": True}},
    )
    client = ring_client.RingClient()
    client._ring = SimpleNamespace(devices=lambda: SimpleNamespace(all_devices=[device]))
    client._authenticated = True
    client._published_generation = 7
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 7)
    clock = [50.0]
    client.camera_privacy = CameraPrivacy(clock=lambda: clock[0])
    client.camera_privacy.observe(ring_privacy_receipt([device], clock[0]))
    event = SimpleNamespace(
        kind="motion", device_name="Garage", doorbot_id=12, id=34, now=100, expires_in=30
    )
    return client, device, event, clock


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

    notifications.send_ring_notification(
        SimpleNamespace(kind="motion", device_name="Driveway"), object()
    )

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

    notifications.send_ring_notification(
        SimpleNamespace(kind="motion", device_name="Driveway"), object()
    )

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


def test_send_ring_notification_enqueues_when_enabled(monkeypatch, camera_event):
    client, _, event, _ = camera_event
    target = notifications.camera_notification_target(event, client, 7)
    captured = []
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)
    monkeypatch.setattr(notifications, "_enqueue_notification", lambda *args: captured.append(args))

    notifications.send_ring_notification(event, target)

    assert captured == [(event, target)]


def test_full_notification_queue_uses_basic_path_without_blocking(monkeypatch, camera_event):
    client, _, event, _ = camera_event
    target = notifications.camera_notification_target(event, client, 7)
    work_queue = queue.Queue(maxsize=1)
    work_queue.put((event, target))
    calls = []
    monkeypatch.setattr(notifications, "_notify_queue", work_queue)
    monkeypatch.setattr(
        notifications,
        "_prepare_ring_notification",
        lambda event, target, *, allow_enrichment=True: calls.append(
            (event, target, allow_enrichment)
        ),
    )

    notifications._enqueue_notification(event, target)

    assert calls == [(event, target, False)]
    assert work_queue.qsize() == 1


@pytest.mark.parametrize(
    "meta",
    [
        {"doorbot_id": "bad"},
        {"doorbot_id": None},
        {"doorbot_id": float("inf")},
        {"doorbot_id": 12, "now": float("nan")},
    ],
)
def test_bad_metadata_yields_account_bound_actionless_alert(camera_event, meta):
    client, _, _, _ = camera_event
    target = notifications.camera_notification_target(SimpleNamespace(**meta), client, 7)
    assert target.client is client and target.generation == 7
    assert target.device_id is None and target.preview_lease is None


def test_notification_target_captures_original_grant(camera_event):
    client, device, event, _ = camera_event
    target = notifications.camera_notification_target(event, client, 7)
    assert target.client is client and target.preview_lease.device is device
    assert client.camera_privacy.valid(target.preview_lease)
    assert target.device_id == 12 and target.event_id == 34 and target.expires_at == 130


def replace_account(monkeypatch, client):
    replacement = ring_client.RingClient()
    replacement._ring = client._ring  # Deliberately reuse camera and event identifiers.
    replacement._authenticated = True
    replacement._published_generation = 8
    monkeypatch.setattr(ring_client, "_client", replacement)
    monkeypatch.setattr(ring_client, "_active_client_generation", 8)
    monkeypatch.setattr(
        replacement, "get_event_notification_context", Mock(side_effect=AssertionError)
    )
    return replacement


@pytest.mark.parametrize("retirement", ["logout", "replacement", "privacy", "expiry"])
@pytest.mark.parametrize("stage", ["worker", "delivery"])
def test_retirement_blocks_queued_camera_notification(monkeypatch, camera_event, retirement, stage):
    from gi.repository import GLib

    client, device, event, clock = camera_event
    pending = []
    enriched = Mock(return_value={"description": "household detail", "image_bytes": b"private"})
    delivered = Mock()
    monkeypatch.setattr(client, "get_event_notification_context", enriched)
    monkeypatch.setattr(notifications, "send_notification", delivered)
    monkeypatch.setattr(notifications, "_pixbuf_from_bytes", lambda _: object())
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)
    monkeypatch.setattr(GLib, "idle_add", pending.append)
    target = notifications.camera_notification_target(event, client, 7)
    if stage == "delivery":
        notifications._prepare_ring_notification(event, target)
        enriched.assert_called_once()

    if retirement == "logout":
        monkeypatch.setattr(ring_client, "_client", None)
    elif retirement == "replacement":
        replacement = replace_account(monkeypatch, client)
    elif retirement == "privacy":
        client.camera_privacy.begin_change(device)
    else:
        clock[0] += 121
    if stage == "worker":
        notifications._prepare_ring_notification(event, target)
        enriched.assert_not_called()
    for callback in pending:
        assert callback() is False
    delivered.assert_not_called()
    if retirement == "replacement":
        replacement.get_event_notification_context.assert_not_called()


@pytest.mark.parametrize("previews,summaries", [(True, False), (False, True), (False, False)])
def test_display_reapplies_current_image_summary_and_custom_preferences(
    monkeypatch, camera_event, previews, summaries
):
    from gi.repository import GLib

    client, _, event, _ = camera_event
    target = notifications.camera_notification_target(event, client, 7)
    cfg = {
        **DEFAULT_CFG,
        "custom_notification_messages": True,
        "custom_motion_message": "old message",
    }
    monkeypatch.setattr(notifications._config, "load", lambda: cfg)
    monkeypatch.setattr(
        client,
        "get_event_notification_context",
        lambda *_args, **_kwargs: {"description": "household detail", "image_bytes": b"private"},
    )
    monkeypatch.setattr(notifications, "_pixbuf_from_bytes", lambda _: "decoded")
    delivered, pending = Mock(), []
    monkeypatch.setattr(notifications, "send_notification", delivered)
    monkeypatch.setattr(GLib, "idle_add", pending.append)
    notifications._prepare_ring_notification(event, target)
    cfg.update(
        notification_summaries=summaries,
        notification_preview_images=previews,
        custom_notification_messages=False,
    )
    assert pending.pop()() is False
    assert delivered.call_args.args[1] == ("household detail" if summaries else "")
    assert delivered.call_args.kwargs["image_pixbuf"] == ("decoded" if previews else None)


def test_denial_and_restore_before_gtk_dispatch_never_enriches_old_event(monkeypatch, camera_event):
    from gi.repository import GLib

    client, device, event, clock = camera_event
    pending, queued = [], []
    monkeypatch.setattr(GLib, "idle_add", lambda callback, *args: pending.append((callback, args)))
    monkeypatch.setattr(notifications, "_enqueue_notification", lambda *args: queued.append(args))
    monkeypatch.setattr(notifications._config, "load", lambda: DEFAULT_CFG)
    enriched = Mock()
    monkeypatch.setattr(client, "get_event_notification_context", enriched)
    client._on_ring_event(event)
    token = client.camera_privacy.begin_change(device)
    client.camera_privacy.end_change(token)
    client.camera_privacy.observe(ring_privacy_receipt([device], clock[0]))
    dispatch, args = pending.pop()
    dispatch(*args)
    notifications._prepare_ring_notification(*queued.pop())
    assert pending == []
    enriched.assert_not_called()


@pytest.mark.parametrize("retirement", ["privacy", "expiry", "replacement", "logout"])
def test_visible_camera_notification_withdrawn_and_queued_action_inert(
    monkeypatch, camera_event, retirement
):
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    client, device, event, clock = camera_event
    target = notifications.camera_notification_target(event, client, 7)
    pending, ticks = [], []
    monkeypatch.setattr(
        GLib, "idle_add", lambda callback, *args: pending.append((callback, args)) or 1
    )
    monkeypatch.setattr(
        GLib, "timeout_add_seconds", lambda seconds, callback: ticks.append(callback) or 99
    )
    monkeypatch.setattr(GLib, "source_remove", lambda _: None)
    app = Mock()
    monkeypatch.setattr(Gtk.Application, "get_default", lambda: app)
    shown = Mock()
    notifications._active_notifications.append(shown)
    notifications._camera_notifications.add(shown, target)
    notifications._open_notification_target(target)
    try:
        if retirement == "privacy":
            client.camera_privacy.begin_change(device)
        elif retirement == "expiry":
            clock[0] += 121
        elif retirement == "replacement":
            replace_account(monkeypatch, client)
            client.camera_privacy.invalidate()
        else:
            monkeypatch.setattr(ring_client, "_client", None)
            client.camera_privacy.invalidate()
        if retirement == "expiry":
            ticks[0]()
        for callback, args in pending:
            callback(*args)
        shown.close.assert_called_once()
        assert not notifications._camera_notifications.visible
        assert notifications._camera_notifications.watch is None
        assert shown not in notifications._active_notifications
        app.activate.assert_not_called()
    finally:
        notifications._drop_notification(shown)


@pytest.mark.asyncio
async def test_enrichment_respects_grant_across_history_wait_and_preferences(
    monkeypatch, camera_event
):
    client, device, event, clock = camera_event
    target = notifications.camera_notification_target(event, client, 7)
    get_image = AsyncMock(return_value=b"preview")
    monkeypatch.setattr(client, "_async_event_preview_bytes", get_image)

    async def get_history(_device, _id):
        return {"id": 34, "description": "private text"}

    monkeypatch.setattr(client, "_async_find_history_event", get_history)
    assert await client._async_event_notification_context(
        event, target.preview_lease, include_image=False, include_summary=True
    ) == {"description": "private text", "image_bytes": None}
    get_image.assert_not_called()

    async def revoke_then_restore(_device, _id):
        token = client.camera_privacy.begin_change(device)
        client.camera_privacy.end_change(token)
        client.camera_privacy.observe(ring_privacy_receipt([device], clock[0]))
        return {"id": 34, "description": "private text"}

    monkeypatch.setattr(client, "_async_find_history_event", revoke_then_restore)
    assert (
        await client._async_event_notification_context(
            event, target.preview_lease, include_image=True, include_summary=True
        )
        == {}
    )
    get_image.assert_not_called()


def test_actionless_basic_alert_registered_and_withdrawn_on_shutdown(monkeypatch, camera_event):
    import gi

    gi.require_version("Notify", "0.7")
    from gi.repository import GLib, Notify

    client, _, _, _ = camera_event
    target = notifications.camera_notification_target(SimpleNamespace(), client, 7)
    broker_notification = Mock()
    monkeypatch.setattr(GLib, "timeout_add_seconds", lambda *_: 99)
    monkeypatch.setattr(GLib, "source_remove", lambda _: None)
    monkeypatch.setattr(notifications, "_ensure_notify", lambda: True)
    monkeypatch.setattr(Notify.Notification, "new", lambda *_: broker_notification)
    notifications.send_notification("basic", target=target)
    broker_notification.show.assert_called_once()
    broker_notification.add_action.assert_not_called()
    notifications.withdraw_camera_notifications()
    broker_notification.close.assert_called_once()
    assert not notifications._camera_notifications.visible


@pytest.fixture
def displayed_banners(monkeypatch, camera_event):
    """Show four camera events and an Alarm through a fake desktop broker."""
    import gi

    gi.require_version("Notify", "0.7")
    from gi.repository import GLib, Notify

    client, _, event, _ = camera_event
    cfg, handles = dict(DEFAULT_CFG), []
    monkeypatch.setattr(notifications._config, "load", lambda: cfg.copy())
    monkeypatch.setattr(notifications._config, "save", cfg.update)
    monkeypatch.setattr(notifications, "_ensure_notify", lambda: True)
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *args: cb(*args))
    monkeypatch.setattr(GLib, "timeout_add_seconds", lambda *_: 99)
    monkeypatch.setattr(GLib, "source_remove", lambda _: None)
    monkeypatch.setattr(notifications, "_pixbuf_from_bytes", lambda _: object())

    def new(*_):
        handle = Mock()
        handles.append(handle)
        return handle

    monkeypatch.setattr(Notify.Notification, "new", new)
    for camera, kind, context, custom in (
        (12, "motion", {"image_bytes": b"picture"}, False),
        (12, "ding", {"description": "Ring description"}, False),
        (12, "motion", {"description": "Hidden by custom text"}, True),
        (13, "motion", {}, False),
    ):
        event = SimpleNamespace(**{**vars(event), "doorbot_id": camera, "kind": kind})
        cfg.update(custom_notification_messages=custom, custom_motion_message="Local message")
        monkeypatch.setattr(client, "get_event_notification_context", Mock(return_value=context))
        target = notifications.camera_notification_target(event, client, 7)
        notifications._prepare_ring_notification(event, target)
    notifications.send_notification(
        "Alarm", target=notifications.AlarmNotificationTarget(7, "here")
    )
    assert len(handles) == 5
    for handle in handles:
        handle.show.assert_called_once()
    try:
        yield dict(zip(("image", "summary", "custom", "basic", "alarm"), handles, strict=True))
    finally:
        notifications.withdraw_camera_notifications()
        notifications.withdraw_alarm_notifications()


@pytest.mark.parametrize(
    "key,withdrawn",
    [
        ("notification_preview_images", {"image"}),
        ("notification_summaries", {"summary"}),
        ("notify_doorbell", {"summary"}),
        ("notify_motion", {"image", "custom", "basic"}),
        ("show_notifications", {"image", "summary", "custom", "basic"}),
    ],
)
def test_settings_save_withdraws_only_affected_visible_banners(
    monkeypatch, displayed_banners, key, withdrawn
):
    from halo_gtk.settings_page import Gtk, SettingsPage

    page = SettingsPage.__new__(SettingsPage)
    page._refreshing = False
    page.refresh = Mock()
    switch = SimpleNamespace(get_active=lambda: False)
    if key == "show_notifications":
        page._on_notifications_toggled(switch, None)
    else:
        page._save_switch_setting(switch, None, config_key=key)
    assert {name for name, handle in displayed_banners.items() if handle.close.called} == withdrawn
    app = Mock()
    monkeypatch.setattr(Gtk.Application, "get_default", lambda: app)
    for name in withdrawn:
        target = displayed_banners[name].add_action.call_args.args[-1]
        notifications._open_notification_target_on_main(target)
    app.activate.assert_not_called()


@pytest.mark.parametrize("save_succeeds", [True, False])
def test_rule_dialog_applies_visible_camera_rule_after_save(
    monkeypatch, displayed_banners, save_succeeds
):
    from halo_gtk import notification_rules
    from halo_gtk.notification_preferences import NotificationPreferences

    dialog = NotificationPreferences.__new__(NotificationPreferences)
    dialog._original = notification_rules.normalize({})
    on, off = (SimpleNamespace(get_active=lambda value=value: value) for value in (True, False))
    keep = SimpleNamespace(get_selected=lambda: 0)
    dialog._devices = [("camera:12", on, off, on, keep)]
    dialog._reset_devices = False
    dialog._start = SimpleNamespace(get_text=lambda: "22:00")
    dialog._end = SimpleNamespace(get_text=lambda: "07:00")
    dialog._quiet = off
    dialog._snooze = keep
    dialog._message = Mock()
    dialog.close = Mock()
    if not save_succeeds:
        monkeypatch.setattr(notifications._config, "save", Mock(side_effect=OSError("full")))
    dialog._save()
    withdrawn = {name for name, handle in displayed_banners.items() if handle.close.called}
    assert withdrawn == ({"image", "custom"} if save_succeeds else set())
    assert dialog.close.called is save_succeeds
