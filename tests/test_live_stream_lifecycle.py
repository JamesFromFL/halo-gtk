"""Resource ownership tests for the GTK live-stream generation bundle."""

import asyncio
from types import SimpleNamespace

import pytest

from halo_gtk.live_stream import LiveStreamView, _StreamResources


class FakeDevice:
    def __init__(self):
        self.closed_sessions = []

    async def close_webrtc_stream(self, session_id):
        self.closed_sessions.append(session_id)


class FakePeerConnection:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


class FakeMicrophoneTrack:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    async def start_capture(self):
        self.start_calls += 1

    async def stop_capture(self):
        self.stop_calls += 1


class FakeTrack:
    async def recv(self):
        from aiortc.mediastreams import MediaStreamError

        raise MediaStreamError


class FakeStartPeerConnection(FakePeerConnection):
    def __init__(self, _configuration):
        super().__init__()
        self.localDescription = None
        self.signalingState = "stable"
        self.connectionState = "new"
        self.handlers = {}

    def addTransceiver(self, *_args, **_kwargs):
        return None

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback

        return register

    async def createOffer(self):
        return SimpleNamespace(sdp="v=0", type="offer")

    async def setLocalDescription(self, description):
        self.localDescription = description


class BlockingStartPeerConnection(FakeStartPeerConnection):
    def __init__(self, configuration):
        super().__init__(configuration)
        self.offer_started = asyncio.Event()

    async def createOffer(self):
        self.offer_started.set()
        await asyncio.Event().wait()


class LoopClient:
    def __init__(self):
        self.tasks = []

    def submit(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


def make_bare_view(resources):
    view = LiveStreamView.__new__(LiveStreamView)
    view._resources = resources
    view._stream_token = resources.token
    view._closing = False
    return view


@pytest.mark.asyncio
async def test_stream_resources_cleanup_is_idempotent():
    device = FakeDevice()
    peer = FakePeerConnection()
    mic = FakeMicrophoneTrack()
    resources = _StreamResources(
        token=1,
        device=device,
        client=object(),
        session_id="session-1",
        pc=peer,
        mic_track=mic,
        ring_session_started=True,
    )

    await resources.cleanup()
    await resources.cleanup()

    assert mic.stop_calls == 1
    assert device.closed_sessions == ["session-1"]
    assert peer.close_calls == 1


@pytest.mark.asyncio
async def test_stream_resources_cleanup_cancels_children_but_not_owner():
    child_started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def child():
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    child_task = asyncio.create_task(child())
    await child_started.wait()
    resources = _StreamResources(
        token=1,
        device=FakeDevice(),
        client=object(),
        session_id="session-1",
    )
    resources.tasks.update({asyncio.current_task(), child_task})

    await resources.cleanup()

    assert child_task.cancelled()
    assert child_cancelled.is_set()
    assert asyncio.current_task().cancelling() == 0


@pytest.mark.asyncio
async def test_stream_resources_do_not_close_unstarted_ring_session():
    device = FakeDevice()
    resources = _StreamResources(
        token=1,
        device=device,
        client=object(),
        session_id="session-1",
        pc=FakePeerConnection(),
        mic_track=FakeMicrophoneTrack(),
    )

    await resources.cleanup()

    assert device.closed_sessions == []


@pytest.mark.asyncio
async def test_talkback_activates_camera_speaker_before_microphone(monkeypatch):
    import halo_gtk.live_stream as live_stream

    calls = []

    async def activate(_device, session_id):
        calls.append(("speaker", session_id))

    class OrderedMicrophone(FakeMicrophoneTrack):
        async def start_capture(self):
            calls.append(("microphone", None))
            await super().start_capture()

    monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", activate)
    microphone = OrderedMicrophone()
    resources = _StreamResources(
        token=2,
        device=FakeDevice(),
        client=object(),
        session_id="session-2",
        mic_track=microphone,
        connected=True,
        talkback_generation=1,
        talkback_requested=True,
    )
    view = make_bare_view(resources)

    await view._async_start_talking(resources, 1)

    assert calls == [("speaker", "session-2"), ("microphone", None)]
    assert microphone.start_calls == 1


@pytest.mark.asyncio
async def test_talkback_stop_invalidates_delayed_speaker_activation(monkeypatch):
    import halo_gtk.live_stream as live_stream

    activation_started = asyncio.Event()
    finish_activation = asyncio.Event()

    async def activate(_device, _session_id):
        activation_started.set()
        await finish_activation.wait()

    monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", activate)
    client = LoopClient()
    microphone = FakeMicrophoneTrack()
    resources = _StreamResources(
        token=3,
        device=FakeDevice(),
        client=client,
        session_id="session-3",
        mic_track=microphone,
        connected=True,
    )
    view = make_bare_view(resources)

    view.start_talking()
    await activation_started.wait()
    view.stop_talking()
    await asyncio.sleep(0)
    finish_activation.set()
    await asyncio.gather(*client.tasks)

    assert microphone.start_calls == 0
    assert microphone.stop_calls == 1


@pytest.mark.asyncio
async def test_talkback_requested_while_connecting_starts_when_track_is_ready(monkeypatch):
    import halo_gtk.live_stream as live_stream

    activated = []

    async def activate(_device, session_id):
        activated.append(session_id)

    monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", activate)
    client = LoopClient()
    resources = _StreamResources(
        token=4,
        device=FakeDevice(),
        client=client,
        session_id="session-4",
    )
    view = make_bare_view(resources)

    view.start_talking()
    await asyncio.sleep(0)
    microphone = FakeMicrophoneTrack()
    resources.mic_track = microphone
    resources.connected = True
    await asyncio.gather(*client.tasks)

    assert activated == ["session-4"]
    assert microphone.start_calls == 1


@pytest.mark.asyncio
async def test_talkback_waits_for_connected_stream_before_speaker_activation(monkeypatch):
    import halo_gtk.live_stream as live_stream

    activated = []

    async def activate(_device, session_id):
        activated.append(session_id)

    monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", activate)
    client = LoopClient()
    microphone = FakeMicrophoneTrack()
    resources = _StreamResources(
        token=5,
        device=FakeDevice(),
        client=client,
        session_id="session-5",
        mic_track=microphone,
    )
    view = make_bare_view(resources)

    view.start_talking()
    await asyncio.sleep(0.02)
    assert activated == []
    assert microphone.start_calls == 0

    resources.connected = True
    await asyncio.gather(*client.tasks)

    assert activated == ["session-5"]
    assert microphone.start_calls == 1


@pytest.mark.asyncio
async def test_talkback_cancelled_while_connecting_never_starts(monkeypatch):
    import halo_gtk.live_stream as live_stream

    activated = []

    async def activate(_device, session_id):
        activated.append(session_id)

    monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", activate)
    client = LoopClient()
    resources = _StreamResources(
        token=6,
        device=FakeDevice(),
        client=client,
        session_id="session-6",
    )
    view = make_bare_view(resources)

    view.start_talking()
    await asyncio.sleep(0)
    view.stop_talking()
    resources.mic_track = FakeMicrophoneTrack()
    await asyncio.gather(*client.tasks)

    assert activated == []
    assert resources.mic_track.start_calls == 0


@pytest.mark.asyncio
async def test_start_failure_closes_partial_ring_and_peer_sessions(monkeypatch):
    import aiortc

    import halo_gtk.live_stream as live_stream

    class FailingDevice(FakeDevice):
        name = "Test Camera"

        def get_ice_servers(self):
            return []

        async def generate_async_webrtc_stream(self, *_args, **_kwargs):
            raise RuntimeError("negotiation failed")

    peer_connections = []

    def make_peer(configuration):
        peer = FakeStartPeerConnection(configuration)
        peer_connections.append(peer)
        return peer

    idle_calls = []
    monkeypatch.setattr(aiortc, "RTCPeerConnection", make_peer)
    monkeypatch.setattr(live_stream, "MicrophoneTrack", FakeMicrophoneTrack)
    monkeypatch.setattr(
        live_stream.GLib,
        "idle_add",
        lambda *args: idle_calls.append(args),
    )

    device = FailingDevice()
    resources = _StreamResources(
        token=7,
        device=device,
        client=object(),
        session_id="session-7",
    )
    view = make_bare_view(resources)

    await view._async_start(resources)

    assert view._resources is None
    assert resources.cleanup_started is True
    assert device.closed_sessions == ["session-7"]
    assert peer_connections[0].close_calls == 1
    assert len(idle_calls) == 1
    assert idle_calls[0][1:] == (7, "failed", "Failed to connect: negotiation failed")


@pytest.mark.asyncio
async def test_natural_video_end_cleans_resources_and_notifies_once(monkeypatch):
    import halo_gtk.live_stream as live_stream

    idle_calls = []
    monkeypatch.setattr(
        live_stream.GLib,
        "idle_add",
        lambda *args: idle_calls.append(args),
    )
    device = FakeDevice()
    peer = FakePeerConnection()
    resources = _StreamResources(
        token=9,
        device=device,
        client=object(),
        session_id="session-9",
        pc=peer,
        ring_session_started=True,
    )
    view = make_bare_view(resources)

    await view._receive_frames(FakeTrack(), resources)
    await view._terminate_resources(resources, "failed", "duplicate")

    assert view._resources is None
    assert device.closed_sessions == ["session-9"]
    assert peer.close_calls == 1
    assert len(idle_calls) == 1
    assert idle_calls[0][1:] == (9, "stopped", "Stream ended")


@pytest.mark.asyncio
async def test_stream_becomes_active_only_after_first_video_buffer(monkeypatch):
    import numpy as np

    import halo_gtk.live_stream as live_stream

    live_stream.Gst.init(None)

    class OneFrameTrack:
        def __init__(self):
            self.sent = False

        async def recv(self):
            if self.sent:
                from aiortc.mediastreams import MediaStreamError

                raise MediaStreamError
            self.sent = True
            return SimpleNamespace(to_ndarray=lambda **_kwargs: np.zeros((2, 4, 3), dtype=np.uint8))

    class AcceptingAppSrc:
        def set_property(self, *_args):
            return None

        def emit(self, *_args):
            return live_stream.Gst.FlowReturn.OK

    idle_calls = []
    monkeypatch.setattr(
        live_stream.GLib,
        "idle_add",
        lambda *args: idle_calls.append(args),
    )
    resources = _StreamResources(
        token=10,
        device=FakeDevice(),
        client=object(),
        session_id="session-10",
        pc=FakePeerConnection(),
    )
    view = make_bare_view(resources)
    view._video_appsrc = AcceptingAppSrc()
    view._last_frame_rgb = None

    await view._receive_frames(OneFrameTrack(), resources)

    assert [call[0].__name__ for call in idle_calls] == [
        "_on_connected",
        "_on_stream_terminal",
    ]
    assert resources.connected is True


@pytest.mark.asyncio
async def test_stop_during_offer_cleans_only_stopped_generation(monkeypatch):
    import aiortc

    import halo_gtk.live_stream as live_stream

    class Device(FakeDevice):
        name = "Test Camera"

        def get_ice_servers(self):
            return []

    peers = []

    def make_peer(configuration):
        peer = BlockingStartPeerConnection(configuration)
        peers.append(peer)
        return peer

    idle_calls = []
    monkeypatch.setattr(aiortc, "RTCPeerConnection", make_peer)
    monkeypatch.setattr(live_stream, "MicrophoneTrack", FakeMicrophoneTrack)
    monkeypatch.setattr(
        live_stream.GLib,
        "idle_add",
        lambda *args: idle_calls.append(args),
    )

    client = LoopClient()
    device = Device()
    old_resources = _StreamResources(
        token=11,
        device=device,
        client=client,
        session_id="session-11",
    )
    view = make_bare_view(old_resources)
    view._pipeline = None
    view._bus = None
    view._bus_handlers = []
    old_resources.start_future = asyncio.create_task(view._async_start(old_resources))
    await asyncio.sleep(0)
    await asyncio.wait_for(peers[0].offer_started.wait(), timeout=1)

    view.stop()
    replacement = _StreamResources(
        token=view._stream_token,
        device=device,
        client=client,
        session_id="session-12",
    )
    view._closing = False
    view._resources = replacement
    await asyncio.gather(*client.tasks, return_exceptions=True)
    await asyncio.gather(old_resources.start_future, return_exceptions=True)

    assert view._resources is replacement
    assert old_resources.cleanup_started is True
    assert peers[0].close_calls == 1
    assert device.closed_sessions == []
    assert idle_calls == []
