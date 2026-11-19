import asyncio
from types import SimpleNamespace

import pytest

from halo_gtk import live_stream, ring_media
from halo_gtk.live_stream import LiveStreamView, _StreamResources
from tests.privacy_helpers import grant


@pytest.mark.asyncio
async def test_late_capture_error_stops_pipeline_and_reports_once(monkeypatch):
    messages = []
    outcomes = []
    states = []
    polled = asyncio.Event()

    class Bus:
        def timed_pop_filtered(self, timeout, _types):
            assert timeout == 0
            polled.set()
            return messages.pop() if messages else None

    pipeline = SimpleNamespace(
        get_by_name=lambda _name: object(),
        set_state=states.append,
        get_bus=Bus,
    )
    monkeypatch.setattr(ring_media.Gst, "parse_launch", lambda _description: pipeline)
    track = ring_media.MicrophoneTrack()
    track.set_on_capture_error(lambda: outcomes.append("failed"))
    await track.start_capture()
    await asyncio.wait_for(polled.wait(), 0.5)
    messages.append(object())
    async with asyncio.timeout(0.5):
        while track._active:
            await asyncio.sleep(0.01)
    assert outcomes == ["failed"]
    assert track._mic_pipeline is None
    assert states[-1] == ring_media.Gst.State.NULL
    await track.stop_capture()
    assert track._capture_monitor is None


@pytest.mark.asyncio
async def test_stop_cancels_bus_monitor_without_false_failure(monkeypatch):
    bus = SimpleNamespace(timed_pop_filtered=lambda *_args: None)
    pipeline = SimpleNamespace(
        get_by_name=lambda _: object(), set_state=lambda _: None, get_bus=lambda: bus
    )
    monkeypatch.setattr(ring_media.Gst, "parse_launch", lambda _: pipeline)
    outcomes = []
    track = ring_media.MicrophoneTrack()
    track.set_on_capture_error(lambda: outcomes.append("failed"))
    await track.start_capture()
    task = track._capture_monitor
    await asyncio.sleep(0)
    await track.stop_capture()
    assert task.done() and outcomes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["speaker", "capture"])
async def test_start_failure_produces_current_ui_outcome(monkeypatch, failure):
    callbacks = []
    outcomes = []
    monkeypatch.setattr(live_stream.GLib, "idle_add", lambda callback: callbacks.append(callback))

    async def activate(*_):
        if failure == "speaker":
            raise RuntimeError("unavailable")

    async def capture():
        if failure == "capture":
            raise RuntimeError("unavailable")

    monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", activate)
    resources = _StreamResources(
        token=1,
        device=SimpleNamespace(id=1),
        client=object(),
        session_id="fake",
        mic_track=SimpleNamespace(start_capture=capture),
        connected=True,
        talkback_generation=1,
        talkback_requested=True,
        talkback_callback=outcomes.append,
    )
    resources.client = grant(resources.client, resources.device)
    resources.privacy_lease = resources.client.camera_privacy.acquire(resources.device, "live")
    view = LiveStreamView.__new__(LiveStreamView)
    view._resources = resources
    view._stream_token = 1
    view._closing = False
    await view._async_start_talking(resources, 1)
    for callback in callbacks:
        callback()
    assert outcomes == ["failed"]
    assert not resources.talkback_requested


def test_stale_capture_result_cannot_change_new_request(monkeypatch):
    callbacks = []
    outcomes = []
    monkeypatch.setattr(live_stream.GLib, "idle_add", lambda callback: callbacks.append(callback))
    resources = _StreamResources(
        token=1,
        device=object(),
        client=object(),
        session_id="fake",
        talkback_generation=1,
        talkback_requested=True,
        talkback_callback=outcomes.append,
    )
    view = LiveStreamView.__new__(LiveStreamView)
    view._resources = resources
    view._stream_token = 1
    view._closing = False
    view._talkback_outcome(resources, 1, "failed")
    resources.talkback_generation = 2
    callbacks[0]()
    assert outcomes == []


def test_native_appsrc_does_not_accumulate_obsolete_video():
    Gst = live_stream.Gst
    Gst.init(None)
    pipeline = Gst.parse_launch(
        live_stream.VIDEO_SOURCE + "! identity sleep-time=10000 ! fakesink sync=false async=false"
    )
    source = pipeline.get_by_name("vsrc")
    source.set_property(
        "caps", Gst.Caps.from_string("video/x-raw,format=RGB,width=320,height=180,framerate=30/1")
    )
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for _index in range(100):
            source.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(320 * 180 * 3)))
        assert source.get_property("current-level-buffers") <= 2
        assert source.get_property("current-level-bytes") <= 2 * 320 * 180 * 3
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_native_audio_source_keeps_its_separate_latency_budget():
    Gst = live_stream.Gst
    Gst.init(None)
    pipeline = Gst.parse_launch(
        live_stream.AUDIO_SOURCE + "! identity sleep-time=10000 ! fakesink sync=false async=false"
    )
    source = pipeline.get_by_name("asrc")
    source.set_property(
        "caps",
        Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved"),
    )
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for index in range(100):
            buffer = Gst.Buffer.new_wrapped(bytes(960 * 2))
            buffer.pts = index * 20_000_000
            buffer.duration = 20_000_000
            source.emit("push-buffer", buffer)
        assert source.get_property("current-level-time") <= 500_000_000
        assert source.get_property("current-level-bytes") <= 25 * 960 * 2
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_talk_control_tracks_capture_until_release_and_drops_late_callbacks(monkeypatch):
    from halo_gtk import talk_button

    calls, states, callbacks = [], [], []

    class Control:
        _held_session = None
        _press_revision = 0
        release = talk_button.HoldToTalkButton.release

        def _get_session(self):
            return SimpleNamespace(
                start_talking=lambda **kwargs: callbacks.append(kwargs["on_state_changed"]),
                stop_talking=lambda: calls.append("stop"),
            )

        def is_sensitive(self):
            return True

        def _show_state(self, state):
            states.append(state)

    button = Control()
    messages = []
    monkeypatch.setattr(talk_button, "show_toast", lambda _, text: messages.append(text))
    for outcome in ("active", "failed"):
        talk_button.HoldToTalkButton._press(button)
        talk_button.HoldToTalkButton._press(button)
        callbacks[-1](outcome)
        button.release()
        callbacks[-1]("active")
    assert len(callbacks) == 2 and calls == ["stop", "stop"]
    assert states == ["starting", "active", "stopped", "starting", "stopped"]
    assert messages == ["Microphone unavailable. Check your input device."]
