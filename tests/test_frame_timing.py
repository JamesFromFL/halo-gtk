from fractions import Fraction
from types import SimpleNamespace

import pytest

from halo_gtk.frame_timing import FrameClock


def stamps(values, *, rate=90_000, duration=33_333_333):
    clock = FrameClock()
    return [
        clock.stamp(SimpleNamespace(pts=x, time_base=Fraction(1, rate)), duration) for x in values
    ]


def test_video_preserves_15fps_and_variable_rate_gaps():
    assert [x[0] for x in stamps([900_000, 906_000, 912_000, 915_000, 924_000])] == [
        0,
        66_666_666,
        133_333_333,
        166_666_666,
        266_666_666,
    ]


def test_video_above_30fps_does_not_inherit_fallback_duration():
    assert [x[0] for x in stamps([0, 1500, 3000])] == [0, 16_666_666, 33_333_333]


def test_audio_preserves_a_missing_40ms_packet_interval():
    assert [x[0] for x in stamps([0, 960, 1920, 4800], rate=48_000, duration=20_000_000)] == [
        0,
        20_000_000,
        40_000_000,
        100_000_000,
    ]


def test_clock_reset_and_large_jump_rebase_without_backlog():
    result = stamps([6000, 9000, 0, 900_000_000, 900_003_000])
    assert result[2][1] and result[3][1]
    assert all(a[0] < b[0] for a, b in zip(result, result[1:], strict=False))
    assert result[-1][0] < 200_000_000


def test_missing_timestamp_uses_duration_then_reanchors():
    result = stamps([0, None, 30_000, 33_000])
    assert [x[0] for x in result] == [0, 33_333_333, 66_666_666, 99_999_999]
    assert not any(x[1] for x in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("audio", [False, True])
async def test_actual_receivers_preserve_source_intervals(monkeypatch, audio):
    import av
    from aiortc.mediastreams import MediaStreamError

    from halo_gtk import live_stream

    Gst = live_stream.Gst
    Gst.init(None)
    captured = []
    source = SimpleNamespace(
        set_property=lambda *_: None,
        emit=lambda _, buffer: captured.append(buffer.pts) or Gst.FlowReturn.OK,
    )
    resources = live_stream._StreamResources(
        token=1, device=object(), client=object(), session_id="fixture", connected=True
    )
    view = live_stream.LiveStreamView.__new__(live_stream.LiveStreamView)
    view._resources, view._stream_token, view._closing = resources, 1, False
    view._audio_appsrc = view._video_appsrc = source
    monkeypatch.setattr(live_stream.GLib, "idle_add", lambda *_: None)
    pts = [0, 960, 1920, 4800] if audio else [0, 6000, 12000, 15000, 24000]
    rate = 48000 if audio else 90000
    frames = []
    for value in pts:
        if audio:
            frame = av.AudioFrame(format="s16", layout="mono", samples=960)
            frame.sample_rate = rate
        else:
            frame = av.VideoFrame(320, 180, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts, frame.time_base = value, Fraction(1, rate)
        frames.append(frame)

    class Track:
        async def recv(self):
            if not frames:
                raise MediaStreamError
            return frames.pop(0)

    receiver = view._receive_audio_frames if audio else view._receive_frames
    await receiver(Track(), resources)
    assert captured == [int(Fraction(value, rate) * 1_000_000_000) for value in pts]
