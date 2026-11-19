"""Tests for shared live stream session ownership."""

from types import SimpleNamespace

import pytest


class FakeView:
    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.volume = None
        self._on_state_changed = None

    def start_for_device(self, _device):
        self.starts += 1

    def stop(self):
        self.stops += 1

    def set_volume(self, value):
        self.volume = value

    def set_on_stream_state_changed(self, callback):
        self._on_state_changed = callback

    def connect_stream(self):
        if self._on_state_changed is not None:
            self._on_state_changed("active")

    def fail_stream(self):
        if self._on_state_changed is not None:
            self._on_state_changed("failed")

    def end_stream(self):
        if self._on_state_changed is not None:
            self._on_state_changed("stopped")

    def get_parent(self):
        return None


def test_manager_reuses_existing_session_without_restarting():
    from halo_gtk.live_sessions import LiveSessionManager

    views = []

    def make_view():
        view = FakeView()
        views.append(view)
        return view

    manager = LiveSessionManager(view_factory=make_view, max_streams_factory=lambda: 4)
    device = SimpleNamespace(id=10, name="Door")

    first = manager.acquire(device, owner="live-monitoring", volume=0.0)
    second = manager.acquire(device, owner="focused-live", volume=1.0)

    assert first is second
    assert len(views) == 1
    assert views[0].starts == 1
    assert views[0].volume == 1.0
    assert first.owners == {"live-monitoring", "focused-live"}


def test_connecting_stream_occupies_cap_until_it_fails():
    from halo_gtk.live_sessions import LiveSessionManager, StreamLimitExceeded, StreamState

    manager = LiveSessionManager(view_factory=FakeView, max_streams_factory=lambda: 1)
    session = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")

    assert session.state is StreamState.CONNECTING
    assert session.active is True
    assert manager.active_count() == 1
    with pytest.raises(StreamLimitExceeded):
        manager.acquire(SimpleNamespace(id=2), owner="live-monitoring")

    session.view.fail_stream()
    assert session.state is StreamState.FAILED
    assert session.active is False
    assert manager.active_count() == 0


def test_failed_stream_restarts_when_reacquired():
    from halo_gtk.live_sessions import LiveSessionManager, StreamState

    manager = LiveSessionManager(view_factory=FakeView, max_streams_factory=lambda: 1)
    session = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")
    session.view.fail_stream()

    restarted = manager.acquire(session.device, owner="live-monitoring")

    assert restarted is session
    assert restarted.state is StreamState.CONNECTING
    assert restarted.view.starts == 2
    restarted.view.connect_stream()
    assert restarted.state is StreamState.ACTIVE


def test_manager_enforces_stream_limit_for_new_sessions():
    from halo_gtk.live_sessions import LiveSessionManager, StreamLimitExceeded

    manager = LiveSessionManager(view_factory=FakeView, max_streams_factory=lambda: 1)
    manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")

    try:
        manager.acquire(SimpleNamespace(id=2), owner="live-monitoring")
    except StreamLimitExceeded:
        pass
    else:
        raise AssertionError("expected StreamLimitExceeded")


def test_release_only_stops_when_last_owner_is_removed():
    from halo_gtk.live_sessions import LiveSessionManager

    manager = LiveSessionManager(view_factory=FakeView, max_streams_factory=lambda: 4)
    device = SimpleNamespace(id=10, name="Door")
    session = manager.acquire(device, owner="live-monitoring")
    manager.acquire(device, owner="focused-live")

    manager.release(10, owner="focused-live")
    assert session.active is True
    assert session.view.stops == 0

    manager.release(10, owner="live-monitoring")
    assert session.active is False
    assert session.view.stops == 1
    assert manager.session_for(10) is None


def test_enforce_limit_stops_all_sessions_when_over_cap():
    from halo_gtk.live_sessions import LiveSessionManager

    manager = LiveSessionManager(view_factory=FakeView, max_streams_factory=lambda: 4)
    first = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")
    second = manager.acquire(SimpleNamespace(id=2), owner="live-monitoring")

    assert manager.enforce_limit(1) is False
    assert first.view.stops == 1
    assert second.view.stops == 1
    assert manager.active_count() == 0


def test_natural_stream_end_frees_the_cap_slot():
    from halo_gtk.live_sessions import LiveSessionManager, StreamLimitExceeded

    manager = LiveSessionManager(view_factory=FakeView, max_streams_factory=lambda: 1)
    session = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")
    assert manager.active_count() == 1

    # The stream dies on its own (camera drop / keepalive timeout) — frees the slot.
    session.view.end_stream()
    assert session.active is False
    assert manager.active_count() == 0

    # The freed slot lets a different camera start.
    other = manager.acquire(SimpleNamespace(id=2), owner="live-monitoring")
    assert other.active is True
    assert manager.active_count() == 1

    # Re-acquiring the dead camera now respects the cap (full again).
    try:
        manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")
    except StreamLimitExceeded:
        pass
    else:
        raise AssertionError("expected StreamLimitExceeded")


def test_ended_session_restarts_when_reacquired_within_cap():
    from halo_gtk.live_sessions import LiveSessionManager

    manager = LiveSessionManager(view_factory=FakeView, max_streams_factory=lambda: 4)
    session = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")
    session.view.end_stream()
    assert session.active is False

    again = manager.acquire(session.device, owner="live-monitoring")
    assert again is session
    assert session.active is True
    assert session.view.starts == 2


def test_stale_view_callback_does_not_mutate_replacement_session():
    from halo_gtk.live_sessions import LiveSessionManager, StreamState

    views = []

    def make_view():
        view = FakeView()
        views.append(view)
        return view

    manager = LiveSessionManager(view_factory=make_view, max_streams_factory=lambda: 1)
    first = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")
    manager.release(1, owner="live-monitoring")
    replacement = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")

    first.view.fail_stream()

    assert replacement is not first
    assert replacement.state is StreamState.CONNECTING


def test_stop_all_continues_after_one_view_fails_to_stop():
    from halo_gtk.live_sessions import LiveSessionManager

    class FailingView(FakeView):
        def stop(self):
            super().stop()
            raise RuntimeError("pipeline failed")

    views = iter((FailingView(), FakeView()))
    manager = LiveSessionManager(view_factory=lambda: next(views))
    first = manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")
    second = manager.acquire(SimpleNamespace(id=2), owner="live-monitoring")

    manager.stop_all()

    assert first.view.stops == second.view.stops == 1
    assert manager.session_for(1) is None
    assert manager.session_for(2) is None


def test_failed_release_does_not_leave_a_registered_session():
    from halo_gtk.live_sessions import LiveSessionManager

    class FailingView(FakeView):
        def stop(self):
            raise RuntimeError("pipeline failed")

    manager = LiveSessionManager(view_factory=FailingView)
    manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")

    with pytest.raises(RuntimeError, match="pipeline failed"):
        manager.release(1, owner="live-monitoring")

    assert manager.session_for(1) is None


def test_startup_error_is_preserved_when_partial_cleanup_also_fails():
    from halo_gtk.live_sessions import LiveSessionManager

    class FailingView(FakeView):
        def start_for_device(self, _device):
            raise ValueError("stream could not start")

        def stop(self):
            raise RuntimeError("pipeline could not stop")

    manager = LiveSessionManager(view_factory=FailingView)

    with pytest.raises(ValueError, match="stream could not start"):
        manager.acquire(SimpleNamespace(id=1), owner="live-monitoring")

    assert manager.session_for(1) is None


@pytest.mark.parametrize("remote_outcome", ["success", "failed", "timeout"])
@pytest.mark.asyncio
async def test_pending_switch_waits_for_cleanup_and_cancels_superseded_camera(
    monkeypatch, remote_outcome
):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from halo_gtk import live_sessions, live_stream

    open_peers = []
    events = []
    release = asyncio.Event()
    entered = asyncio.Event()
    monkeypatch.setattr(live_sessions.GLib, "idle_add", lambda callback, *args: callback(*args))
    monkeypatch.setattr(live_stream, "_RING_CLOSE_TIMEOUT", 0.05)

    async def remote_close(session_id):
        events.append(("remote", session_id))
        entered.set()
        await release.wait()
        if remote_outcome == "failed":
            raise RuntimeError("fixture close rejected")

    class DelayedView(FakeView):
        def start_for_device(self, device):
            super().start_for_device(device)
            assert not open_peers
            open_peers.append(self)
            self.resources = live_stream._StreamResources(
                token=1,
                client=None,
                device=device,
                session_id=str(device.id),
                ring_session_started=True,
                pc=SimpleNamespace(close=AsyncMock(side_effect=lambda: open_peers.remove(self))),
            )

        def stop(self):
            super().stop()
            self.cleanup = asyncio.create_task(self.resources.cleanup())
            return self.resources.closed

    manager = live_sessions.LiveSessionManager(
        view_factory=DelayedView, max_streams_factory=lambda: 1
    )
    a, b = (SimpleNamespace(id=key, close_webrtc_stream=remote_close) for key in (1, 2))
    abort = Mock()
    a._webrtc_streams = {
        "1": SimpleNamespace(websocket=SimpleNamespace(transport=SimpleNamespace(abort=abort)))
    }
    first = manager.acquire(a, owner="focused")
    manager.release(1, owner="focused")
    await entered.wait()
    assert not open_peers and manager.active_count() == 1
    assert manager.preview_session_for(a) is first
    pending_b = manager.acquire(b, owner="focused")
    assert pending_b.state == live_sessions.StreamState.WAITING and pending_b.view.starts == 0
    manager.release(2, owner="focused")
    pending_a = manager.acquire(a, owner="focused")
    assert pending_a.state == live_sessions.StreamState.WAITING and pending_a.view.starts == 0
    if remote_outcome != "timeout":
        release.set()
    await asyncio.wait_for(first.view.cleanup, 1)
    assert first.view.resources.cleanup_failed is (remote_outcome != "success")
    assert open_peers == [pending_a.view]
    assert events == [("remote", "1")]
    assert abort.call_count == (0 if remote_outcome == "success" else 1)
    assert pending_b.view.starts == 0 and manager.active_count() == 1
    release.set()
    manager.stop_all()
    await pending_a.view.cleanup
    assert not open_peers and manager.active_count() == 0
