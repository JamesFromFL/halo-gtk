"""Real H.264 depayload/receiver controls plus bounded adversarial assembly."""

import re
import weakref
from fractions import Fraction
from types import SimpleNamespace

import aiortc
import av
import numpy as np
import pytest
from aiortc.codecs.h264 import H264PayloadDescriptor
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtpPacket

from halo_gtk import ring_media  # noqa: F401 -- installs the actual receiver hook
from halo_gtk.receive_video import H264JitterBuffer, VideoJitterBuffer, install_video_receiver_patch


def packet(sequence, timestamp, payload=b"\x65\x80\x11", *, marker=True, ssrc=7, pt=96):
    value = RtpPacket(payload_type=pt, sequence_number=sequence % 65536, timestamp=timestamp)
    value.ssrc, value.marker, value.payload = ssrc, marker, payload
    value._data = H264PayloadDescriptor.parse(payload)[1]
    return value


def test_marker_releases_complete_au_without_next_timestamp():
    jitter = H264JitterBuffer()
    pli, frame = jitter.add(packet(10, 100))
    assert not pli and frame.timestamp == 100
    assert frame.data == b"\0\0\0\1\x65\x80\x11"
    assert jitter.buffered_packets == jitter.buffered_bytes == 0
    assert not jitter.waiting_for_idr


def test_missing_markers_use_contiguous_timestamp_fallback():
    jitter = H264JitterBuffer()
    assert jitter.add(packet(1, 100, marker=False))[1] is None
    first = jitter.add(packet(2, 200, b"\x61\x80\x22", marker=False))[1]
    second = jitter.add(packet(3, 300, b"\x61\x80\x33", marker=False))[1]
    assert [first.timestamp, second.timestamp] == [100, 200]


def test_fu_reorder_duplicate_sequence_wrap_and_timestamp_wrap():
    jitter = H264JitterBuffer()
    start = packet(65534, 0xFFFFFF00, b"\x7c\x85\x80a", marker=False)
    middle = packet(65535, 0xFFFFFF00, b"\x7c\x05b", marker=False)
    end = packet(0, 0xFFFFFF00, b"\x7c\x45c")
    assert jitter.add(start)[1] is None
    assert jitter.add(end)[1] is None
    assert jitter.add(end)[1] is None
    complete = jitter.add(middle)[1]
    assert complete.data == b"\0\0\0\1\x65\x80abc"
    assert jitter.add(middle)[1] is None
    assert jitter.add(packet(1, 10, b"\x61\x80x"))[1].timestamp == 10


@pytest.mark.parametrize("limit", ["time", "horizon"])
def test_missing_fragment_recovers_with_pli_and_gates_dependents(limit):
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    if limit == "time":
        now[0] = 0.101
    assert jitter.add(packet(4, 200, b"\x61\x80p"))[1] is None
    assert jitter.add(packet(5, 300, b"\x61\x80q"))[1] is None
    assert jitter.snapshot()["recovery_requests"] == 1
    assert jitter.waiting_for_idr
    assert jitter.add(packet(6, 400))[1].timestamp == 400
    assert jitter.add(packet(7, 500, b"\x61\x80p"))[1].timestamp == 500
    assert not jitter.waiting_for_idr


def test_lost_pli_retries_on_arrivals_with_one_second_cooldown():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    flags = []
    for seq, when in enumerate((0, 0.2, 0.9, 1.01, 1.2, 2.02)):
        now[0] = when
        flags.append(jitter.add(packet(seq, seq * 100, b"\x61\x80p"))[0])
    assert flags == [True, False, False, True, False, True]
    assert jitter.snapshot()["loss_events"] == 1


@pytest.mark.parametrize("bound", ["packets", "bytes"])
def test_bounds_never_emit_a_truncated_idr(bound):
    jitter = H264JitterBuffer(
        capacity=4 if bound == "packets" else 512, max_bytes=45 if bound == "bytes" else 1024
    )
    outputs = []
    for seq in range(9):
        header = 0x85 if seq == 0 else 0x45 if seq == 8 else 0x05
        _, result = jitter.add(packet(seq, 100, bytes([0x7C, header, 0x80, 0x11]), marker=seq == 8))
        outputs.extend([result] if result else [])
        assert jitter.buffered_packets <= jitter.capacity
        assert jitter.buffered_bytes <= jitter.max_bytes
    assert not outputs
    assert jitter.waiting_for_idr
    assert jitter.add(packet(9, 200))[1] is not None


@pytest.mark.parametrize("payload", [b"\x7c\xc5\x80", b"\x7c\x45\x80", b"\x65\x40x"])
def test_malformed_or_joined_mid_idr_never_becomes_a_complete_au(payload):
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, payload))[1] is None
    now[0] = 0.11
    pli, frame = jitter.add(packet(2, 200, b"\x61\x80p"))
    assert frame is None
    assert jitter.waiting_for_idr
    assert jitter.snapshot()["recovery_requests"] >= 1


def test_conflicting_duplicate_fences_that_au_and_earlier_dependent_data():
    jitter = H264JitterBuffer()
    original = packet(1, 100, b"\x7c\x85\x80a", marker=False)
    assert jitter.add(original)[1] is None
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80b", marker=False))[0]
    assert jitter.add(packet(2, 100, b"\x7c\x45c"))[1] is None
    assert jitter.add(packet(3, 200))[1] is not None


def test_source_change_forward_gap_and_backward_restart_need_idr():
    jitter = H264JitterBuffer()
    assert jitter.add(packet(1000, 100))[1]
    assert jitter.add(packet(1001, 200, b"\x61\x80p", ssrc=8))[1] is None
    assert jitter.add(packet(1002, 300, ssrc=8))[1]
    assert jitter.add(packet(3000, 400, b"\x61\x80p", ssrc=8))[1] is None
    assert jitter.add(packet(3001, 500, ssrc=8))[1]
    pli, frame = jitter.add(packet(10, 600, ssrc=8))
    assert pli and frame
    assert jitter.add(packet(3000, 400, ssrc=8))[1] is None


def test_stap_a_accepts_config_and_single_byte_end_nal():
    jitter = H264JitterBuffer()
    # STAP contains an IDR slice and valid one-byte end-of-sequence NAL.
    payload = b"\x78\x00\x03\x65\x80\x11\x00\x01\x0a"
    assert jitter.add(packet(1, 100, payload))[1].data.endswith(b"\0\0\0\1\x0a")


def test_single_oversized_packet_is_rejected_without_retained_payload():
    jitter = H264JitterBuffer()
    pli, frame = jitter.add(packet(1, 100, b"\x65\x80" + b"x" * (1024 * 1024)))
    assert pli and frame is None
    assert jitter.buffered_bytes == jitter.buffered_packets == 0
    assert jitter.add(packet(2, 100))[1] is None
    assert jitter.add(packet(3, 200))[1]


def test_late_repair_cannot_reintroduce_expired_fragmented_idr():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    now[0] = 0.2
    assert jitter.add(packet(4, 200, b"\x61\x80p"))[1] is None
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1] is None
    assert jitter.add(packet(5, 300))[1]


@pytest.mark.parametrize("repair_at,accepted", [(0.05, True), (0.101, False), (10.0, False)])
def test_first_arrival_after_hole_deadline_cannot_repair_expired_idr(repair_at, accepted):
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    now[0] = repair_at
    pli, frame = jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))
    assert bool(frame) is accepted
    assert pli is not accepted
    assert jitter.snapshot()["reorder_timeouts"] == int(not accepted)
    if accepted:
        assert frame.data == b"\0\0\0\1\x65\x80abc"
    else:
        assert jitter.buffered_packets == 0
        assert jitter.add(packet(4, 200, b"\x61\x80p"))[1] is None
        assert jitter.add(packet(5, 300))[1]


def test_hole_grace_begins_when_gap_is_observed_not_at_first_fragment():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    now[0] = 0.45
    assert jitter.add(packet(3, 100, b"\x7c\x45c"))[1] is None
    now[0] = 0.5
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1]
    assert jitter.snapshot()["loss_events"] == 0


def test_whole_au_age_is_separate_bounded_limit_for_contiguous_fragments():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    assert jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))[1] is None
    now[0] = 0.8
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1] is None
    now[0] = 1.01
    pli, frame = jitter.add(packet(3, 100, b"\x7c\x45c"))
    assert pli and frame is None
    assert jitter.snapshot()["assembly_timeouts"] == 1
    assert jitter.snapshot()["reorder_timeouts"] == 0
    assert jitter.buffered_bytes == jitter.buffered_packets == 0


def test_repaired_head_does_not_leave_gap_timeout_on_complete_queued_au():
    now = [0.0]
    jitter = H264JitterBuffer(clock=lambda: now[0])
    jitter.add(packet(1, 100, b"\x7c\x85\x80a", marker=False))
    jitter.add(packet(3, 100, b"\x7c\x45c"))
    jitter.add(packet(4, 200, b"\x61\x80p"))
    now[0] = 0.05
    assert jitter.add(packet(2, 100, b"\x7c\x05b", marker=False))[1].timestamp == 100
    now[0] = 0.15
    assert jitter.add(packet(5, 300, b"\x61\x80q"))[1].timestamp == 200
    assert jitter.snapshot()["loss_events"] == 0


@pytest.fixture(scope="module", params=[False, True], ids=["ordinary", "large-idr"])
def encoded_pictures(request):
    width, height = (640, 360) if request.param else (160, 96)
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = width, height, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 30), Fraction(30, 1)
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "crf": "18",
        "x264-params": "keyint=6:min-keyint=6:scenecut=0:repeat-headers=1",
    }
    noise = np.random.default_rng(715).integers(0, 256, (height, width, 3), dtype=np.uint8)
    pictures = []
    for index in range(12):
        image = noise.copy()
        image[:8, :8] = index * 16
        frame = av.VideoFrame.from_ndarray(image, format="rgb24")
        frame.pts = index
        pictures.extend(bytes(value) for value in encoder.encode(frame))
    pictures.extend(bytes(value) for value in encoder.encode(None))
    assert len(pictures) == 12
    if request.param:
        assert sum(index == 0 for _, index, _ in _trace(pictures)[0]) > 128
    return pictures


def _trace(pictures, *, wrap=False, paced=False):
    sequence, timestamp = (65500, 0xFFFFF000) if wrap else (1000, 90000)
    trace, expected = [], []
    start = 0.0
    for index, picture in enumerate(pictures):
        nals = [nal for nal in re.split(b"\x00\x00\x00?\x01", picture) if nal]
        expected.append(b"".join(b"\0\0\0\1" + nal for nal in nals))
        payloads = []
        for nal in nals:
            if len(nal) <= 1000:
                payloads.append(nal)
            else:
                chunks = [nal[offset : offset + 998] for offset in range(1, len(nal), 998)]
                for part, chunk in enumerate(chunks):
                    header = (
                        (nal[0] & 31)
                        | (0x80 if part == 0 else 0)
                        | (0x40 if part == len(chunks) - 1 else 0)
                    )
                    payloads.append(bytes([(nal[0] & 0xE0) | 28, header]) + chunk)
        duration = 0.5 if paced and len(payloads) > 128 else (len(payloads) - 1) * 0.000015
        for part, payload in enumerate(payloads):
            due = start + duration * part / max(1, len(payloads) - 1)
            trace.append(
                (
                    due,
                    index,
                    packet(
                        sequence,
                        (timestamp + index * 3000) & 0xFFFFFFFF,
                        payload,
                        marker=part == len(payloads) - 1,
                    ),
                )
            )
            sequence += 1
        start += duration + 1 / 30
    return trace, expected


async def _receive(trace, *, rtx_repair=False):
    receiver = RTCRtpReceiver("video", SimpleNamespace(state="new"))
    assert isinstance(receiver._RTCRtpReceiver__jitter_buffer, VideoJitterBuffer)
    now = [0.0]
    receiver._RTCRtpReceiver__jitter_buffer._h264.clock = lambda: now[0]
    # Exercise the real depayloader, NACK/PLI, RTX and decoder-queue boundary
    # without starting a decoder thread or opening a transport.
    receiver._RTCRtpReceiver__decoder_thread = True
    receiver._set_rtcp_ssrc(999)
    receiver._RTCRtpReceiver__codecs[96] = RTCRtpCodecParameters(
        mimeType="video/H264", clockRate=90000, payloadType=96
    )
    receiver._RTCRtpReceiver__codecs[97] = RTCRtpCodecParameters(
        mimeType="video/rtx", clockRate=90000, payloadType=97, parameters={"apt": 96}
    )
    receiver._RTCRtpReceiver__rtx_ssrc[8] = 7
    feedback = []

    async def send_rtcp(value):
        feedback.append(value)

    receiver._send_rtcp = send_rtcp
    withheld = None
    try:
        for index, (due, _, value) in enumerate(trace):
            now[0] = due
            if rtx_repair and index == 4:
                withheld = value
                continue
            await receiver._handle_rtp_packet(value, int(due * 1000))
            if withheld is not None and index == 6:
                repaired = RtpPacket(
                    payload_type=97, sequence_number=77, timestamp=withheld.timestamp
                )
                repaired.ssrc, repaired.marker = 8, withheld.marker
                repaired.payload = withheld.sequence_number.to_bytes(2, "big") + withheld.payload
                await receiver._handle_rtp_packet(repaired, int(due * 1000))
                withheld = None
    finally:
        receiver._RTCRtpReceiver__decoder_thread = None
    output = []
    queue = receiver._RTCRtpReceiver__decoder_queue
    while not queue.empty():
        codec, frame = queue.get_nowait()
        assert codec.mimeType == "video/H264"
        output.append(frame)
    return output, feedback, receiver._RTCRtpReceiver__jitter_buffer.snapshot()


@pytest.mark.parametrize("case", ["ordinary", "reorder", "wrap", "paced", "delayed-boundary"])
async def test_real_receiver_preserves_and_decodes_every_complete_picture(encoded_pictures, case):
    trace, expected = _trace(encoded_pictures, wrap=case == "wrap", paced=case == "paced")
    if case == "reorder":
        trace[3], trace[4] = (trace[3][0], *trace[4][1:]), (trace[4][0], *trace[3][1:])
    if case == "delayed-boundary":
        trace = [(due + (0.18 if index else 0), index, value) for due, index, value in trace]
    output, _, stats = await _receive(trace)
    assert [frame.data for frame in output] == expected
    assert [frame.timestamp for frame in output] == list(range(0, 12 * 3000, 3000))
    assert stats["completed_aus"] == 12
    assert stats["loss_events"] == stats["reorder_timeouts"] == stats["assembly_timeouts"] == 0
    decoder = av.CodecContext.create("h264", "r")
    assert sum(len(decoder.decode(av.Packet(frame.data))) for frame in output) == 12


async def test_real_receiver_loss_requests_keyframe_and_recovers_cleanly(encoded_pictures):
    trace, expected = _trace(encoded_pictures)
    del trace[4]  # One FU-A fragment is genuinely absent, not reordered.
    output, feedback, stats = await _receive(trace)
    assert [frame.data for frame in output] == expected[6:]
    assert any(type(value).__name__ == "RtcpPsfbPacket" and value.fmt == 1 for value in feedback)
    assert stats["loss_events"] == 1 and not stats["waiting_for_idr"]
    decoder = av.CodecContext.create("h264", "r")
    assert sum(len(decoder.decode(av.Packet(frame.data))) for frame in output) == 6


async def test_real_receiver_rtx_repairs_fragment_before_deadline(encoded_pictures):
    trace, expected = _trace(encoded_pictures)
    output, _, stats = await _receive(trace, rtx_repair=True)
    assert [frame.data for frame in output] == expected
    assert stats["loss_events"] == 0


def test_other_codec_uses_generic_video_buffer_without_retaining_receiver():
    class Receiver:
        pass

    receiver = Receiver()
    codecs = receiver._RTCRtpReceiver__codecs = {96: SimpleNamespace(mimeType="video/VP8")}
    jitter = VideoJitterBuffer(receiver)
    owner = weakref.ref(receiver)
    del receiver
    assert owner() is None
    assert jitter.add(packet(1, 100))[1] is None
    assert jitter.add(packet(2, 200))[1].timestamp == 100
    assert jitter.snapshot() is None
    # Negotiation mutates the original map in place. A codec change drops any
    # old pending assembly; H.264 can then release a complete IDR immediately.
    codecs[96] = SimpleNamespace(mimeType="video/H264")
    assert jitter.add(packet(3, 300))[1].timestamp == 300


def test_receiver_hook_is_idempotent_and_rejects_unvalidated_version(monkeypatch):
    installed = RTCRtpReceiver.__init__
    assert install_video_receiver_patch()
    assert RTCRtpReceiver.__init__ is installed
    monkeypatch.setattr(aiortc, "__version__", "1.16.0")
    assert not install_video_receiver_patch()
    assert RTCRtpReceiver.__init__ is installed


@pytest.mark.parametrize("boundary", ["constructor", "packet-handler"])
def test_receiver_hook_rejects_changed_private_contract(monkeypatch, boundary):
    original = RTCRtpReceiver.__init__.__wrapped__
    monkeypatch.setattr(RTCRtpReceiver, "_halo_h264_assembly_patch_applied", False)
    monkeypatch.setattr(RTCRtpReceiver, "__init__", original)
    if boundary == "constructor":
        monkeypatch.setattr(RTCRtpReceiver, "__init__", lambda self, kind, transport: None)
    else:

        async def changed(self, packet, arrival_time_ms):
            return None

        monkeypatch.setattr(RTCRtpReceiver, "_handle_rtp_packet", changed)
    before = RTCRtpReceiver.__init__
    assert not install_video_receiver_patch()
    assert RTCRtpReceiver.__init__ is before
