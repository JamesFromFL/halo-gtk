"""Live stream viewer — WebRTC negotiation via ring-doorbell + aiortc, rendered
through a dual-chain GStreamer pipeline:

  Video: appsrc → videoconvert → gtk4paintablesink
  Audio: appsrc → audioconvert → audioresample → volume → autoaudiosink

``LiveStreamView`` is the embeddable desktop widget used by the shared session
manager.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")

from aiortc.mediastreams import MediaStreamError  # noqa: E402
from gi.repository import GLib, Gst, Gtk  # noqa: E402

# Importing the media module applies the validated aiortc compatibility patches.
from halo_gtk.ring_media import (  # noqa: E402
    MicrophoneTrack,
    activate_ring_camera_speaker,
    audio_frame_to_s16le_interleaved,
    gst_push_succeeded,
    retain_aiortc_connect_tasks,
)

_log = logging.getLogger(__name__)


@dataclass(eq=False)
class _StreamResources:
    """All async resources owned by one LiveStreamView generation."""

    token: int
    device: Any
    client: Any
    session_id: str
    pc: Any = None
    mic_track: MicrophoneTrack | None = None
    ring_session_started: bool = False
    tasks: set[asyncio.Task] = field(default_factory=set)
    start_future: concurrent.futures.Future | None = None
    cleanup_started: bool = False
    terminal_notified: bool = False
    connected: bool = False

    async def cleanup(self) -> None:
        if self.cleanup_started:
            return
        self.cleanup_started = True
        cancellation_requested = False

        async def run_step(awaitable, label: str) -> None:
            nonlocal cancellation_requested
            try:
                await awaitable
            except asyncio.CancelledError:
                cancellation_requested = True
            except Exception as exc:
                _log.debug("%s cleanup failed: %s", label, exc)

        if self.mic_track is not None:
            await run_step(self.mic_track.stop_capture(), "Microphone")

        current = asyncio.current_task()
        pending = [task for task in self.tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await run_step(
                asyncio.gather(*pending, return_exceptions=True),
                "Live stream tasks",
            )
        self.tasks.clear()

        if self.ring_session_started:
            await run_step(
                self.device.close_webrtc_stream(self.session_id),
                "Ring WebRTC session",
            )
        if self.pc is not None:
            await run_step(self.pc.close(), "RTCPeerConnection")

        if cancellation_requested:
            raise asyncio.CancelledError


def _audio_sink_description() -> str:
    """Prefer a deterministic desktop audio sink over autoaudiosink."""
    if Gst.ElementFactory.find("pulsesink") is not None:
        return 'pulsesink name=asink client-name="Halo" sync=false async=false'
    if Gst.ElementFactory.find("pipewiresink") is not None:
        return 'pipewiresink name=asink client-name="Halo" sync=false async=false'
    return "autoaudiosink name=asink sync=false"


class LiveStreamView(Gtk.Box):
    """Embeddable live camera feed widget — GStreamer + WebRTC, no dialog chrome.

    Usage::

        view = LiveStreamView()
        view.start_for_device(device)   # begins WebRTC negotiation
        ...
        view.stop()                     # call on widget destruction / navigation
        png = view.get_current_frame_png()  # for screenshot
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._device = None
        self._resources: _StreamResources | None = None
        self._pipeline: Gst.Pipeline | None = None
        self._video_appsrc: Gst.Element | None = None
        self._audio_appsrc: Gst.Element | None = None
        self._vol_element: Gst.Element | None = None
        self._audio_sink: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._bus_handlers: list[int] = []
        self._closing = False
        self._stream_token = 0
        self._on_stream_state_changed_cb = None
        # Last decoded frame as a raw numpy array (h, w, 3) — updated every frame.
        # Read from GTK thread for screenshots; written from asyncio thread.
        # Single-reference replacement is safe under the GIL.
        self._last_frame_rgb = None

        self._build_pipeline()
        self._build_ui()

    def set_on_stream_state_changed(self, callback) -> None:
        """Register a callback fired (GTK thread) for lifecycle transitions."""
        self._on_stream_state_changed_cb = callback

    # ------------------------------------------------------------------
    # GStreamer pipeline
    # ------------------------------------------------------------------

    def _build_pipeline(self) -> None:
        audio_sink = _audio_sink_description()
        self._pipeline = Gst.parse_launch(
            "appsrc name=vsrc format=time is-live=true do-timestamp=false block=false "
            "! videoconvert "
            "! gtk4paintablesink name=vsink sync=false  "
            "appsrc name=asrc format=time is-live=true do-timestamp=false block=false "
            "! queue max-size-time=500000000 max-size-buffers=0 max-size-bytes=0 leaky=downstream "
            "! audioconvert "
            "! audioresample "
            "! volume name=vol "
            f"! {audio_sink}"
        )
        self._video_appsrc = self._pipeline.get_by_name("vsrc")
        self._audio_appsrc = self._pipeline.get_by_name("asrc")
        self._vol_element = self._pipeline.get_by_name("vol")
        self._audio_sink = self._pipeline.get_by_name("asink")
        self._paintable = self._pipeline.get_by_name("vsink").get_property("paintable")
        _log.debug("Live audio sink: %s", self._audio_sink.get_factory().get_name())

        self._bus = self._pipeline.get_bus()
        self._attach_bus_watch()

    def _attach_bus_watch(self) -> None:
        if self._bus is None or self._bus_handlers:
            return
        self._bus.add_signal_watch()
        self._bus_handlers.append(self._bus.connect("message", self._on_gst_message))

    def _detach_bus_watch(self) -> None:
        if self._bus is None:
            self._bus_handlers.clear()
            return
        if not self._bus_handlers:
            return
        for handler_id in self._bus_handlers:
            with contextlib.suppress(TypeError, ValueError):
                self._bus.disconnect(handler_id)
        self._bus_handlers.clear()
        with contextlib.suppress(Exception):
            self._bus.remove_signal_watch()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self.append(overlay)

        video = Gtk.Picture(
            paintable=self._paintable,
            content_fit=Gtk.ContentFit.CONTAIN,
            hexpand=True,
            vexpand=True,
        )
        overlay.set_child(video)

        self._status_label = Gtk.Label(
            label="Connecting…",
            css_classes=["dim-label"],
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        overlay.add_overlay(self._status_label)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_for_device(self, device) -> None:
        """Begin WebRTC negotiation for *device*.  Safe to call from GTK thread."""
        self._closing = False
        self._stream_token += 1
        stream_token = self._stream_token
        previous = self._resources
        self._resources = None
        if previous is not None:
            self._schedule_cleanup(previous)
        self._device = device
        self._last_frame_rgb = None

        # The pipeline may be in NULL state from a previous stop() call.
        # Transition NULL → PLAYING so that incoming appsrc buffers are accepted.
        # Reset the video appsrc caps to format-only so width/height are
        # re-negotiated from the first decoded frame of the new stream.
        if self._pipeline is not None:
            self._attach_bus_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline.set_state(Gst.State.PLAYING)

        self._set_status("Connecting…")

        from halo_gtk.ring_client import get_client

        client = get_client()
        if client is None:
            GLib.idle_add(
                self._on_stream_terminal,
                stream_token,
                "failed",
                "Not signed in to Ring",
            )
            return
        resources = _StreamResources(
            token=stream_token,
            device=device,
            client=client,
            session_id=str(uuid.uuid4()),
        )
        self._resources = resources
        resources.start_future = self._submit(
            client,
            self._async_start(resources),
            "Live stream startup",
        )
        if resources.start_future is None:
            self._resources = None
            GLib.idle_add(
                self._on_stream_terminal,
                stream_token,
                "failed",
                "Could not start the Ring worker",
            )

    def stop(self) -> None:
        """Stop WebRTC and GStreamer.  Safe to call from GTK thread."""
        self._closing = True
        self._stream_token += 1
        resources = self._resources
        self._resources = None
        if resources is not None:
            self._schedule_cleanup(resources)
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self._detach_bus_watch()

    def set_volume(self, value: float) -> None:
        volume = max(0.0, min(1.0, value))
        if self._vol_element is not None:
            self._vol_element.set_property("volume", volume)
        if self._audio_sink is not None and self._audio_sink.find_property("mute") is not None:
            self._audio_sink.set_property("mute", volume <= 0)
        if self._audio_sink is not None and self._audio_sink.find_property("volume") is not None:
            self._audio_sink.set_property("volume", volume)

    def get_paintable(self):
        """Return the GTK paintable backing the live video sink."""
        return self._paintable

    def start_talking(self) -> None:
        """Begin sending microphone audio to Ring.  Safe to call from GTK thread."""
        resources = self._resources
        if resources is None or resources.mic_track is None:
            return
        self._submit(
            resources.client,
            self._async_start_talking(resources),
            "Microphone start",
        )

    async def _async_start_talking(self, resources: _StreamResources) -> None:
        """Activate the camera speaker before sending microphone samples."""
        if not self._is_current_resources(resources):
            return
        await activate_ring_camera_speaker(resources.device, resources.session_id)
        if self._is_current_resources(resources) and resources.mic_track is not None:
            await resources.mic_track.start_capture()

    def stop_talking(self) -> None:
        """Stop sending microphone audio.  Safe to call from GTK thread."""
        resources = self._resources
        if resources is None or resources.mic_track is None:
            return
        self._submit(
            resources.client,
            resources.mic_track.stop_capture(),
            "Microphone stop",
        )

    def get_current_frame_png(self) -> bytes | None:
        """Return the most recently decoded video frame as PNG bytes, or None."""
        arr = self._last_frame_rgb
        if arr is None:
            return None
        try:
            import io

            from PIL import Image

            img = Image.fromarray(arr, "RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:
            _log.debug("Screenshot encode failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Stream startup — asyncio loop
    # ------------------------------------------------------------------

    async def _async_start(self, resources: _StreamResources) -> None:
        from aiortc import (
            RTCConfiguration,
            RTCIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp

        start_task = asyncio.current_task()
        if start_task is not None:
            resources.tasks.add(start_task)
        started = False
        terminal_message = "Stream stopped"

        try:
            if not self._is_current_resources(resources):
                return

            pc = RTCPeerConnection(
                RTCConfiguration(iceServers=[RTCIceServer(urls=resources.device.get_ice_servers())])
            )
            resources.pc = pc
            if not self._is_current_resources(resources):
                return

            pc.addTransceiver("video", direction="recvonly")
            resources.mic_track = MicrophoneTrack()
            pc.addTransceiver(resources.mic_track, direction="sendrecv")

            def is_current_stream() -> bool:
                return (
                    self._is_current_resources(resources)
                    and getattr(pc, "signalingState", None) != "closed"
                )

            async def safe_set_remote_description(sdp: str) -> None:
                if not is_current_stream():
                    return
                try:
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
                except Exception as exc:
                    if is_current_stream():
                        await self._terminate_resources(
                            resources,
                            "failed",
                            f"Failed to apply Ring stream answer: {exc}",
                        )
                finally:
                    retain_aiortc_connect_tasks(
                        pc,
                        lambda task, label: self._retain_resource_task(
                            resources,
                            task,
                            label,
                        ),
                    )

            async def safe_add_ice_candidate(candidate) -> None:
                if not is_current_stream():
                    return
                try:
                    await pc.addIceCandidate(candidate)
                except Exception as exc:
                    _log.debug("Ignoring Ring ICE candidate: %s", exc)

            def on_rtc_message(msg) -> None:
                if not is_current_stream():
                    return
                if getattr(msg, "answer", None):
                    self._spawn_resource_task(
                        resources,
                        safe_set_remote_description(msg.answer),
                        "Ring stream answer",
                    )
                elif (
                    getattr(msg, "candidate", None) is not None
                    and getattr(msg, "sdp_m_line_index", None) is not None
                ):
                    try:
                        candidate = candidate_from_sdp(msg.candidate)
                        candidate.sdpMLineIndex = msg.sdp_m_line_index
                        candidate.sdpMid = str(msg.sdp_m_line_index)
                        self._spawn_resource_task(
                            resources,
                            safe_add_ice_candidate(candidate),
                            "Ring ICE candidate",
                        )
                    except Exception as exc:
                        _log.debug("ICE candidate parse error: %s", exc)
                elif getattr(msg, "error_code", None):
                    self._spawn_resource_task(
                        resources,
                        self._terminate_resources(
                            resources,
                            "failed",
                            f"Stream error {msg.error_code}: {msg.error_message}",
                        ),
                        "Ring stream error cleanup",
                    )

            @pc.on("icecandidate")
            def on_icecandidate(candidate) -> None:
                if candidate is not None and is_current_stream():
                    self._spawn_resource_task(
                        resources,
                        resources.device.on_webrtc_candidate(
                            resources.session_id,
                            candidate.candidate,
                            candidate.sdpMLineIndex or 0,
                        ),
                        "Local ICE candidate",
                    )

            @pc.on("connectionstatechange")
            def on_connectionstatechange() -> None:
                if getattr(pc, "connectionState", None) == "failed" and is_current_stream():
                    self._spawn_resource_task(
                        resources,
                        self._terminate_resources(
                            resources,
                            "failed",
                            "WebRTC connection failed",
                        ),
                        "WebRTC failure cleanup",
                    )

            @pc.on("track")
            def on_track(track) -> None:
                if not is_current_stream():
                    return
                _log.debug("Received %s track from Ring", track.kind)
                if track.kind == "video":
                    self._spawn_resource_task(
                        resources,
                        self._receive_frames(track, resources),
                        "Video receiver",
                    )
                elif track.kind == "audio":
                    _log.debug("Ring audio track received; starting audio receiver")
                    self._spawn_resource_task(
                        resources,
                        self._receive_audio_frames(track, resources),
                        "Audio receiver",
                    )

            offer = await pc.createOffer()
            if not is_current_stream():
                return
            await pc.setLocalDescription(offer)
            retain_aiortc_connect_tasks(
                pc,
                lambda task, label: self._retain_resource_task(
                    resources,
                    task,
                    label,
                ),
            )
            if not is_current_stream():
                return

            resources.ring_session_started = True
            await resources.device.generate_async_webrtc_stream(
                pc.localDescription.sdp,
                resources.session_id,
                on_rtc_message,
                keep_alive_timeout=300,
            )
            if not is_current_stream():
                return
            started = True
            _log.debug(
                "WebRTC stream initiated for %s (session %s)",
                resources.device.name,
                resources.session_id[:8],
            )
        except asyncio.CancelledError:
            terminal_message = "Stream startup cancelled"
            raise
        except Exception as exc:
            terminal_message = f"Failed to connect: {exc}"
            if self._is_current_resources(resources):
                _log.warning("Stream start failed for %s: %s", resources.device.name, exc)
        finally:
            if start_task is not None:
                resources.tasks.discard(start_task)
            if not started:
                was_current = self._is_current_resources(resources)
                if was_current:
                    self._resources = None
                with contextlib.suppress(asyncio.CancelledError):
                    await resources.cleanup()
                if was_current and not resources.terminal_notified:
                    resources.terminal_notified = True
                    GLib.idle_add(
                        self._on_stream_terminal,
                        resources.token,
                        "failed",
                        terminal_message,
                    )

    # ------------------------------------------------------------------
    # Video frame loop — asyncio loop
    # ------------------------------------------------------------------

    def _is_current_stream_token(self, stream_token: int) -> bool:
        return stream_token == self._stream_token and not self._closing

    def _is_current_resources(self, resources: _StreamResources) -> bool:
        return (
            self._resources is resources
            and not resources.cleanup_started
            and self._is_current_stream_token(resources.token)
        )

    async def _receive_frames(self, track, resources: _StreamResources) -> None:
        import numpy as np

        _log.debug("Video frame receiver started")
        # Per-stream state lives in locals (not on self) so a freshly-started
        # receiver can't race a still-running old one over caps/PTS.
        video_caps_set = False
        video_pts = 0
        terminal_state = "stopped"
        terminal_message = "Stream ended"
        try:
            while self._is_current_resources(resources):
                frame = await track.recv()
                if not self._is_current_resources(resources):
                    break
                rgb = frame.to_ndarray(format="rgb24")
                h, w = rgb.shape[:2]

                # Store the un-padded frame for screenshots.
                self._last_frame_rgb = rgb

                # GStreamer requires RGB row stride to be a multiple of 4 bytes.
                w_caps = (w + 3) & ~3
                if w_caps != w:
                    padded = np.zeros((h, w_caps, 3), dtype=np.uint8)
                    padded[:, :w, :] = rgb
                    raw: bytes = padded.tobytes()
                else:
                    raw = rgb.tobytes()

                if not video_caps_set:
                    caps = Gst.Caps.from_string(
                        f"video/x-raw,format=RGB,width={w_caps},height={h},framerate=30/1"
                    )
                    self._video_appsrc.set_property("caps", caps)
                    video_caps_set = True
                    if w_caps != w:
                        _log.debug("Video stream: %dx%d (padded to %dx%d)", w, h, w_caps, h)
                    else:
                        _log.debug("Video stream: %dx%d", w, h)

                duration = Gst.SECOND // 30
                buffer = Gst.Buffer.new_wrapped(raw)
                buffer.pts = video_pts
                buffer.dts = video_pts
                buffer.duration = duration
                video_pts += duration
                result = self._video_appsrc.emit("push-buffer", buffer)
                if not gst_push_succeeded(result):
                    _log.debug("Stopping video receiver after appsrc push result: %s", result)
                    terminal_state = "failed"
                    terminal_message = "Video pipeline stopped accepting frames"
                    break
                if not resources.connected:
                    resources.connected = True
                    GLib.idle_add(self._on_connected, resources.token)

        except MediaStreamError:
            _log.debug("Video track ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.debug("Video frame error: %s", exc)
            terminal_state = "failed"
            terminal_message = f"Video stream failed: {exc}"
        finally:
            if self._is_current_resources(resources):
                await self._terminate_resources(
                    resources,
                    terminal_state,
                    terminal_message,
                )

    # ------------------------------------------------------------------
    # Audio frame loop — asyncio loop
    # ------------------------------------------------------------------

    async def _receive_audio_frames(self, track, resources: _StreamResources) -> None:
        _log.debug("Audio frame receiver started")
        audio_caps_set = False
        audio_pts = 0
        audio_push_logged = False
        try:
            while self._is_current_resources(resources):
                frame = await track.recv()
                if not self._is_current_resources(resources):
                    break
                raw, channels, rate, samples = audio_frame_to_s16le_interleaved(frame)

                if not audio_caps_set:
                    caps_str = (
                        "audio/x-raw,format=S16LE,layout=interleaved,"
                        f"channels={channels},rate={rate}"
                    )
                    self._audio_appsrc.set_property("caps", Gst.Caps.from_string(caps_str))
                    audio_caps_set = True
                    _log.debug("Ring audio stream: S16LE interleaved %dch %dHz", channels, rate)

                duration = samples * Gst.SECOND // rate if rate > 0 else Gst.SECOND // 50
                buffer = Gst.Buffer.new_wrapped(raw)
                buffer.pts = audio_pts
                buffer.dts = audio_pts
                buffer.duration = duration
                audio_pts += duration
                result = self._audio_appsrc.emit("push-buffer", buffer)
                if not audio_push_logged:
                    _log.debug("Ring audio push result: %s", result.value_nick)
                    audio_push_logged = True
                if not gst_push_succeeded(result):
                    _log.debug("Stopping audio receiver after appsrc push result: %s", result)
                    break

        except MediaStreamError:
            _log.debug("Audio track ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.debug("Audio frame error: %s", exc)

    # ------------------------------------------------------------------
    # Async task ownership
    # ------------------------------------------------------------------

    def _submit(
        self,
        client,
        coro,
        label: str,
    ) -> concurrent.futures.Future | None:
        """Submit work to the Ring loop and observe every terminal exception."""
        try:
            future = client.submit(coro)
        except Exception as exc:
            coro.close()
            _log.warning("%s could not be submitted: %s", label, exc)
            return None

        def observe(done: concurrent.futures.Future) -> None:
            try:
                error = done.exception()
            except concurrent.futures.CancelledError:
                return
            except Exception as exc:
                _log.debug("%s result could not be read: %s", label, exc)
                return
            if error is not None:
                _log.warning("%s failed: %s", label, error)

        future.add_done_callback(observe)
        return future

    def _schedule_cleanup(self, resources: _StreamResources) -> None:
        if resources.start_future is not None and not resources.start_future.done():
            resources.start_future.cancel()
        self._submit(
            resources.client,
            resources.cleanup(),
            "Live stream cleanup",
        )

    def _spawn_resource_task(
        self,
        resources: _StreamResources,
        coro,
        label: str,
    ) -> asyncio.Task:
        return self._retain_resource_task(
            resources,
            asyncio.create_task(coro),
            label,
        )

    def _retain_resource_task(
        self,
        resources: _StreamResources,
        task: asyncio.Task,
        label: str,
    ) -> asyncio.Task:
        if task in resources.tasks:
            return task
        resources.tasks.add(task)

        def observe(done: asyncio.Task) -> None:
            resources.tasks.discard(done)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except Exception as exc:
                _log.debug("%s result could not be read: %s", label, exc)
                return
            if error is not None:
                _log.warning("%s failed: %s", label, error)

        task.add_done_callback(observe)
        return task

    async def _terminate_resources(
        self,
        resources: _StreamResources,
        state: str,
        message: str,
    ) -> None:
        was_current = self._is_current_resources(resources)
        if was_current:
            self._resources = None
        with contextlib.suppress(asyncio.CancelledError):
            await resources.cleanup()
        if was_current and not resources.terminal_notified:
            resources.terminal_notified = True
            GLib.idle_add(
                self._on_stream_terminal,
                resources.token,
                state,
                message,
            )

    # ------------------------------------------------------------------
    # GTK-thread status helpers
    # ------------------------------------------------------------------

    def _set_status(self, message: str) -> bool:
        self._status_label.set_label(message)
        self._status_label.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _on_connected(self, stream_token: int) -> bool:
        if not self._is_current_stream_token(stream_token):
            return GLib.SOURCE_REMOVE
        self._status_label.set_visible(False)
        if self._on_stream_state_changed_cb is not None:
            self._on_stream_state_changed_cb("active")
        return GLib.SOURCE_REMOVE

    def _on_stream_terminal(self, stream_token: int, state: str, message: str) -> bool:
        if not self._is_current_stream_token(stream_token):
            return GLib.SOURCE_REMOVE
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self._set_status(message)
        if self._on_stream_state_changed_cb is not None:
            self._on_stream_state_changed_cb(state)
        return GLib.SOURCE_REMOVE

    def _on_gst_message(self, _bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            _log.warning("GStreamer error from %s: %s (%s)", message.src.name, err, debug)
            resources = self._resources
            if resources is not None and self._is_current_resources(resources):
                self._submit(
                    resources.client,
                    self._terminate_resources(
                        resources,
                        "failed",
                        f"Media pipeline failed: {err}",
                    ),
                    "GStreamer failure cleanup",
                )
        elif message.type == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            _log.warning("GStreamer warning from %s: %s (%s)", message.src.name, err, debug)
