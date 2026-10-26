"""Installed media compatibility contracts; loopback only, no Ring or hardware."""

import asyncio
import fractions
import time
from unittest.mock import patch

import av
import numpy as np
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, VideoStreamTrack
from aiortc.codecs import CODECS
from aiortc.codecs.h264 import H264Decoder
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import AudioStreamTrack
from aiortc.rtp import RtpPacket

from halo_gtk import ring_media
from halo_gtk.live_stream import LiveStreamView, _StreamResources


@pytest.mark.asyncio
async def test_all_private_patches_install_and_large_idr_fits():
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        video = peer.addTransceiver("video")
        audio = peer.addTransceiver("audio")
        assert video.receiver._RTCRtpReceiver__jitter_buffer.capacity == 512
        assert audio.receiver._RTCRtpReceiver__jitter_buffer.capacity == 16
        jitter = video.receiver._RTCRtpReceiver__jitter_buffer
        output = None
        for i in range(160):
            packet = RtpPacket(sequence_number=i, timestamp=0 if i < 159 else 3000)
            packet._data = b"x" * 1172
            _, frame = jitter.add(packet)
            output = frame or output
        assert output is not None and len(output.data) == 159 * 1172
        variants = [
            (c.parameters.get("packetization-mode"), c.parameters.get("profile-level-id"))
            for c in CODECS["video"]
            if c.mimeType.lower() == "video/h264"
        ]
        for item in [("0", "42001f"), ("0", "42e01f"), ("1", "640032"), ("0", "640032")]:
            assert variants.count(item) == 1
        pts = [c.payloadType for c in CODECS["video"]]
        assert len(pts) == len(set(pts)) and all(0 <= pt <= 127 for pt in pts)
        decoder = H264Decoder()
        assert decoder.codec.flags & 8
        assert decoder._halo_output_corrupt_patch_applied
        assert decoder._halo_quiet_decode_patch_applied
        decoder._halo_decode_last_log = time.monotonic()
        assert decoder.decode(JitterFrame(b"\0\0\0\1\x65broken", 0)) == []
        assert decoder._halo_decode_error_count == 1
        before = len(CODECS["video"])
        ring_media.patch_aiortc_h264()
        assert len(CODECS["video"]) == before
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_actual_private_connect_task_is_retained():
    with patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]):
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        try:
            peer.addTransceiver("video")
            await peer.setLocalDescription(await peer.createOffer())
            retained = []
            ring_media.retain_aiortc_connect_tasks(peer, lambda task, label: retained.append(task))
            assert retained
            assert all(t.get_coro().cr_frame.f_locals.get("self") is peer for t in retained)
            await asyncio.wait_for(asyncio.gather(*retained), timeout=5)
        finally:
            await peer.close()


def test_patched_decoder_accepts_real_high_profile_level_50_bitstream():
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = 320, 180
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = fractions.Fraction(1, 30)
    encoder.options = {"profile": "high", "level": "5.0", "preset": "medium", "tune": "zerolatency"}
    encoder.thread_count = 1
    array = np.random.default_rng(42).integers(0, 256, (180, 320, 3), dtype=np.uint8)
    frame = av.VideoFrame.from_ndarray(array, format="rgb24")
    frame.pts = 0
    encoded = b"".join(bytes(p) for p in encoder.encode(frame))
    encoded += b"".join(bytes(p) for p in encoder.encode(None))
    assert b"\x67\x64\x00\x32" in encoded  # SPS: High Profile, level 5.0
    decoder = H264Decoder()
    output = decoder.decode(JitterFrame(encoded, 0))
    assert output and (output[0].width, output[0].height) == (320, 180)
    assert decoder.codec.profile == "High"


class SyntheticVideo(VideoStreamTrack):
    async def recv(self):
        pts, base = await self.next_timestamp()
        frame = av.VideoFrame.from_ndarray(np.full((180, 320, 3), 80, np.uint8), format="rgb24")
        frame.pts, frame.time_base = pts, base
        return frame


class SyntheticPTT(AudioStreamTrack):
    """Simulated PTT sends a synthetic tone; never opens pulsesrc."""

    def __init__(self):
        super().__init__()
        self.active = False
        self.samples = 0
        self.started = time.monotonic()

    async def start_capture(self):
        self.active = True

    async def stop_capture(self):
        self.active = False

    async def recv(self):
        deadline = self.started + self.samples / 48000
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        amplitude = 8000 if self.active else 0
        values = (np.sin(np.arange(960) * 2 * np.pi * 440 / 48000) * amplitude).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(values.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.time_base = fractions.Fraction(1, 48000)
        frame.pts = self.samples
        self.samples += 960
        return frame


@pytest.mark.asyncio
async def test_three_h264_audio_loopback_renewals_with_simulated_ptt(monkeypatch):
    for generation in range(3):
        with patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]):
            sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            video, microphone = SyntheticVideo(), SyntheticPTT()
            sender.addTrack(video)
            sender.addTrack(microphone)
            for transceiver in sender.getTransceivers():
                codecs = RTCRtpSender.getCapabilities(transceiver.kind).codecs
                if transceiver.kind == "video":
                    transceiver.setCodecPreferences(
                        [c for c in codecs if c.mimeType == "video/H264"]
                    )
            counts = {"video": 0, "audio": 0, "tone": 0}
            tasks = []

            async def consume(track, counts=counts):
                while True:
                    frame = await track.recv()
                    counts[track.kind] += 1
                    if track.kind == "video":
                        assert (frame.width, frame.height) == (320, 180)
                    elif np.abs(frame.to_ndarray().astype(np.int64)).max() > 1000:
                        counts["tone"] += 1

            @receiver.on("track")
            def on_track(track, tasks=tasks, consume=consume):
                tasks.append(asyncio.create_task(consume(track)))

            try:
                await sender.setLocalDescription(await sender.createOffer())
                assert "stun:" not in sender.localDescription.sdp
                assert "127.0.0.1" in sender.localDescription.sdp
                await receiver.setRemoteDescription(sender.localDescription)
                await receiver.setLocalDescription(await receiver.createAnswer())
                await sender.setRemoteDescription(receiver.localDescription)
                async with asyncio.timeout(8):
                    while counts["video"] < 3 or counts["audio"] < 3:
                        await asyncio.sleep(0.02)
                import halo_gtk.live_stream as live_stream

                speaker = []

                async def fake_speaker(device, session, messages=speaker):
                    messages.append(session)

                monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", fake_speaker)
                resources = _StreamResources(
                    token=generation,
                    device=object(),
                    client=object(),
                    session_id=f"fake-{generation}",
                    mic_track=microphone,
                    connected=True,
                    talkback_requested=True,
                    talkback_generation=1,
                )
                view = LiveStreamView.__new__(LiveStreamView)
                view._resources, view._stream_token, view._closing = resources, generation, False
                await view._async_start_talking(resources, 1)
                async with asyncio.timeout(4):
                    while counts["tone"] < 3:
                        await asyncio.sleep(0.02)
                await microphone.stop_capture()
                assert speaker == [f"fake-{generation}"] and not microphone.active
                assert counts["video"] >= 3 and counts["audio"] >= 3
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await sender.close()
                await receiver.close()
                video.stop()
                microphone.stop()
            assert sender.connectionState == receiver.connectionState == "closed"


@pytest.mark.asyncio
async def test_cancel_halo_startup_closes_real_peer_without_ring(monkeypatch):
    import halo_gtk.live_stream as live_stream

    started = asyncio.Event()
    closed = []

    class Device:
        name = "Synthetic local cancellation"

        def get_ice_servers(self):
            return []

        async def generate_async_webrtc_stream(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        async def close_webrtc_stream(self, session):
            closed.append(session)

    monkeypatch.setattr(live_stream.GLib, "idle_add", lambda *args: None)
    resources = _StreamResources(token=1, device=Device(), client=object(), session_id="fake")
    view = LiveStreamView.__new__(LiveStreamView)
    view._resources, view._stream_token, view._closing = resources, 1, False
    with patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]):
        task = asyncio.create_task(view._async_start(resources))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert resources.cleanup_started
    assert resources.pc.connectionState == "closed"
    assert closed == ["fake"]
