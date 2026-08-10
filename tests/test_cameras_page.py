"""Tests for camera page ordering helpers."""

import io
from types import SimpleNamespace

from PIL import Image
from ring_doorbell.const import PUSH_ACTION_DING

from halo_gtk.cameras_page import (
    CamerasPage,
    _apply_preview_blur,
    _motion_off_base_font_size,
    _preview_blur_radius,
    _reorder_ids,
)


def test_reorder_ids_moves_source_before_destination():
    assert _reorder_ids([1, 2, 3], {1, 2, 3}, 3, 1) == [3, 1, 2]


def test_reorder_ids_ignores_missing_source_or_destination():
    assert _reorder_ids([1, 2, 3], {1, 2, 3}, 4, 1) == [1, 2, 3]
    assert _reorder_ids([1, 2, 3], {1, 2, 3}, 3, 4) == [1, 2, 3]


def test_reorder_ids_filters_unavailable_devices():
    assert _reorder_ids([1, 2, 3, 4], {1, 3, 4}, 4, 1) == [4, 1, 3]


def test_preview_blur_returns_valid_png():
    image = Image.new("RGB", (64, 36), color=(200, 20, 20))
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    blurred = _apply_preview_blur(buf.getvalue())

    with Image.open(io.BytesIO(blurred)) as result:
        assert result.size == (64, 36)
        assert result.format == "PNG"


def test_preview_blur_radius_scales_with_snapshot_size():
    assert _preview_blur_radius(640, 360) == 9.0
    assert _preview_blur_radius(1280, 720) == 18.0


def test_motion_off_font_size_scales_with_snapshot_size():
    assert _motion_off_base_font_size(640, 360) == 65
    assert _motion_off_base_font_size(1280, 720) == 130


def test_live_monitoring_start_requires_selection_within_max():
    page = CamerasPage.__new__(CamerasPage)
    page._show_monitoring_controls = True
    page._current_visible_ids = lambda: [1, 2, 3, 4, 5]
    page._max_live_monitoring_streams = lambda: 4

    assert page._can_start_live_monitoring() is False

    page._current_visible_ids = lambda: [1, 2, 3, 4]
    assert page._can_start_live_monitoring() is True


def test_live_monitoring_stream_gate_denies_above_max(monkeypatch):
    from halo_gtk import cameras_page

    class FakeManager:
        def __init__(self):
            self.sessions = {
                device_id: SimpleNamespace(active=True, owners={"live-monitoring"})
                for device_id in [1, 2, 3, 4]
            }

        def session_for(self, device_id):
            return self.sessions.get(device_id)

        def active_count(self):
            return 4

    page = CamerasPage.__new__(CamerasPage)
    page._active_monitoring_stream_ids = {1, 2, 3, 4}
    page._cards = {device_id: object() for device_id in [1, 2, 3, 4, 5]}
    page._max_live_monitoring_streams = lambda: 4
    monkeypatch.setattr(cameras_page, "get_live_session_manager", FakeManager)

    assert page._reserve_live_monitoring_stream(2) is True
    assert page._reserve_live_monitoring_stream(5) is False


def test_live_monitoring_stream_gate_drops_failed_manager_session(monkeypatch):
    from halo_gtk import cameras_page

    class FakeManager:
        def __init__(self):
            self.sessions = {
                1: SimpleNamespace(active=False, owners={"live-monitoring"}),
                2: SimpleNamespace(active=True, owners={"live-monitoring"}),
            }

        def session_for(self, device_id):
            return self.sessions.get(device_id)

        def active_count(self):
            return sum(session.active for session in self.sessions.values())

    page = CamerasPage.__new__(CamerasPage)
    page._active_monitoring_stream_ids = {1, 2}
    page._cards = {device_id: object() for device_id in [1, 2, 3]}
    page._max_live_monitoring_streams = lambda: 2
    monkeypatch.setattr(cameras_page, "get_live_session_manager", FakeManager)

    assert page._reserve_live_monitoring_stream(3) is True
    assert page._active_monitoring_stream_ids == {2, 3}


def test_live_monitoring_start_volume_follows_config(monkeypatch):
    from halo_gtk import config

    page = CamerasPage.__new__(CamerasPage)

    monkeypatch.setattr(config, "load", lambda: {"live_monitoring_unmute_on_start": False})
    assert page._live_monitoring_start_volume() == 0.0

    monkeypatch.setattr(config, "load", lambda: {"live_monitoring_unmute_on_start": True})
    assert page._live_monitoring_start_volume() == 1.0


def test_event_refresh_prefers_event_preview(monkeypatch):
    from halo_gtk import cameras_page

    class FakeClient:
        def __init__(self):
            self.event_preview_calls = 0
            self.snapshot_calls = 0

        def event_preview_for_device(self, _device, _event):
            self.event_preview_calls += 1
            return b"event-preview"

        def snapshot_for_device(self, _device):
            self.snapshot_calls += 1
            return b"snapshot"

    client = FakeClient()
    page = CamerasPage.__new__(CamerasPage)
    page._show_monitoring_controls = False
    captured = {}
    page._set_card_snapshot = lambda device_id, data, motion_off=False, generation=None: (
        captured.update({"device_id": device_id, "data": data, "motion_off": motion_off})
    )
    device = SimpleNamespace(id=12, name="Front Door", motion_detection=True)
    event = SimpleNamespace(id=34)

    monkeypatch.setattr(cameras_page, "get_client", lambda: client)
    monkeypatch.setattr(cameras_page.GLib, "idle_add", lambda callback, *args: callback(*args))

    page._load_snapshot(device, event)

    assert client.event_preview_calls == 1
    assert client.snapshot_calls == 0
    assert captured == {"device_id": 12, "data": b"event-preview", "motion_off": False}


def test_stale_snapshot_generation_does_not_update_cache():
    page = CamerasPage.__new__(CamerasPage)
    page._snapshot_generation = 2
    page._snapshot_cache = {}
    page._cards = {}

    page._set_card_snapshot(12, b"old", False, generation=1)
    assert page._snapshot_cache == {}

    page._set_card_snapshot(12, b"new", False, generation=2)
    assert page._snapshot_cache == {12: b"new"}


def test_stale_snapshot_finish_does_not_clear_current_inflight():
    page = CamerasPage.__new__(CamerasPage)
    page._snapshot_generation = 2
    page._snapshot_inflight_generations = {12: 2}
    page._snapshot_pending_events = {}
    page._snapshot_restart_timer_ids = set()

    page._finish_snapshot_load(12, generation=1)

    assert page._snapshot_inflight_generations == {12: 2}


def test_stale_camera_fetch_error_is_ignored():
    page = CamerasPage.__new__(CamerasPage)
    page._devices_request_generation = 2

    assert page._show_fetch_error("stale", generation=1) is False


def test_snapshot_updates_switch_event_client_without_duplicate_subscription():
    class FakeClient:
        def __init__(self):
            self.added = []
            self.removed = []

        def add_event_callback(self, callback):
            self.added.append(callback)

        def remove_event_callback(self, callback):
            self.removed.append(callback)

    page = CamerasPage.__new__(CamerasPage)
    page._snapshot_updates_active = False
    page._event_client = None
    first = FakeClient()
    second = FakeClient()

    page._activate_snapshot_updates(first)
    page._activate_snapshot_updates(first)
    page._activate_snapshot_updates(second)

    assert first.added == [page._on_ring_event]
    assert first.removed == [page._on_ring_event]
    assert second.added == [page._on_ring_event]
    assert page._event_client is second
    assert page._snapshot_updates_active is True


def test_deactivate_snapshot_updates_cancels_hidden_page_work(monkeypatch):
    from halo_gtk import cameras_page

    class FakeClient:
        def __init__(self):
            self.removed = []

        def remove_event_callback(self, callback):
            self.removed.append(callback)

    client = FakeClient()
    page = CamerasPage.__new__(CamerasPage)
    page._snapshot_updates_active = True
    page._event_client = client
    page._devices_request_generation = 3
    page._snapshot_generation = 7
    page._refresh_timers = {1: 41, 2: 42}
    page._snapshot_inflight_generations = {1: 7}
    page._snapshot_pending_events = {1: object()}
    page._snapshot_restart_timer_ids = {1}
    removed_sources = []
    monkeypatch.setattr(cameras_page.GLib, "source_remove", removed_sources.append)

    page.deactivate_snapshot_updates()

    assert client.removed == [page._on_ring_event]
    assert removed_sources == [41, 42]
    assert page._event_client is None
    assert page._snapshot_updates_active is False
    assert page._devices_request_generation == 4
    assert page._snapshot_generation == 8
    assert page._refresh_timers == {}
    assert page._snapshot_inflight_generations == {}
    assert page._snapshot_pending_events == {}
    assert page._snapshot_restart_timer_ids == set()


def test_legacy_doorbell_push_refreshes_matching_camera_snapshot():
    device = SimpleNamespace(id=12, name="Front Door")
    page = CamerasPage.__new__(CamerasPage)
    page._snapshot_updates_active = True
    page._cards = {12: SimpleNamespace(device=device)}
    cancelled = []
    queued = []
    page._cancel_refresh_timer = cancelled.append
    page._queue_snapshot_load = lambda queued_device, event, **kwargs: queued.append(
        (queued_device, event, kwargs)
    )
    event = SimpleNamespace(kind=PUSH_ACTION_DING, doorbot_id="12")

    page._on_ring_event(event)

    assert cancelled == [12]
    assert queued == [(device, event, {"restart_timer": True})]
