# GTK live renderer comparison

The experiment compares two renderers behind the same H.264/Opus receive path. The shipped RGB bridge calls Halo’s actual video receive loop. It converts a decoded PyAV frame to NumPy RGB, retains the image for screenshots, wraps a byte copy in a GStreamer buffer, and pushes it through `videoconvert` into `gtk4paintablesink`. The experimental bridge retains the decoded frame and copies its three I420 planes into separate GStreamer memory blocks. It supplies their offsets and strides as video metadata and leaves color conversion to GStreamer. Both paths enforce the camera privacy lease. Only the benchmark contains the I420 bridge.

## Results on this host

**Retain the shipped RGB bridge.** All four 1080p streams reached the sink at 30.0 fps per camera during both 45-second runs. RGB used 73.7% process CPU and 472.0 MiB peak RSS. The planar I420 candidate used 142.8% CPU and 363.1 MiB. Its 95th-percentile video age was 10.9 ms, compared with 13.7 ms for RGB. Both bridges reported zero video appsrc drops in steady delivery. The combination of memory savings, higher CPU use, and similar throughput supports keeping the existing renderer for this release. These measurements apply to this virtual display and synthetic source on this shared desktop.

First paint below is the slowest camera in each initial opening. The two rates show buffer arrivals at the video sink and native paintable content updates per second per camera. Negative skew means the video flash arrived before the audio tone.

| Cameras × duration | Bridge | CPU % | Peak MiB | First paint ms | Sink / paintable Hz | p95 video age ms | Median skew ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 × 8 s | RGB | 19.1 | 281.7 | 57 | 30.0 / 30.0 | 5.0 | -95.4 |
| 1 × 8 s | I420 | 33.9 | 242.7 | 57 | 30.0 / 30.1 | 3.5 | -96.4 |
| 4 × 8 s | RGB | 77.0 | 462.6 | 96 | 30.0 / 29.1 | 16.2 | -87.7 |
| 4 × 8 s | I420 | 134.6 | 355.9 | 87 | 30.0 / 27.5 | 10.6 | -90.1 |
| 4 × 45 s | RGB | 73.7 | 472.0 | 93 | 30.0 / 29.4 | 13.7 | -87.5 |
| 4 × 45 s | I420 | 142.8 | 363.1 | 94 | 30.0 / 28.0 | 10.9 | -90.2 |

All one- and four-camera peers closed and reopened in the same process. On immediate reopen, the first paint ranged from 49 to 96 ms across cameras and bridges. Peak process RSS rose on the second cycle for both bridges, so the first-cycle RSS figures should not be read as long-run memory ceilings. The raw records include each reopened camera and its process high-water mark. [All 14 measurement records](live-renderer-benchmark-results.jsonl)

### Burst, slow sink and recovery

Each row shows the first seven seconds under the imposed delays, followed by the settled interval starting one second after their removal. Drops are totals across the complete case. Skew is the absolute difference between the flash and tone arrival times at their sinks.

| Cameras | Bridge | Video appsrc drops | p95 video age, stress → settled ms | p95 absolute skew, stress → settled ms | Matched pulses, stress / settled |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | RGB | 44 | 160.6 → 5.7 | 44.2 → 95.6 | 6 / 6 |
| 1 | I420 | 52 | 157.7 → 4.3 | 43.4 → 97.2 | 6 / 6 |
| 4 | RGB | 160 | 179.3 → 18.4 | 70.4 → 93.7 | 24 / 24 |
| 4 | I420 | 184 | 170.0 → 9.2 | 49.7 → 91.6 | 24 / 24 |

With four cameras, video age fell back below 20 ms at p95 for both renderers after the imposed delays ended. The slow sink brought the delayed video markers nearer to the audio markers while dropping pictures. In the sustained RGB run, decoded video was already 11.6 ms old at p95 when the application received it, compared with 88.9 ms for decoded audio. That difference is present before GStreamer presentation. The experiment supports measuring audio and video separately on real hardware before choosing a playback-clock change. The receivers reported zero lost RTP packets across all runs.

## Measurement

Each camera uses its own loopback aiortc sender and receiver, no ICE servers, and only 127.0.0.1 host candidates. All cameras replay identical pre-encoded 1920×1080 H.264 pictures at 30 fps and send 48 kHz audio as Opus. The generated scene moves and flashes for the first 100 ms of every second. A 500 Hz tone occupies the same 100 ms. Audio goes through Halo’s receive path into a silent GStreamer sink. Only generated inputs, loopback peers, and the silent output sink participate.

The run used Arch Linux on an eight-core, 16-thread Intel Core i9-11900K, Python 3.14.10, aiortc 1.15.0, PyAV 17.1.0, NumPy 2.4.4, GStreamer 1.28.9, and GTK 4.24.1. Both renderers used the same source revision, `556284ad62940d69aab894d9bcb66836f8a5d765`. Each process records the SHA-256 of the loaded production video module in its output. The display used GTK’s Broadway backend with the Cairo renderer and a private Unix socket.

Normal runs open the same number of streams twice in immediate succession. Each cycle closes both peers, stops the pipelines, and destroys the window before the next cycle begins. The stress run groups original timestamps into 120 ms delivery bursts and delays the video sink by 40 ms per picture for seven seconds. It then removes both delays for seven seconds. The sustained run keeps four streams open for 45 seconds. Cases run sequentially on a shared desktop with other qualification and desktop work in progress. The input uses the same zero-injected-loss pattern for both bridges. The separate receive-path tests cover missing H.264 fragments and keyframe recovery. [Receive-path tests](receive-path-review-2026-11-16.md)

- CPU is total process CPU time during the streaming interval divided by wall time. One fully used logical CPU is 100%. It includes the synthetic senders, receivers, GTK and GStreamer. RSS is the process high-water mark, including setup and the fixture encoder. A second cycle shares the process high-water mark with the first.
- First paint is the first native paintable content update with a nonzero image width after a video buffer reaches the sink. Its clock starts before loopback negotiation. Broadway provides evidence at the GTK paintable boundary. The physical compositor and monitor are outside the measurement.
- Video age is elapsed monotonic time at the sink minus the source schedule represented by the buffer timestamp. Audio and video decoded-age samples record the same comparison as each decoded track yields frames. Video appsrc reports its dropped-buffer counter directly. Paintable updates can combine multiple buffers.
- Signed skew matches the onset of the flash and tone for the same second. Negative values mean video reached its sink first. Video age, skew and frame counts include transport and decoder behavior. Physical speaker latency and real-camera clock drift require separate measurement.
- The planar candidate retains a source for screenshots. Its screenshot export, renegotiation outside I420, camera-specific colorimetry, and hardware rendering would each need separate qualification.

## Reproduce

Install the existing project dependencies, the GTK4 GStreamer paintable sink, and `gtk4-broadwayd`. The wrapper selects the repository’s `.venv/bin/python` unless `HALO_BENCHMARK_PYTHON` identifies another project environment. Run a matching case for each bridge. To use the exact measured code, export the `src` tree from the revision above into a temporary directory, copy both benchmark scripts into its `scripts` directory, and select the installed environment with `HALO_BENCHMARK_PYTHON`. The wrapper creates temporary HOME/XDG directories and tears down its private display when the process exits.

```bash
for bridge in rgb i420; do
    for cameras in 1 4; do
        scripts/benchmark_live_renderer.sh --bridge "$bridge" --cameras "$cameras" --seconds 8 --cycles 2
        scripts/benchmark_live_renderer.sh --bridge "$bridge" --cameras "$cameras" --seconds 14 \
            --burst-ms 120 --sink-delay-ms 40 --steady-after 7
    done
    scripts/benchmark_live_renderer.sh --bridge "$bridge" --cameras 4 --seconds 45
done
```

The script outputs one JSON object per completed cycle. Each camera’s four frame counts are sent, yielded by the decoded track, received at the video sink, and GTK paintable updates. Frames in transit at the end can account for a difference between sent and received counts. The CPU comparison is between the two complete loopback configurations. Attributing CPU to a particular copy would require separate component profiling.
