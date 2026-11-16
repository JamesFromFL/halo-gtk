"""Bounded H.264 assembly at the pinned aiortc depayloaded-packet boundary.

RFC 6184 sections 5.1 and 5.8: marker is an early AU-end indication, with
timestamp transition as a fallback; FU-A fragments must be consecutive in RTP
sequence order. Adapted from Halo Server's receive assembler. No HomeKit output,
decoded media, playout timer or transport policy lives here.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections import deque

import aiortc
from aiortc.jitterbuffer import JitterBuffer, JitterFrame
from aiortc.rtcrtpreceiver import RTCRtpReceiver

_log = logging.getLogger(__name__)

PACKET_CAPACITY = 512
MAX_BUFFER_BYTES = 1024 * 1024  # payload plus aiortc's depayloaded copy
MAX_PENDING_TIMESTAMPS = 3
REORDER_GRACE_SECONDS = 0.100
MAX_ASSEMBLY_SECONDS = 1.0
PLI_RETRY_SECONDS = 1.0
_COUNTER_LIMIT = 2**53 - 1


class _IncompleteH264(ValueError):
    """A prefix/fragment may still arrive out of order within the grace bound."""


def _validate_packets(packets, *, require_start):
    """Validate RTP NAL boundaries before accepting aiortc's concatenated data.

    aiortc's depayloader does not validate FU end/start pairing. In particular,
    concatenating a middle FU after eviction could otherwise emit a partial IDR.
    This deliberately supports non-interleaved single NAL, STAP-A and FU-A only.
    """
    active = None
    first_vcl = None
    vcl_types = set()

    def nal_header(nal):
        nonlocal first_vcl
        if not nal or nal[0] & 0x80 or not 1 <= nal[0] & 31 <= 23:
            raise ValueError("invalid H.264 NAL")
        kind = nal[0] & 31
        if kind in {2, 3, 4}:
            raise ValueError("partitioned H.264 slices are not supported")
        if kind in {1, 5}:
            if len(nal) < 2:
                raise ValueError("truncated H.264 slice")
            vcl_types.add(kind)
            if first_vcl is None:
                # first_mb_in_slice is unsigned Exp-Golomb: 0 is the single bit 1.
                # Requiring it protects startup/recovery from joining a later slice.
                first_vcl = kind
                if require_start and not nal[1] & 0x80:
                    raise _IncompleteH264("first VCL slice does not start at macroblock zero")

    for packet in packets:
        payload = packet.payload
        if len(payload) < 2 or payload[0] & 0x80:
            raise ValueError("invalid H.264 payload")
        kind = payload[0] & 31
        if kind == 28:
            if len(payload) < 3 or payload[1] & 0x20:
                raise ValueError("invalid FU-A header")
            start, end = bool(payload[1] & 0x80), bool(payload[1] & 0x40)
            header = (payload[0] & 0xE0) | (payload[1] & 31)
            if start and end:
                raise ValueError("FU-A start and end cannot both be set")
            if start:
                if active is not None:
                    raise ValueError("FU-A start before previous end")
                nal_header(bytes([header]) + payload[2:])
                active = header
            elif active != header:
                raise _IncompleteH264("FU-A continuation without matching start")
            if end:
                active = None
        else:
            if active is not None:
                raise _IncompleteH264("NAL before FU-A end")
            if kind == 24:
                offset = 1
                while offset < len(payload):
                    if offset + 2 > len(payload):
                        raise ValueError("truncated STAP-A length")
                    size = int.from_bytes(payload[offset : offset + 2], "big")
                    offset += 2
                    if size < 1 or offset + size > len(payload):
                        raise ValueError("invalid STAP-A NAL length")
                    nal_header(payload[offset : offset + size])
                    offset += size
            else:
                nal_header(payload)
    if active is not None:
        raise _IncompleteH264("AU ends before FU-A end")
    if len(vcl_types) > 1:
        raise ValueError("mixed IDR and dependent slices in one AU")
    return first_vcl


class H264JitterBuffer:
    """Finite reorder window; healthy complete AUs have no added wait.

    Limits expire on packet arrival. Idle streams allocate no timer and cannot
    grow memory. After loss, dependent pictures are discarded until a complete
    IDR arrives. Config-only AUs may still pass for downstream decoder setup.
    """

    def __init__(
        self,
        *,
        capacity=PACKET_CAPACITY,
        max_bytes=MAX_BUFFER_BYTES,
        grace=REORDER_GRACE_SECONDS,
        assembly_age=MAX_ASSEMBLY_SECONDS,
        clock=time.monotonic,
    ):
        self._capacity = capacity
        self.max_bytes = max_bytes
        self.grace = grace
        self.assembly_age = assembly_age
        self._gap_since = None
        self.clock = clock
        self._packets = {}
        self._bytes = 0
        self._high = None
        self._next = None
        self._retired = None
        self._identity = None
        self._latest_timestamp = None
        self._idr_timestamp = None
        self._rejected = deque(maxlen=8)
        self.waiting_for_idr = True
        self._last_pli = None
        self._recovering = False
        self._signal = False
        self._counts = dict.fromkeys(
            (
                "completed_aus",
                "discarded_aus",
                "loss_events",
                "recovery_requests",
                "reorder_timeouts",
                "assembly_timeouts",
            ),
            0,
        )

    @property
    def capacity(self):
        return self._capacity

    @property
    def buffered_packets(self):
        return len(self._packets)

    @property
    def buffered_bytes(self):
        return self._bytes

    def _count(self, name, value=1):
        self._counts[name] = min(_COUNTER_LIMIT, self._counts[name] + value)

    def snapshot(self):
        return {
            **self._counts,
            "buffered_packets": self.buffered_packets,
            "buffered_bytes": self.buffered_bytes,
            "waiting_for_idr": self.waiting_for_idr,
        }

    def _loss(self):
        self.waiting_for_idr = True
        if not self._recovering:
            self._count("loss_events")
            self._recovering = True
        now = self.clock()
        if self._last_pli is None or now - self._last_pli >= PLI_RETRY_SECONDS:
            self._signal = True
            self._last_pli = now
            self._count("recovery_requests")

    def _remove(self, end):
        self._gap_since = None
        for sequence in [s for s in self._packets if s <= end]:
            _, _, size = self._packets.pop(sequence)
            self._bytes -= size
        self._retired = end if self._retired is None else max(end, self._retired)
        self._next = end + 1

    def _drop_oldest(self):
        if not self._packets:
            self._loss()
            return
        first = min(self._packets)
        timestamp = self._packets[first][0].timestamp
        self._discard_timestamp(timestamp)

    def _discard_timestamp(self, timestamp):
        self._loss()
        self._rejected.append(timestamp)
        matching = [s for s, (p, _, _) in self._packets.items() if p.timestamp == timestamp]
        if matching:
            end = max(matching)
            self._count(
                "discarded_aus",
                len({p.timestamp for s, (p, _, _) in self._packets.items() if s <= end}),
            )
            self._remove(end)

    def _mark_gap(self, now, known=True):
        if not known:
            self._gap_since = None
        elif self._gap_since is None:
            self._gap_since = now

    def _expire_pending(self, now):
        # Check existing evidence BEFORE inserting a possible repair. The short
        # grace starts only once a hole/prefix is known, not at a healthy AU's
        # first fragment. A separate long bound covers endless contiguous input.
        while self._packets:
            first = min(self._packets)
            timestamp = self._packets[first][0].timestamp
            oldest = min(t for p, t, _ in self._packets.values() if p.timestamp == timestamp)
            if self._gap_since is not None and now - self._gap_since >= self.grace:
                self._count("reorder_timeouts")
                if self._next not in self._packets:
                    # Missing data precedes this retained AU: lose continuity,
                    # but allow an intact retained IDR to establish a new epoch.
                    self._loss()
                    self._next = first
                    self._gap_since = None
                    continue
            elif now - oldest >= self.assembly_age:
                self._count("assembly_timeouts")
            else:
                return
            self._drop_oldest()

    def _window_exceeded(self):
        return (
            len({v[0].timestamp for v in self._packets.values()}) >= MAX_PENDING_TIMESTAMPS
            or max(self._packets) - self._next >= self.capacity
        )

    def _take(self, now):
        # This loop can discard dependent AUs during recovery; at most capacity
        # packets are inspected. aiortc accepts one returned AU per RTP arrival.
        for _ in range(self.capacity):
            self._expire_pending(now)
            if not self._packets:
                return None
            first = min(self._packets)
            if self._next not in self._packets:
                self._mark_gap(now)
                if self.waiting_for_idr or self._window_exceeded():
                    self._loss()
                    self._next = first
                    self._gap_since = None
                else:
                    return None
            timestamp = self._packets[self._next][0].timestamp
            packets = []
            end = None
            gap_known = False
            for sequence in range(self._next, self._next + self.capacity):
                entry = self._packets.get(sequence)
                if entry is None:
                    gap_known = sequence < max(self._packets)
                    break
                packet = entry[0]
                if packet.timestamp != timestamp:
                    end = sequence - 1  # marker-free, contiguous boundary fallback
                    break
                packets.append(packet)
                if packet.marker:
                    end = sequence
                    break
            if end is not None:
                try:
                    vcl = _validate_packets(packets, require_start=self.waiting_for_idr)
                except _IncompleteH264:
                    gap_known = True  # A reordered prefix/end may still arrive.
                except ValueError:
                    self._drop_oldest()
                    continue
                else:
                    self._remove(end)
                    if self.waiting_for_idr and vcl == 1:
                        self._loss()
                        self._count("discarded_aus")
                        continue
                    if vcl == 5:
                        self.waiting_for_idr = False
                        self._last_pli = None
                        self._recovering = False
                        self._idr_timestamp = timestamp
                    self._count("completed_aus")
                    return JitterFrame(
                        data=b"".join(packet._data for packet in packets), timestamp=timestamp
                    )
            self._mark_gap(now, gap_known)
            if self._window_exceeded():
                self._drop_oldest()
                continue
            return None
        return None

    def add(self, packet):
        now = self.clock()
        self._signal = False
        identity = (packet.ssrc, packet.payload_type)
        sequence = packet.sequence_number
        if self._high is not None:
            sequence = self._high + ((sequence - self._high + 32768) % 65536) - 32768
        timestamp_advanced = self._latest_timestamp is not None and (
            0 < (packet.timestamp - self._latest_timestamp) % (2**32) < 2**31
        )
        restarted_sequence = (
            self._high is not None and self._high - sequence >= self.capacity and timestamp_advanced
        )
        if self._identity is not None and (self._identity != identity or restarted_sequence):
            self._count("discarded_aus", len({v[0].timestamp for v in self._packets.values()}))
            self._packets.clear()
            self._bytes = 0
            self._gap_since = None
            self._high = self._next = self._retired = None
            self._latest_timestamp = packet.timestamp
            self._idr_timestamp = None
            self._rejected.clear()
            self._last_pli = None
            self._loss()
            sequence = packet.sequence_number
        self._identity = identity
        self._expire_pending(now)
        if self._idr_timestamp is not None and (
            (packet.timestamp - self._idr_timestamp) % (2**32) >= 2**31
        ):
            # A completed IDR begins a new decoding epoch. Late old-GOP packets
            # cannot be reintroduced after sequence restart or late RTX repair.
            return self._signal, None
        if self._latest_timestamp is None or timestamp_advanced:
            self._latest_timestamp = packet.timestamp
        self._high = sequence if self._high is None else max(sequence, self._high)
        if self._retired is not None and sequence <= self._retired:
            return self._signal, None
        if packet.timestamp in self._rejected:
            return self._signal, None
        existing = self._packets.get(sequence)
        if existing:
            previous = existing[0]
            if (previous.timestamp, previous.marker, previous.payload) != (
                packet.timestamp,
                packet.marker,
                packet.payload,
            ):
                self._discard_timestamp(previous.timestamp)
            return self._signal, None
        size = len(packet.payload) + len(packet._data)
        if size > self.max_bytes:
            self._discard_timestamp(packet.timestamp)
            return self._signal, None
        while self._packets and (
            len(self._packets) >= self.capacity or self._bytes + size > self.max_bytes
        ):
            self._drop_oldest()
        if packet.timestamp in self._rejected:
            return self._signal, None
        self._packets[sequence] = (packet, now, size)
        self._bytes += size
        if self._next is None or (self._retired is None and sequence < self._next):
            self._next = sequence
        frame = self._take(now)
        return self._signal, frame


class VideoJitterBuffer:
    """H.264-only dispatch without changing aiortc's packet-handler contract."""

    def __init__(self, receiver):
        # aiortc 1.15.0 populates this dictionary in place during receive().
        # Retaining the receiver would create a cycle holding buffered media.
        self._codecs = receiver._RTCRtpReceiver__codecs
        self._capacity = PACKET_CAPACITY
        self._h264 = H264JitterBuffer()
        self._fallback = JitterBuffer(capacity=PACKET_CAPACITY, is_video=True)
        self._kind = None

    @property
    def capacity(self):
        return self._capacity

    def add(self, packet):
        codec = self._codecs.get(packet.payload_type)
        kind = str(getattr(codec, "mimeType", "")).lower()
        if self._kind is not None and kind != self._kind:
            self._h264 = H264JitterBuffer()
            self._fallback = JitterBuffer(capacity=PACKET_CAPACITY, is_video=True)
        self._kind = kind
        if kind == "video/h264":
            return self._h264.add(packet)
        return self._fallback.add(packet)

    def snapshot(self):
        return self._h264.snapshot() if self._kind == "video/h264" else None


def install_video_receiver_patch() -> bool:
    """Replace only the pinned receiver's video assembly boundary."""
    if aiortc.__version__ != "1.15.0":
        _log.warning("H.264 receive assembly is not validated for aiortc %s", aiortc.__version__)
        return False
    if getattr(RTCRtpReceiver, "_halo_h264_assembly_patch_applied", False):
        return True

    original_init = RTCRtpReceiver.__init__
    init_code = getattr(original_init, "__code__", None)
    handler = RTCRtpReceiver._handle_rtp_packet
    handler_code = getattr(handler, "__code__", None)
    if (
        init_code is None
        or init_code.co_varnames[:3] != ("self", "kind", "transport")
        or not {
            "_RTCRtpReceiver__codecs",
            "_RTCRtpReceiver__jitter_buffer",
            "JitterBuffer",
        }.issubset(init_code.co_names)
        or not inspect.iscoroutinefunction(handler)
        or handler_code is None
        or handler_code.co_varnames[:3] != ("self", "packet", "arrival_time_ms")
        or not {
            "depayload",
            "unwrap_rtx",
            "_RTCRtpReceiver__jitter_buffer",
            "_RTCRtpReceiver__decoder_queue",
            "_send_rtcp_pli",
        }.issubset(handler_code.co_names)
    ):
        _log.warning("aiortc 1.15.0 receiver contract changed; video assembly is unpatched")
        return False

    @functools.wraps(original_init)
    def initialize(self, kind, transport):
        original_init(self, kind, transport)
        if kind == "video":
            self._RTCRtpReceiver__jitter_buffer = VideoJitterBuffer(self)

    RTCRtpReceiver.__init__ = initialize
    RTCRtpReceiver._halo_jitter_buffer_patch_applied = True
    RTCRtpReceiver._halo_h264_assembly_patch_applied = True
    return True
