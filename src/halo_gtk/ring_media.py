"""Ring WebRTC media primitives used by the desktop live viewer.

The aiortc compatibility patches, ``MicrophoneTrack``, and frame/audio helpers
live here so ``live_stream.py`` can focus on GTK and stream lifecycle. Importing
this module applies the compatibility patches before a peer connection is created.

These patches are intentionally pinned to validated dependency versions; getting
them wrong makes some camera streams decode as garbage.
"""

from __future__ import annotations

import asyncio
import fractions
import functools
import json
import logging
import time
from importlib import metadata as importlib_metadata

import gi

gi.require_version("Gst", "1.0")

from aiortc.mediastreams import AudioStreamTrack, MediaStreamError  # noqa: E402
from av import AudioFrame  # noqa: E402
from gi.repository import Gst  # noqa: E402

_log = logging.getLogger(__name__)

_RING_DOORBELL_WEBRTC_CLOSE_VERSIONS = frozenset({"0.9.14"})
_RING_DOORBELL_SPEAKER_VERSIONS = frozenset({"0.9.14"})
_VALIDATED_INTERCOM_VIDEO_KINDS = frozenset({"stickup_cam_mini_v3"})
_SPEAKER_STATE_ATTRIBUTE = "_halo_camera_speaker_activated"


class RingSpeakerControlError(RuntimeError):
    """Raised when Halo cannot activate a Ring camera speaker."""


def supports_ring_camera_intercom(device) -> bool:
    """Return whether a camera exposes Halo's validated speaker contract."""
    try:
        version = importlib_metadata.version("ring-doorbell")
    except importlib_metadata.PackageNotFoundError:
        return False
    if version not in _RING_DOORBELL_SPEAKER_VERSIONS:
        return False
    try:
        kind = getattr(device, "kind", None)
        if kind not in _VALIDATED_INTERCOM_VIDEO_KINDS and not device.has_capability("video"):
            return False
        return all(
            callable(getattr(device, attribute, None))
            for attribute in (
                "generate_async_webrtc_stream",
                "on_webrtc_candidate",
                "close_webrtc_stream",
                "get_ice_servers",
            )
        )
    except Exception:
        return False


async def activate_ring_camera_speaker(
    device,
    halo_session_id: str,
    *,
    timeout: float = 5.0,
) -> bool:
    """Activate the camera speaker once for a validated live-view session."""
    try:
        version = importlib_metadata.version("ring-doorbell")
    except importlib_metadata.PackageNotFoundError:
        version = "not-installed"
    if version not in _RING_DOORBELL_SPEAKER_VERSIONS:
        raise RingSpeakerControlError(
            f"ring-doorbell {version} is not validated for camera speaker control"
        )

    streams = getattr(device, "_webrtc_streams", None)
    if not isinstance(streams, dict):
        raise RingSpeakerControlError("Ring camera WebRTC stream registry is unavailable")
    stream = streams.get(halo_session_id)
    if stream is None:
        raise RingSpeakerControlError("Ring camera WebRTC stream is unavailable")

    offered = getattr(stream, "_offered_event", None)
    answered = getattr(stream, "_sdp_answer_event", None)
    get_session_message = getattr(stream, "get_session_message", None)
    if (
        not callable(getattr(offered, "wait", None))
        or not callable(getattr(answered, "wait", None))
        or not callable(get_session_message)
    ):
        raise RingSpeakerControlError("Ring camera speaker signaling contract changed")

    try:
        async with asyncio.timeout(timeout):
            await offered.wait()
            await answered.wait()
            while getattr(stream, "session_id", None) is None:
                if not getattr(stream, "is_alive", False):
                    raise RingSpeakerControlError("Ring camera WebRTC stream closed")
                await asyncio.sleep(0.01)
    except TimeoutError as exc:
        raise RingSpeakerControlError("Ring camera speaker signaling timed out") from exc

    if not getattr(stream, "is_alive", False):
        raise RingSpeakerControlError("Ring camera WebRTC stream closed")
    if getattr(stream, _SPEAKER_STATE_ATTRIBUTE, False):
        return False

    websocket = getattr(stream, "websocket", None)
    send = getattr(websocket, "send", None)
    if not callable(send):
        raise RingSpeakerControlError("Ring camera WebRTC signaling socket is unavailable")
    message = get_session_message("camera_options", {"stealth_mode": False})
    try:
        await send(json.dumps(message, separators=(",", ":")))
    except Exception as exc:
        raise RingSpeakerControlError("Ring camera speaker command failed") from exc
    setattr(stream, _SPEAKER_STATE_ATTRIBUTE, True)
    return True


def patch_ring_doorbell_webrtc_close() -> bool:
    """Prevent ring-doorbell's reader task from awaiting itself on a Ring close."""
    try:
        version = importlib_metadata.version("ring-doorbell")
    except importlib_metadata.PackageNotFoundError:
        _log.debug("Could not apply ring-doorbell WebRTC close patch: package not installed")
        return False

    if version not in _RING_DOORBELL_WEBRTC_CLOSE_VERSIONS:
        _log.debug(
            "Skipping ring-doorbell WebRTC close patch for unvalidated version %s",
            version,
        )
        return False

    from ring_doorbell.webrtcstream import RingWebRtcStream

    if getattr(RingWebRtcStream, "_halo_reader_self_await_patch_applied", False):
        return True

    original_close = RingWebRtcStream._close
    code = getattr(original_close, "__code__", None)
    names = set(getattr(code, "co_names", ()))
    varnames = tuple(getattr(code, "co_varnames", ()))
    required_names = {"_on_close_callback", "done", "read_task", "websocket"}
    if (
        code is None
        or not required_names.issubset(names)
        or varnames[:2] != ("self", "closed_by_self")
    ):
        _log.warning(
            "ring-doorbell %s WebRTC close contract changed; "
            "natural stream renewal cleanup is unpatched",
            version,
        )
        return False

    @functools.wraps(original_close)
    async def _close_without_self_await(self, *, closed_by_self: bool) -> None:
        if getattr(self, "read_task", None) is asyncio.current_task():
            self.read_task = None
        await original_close(self, closed_by_self=closed_by_self)

    RingWebRtcStream._close = _close_without_self_await
    RingWebRtcStream._halo_reader_self_await_patch_applied = True
    _log.debug("Applied ring-doorbell WebRTC reader self-await patch")
    return True


def patch_aiortc_h264() -> None:
    """Monkey-patch aiortc for H264 and RTP receiver compatibility with Ring cameras.

    Three patches are applied at module import time so they take effect before
    any RTCPeerConnection or H264Decoder is created.

    Patch 1 — JitterBuffer capacity (128 → 512)
        The stickup_cam_mini_v3 sends each IDR keyframe as 159 RTP packets:
        1 SPS + 1 PPS + 157 FU-A fragments at 1172 bytes each (~184 KB total).
        aiortc's video JitterBuffer has capacity=128.  When packet #128 arrives
        (still part of the IDR), delta >= capacity triggers smart_remove(1),
        which removes packets until the timestamp changes — but SPS, PPS, and
        all 157 IDR fragments share the same RTP timestamp, so smart_remove
        wipes the ENTIRE keyframe.  The decoder never receives SPS+PPS+IDR and
        every subsequent P-frame fails with AVERROR_INVALIDDATA.  Capacity 512
        (power-of-2 requirement) comfortably fits the largest Ring IDR.

    Patch 2 — SDP offer H264 codec variants
        Two additions to the CODECS["video"] list:
        a. packetization-mode=0 Baseline variants: aiortc only offers mode=1;
           cameras that answer with mode=0 cause is_codec_compatible() to return
           False, raising OperationError in setRemoteDescription().
        b. High Profile Level 5.0 (profile-level-id=640032): the stickup_cam_mini_v3
           streams High Profile H264 (profile_idc=100, confirmed from SPS bytes
           0x67 0x64 0x00 0x32 in the RTP stream) but Ring's SDP answers with
           Baseline (42001f) because our offer only lists Baseline.  Adding
           640032 lets Ring negotiate the correct profile so the SPS in the
           bitstream matches what was negotiated, eliminating the profile
           mismatch that causes decode errors after the buffer fix.

    Patch 3 — H264Decoder AV_CODEC_FLAG_OUTPUT_CORRUPT (0x8)
        Instructs FFmpeg to output error-concealed frames rather than silently
        dropping them, helping the stream stay visible through transient errors.

    Patch 4 — H264Decoder warning throttling
        aiortc logs every FFmpeg decode rejection at WARNING. With Ring streams,
        one lost/corrupt keyframe can make FFmpeg reject many dependent frames
        until the next clean keyframe. The stream recovers, so repeated warnings
        are not actionable in the terminal. We keep the failure count in debug
        logging instead of flooding stderr.
    """
    try:
        import av
        from aiortc.codecs import CODECS
        from aiortc.codecs import h264 as aiortc_h264
        from aiortc.codecs.h264 import H264Decoder
        from aiortc.jitterbuffer import JitterBuffer
        from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters
        from aiortc.rtcrtpreceiver import RTCRtpReceiver

        # --- Patch 1: increase video JitterBuffer capacity 128 → 512 ---
        if not getattr(RTCRtpReceiver, "_halo_jitter_buffer_patch_applied", False):
            _orig_receiver_init = RTCRtpReceiver.__init__

            def _patched_receiver_init(self, kind: str, transport) -> None:
                _orig_receiver_init(self, kind, transport)
                if kind == "video":
                    # Replace the 128-slot buffer created by __init__ with a 512-slot
                    # one.  Name mangling: __jitter_buffer → _RTCRtpReceiver__jitter_buffer.
                    self._RTCRtpReceiver__jitter_buffer = JitterBuffer(
                        capacity=512,
                        is_video=True,
                    )

            RTCRtpReceiver.__init__ = _patched_receiver_init
            RTCRtpReceiver._halo_jitter_buffer_patch_applied = True
            _log.debug("Patched RTCRtpReceiver video JitterBuffer capacity: 128 → 512")

        # --- Patch 2: add H264 codec variants to the SDP offer ---
        existing_h264 = {
            (c.parameters.get("packetization-mode"), c.parameters.get("profile-level-id"))
            for c in CODECS["video"]
            if c.mimeType.lower() == "video/h264"
        }
        base_pt = max((c.payloadType for c in CODECS["video"]), default=102) + 1
        additions = [
            # packetization-mode=0 Baseline (cameras that negotiate mode=0)
            ("0", "42001f"),
            ("0", "42e01f"),
            # High Profile Level 5.0, both modes (stickup_cam_mini_v3 bitstream)
            ("1", "640032"),
            ("0", "640032"),
        ]
        for mode, profile in additions:
            if (mode, profile) not in existing_h264:
                CODECS["video"].append(
                    RTCRtpCodecParameters(
                        mimeType="video/H264",
                        clockRate=90000,
                        payloadType=base_pt,
                        rtcpFeedback=[
                            RTCRtcpFeedback(type="nack"),
                            RTCRtcpFeedback(type="nack", parameter="pli"),
                            RTCRtcpFeedback(type="goog-remb"),
                        ],
                        parameters={
                            "level-asymmetry-allowed": "1",
                            "packetization-mode": mode,
                            "profile-level-id": profile,
                        },
                    )
                )
                base_pt += 1
        _log.debug("Added H264 SDP variants (mode=0 Baseline, High Profile 640032)")

        # --- Patch 3: set AV_CODEC_FLAG_OUTPUT_CORRUPT on the H264Decoder ---
        if not getattr(H264Decoder, "_halo_output_corrupt_patch_applied", False):
            _orig_h264_init = H264Decoder.__init__

            def _permissive_init(self) -> None:
                _orig_h264_init(self)
                # AV_CODEC_FLAG_OUTPUT_CORRUPT (1 << 3): output frames even when
                # avcodec_send_packet() reports AVERROR_INVALIDDATA, using FFmpeg's
                # error concealment to keep the stream visible through transient errors.
                self.codec.flags |= 0x8  # AV_CODEC_FLAG_OUTPUT_CORRUPT

            H264Decoder.__init__ = _permissive_init
            H264Decoder._halo_output_corrupt_patch_applied = True
            _log.debug("Applied H264 decoder patch (AV_CODEC_FLAG_OUTPUT_CORRUPT)")

        if not getattr(H264Decoder, "_halo_quiet_decode_patch_applied", False):

            def _quiet_decode(self, encoded_frame):
                try:
                    packet = av.Packet(encoded_frame.data)
                    packet.pts = encoded_frame.timestamp
                    packet.time_base = aiortc_h264.VIDEO_TIME_BASE
                    return self.codec.decode(packet)
                except av.FFmpegError as exc:
                    now = time.monotonic()
                    count = getattr(self, "_halo_decode_error_count", 0) + 1
                    last_log = getattr(self, "_halo_decode_last_log", 0.0)
                    self._halo_decode_error_count = count
                    if now - last_log >= 30:
                        _log.debug(
                            "H264Decoder skipped corrupt frames; latest error after %d skips: %s",
                            count,
                            exc,
                        )
                        self._halo_decode_last_log = now
                        self._halo_decode_error_count = 0
                    return []

            H264Decoder.decode = _quiet_decode
            H264Decoder._halo_quiet_decode_patch_applied = True
            _log.debug("Applied H264 decoder warning throttle")

        _log.debug("aiortc H264/RTP patches applied")
    except Exception as exc:
        _log.debug("Could not apply aiortc compatibility patches: %s", exc)


patch_aiortc_h264()
patch_ring_doorbell_webrtc_close()


MIC_SAMPLE_RATE = 48000
MIC_SAMPLES = 960  # 20 ms at 48 kHz — matches aiortc's AUDIO_PTIME
MIC_PULL_TIMEOUT_NS = 25 * Gst.MSECOND


def gst_push_succeeded(result) -> bool:
    """Return True only while appsrc accepted the buffer."""
    return result == Gst.FlowReturn.OK


def retain_aiortc_connect_tasks(peer_connection, retain) -> None:
    """Retain aiortc's untracked private connection task for one peer."""
    for task in asyncio.all_tasks():
        if task.done():
            continue
        coro = task.get_coro()
        frame = getattr(coro, "cr_frame", None)
        code = getattr(coro, "cr_code", None)
        if (
            frame is not None
            and code is not None
            and code.co_name == "__connect"
            and frame.f_locals.get("self") is peer_connection
        ):
            retain(task, "aiortc connection")


def audio_frame_to_s16le_interleaved(frame) -> tuple[bytes, int, int, int]:
    """Return (raw bytes, channels, rate, samples) for a PyAV audio frame."""
    import numpy as np

    arr = frame.to_ndarray()
    channels = audio_frame_channels(frame, arr)
    samples = int(getattr(frame, "samples", 0) or 0)
    rate = int(getattr(frame, "sample_rate", 0) or MIC_SAMPLE_RATE)

    # PyAV planar audio is channels x samples.  GStreamer appsrc is simpler
    # and more reliable when we feed it interleaved S16LE.
    interleaved = arr.T.reshape(-1) if frame.format.is_planar else arr.reshape(-1)

    if interleaved.dtype.kind == "f":
        interleaved = np.clip(interleaved, -1.0, 1.0)
        interleaved = (interleaved * 32767).astype(np.int16)
    elif interleaved.dtype != np.int16:
        info = np.iinfo(interleaved.dtype) if interleaved.dtype.kind in {"i", "u"} else None
        if info is not None and interleaved.dtype.itemsize > 2:
            shift = (interleaved.dtype.itemsize - 2) * 8
            interleaved = (interleaved >> shift).astype(np.int16)
        else:
            interleaved = interleaved.astype(np.int16)

    if samples <= 0 and channels > 0:
        samples = max(1, interleaved.size // channels)
    return interleaved.tobytes(), channels, rate, samples


def audio_frame_channels(frame, arr) -> int:
    layout = getattr(frame, "layout", None)
    channels = getattr(layout, "channels", None)
    if channels is not None:
        try:
            return max(1, len(channels))
        except TypeError:
            pass
    if frame.format.is_planar and getattr(arr, "ndim", 0) >= 1:
        return max(1, int(arr.shape[0]))
    if getattr(arr, "ndim", 0) >= 2:
        return max(1, int(arr.shape[1]))
    return 1


class MicrophoneTrack(AudioStreamTrack):
    """AudioStreamTrack that captures from the system microphone via GStreamer pulsesrc.

    Sends silence (zeroed frames at 48 kHz mono) by default so the Opus encoder
    always receives valid, consistently-formatted frames.  Call start_capture() /
    stop_capture() from the asyncio event loop to toggle live mic input.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._active = False
        self._mic_pipeline: Gst.Pipeline | None = None
        self._mic_appsink: Gst.Element | None = None
        self._start: float | None = None
        self._timestamp: int = 0

    async def start_capture(self) -> None:
        """Start the GStreamer pulsesrc pipeline and begin sending mic audio."""
        if self._active:
            return
        pipeline = Gst.parse_launch(
            "pulsesrc blocksize=1920 "
            "! audioconvert "
            "! audioresample "
            "! capsfilter caps=audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved "
            "! appsink name=sink emit-signals=false max-buffers=1 drop=true"
        )
        self._mic_pipeline = pipeline
        self._mic_appsink = pipeline.get_by_name("sink")
        pipeline.set_state(Gst.State.PLAYING)
        self._active = True
        _log.debug("Microphone capture started")

    async def stop_capture(self) -> None:
        """Stop mic capture and revert to silence."""
        if not self._active:
            return
        self._active = False
        if self._mic_pipeline is not None:
            self._mic_pipeline.set_state(Gst.State.NULL)
            self._mic_pipeline = None
            self._mic_appsink = None
        _log.debug("Microphone capture stopped")

    def _pull_sample_blocking(self, appsink: Gst.Element) -> bytes | None:
        """Pull one buffer with a native timeout, intended for run_in_executor."""
        sample = appsink.emit("try-pull-sample", MIC_PULL_TIMEOUT_NS)
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        data = bytes(info.data)
        buf.unmap(info)
        return data

    async def recv(self) -> AudioFrame:  # type: ignore[override]
        if self.readyState != "live":
            raise MediaStreamError

        # Pace output to a fixed 20 ms interval independent of mic availability.
        if self._start is None:
            self._start = time.monotonic()
            self._timestamp = 0
        else:
            self._timestamp += MIC_SAMPLES
            wait = self._start + (self._timestamp / MIC_SAMPLE_RATE) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)

        frame = AudioFrame(format="s16", layout="mono", samples=MIC_SAMPLES)
        frame.sample_rate = MIC_SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, MIC_SAMPLE_RATE)

        appsink = self._mic_appsink
        if self._active and appsink is not None:
            loop = asyncio.get_running_loop()
            try:
                raw = await loop.run_in_executor(None, self._pull_sample_blocking, appsink)
                if raw:
                    # Pad or trim to exactly the expected frame size.
                    needed = MIC_SAMPLES * 2  # S16LE = 2 bytes per sample
                    raw = raw[:needed].ljust(needed, b"\x00")
                    frame.planes[0].update(raw)
                    return frame
            except Exception as exc:  # noqa: BLE001
                _log.debug("Mic pull error: %s", exc)

        # Silence fallback — zeroed plane.
        for p in frame.planes:
            p.update(bytes(p.buffer_size))
        return frame
