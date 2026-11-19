"""Opt-in native history playback qualification with synthetic local media.

Run HALO_NATIVE_MEDIA_TESTS=1 on an isolated GTK display. The actual history
player and GTK video sink decode a temporary H.264/AAC file. Audio is routed
exclusively to fakesink; no Ring client, microphone, or speaker is used.
"""

import fractions
import io
import os
import time

import av
import gi
import numpy as np
import pytest
from PIL import Image

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gdk, GLib, Gst  # noqa: E402

from halo_gtk import ring_client  # noqa: E402
from halo_gtk.history_page import _VideoPlayer  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("HALO_NATIVE_MEDIA_TESTS") != "1",
    reason="Requires an explicitly enabled, isolated GTK display",
)


def _wait_until(predicate, timeout=5):
    if predicate():
        return
    deadline = time.monotonic() + timeout
    loop = GLib.MainLoop()
    succeeded = False
    failure = None

    def check():
        nonlocal succeeded, failure
        try:
            succeeded = bool(predicate())
        except Exception as exc:
            failure = exc
            loop.quit()
            return GLib.SOURCE_REMOVE
        if succeeded or time.monotonic() >= deadline:
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(10, check)
    loop.run()
    if failure is not None:
        raise failure
    assert succeeded, "Native playback did not reach the expected state"


def _write_synthetic_clip(path):
    with av.open(str(path), "w") as output:
        video = output.add_stream("libx264", rate=30)
        video.width, video.height = 320, 180
        video.pix_fmt = "yuv420p"
        video.options = {"preset": "ultrafast", "tune": "zerolatency"}
        audio = output.add_stream("aac", rate=48000)
        audio.layout = "mono"
        for index in range(60):
            pixels = np.empty((180, 320, 3), dtype=np.uint8)
            pixels[:] = (220, 35, 50) if index < 30 else (30, 170, 210)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts, frame.time_base = index, fractions.Fraction(1, 30)
            for packet in video.encode(frame):
                output.mux(packet)
            samples = np.arange(1600) + index * 1600
            tone = (0.1 * np.sin(samples * 2 * np.pi * 440 / 48000)).astype(np.float32)
            sound = av.AudioFrame.from_ndarray(tone.reshape(1, -1), format="fltp", layout="mono")
            sound.sample_rate = 48000
            sound.pts, sound.time_base = index * 1600, fractions.Fraction(1, 48000)
            for packet in audio.encode(sound):
                output.mux(packet)
        for stream in (video, audio):
            for packet in stream.encode(None):
                output.mux(packet)


def test_native_history_decodes_advances_pauses_and_cleans_up(tmp_path, monkeypatch):
    Adw.init()
    Gst.init(None)
    assert Gdk.Display.get_default() is not None, "An isolated GTK display must be running"
    assert ring_client.get_client() is None

    def reject_client(*_args, **_kwargs):
        pytest.fail("Native media qualification must not construct a Ring client")

    monkeypatch.setattr(ring_client.RingClient, "__init__", reject_client)
    clip = tmp_path / "synthetic-h264-aac.mp4"
    _write_synthetic_clip(clip)
    ended = []
    audio_buffers = []
    errors = []
    view = _VideoPlayer(on_finished=lambda: ended.append(True))
    window = Adw.Window(default_width=640, default_height=480)
    window.set_content(view)
    assert view._player is not None
    audio_sink = Gst.ElementFactory.make("fakesink", "silent-audio")
    assert audio_sink is not None
    audio_sink.set_property("sync", True)
    audio_sink.set_property("signal-handoffs", True)
    audio_sink.connect("handoff", lambda *_: audio_buffers.append(True))
    view._player.set_property("audio-sink", audio_sink)
    video_sink = view._player.get_property("video-sink")
    assert video_sink.get_factory().get_name() == "gtk4paintablesink"
    error_handler = view._bus.connect(
        "message::error", lambda _bus, message: errors.append(message.parse_error()[0])
    )
    try:
        window.present()
        view.load_file(clip)
        assert view._play_btn.get_sensitive() and view._scrubber.get_sensitive()
        _wait_until(lambda: view._get_position_ns() >= 200_000_000 and bool(audio_buffers))
        assert view._player.get_property("current-uri") == clip.as_uri()
        _wait_until(lambda: view._paintable.get_intrinsic_width() == 320)
        first_frame = view.get_current_frame_png()
        assert first_frame is not None
        with Image.open(io.BytesIO(first_frame)) as image:
            assert image.size == (320, 180)
            first_colour = image.convert("RGB").getpixel((160, 90))
        assert first_colour[0] > first_colour[2]

        view._play_btn.emit("clicked")
        _wait_until(lambda: view._player.get_state(0)[1] == Gst.State.PAUSED)
        paused_at = view._get_position_ns()
        time.sleep(0.08)
        assert abs(view._get_position_ns() - paused_at) < 50_000_000
        view._play_btn.emit("clicked")
        _wait_until(lambda: view._get_position_ns() >= 1_250_000_000)
        second_frame = view.get_current_frame_png()
        assert second_frame is not None and second_frame != first_frame
        with Image.open(io.BytesIO(second_frame)) as image:
            second_colour = image.convert("RGB").getpixel((160, 90))
        assert second_colour[2] > second_colour[0]
        _wait_until(lambda: bool(ended))
        assert ended == [True]
        assert view._poll_id is None
        assert view._scrubber.get_value() == 1.0
        assert not errors

        # Reusing the same native player must still stop cleanly mid-clip.
        view.load_file(clip)
        _wait_until(lambda: view._get_position_ns() >= 100_000_000)
        view.deactivate()
        assert view._player.get_state(Gst.SECOND)[1] == Gst.State.NULL
        assert view._poll_id is None
        assert view._placeholder.get_visible()
    finally:
        view.deactivate()
        view._bus.disconnect(error_handler)
        # Explicitly unroot: destroy() does not unparent children while this
        # test still holds a reference to the native window.
        window.set_content(None)
        window.destroy()
    assert not view._bus_handlers
    assert ring_client.get_client() is None


def test_native_recording_error_stops_and_retry_reopens_selected_recording(tmp_path):
    Adw.init()
    Gst.init(None)
    missing = tmp_path / "unavailable.mp4"
    clip = tmp_path / "restored.mp4"
    _write_synthetic_clip(clip)
    view = _VideoPlayer(on_retry=lambda: view.load_file(clip))
    window = Adw.Window(content=view)
    view._player.set_property("audio-sink", Gst.ElementFactory.make("fakesink"))
    try:
        window.present()
        view.load_file(missing)
        _wait_until(view._retry_btn.get_visible)
        assert view._player.get_state(Gst.SECOND)[1] == Gst.State.NULL
        assert view._poll_id is None and not view._play_btn.get_sensitive()
        assert view._placeholder.get_label() == "Could not play this recording."
        view._retry_btn.emit("clicked")
        _wait_until(lambda: view._get_position_ns() > 0 and view._duration_ns > 0)
        assert not view._retry_btn.get_visible()
        view._on_play_pause()
        sought = []
        seek = view._seek_to_ns
        view._seek_to_ns = lambda value: (sought.append(value), seek(value))
        from gi.repository import Gtk

        view._scrubber.emit("move-slider", Gtk.ScrollType.STEP_FORWARD)
        assert len(sought) == 1 and sought[0] > 0
        view._display_position(0.3)
        assert len(sought) == 1
    finally:
        view.deactivate()
        window.destroy()


def test_native_live_audio_failure_preserves_video_and_can_reopen_output(monkeypatch):
    from types import SimpleNamespace

    from halo_gtk import live_stream

    Adw.init()
    Gst.init(None)
    monkeypatch.setattr(
        live_stream, "_audio_sink_description", lambda: "fakesink name=asink sync=false"
    )
    view = live_stream.LiveStreamView()
    view._resources = SimpleNamespace(token=view._stream_token)
    monkeypatch.setattr(view, "_privacy_current", lambda *_: True)
    try:
        view._pipeline.set_state(Gst.State.PLAYING)
        view.set_volume(0)
        assert view._audio_pipeline is None
        view.set_volume(0.5)
        old_bus = view._audio_watch[0]
        message = Gst.Message.new_error(
            view._audio_pipeline, GLib.Error("synthetic output failure"), ""
        )
        old_bus.post(message)
        _wait_until(lambda: view._audio_pipeline is None)
        caps = Gst.Caps.from_string("video/x-raw,format=RGB,width=16,height=16,framerate=10/1")
        view._video_appsrc.set_property("caps", caps)
        assert (
            view._video_appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(16 * 16 * 3)))
            == Gst.FlowReturn.OK
        )
        _wait_until(lambda: view._paintable.get_intrinsic_width() == 16)
        view.set_volume(0.5)
        assert view._audio_watch[0] != old_bus
        view._on_audio_error(old_bus, message, view._stream_token)
        assert view._audio_pipeline is not None
    finally:
        view._stop_audio_output()
        view._pipeline.set_state(Gst.State.NULL)
        view._detach_bus_watch()
