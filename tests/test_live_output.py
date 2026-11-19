"""A local sound device can disappear without ending the camera session."""

from types import SimpleNamespace

from halo_gtk.focused_live_page import FocusedLivePage
from halo_gtk.live_stream import LiveStreamView


def test_readiness_uses_frame_state_without_encoding():
    calls = []
    session = SimpleNamespace(state="active", has_current_frame=lambda: True)
    page = SimpleNamespace(
        _session=session,
        _show_session_paintable=lambda: calls.append("shown"),
        _live_ready_source_id=9,
    )
    assert not FocusedLivePage._promote_live_when_ready(page)
    assert page._live_ready_source_id is None and calls == ["shown"]


def test_audio_failure_ignores_retired_outputs_and_leaves_video_intact():
    bus = object()
    sound = object()
    resources = object()
    video = object()
    view = SimpleNamespace(
        _stream_token=7,
        _audio_appsrc=sound,
        _resources=resources,
        _pipeline=video,
        _audio_watch=(bus, 42),
    )
    stopped = []
    view._audio_unavailable = lambda: stopped.append(sound)
    message = SimpleNamespace(parse_error=lambda: ("Device disappeared", ""))
    LiveStreamView._on_audio_error(view, bus, message, 6)
    LiveStreamView._on_audio_error(view, object(), message, 7)
    LiveStreamView._audio_push_failed(view, 7, object())
    assert not stopped
    LiveStreamView._on_audio_error(view, bus, message, 7)
    assert stopped == [sound]
    assert view._pipeline is video and view._resources is resources
