#!/usr/bin/env python3
"""Compare Halo's RGB bridge with planar I420 on isolated H.264/Opus loopbacks.

One process measures one configuration. No Ring service, account or audio hardware
is used. An isolated GTK display is required. See doc/live-renderer-benchmark.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import resource
import statistics
import threading
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import av
import gi
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender
from aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gdk, GLib, Gst, GstVideo, Gtk  # noqa: E402

from halo_gtk import live_stream  # noqa: E402
from halo_gtk.camera_privacy import CameraPrivacy, privacy_time, ring_privacy_receipt  # noqa: E402
from halo_gtk.frame_timing import FrameClock  # noqa: E402

FPS = 30
RATE = 48000


def generate_packets(width, height):
    """Encode a deterministic one-second Annex B loop once, before measuring."""
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = width, height
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, FPS)
    encoder.thread_count = 1
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x264-params": "keyint=30:scenecut=0",
    }
    y, x = np.ogrid[:height, :width]
    scene = (32 + (x // 5 + y // 3) % 128).astype("uint8")
    packets = []
    for i in range(FPS):
        image = np.roll(scene, i * 3, axis=1)
        image[:80, :80] = 235 if i < 3 else 16
        frame = av.VideoFrame.from_ndarray(image, format="gray").reformat(format="yuv420p")
        frame.pts = i
        packets.extend(bytes(p) for p in encoder.encode(frame))
    packets.extend(bytes(p) for p in encoder.encode(None))
    assert len(packets) == FPS
    return packets


class Source:
    def __init__(self, burst_ms, steady_after):
        self.start = 0.0
        self.ready = asyncio.Event()
        self.burst = burst_ms / 1000
        self.steady_after = steady_after

    async def pace(self, at):
        await self.ready.wait()
        if self.burst and (not self.steady_after or at < self.steady_after):
            at = math.ceil(at / self.burst) * self.burst
        await asyncio.sleep(max(0.0, self.start + at - time.monotonic()))


class Video(VideoStreamTrack):
    def __init__(self, source, packets):
        super().__init__()
        self.source = source
        self.packets = packets
        self.sent = 0

    async def recv(self):
        await self.source.pace(self.sent / FPS)
        packet = av.Packet(self.packets[self.sent % FPS])
        packet.pts, packet.time_base = self.sent * (90000 // FPS), Fraction(1, 90000)
        self.sent += 1
        return packet


class Audio(AudioStreamTrack):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.index = 0
        self.tone = (8000 * np.sin(2 * np.pi * 500 * np.arange(960) / RATE)).astype("int16")
        self.silence = np.zeros(960, dtype="int16")

    async def recv(self):
        await self.source.pace(self.index / 50)
        data = self.tone if self.index % 50 < 5 else self.silence
        frame = av.AudioFrame.from_ndarray(data.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts, frame.time_base = self.index * 960, Fraction(1, RATE)
        self.index += 1
        return frame


class MeasuredTrack:
    def __init__(self, track, source):
        self.track = track
        self.source = source
        self.frames = 0
        self.age = []

    async def recv(self):
        frame = await self.track.recv()
        self.frames += 1
        self.age.append(
            1000 * (time.monotonic() - self.source.start - float(frame.pts * frame.time_base))
        )
        return frame


async def receive_i420(track, view, resources):
    """Preserve padded decoder planes and move color conversion into GStreamer."""
    clock = FrameClock()
    dims = None
    while view._is_current_resources(resources) and view._privacy_current(resources):
        frame = await track.recv()
        if frame.format.name != "yuv420p":
            raise ValueError(f"Candidate requires planar I420, got {frame.format.name}")
        if dims != (frame.width, frame.height):
            dims = frame.width, frame.height
            caps = f"video/x-raw,format=I420,width={dims[0]},height={dims[1]},framerate=0/1"
            view._video_appsrc.set_property("caps", Gst.Caps.from_string(caps))
        buf = Gst.Buffer.new()
        offsets, strides = [], []
        for plane in frame.planes:
            offsets.append(buf.get_size())
            strides.append(plane.line_size)
            buf = buf.append(Gst.Buffer.new_wrapped(bytes(plane)))
        GstVideo.buffer_add_video_meta_full(
            buf,
            GstVideo.VideoFrameFlags.NONE,
            GstVideo.VideoFormat.I420,
            frame.width,
            frame.height,
            3,
            offsets + [0],
            strides + [0],
        )
        buf.pts, discontinuity = clock.stamp(frame, Gst.SECOND // FPS)
        buf.dts = buf.pts
        if discontinuity:
            buf.set_flags(Gst.BufferFlags.DISCONT)

        # Retain a screenshot source like the RGB path without eagerly converting it.
        def push(buf=buf, frame=frame):
            view._benchmark_frame = frame
            if view._video_appsrc.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                raise RuntimeError("Candidate video sink stopped")

        if not resources.privacy_lease.policy.apply(resources.privacy_lease, push):
            break


class Camera:
    def __init__(self, index, args):
        self.args = args
        self.view = live_stream.LiveStreamView()
        policy = CameraPrivacy()
        device = SimpleNamespace(
            id=index,
            _attrs={
                "settings": {
                    "live_view_disabled": False,
                    "motion_detection_enabled": True,
                    "enable_audio_recording": True,
                }
            },
        )
        policy.observe(ring_privacy_receipt([device], privacy_time()))
        self.resources = live_stream._StreamResources(
            token=0,
            device=device,
            session_id="synthetic",
            client=SimpleNamespace(_preview_is_current=lambda: True),
            privacy_lease=policy.acquire(device, "live"),
        )
        self.view._resources = self.resources
        self.view._pipeline.set_state(Gst.State.PLAYING)
        self.view.set_volume(1.0)
        self.marks = {"video": {}, "audio": {}}
        self.high = {"video": False, "audio": False}
        self.age = []
        self.source_pts = []
        self.paint = []
        self.rendered = 0
        self.tracks = {}
        self.tasks = []
        self.packets_lost = 0
        self.video_source = None
        self.source = None
        self.probes = []
        for kind, sink in (
            ("video", self.view._pipeline.get_by_name("vsink")),
            ("audio", self.view._audio_pipeline.get_by_name("asink")),
        ):
            pad = sink.get_static_pad("sink")
            self.probes.append((pad, pad.add_probe(Gst.PadProbeType.BUFFER, self.sink_probe, kind)))
        self.handler = self.view._paintable.connect("invalidate-contents", self.paint_changed)

    def paint_changed(self, paintable):
        if self.rendered and paintable.get_intrinsic_width() > 0:
            self.paint.append(time.monotonic())

    def sink_probe(self, pad, info, kind):
        buffer = info.get_buffer()
        if kind == "video":
            if self.args.sink_delay_ms and (
                not self.args.steady_after
                or time.monotonic() - self.source.start < self.args.steady_after
            ):
                time.sleep(self.args.sink_delay_ms / 1000)
            self.rendered += 1
            caps = pad.get_current_caps().get_structure(0).get_string("format")
            first = buffer.extract_dup(0, 4)
            high = first[1 if caps in {"ARGB", "ABGR", "xRGB", "xBGR"} else 0] > 190
        else:
            data = np.frombuffer(buffer.extract_dup(0, min(512, buffer.get_size())), dtype="<i2")
            high = bool(np.max(data) > 1500)
        now = time.monotonic()
        pts = buffer.pts / Gst.SECOND
        if kind == "video":
            self.age.append(1000 * (now - self.source.start - pts))
            self.source_pts.append(pts)
        if high and not self.high[kind] and round(pts) > 0:
            self.marks[kind][round(pts)] = now
        self.high[kind] = high
        return Gst.PadProbeReturn.OK

    async def connect(self, source, packets):
        self.source = source
        self.video_source = Video(source, packets)
        self.sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.sender.addTrack(self.video_source)
        self.sender.addTrack(Audio(source))
        for transceiver in self.sender.getTransceivers():
            if transceiver.kind == "video":
                codecs = RTCRtpSender.getCapabilities("video").codecs
                transceiver.setCodecPreferences([c for c in codecs if c.mimeType == "video/H264"])

        @self.receiver.on("track")
        def got_track(raw):
            track = self.tracks[raw.kind] = MeasuredTrack(raw, source)
            if raw.kind == "audio":
                consumer = self.view._receive_audio_frames(track, self.resources)
            elif self.args.bridge == "rgb":
                consumer = self.view._receive_frames(track, self.resources)
            else:
                consumer = receive_i420(track, self.view, self.resources)
            self.tasks.append(asyncio.create_task(consumer))

        await self.sender.setLocalDescription(await self.sender.createOffer())
        await self.receiver.setRemoteDescription(self.sender.localDescription)
        await self.receiver.setLocalDescription(await self.receiver.createAnswer())
        await self.sender.setRemoteDescription(self.receiver.localDescription)
        async with asyncio.timeout(10):
            while self.receiver.connectionState != "connected":
                await asyncio.sleep(0.01)

    async def close_peers(self):
        try:
            stats = await self.receiver.getStats()
            self.packets_lost = sum(
                entry.packetsLost for entry in stats.values() if entry.type == "inbound-rtp"
            )
        finally:
            self.view._resources = None
            for task in self.tasks:
                task.cancel()
            results = await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.sender.close()
            await self.receiver.close()
            assert self.sender.connectionState == self.receiver.connectionState == "closed"
            for error in results:
                if isinstance(error, Exception):
                    raise error

    def close_ui(self):
        for pad, probe in self.probes:
            pad.remove_probe(probe)
        self.view._paintable.disconnect(self.handler)
        self.view._last_frame_rgb = None
        self.view._benchmark_frame = None
        self.view._stop_audio_output()
        self.view._pipeline.set_state(Gst.State.NULL)
        self.view._detach_bus_watch()


async def measure(cameras, packets, args):
    source = Source(args.burst_ms, args.steady_after)
    admission = time.monotonic()
    with patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"]):
        try:
            await asyncio.gather(*(cam.connect(source, packets) for cam in cameras))
            cpu = time.process_time()
            source.start = time.monotonic()
            source.ready.set()
            await asyncio.sleep(args.seconds)
            elapsed = time.monotonic() - source.start
            cpu_percent = 100 * (time.process_time() - cpu) / elapsed
            counts = [
                (cam.video_source.sent, cam.tracks["video"].frames, cam.rendered, len(cam.paint))
                for cam in cameras
            ]
            ages = [age for cam in cameras for age in cam.age]
            skew = []
            for cam in cameras:
                video, audio = cam.marks["video"], cam.marks["audio"]
                skew.extend(1000 * (video[i] - audio[i]) for i in video.keys() & audio.keys())
            if not skew or any(not cam.paint for cam in cameras):
                raise RuntimeError("Missing flashed/tone markers or native paintable updates")
            stages = {}
            if args.steady_after:
                for name, start, end in (
                    ("stress", 0, args.steady_after),
                    ("settled", args.steady_after + 1, args.seconds),
                ):
                    sample_ages = [
                        age
                        for cam in cameras
                        for pts, age in zip(cam.source_pts, cam.age, strict=True)
                        if start <= pts < end
                    ]
                    sample_skews = [
                        1000 * abs(cam.marks["video"][i] - cam.marks["audio"][i])
                        for cam in cameras
                        for i in cam.marks["video"].keys() & cam.marks["audio"].keys()
                        if start <= i < end
                    ]
                    if not sample_ages or not sample_skews:
                        raise RuntimeError(f"No timing samples for {name}")
                    stages[name] = {
                        "video_age_p95_ms": round(float(np.percentile(sample_ages, 95)), 1),
                        "abs_skew_p95_ms": round(float(np.percentile(sample_skews, 95)), 1),
                        "pulses": len(sample_skews),
                    }
            return {
                **vars(args),
                "live_stream_sha256": hashlib.sha256(
                    Path(live_stream.__file__).read_bytes()
                ).hexdigest(),
                "stages": stages,
                "elapsed_s": round(elapsed, 3),
                "cpu_pct": round(cpu_percent, 1),
                "peak_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                "first_paint_ms": [round(1000 * (cam.paint[0] - admission)) for cam in cameras],
                "first_paint_after_source_ms": [
                    round(1000 * (cam.paint[0] - source.start)) for cam in cameras
                ],
                "sent_decoded_sink_paints": counts,
                "appsrc_drops": sum(
                    cam.view._video_appsrc.get_property("dropped") for cam in cameras
                ),
                "decoded_age_p95_ms": {
                    kind: round(
                        float(
                            np.percentile(
                                [age for cam in cameras for age in cam.tracks[kind].age], 95
                            )
                        ),
                        1,
                    )
                    for kind in ("video", "audio")
                },
                "age_p50_p95_ms": np.percentile(ages, [50, 95]).round(1).tolist(),
                "paired_pulses": len(skew),
                "video_minus_audio_median_ms": round(statistics.median(skew), 1),
                "absolute_skew_p95_ms": round(float(np.percentile(np.abs(skew), 95)), 1),
            }
        finally:
            await asyncio.gather(*(cam.close_peers() for cam in cameras if hasattr(cam, "sender")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", choices=("rgb", "i420"), required=True)
    parser.add_argument("--cameras", choices=(1, 4), type=int, required=True)
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument(
        "--steady-after",
        type=int,
        default=0,
        help="End stress and return to regular pacing after N seconds",
    )
    parser.add_argument("--cycles", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--burst-ms", type=int, choices=(0, 120), default=0)
    parser.add_argument("--sink-delay-ms", type=int, choices=(0, 40), default=0)
    args = parser.parse_args()
    if (
        not 3 <= args.seconds <= 100
        or not 80 <= args.width <= 1920
        or not 80 <= args.height <= 1080
    ):
        parser.error("Use 3–100 seconds at 80×80 through 1920×1080")
    if args.steady_after and not 3 <= args.steady_after <= args.seconds - 3:
        parser.error("Leave at least three seconds in each phase")
    if args.width % 2 or args.height % 2:
        parser.error("I420 fixtures require even dimensions")
    if os.environ.get("GDK_BACKEND") != "broadway":
        parser.error("Use an isolated Broadway display")
    Gst.init(None)
    Gtk.init()
    if Gdk.Display.get_default() is None:
        parser.error("The isolated GTK display is unavailable")
    packets = generate_packets(args.width, args.height)
    for cycle in range(1, args.cycles + 1):
        result = run_cycle(args, packets)
        print(json.dumps({**result, "cycle": cycle}), flush=True)


def run_cycle(args, packets):
    loop = GLib.MainLoop()
    result, errors = [], []
    with patch.object(
        live_stream,
        "_audio_sink_description",
        return_value="fakesink name=asink sync=false async=false",
    ):
        cameras = [Camera(i, args) for i in range(args.cameras)]
        window = Gtk.Window(default_width=640, default_height=360)
        grid = Gtk.Grid()
        for i, cam in enumerate(cameras):
            grid.attach(cam.view, i % 2, i // 2, 1, 1)
        window.set_child(grid)
        window.present()

        def run():
            try:
                result.append(asyncio.run(measure(cameras, packets, args)))
            except BaseException as exc:
                errors.append(exc)
            finally:
                GLib.idle_add(loop.quit)

        worker = threading.Thread(target=run)
        worker.start()
        loop.run()
        worker.join()
        for cam in cameras:
            cam.close_ui()
        window.destroy()
    if errors:
        raise errors[0]
    result[0]["rtp_packets_lost"] = sum(cam.packets_lost for cam in cameras)
    return result[0]


if __name__ == "__main__":
    main()
