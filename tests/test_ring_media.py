"""Tests for desktop Ring media compatibility helpers."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from ring_doorbell.webrtcstream import RingWebRtcStream

from halo_gtk import ring_media


class _SpeakerSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


def _ready_speaker_stream():
    offered = asyncio.Event()
    offered.set()
    answered = asyncio.Event()
    answered.set()
    socket = _SpeakerSocket()
    stream = SimpleNamespace(
        _offered_event=offered,
        _sdp_answer_event=answered,
        session_id="ring-session",
        is_alive=True,
        websocket=socket,
    )
    stream.get_session_message = lambda method, body: {
        "method": method,
        "body": {**body, "session_id": stream.session_id},
    }
    return stream, socket


def test_microphone_pull_uses_native_gstreamer_timeout():
    class MissingSampleSink:
        def __init__(self):
            self.calls = []

        def emit(self, *args):
            self.calls.append(args)
            return None

    track = ring_media.MicrophoneTrack()
    sink = MissingSampleSink()

    assert track._pull_sample_blocking(sink) is None
    assert sink.calls == [
        ("try-pull-sample", ring_media.MIC_PULL_TIMEOUT_NS),
    ]


@pytest.mark.asyncio
async def test_ring_doorbell_natural_close_does_not_await_reader_task():
    stream = RingWebRtcStream(SimpleNamespace(), 1)
    callback_messages = []
    close_callbacks = 0

    async def close_callback():
        nonlocal close_callbacks
        close_callbacks += 1
        await stream.close()

    stream._on_close_callback = close_callback
    stream._on_message_callback = callback_messages.append
    stream.read_task = asyncio.current_task()

    await stream.handle_close_message(
        {
            "body": {
                "reason": {
                    "code": "stream_timeout",
                    "text": "Live View ended",
                }
            }
        }
    )

    assert close_callbacks == 1
    assert stream.read_task is None
    assert stream.is_alive is False
    assert callback_messages[0].error_code == "stream_timeout"


def test_ring_doorbell_close_patch_rejects_unvalidated_version(monkeypatch):
    monkeypatch.setattr(ring_media.importlib_metadata, "version", lambda _name: "0.9.15")

    assert ring_media.patch_ring_doorbell_webrtc_close() is False


def test_ring_doorbell_close_patch_rejects_changed_contract(monkeypatch):
    async def changed_close(self, *, closed_by_self):
        self.is_alive = not closed_by_self

    monkeypatch.setattr(RingWebRtcStream, "_close", changed_close)
    monkeypatch.delattr(
        RingWebRtcStream,
        "_halo_reader_self_await_patch_applied",
        raising=False,
    )

    assert ring_media.patch_ring_doorbell_webrtc_close() is False


@pytest.mark.asyncio
async def test_speaker_activation_sends_camera_options_once(monkeypatch):
    monkeypatch.setattr(ring_media.importlib_metadata, "version", lambda _name: "0.9.14")
    stream, socket = _ready_speaker_stream()
    device = SimpleNamespace(_webrtc_streams={"halo-session": stream})

    assert await ring_media.activate_ring_camera_speaker(device, "halo-session") is True
    assert await ring_media.activate_ring_camera_speaker(device, "halo-session") is False

    assert socket.messages == [
        {
            "method": "camera_options",
            "body": {"stealth_mode": False, "session_id": "ring-session"},
        }
    ]


def test_intercom_support_requires_validated_video_contract(monkeypatch):
    monkeypatch.setattr(ring_media.importlib_metadata, "version", lambda _name: "0.9.14")
    device = SimpleNamespace(
        kind="stickup_cam_v4",
        has_capability=lambda capability: capability == "video",
        generate_async_webrtc_stream=lambda: None,
        on_webrtc_candidate=lambda: None,
        close_webrtc_stream=lambda: None,
        get_ice_servers=lambda: [],
    )

    assert ring_media.supports_ring_camera_intercom(device) is True
    device.close_webrtc_stream = None
    assert ring_media.supports_ring_camera_intercom(device) is False


@pytest.mark.asyncio
async def test_microphone_capture_uses_one_buffer_leaky_queue(monkeypatch):
    descriptions = []

    class FakePipeline:
        def get_by_name(self, _name):
            return object()

        def set_state(self, _state):
            return None

    def parse_launch(description):
        descriptions.append(description)
        return FakePipeline()

    monkeypatch.setattr(ring_media.Gst, "parse_launch", parse_launch)
    track = ring_media.MicrophoneTrack()

    await track.start_capture()
    await track.stop_capture()

    assert "max-buffers=1 drop=true" in descriptions[0]


@pytest.mark.asyncio
async def test_microphone_frame_pacing_uses_monotonic_clock(monkeypatch):
    monkeypatch.setattr(
        ring_media.time,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock used")),
    )
    monkeypatch.setattr(ring_media.time, "monotonic", lambda: 100.0)
    track = ring_media.MicrophoneTrack()

    frame = await track.recv()

    assert frame.samples == ring_media.MIC_SAMPLES


@pytest.mark.asyncio
async def test_aiortc_connect_task_is_retained_for_matching_peer_only():
    class Peer:
        def __init__(self):
            self.started = asyncio.Event()

        async def __connect(self):
            self.started.set()
            await asyncio.Event().wait()

        def start(self):
            return asyncio.create_task(self.__connect())

    peer = Peer()
    other = Peer()
    peer_task = peer.start()
    other_task = other.start()
    await peer.started.wait()
    await other.started.wait()
    retained = []

    ring_media.retain_aiortc_connect_tasks(
        peer,
        lambda task, label: retained.append((task, label)),
    )

    assert retained == [(peer_task, "aiortc connection")]
    peer_task.cancel()
    other_task.cancel()
    await asyncio.gather(peer_task, other_task, return_exceptions=True)
