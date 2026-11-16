# Camera receive-path review — 16 November 2026

This review compared Halo GTK's direct Ring receive path with Halo Server's
receive-side fixes. Tests used synthetic packets, generated video/audio and
loopback peers. No Ring account, camera, microphone, speaker or alarm was used.

## Confirmed H.264 recovery gap

GTK previously replaced aiortc's 128-packet video buffer with its generic
512-packet buffer. That accommodated large keyframes but retained three
limitations: completed pictures waited for a later RTP timestamp, an orphan
FU-A continuation could be forwarded to the decoder, and an unrepaired packet
hole could hold subsequent pictures until packet-capacity eviction.

A controlled eight-second arrival trace used a missing FU-A fragment, later
dependent pictures and a complete replacement IDR. These are simulated source
times, not measured camera playback latency:

| Observation | Previous GTK algorithm | Updated GTK / server assembler |
| --- | --- | --- |
| Complete marker-ended picture released immediately | No | Yes |
| Orphan FU-A tail forwarded as a picture | Yes | No |
| Pictures released after the unrepaired gap | 0 | 106 |
| Keyframe requests signaled by the assembler | 0 | 1 |

The comparison exercises GTK's actual patched receiver constructor with a
negotiated H.264 codec map. The negative control uses the previous exact generic
512-packet algorithm. It does not predict how long Ring will take to answer a
keyframe request, or whether every real packet loss will be retransmitted.

## Narrow repair

`receive_video.py` adapts the server's H.264 assembler at the same pinned
aiortc 1.15.0 depayloaded-packet boundary. It supports single NAL, STAP-A and
FU-A packets, validates fragment continuity, tolerates bounded reordering and
uses a complete IDR to recover after loss. The existing aiortc packet handler
continues to own NACKs, RTX unwrapping, RTCP PLI transmission and timestamp
mapping. Other video codecs retain the generic buffer; audio is unchanged.

Limits are 512 packets, 1 MiB of payload plus depayloaded copies, a three-
timestamp recovery horizon, 100 ms for an observed reorder gap and one second
for an incomplete picture. Gap time starts when the gap is observed, so a
large intact keyframe arriving over half a second remains acceptable. Recovery
PLI signals are rate limited to one per second. Limits advance on packet
arrival; idle sources allocate no additional timer or growing queue. Healthy
complete pictures acquire no new playout delay.

The receiver hook checks the pinned version and the constructor/packet-handler
contracts before installation, is idempotent, and does not retain a reference
cycle from the buffer back to its receiver. No HomeKit packetization,
transcoding, retransmission cache or output pacing was imported.

## Verification

The focused run passed **68 tests**:

```sh
python -m pytest -q -p no:cacheprovider \
  tests/test_receive_video.py tests/test_media_compatibility.py \
  tests/test_ring_media.py tests/test_frame_timing.py
```

Coverage includes real RTP handler/RTX/PLI behavior, generated libx264 pictures
compared byte-for-byte after assembly, independent FFmpeg picture decoding,
large IDRs exceeding 128 packets, paced large frames, packet and timestamp
wrap, duplicate/conflicting packets, missing fragments, late repair, malformed
NAL boundaries, bounded retention and private-contract rejection. Existing
three-generation H.264/audio loopback with simulated talkback also passes.
The loopback test requires local sockets; it failed under the socket-restricted
sandbox and passed when loopback was enabled. No behavioral assertion was
removed to accommodate the new assembler.

## Audio result

The current aiortc 1.15.0 decoder already selects `libopus`. Twelve synthetic
Ring-style 60 ms SILK packets were decoded through that path and GTK's PCM
conversion, then compared with the public libopus decoder API independently.
For 34,560 samples per channel the measured RMSE was 0 and correlation was 1.
This supports retaining the current decoder and unfiltered PCM path. It does
not establish microphone, speaker or real camera audio quality, and does not
justify importing Halo Server's HomeKit audio conversion workers.

## Follow-up boundaries

- **Playout timing:** `FrameClock` preserves source intervals within each track,
  but audio/video currently use separate origins and sinks with `sync=false`.
  GStreamer therefore does not use those timestamps to synchronize rendering.
  Measure burst delivery, startup skew and late frames before introducing a
  shared clock; simply turning synchronization on would use the wrong origin.
- **Decoder backlog:** the appsrc queues are bounded, but aiortc's earlier
  encoded input and decoded output queues are unbounded. A deliberately slow
  consumer test should establish the required desktop drop/recovery policy
  before another private queue adapter is added.
- **Snapshot coordination:** GTK's fresh-snapshot path requests timestamps
  before reading the image. Visible monitoring tiles and a focused view's
  initial still can invoke it while live sessions exist. The server defaults
  to cached images and only requests a fresh capture deliberately or through
  a qualified missing-cache fallback. Prefer reusing a privacy-authorized live
  frame or cached snapshot and coalescing per-camera work in a separate change.
  No snapshot state-machine change is included in this receive repair.

## Primary references

- [RFC 6184, sections 5.1 and 5.8](https://www.rfc-editor.org/rfc/rfc6184.html):
  H.264 marker semantics, fragmentation and sequence continuity.
- [GStreamer GstBaseSink](https://gstreamer.freedesktop.org/documentation/base/gstbasesink.html):
  clock synchronization and the `sync` property.
- Installed pinned aiortc 1.15.0 source: `jitterbuffer.py`,
  `rtcrtpreceiver.py` and `codecs/opus.py`.
- Halo Server: `media/video_jitter.py`, `tests/test_video_jitter.py` and
  `tests/test_ring_opus_decoder.py`.
