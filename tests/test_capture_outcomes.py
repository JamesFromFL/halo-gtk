import asyncio
from types import SimpleNamespace

import pytest

from halo_gtk import live_stream, ring_media
from halo_gtk.live_stream import LiveStreamView, _StreamResources


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
        device=object(),
        client=object(),
        session_id="fake",
        mic_track=SimpleNamespace(start_capture=capture),
        connected=True,
        talkback_generation=1,
        talkback_requested=True,
        talkback_callback=outcomes.append,
    )
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


@pytest.mark.parametrize("focused", [False, True])
def test_ui_start_failure_resets_actual_microphone_control(focused):
    from halo_gtk.cameras_page import CameraTile
    from halo_gtk.focused_live_page import FocusedLivePage

    messages = []

    class Button:
        active = True

        def get_active(self):
            return self.active

        def set_active(self, active):
            self.active = active

        def handler_block_by_func(self, _):
            pass

        def handler_unblock_by_func(self, _):
            pass

        def set_icon_name(self, _):
            assert focused, "Tile feedback replaced its custom Talk button child"

        def set_tooltip_text(self, text):
            self.tooltip = text

    class Page:
        _on_mic_toggled = (FocusedLivePage if focused else CameraTile)._on_mic_toggled

        def get_root(self):
            return SimpleNamespace(show_toast=messages.append)

    page = Page()
    button = page._mic_btn = Button()
    session = SimpleNamespace(start_talking=lambda **kwargs: kwargs["on_state_changed"]("failed"))
    page._session = page._live_session = session
    page._on_mic_toggled(button)
    assert button.active is False
    assert "unavailable" in button.tooltip
    assert messages == ["Microphone unavailable. Check your input device."]
