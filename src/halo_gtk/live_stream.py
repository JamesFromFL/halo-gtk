"""Live stream viewer — WebRTC negotiation via ring-doorbell + aiortc, rendered
through independent GStreamer video and audio pipelines:

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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")

from aiortc.mediastreams import MediaStreamError  # noqa: E402
from gi.repository import GLib, Gst, Gtk  # noqa: E402

from halo_gtk.camera_privacy import PrivacyLease  # noqa: E402
from halo_gtk.frame_timing import FrameClock  # noqa: E402

# Importing the media module applies the validated aiortc compatibility patches.
from halo_gtk.ring_media import (  # noqa: E402
    MicrophoneTrack,
    activate_ring_camera_speaker,
    audio_frame_to_s16le_interleaved,
    gst_push_succeeded,
    retain_aiortc_connect_tasks,
)
from halo_gtk.stream_timing import timings  # noqa: E402

_log = logging.getLogger(__name__)

_FIRST_FRAME_TIMEOUT = 25.0
_VIDEO_STALL_TIMEOUT = 8.0
_VIDEO_RECOVERY_TIMEOUT = 8.0
_RING_CLOSE_TIMEOUT = 3.0

# A nonblocking producer must bound/drop backlog explicitly. The byte threshold
# alone only emits enough-data; it does not stop this asyncio producer.
VIDEO_SOURCE = (
    "appsrc name=vsrc format=time is-live=true do-timestamp=false block=false "
    "max-buffers=2 max-bytes=0 max-time=0 leaky-type=downstream "
)
AUDIO_SOURCE = (
    "appsrc name=asrc format=time is-live=true do-timestamp=false block=false "
    "max-buffers=0 max-bytes=0 max-time=500000000 leaky-type=downstream "
)


@dataclass(eq=False)
class _StreamResources:
    """All async resources owned by one LiveStreamView generation."""

    token: int
    device: Any
    client: Any
    session_id: str
    privacy_lease: PrivacyLease | None = None
    privacy_callback: Any = None
    pc: Any = None
    mic_track: MicrophoneTrack | None = None
    ring_session_started: bool = False
    tasks: set[asyncio.Task] = field(default_factory=set)
    start_future: concurrent.futures.Future | None = None
    cleanup_started: bool = False
    cleanup_failed: bool = False
    cleanup_complete: asyncio.Event = field(default_factory=asyncio.Event)
    closed: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    admitted_at: float = field(default_factory=time.monotonic)
    last_frame_at: float | None = None
    recovery_at: float | None = None
    terminal_notified: bool = False
    connected: bool = False
    talkback_generation: int = 0
    talkback_requested: bool = False
    talkback_callback: Any = None

    async def cleanup(self) -> None:
        if self.cleanup_started:
            return
        self.cleanup_started = True
        if self.privacy_callback is not None and self.privacy_lease is not None:
            self.privacy_lease.policy.unsubscribe(self.privacy_callback)
            self.privacy_callback = None
        cancellation_requested = False

        async def run_step(awaitable, label: str) -> None:
            nonlocal cancellation_requested
            try:
                await awaitable
            except asyncio.CancelledError:
                cancellation_requested = True
                self.cleanup_failed = True
            except Exception as exc:
                self.cleanup_failed = True
                _log.debug("%s cleanup failed: %s", label, exc)

        if self.mic_track is not None:
            self.talkback_generation += 1
            self.talkback_requested = False
            await run_step(self.mic_track.stop_capture(), "Microphone")
            set_error = getattr(self.mic_track, "set_on_capture_error", None)
            if set_error is not None:
                set_error(None)
        self.talkback_callback = None

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

        if self.pc is not None:
            await run_step(self.pc.close(), "RTCPeerConnection")
        if self.ring_session_started:
            await run_step(self._close_ring_session(), "Ring WebRTC session")

        if self.privacy_lease is not None:
            release_policy = getattr(self.privacy_lease.policy, "release", None)
            if release_policy is not None:
                release_policy(self.privacy_lease, resources_closed=not self.cleanup_failed)
        self.cleanup_complete.set()
        self.closed.set_result(None)
        release = getattr(self.client, "release_media_resources", None)
        if release is not None:
            release(self)

        if cancellation_requested:
            raise asyncio.CancelledError

    async def _close_ring_session(self) -> None:
        # The pinned SDK removes the session from its registry before awaiting
        # websocket closure. Retain the local transport for a bounded fallback.
        stream = getattr(self.device, "_webrtc_streams", {}).get(self.session_id)
        transport = getattr(getattr(stream, "websocket", None), "transport", None)
        try:
            async with asyncio.timeout(_RING_CLOSE_TIMEOUT):
                await self.device.close_webrtc_stream(self.session_id)
        except (Exception, asyncio.CancelledError):
            if transport is not None:
                transport.abort()
            raise

    async def close_for_client(self):
        await self.cleanup()
        await self.cleanup_complete.wait()


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

    def __init__(self, *, media_authority=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._media_authority = media_authority
        self._device = None
        self._resources: _StreamResources | None = None
        self._transport_closed: concurrent.futures.Future | None = None
        self._pipeline: Gst.Pipeline | None = None
        self._video_appsrc: Gst.Element | None = None
        self._audio_appsrc: Gst.Element | None = None
        self._vol_element: Gst.Element | None = None
        self._audio_pipeline = None
        self._audio_watch = None
        self._requested_volume = 0.0
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
        self._pipeline = Gst.parse_launch(
            f"{VIDEO_SOURCE}! videoconvert ! gtk4paintablesink name=vsink sync=false"
        )
        self._video_appsrc = self._pipeline.get_by_name("vsrc")
        self._paintable = self._pipeline.get_by_name("vsink").get_property("paintable")
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

        video = self._video_picture = Gtk.Picture(
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
        policy = getattr(self, "_media_authority", None) or client.camera_privacy
        lease = policy.acquire(device, "live")
        if lease is None or not client._preview_is_current():
            if lease is not None and (release := getattr(policy, "release", None)) is not None:
                release(lease)
            self._last_frame_rgb = None
            self._video_picture.set_paintable(None)
            GLib.idle_add(
                self._on_stream_terminal,
                stream_token,
                "failed",
                policy.view(device).message,
            )
            return
        resources = _StreamResources(
            token=stream_token,
            device=device,
            client=client,
            session_id=str(uuid.uuid4()),
            privacy_lease=lease,
        )
        try:
            client.retain_media_resources(resources)
        except Exception:
            if (release := getattr(policy, "release", None)) is not None:
                release(lease)
            raise
        self._resources = resources
        self._transport_closed = resources.closed
        self.set_volume(self._requested_volume)
        self._video_picture.set_paintable(self._paintable)
        resources.privacy_callback = lambda: self._privacy_changed(resources)
        lease.policy.subscribe(resources.privacy_callback)
        resources.start_future = self._submit(
            client,
            self._async_start(resources),
            "Live stream startup",
        )
        if resources.start_future is None:
            # Submission failed before the coroutine could own any resources.
            # Release synchronous admission too; there will be no async finally.
            lease.policy.unsubscribe(resources.privacy_callback)
            resources.privacy_callback = None
            if (release := getattr(policy, "release", None)) is not None:
                release(lease)
            client.release_media_resources(resources)
            resources.cleanup_started = True
            resources.cleanup_complete.set()
            resources.closed.set_result(None)
            self._resources = None
            GLib.idle_add(
                self._on_stream_terminal,
                stream_token,
                "failed",
                "Could not start the Ring worker",
            )

    def stop(self) -> concurrent.futures.Future | None:
        """Detach immediately and report when this generation finishes transport cleanup."""
        self._closing = True
        self._last_frame_rgb = None
        self._stream_token += 1
        resources = self._resources
        self._resources = None
        if resources is not None:
            resources.talkback_generation += 1
            resources.talkback_requested = False
            self._schedule_cleanup(resources)
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self._stop_audio_output()
        self._detach_bus_watch()
        # Natural termination clears _resources before its async cleanup finishes.
        if resources is not None:
            return resources.closed
        return getattr(self, "_transport_closed", None)

    def set_volume(self, value: float) -> None:
        self._requested_volume = max(0.0, min(1.0, value))
        resources = self._resources
        if (
            self._requested_volume == 0
            or resources is None
            or not self._privacy_current(resources, "audio")
        ):
            self._stop_audio_output()
            return
        try:
            if self._audio_pipeline is None:
                pipeline = Gst.parse_launch(
                    f"{AUDIO_SOURCE}! queue max-size-time=500000000 max-size-buffers=0 "
                    "max-size-bytes=0 leaky=downstream ! audioconvert ! audioresample "
                    f"! volume name=vol ! {_audio_sink_description()}"
                )
                self._audio_pipeline = pipeline
                self._vol_element = pipeline.get_by_name("vol")
                self._vol_element.set_property("volume", self._requested_volume)
                bus = pipeline.get_bus()
                bus.add_signal_watch()
                self._audio_watch = (
                    bus,
                    bus.connect("message::error", self._on_audio_error, resources.token),
                )
                if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("Audio output could not start")
                self._audio_appsrc = pipeline.get_by_name("asrc")
            else:
                self._vol_element.set_property("volume", self._requested_volume)
        except (GLib.Error, RuntimeError) as exc:
            _log.warning("Local audio output failed: %s", exc)
            self._audio_unavailable()

    def _stop_audio_output(self):
        pipeline = getattr(self, "_audio_pipeline", None)
        self._audio_appsrc = None
        self._vol_element = None
        self._audio_pipeline = None
        if pipeline is not None:
            if self._audio_watch is not None:
                bus, handler = self._audio_watch
                self._audio_watch = None
                bus.disconnect(handler)
                bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)

    def _on_audio_error(self, bus, message, token):
        if token != self._stream_token or self._audio_watch is None or bus != self._audio_watch[0]:
            return
        _log.warning("Local audio output failed: %s", message.parse_error()[0])
        self._audio_unavailable()

    def _audio_push_failed(self, token, output):
        if token == self._stream_token and output is self._audio_appsrc:
            self._audio_unavailable()
        return GLib.SOURCE_REMOVE

    def _audio_unavailable(self):
        from halo_gtk.ui_feedback import show_toast

        self._stop_audio_output()
        show_toast(self, "Camera audio output is unavailable. Mute and unmute to retry.")

    def get_paintable(self):
        """Return the GTK paintable backing the live video sink."""
        resources = self._resources
        return (
            self._paintable if resources is not None and self._privacy_current(resources) else None
        )

    def start_talking(self, *, on_state_changed=None) -> None:
        """Begin sending microphone audio to Ring.  Safe to call from GTK thread."""
        resources = self._resources
        if resources is None or not self._privacy_current(resources, "talkback"):
            if on_state_changed is not None:
                on_state_changed("failed")
            return
        resources.talkback_generation += 1
        resources.talkback_requested = True
        resources.talkback_callback = on_state_changed
        generation = resources.talkback_generation
        self._talkback_outcome(resources, generation, "starting")
        future = self._submit(
            resources.client,
            self._async_start_talking(resources, generation),
            "Microphone start",
        )
        if future is None:
            self._talkback_failed(resources, generation)

    async def _async_start_talking(
        self,
        resources: _StreamResources,
        generation: int,
    ) -> None:
        """Activate the camera speaker before sending microphone samples."""
        try:
            async with asyncio.timeout(12):
                while self._talkback_request_is_current(resources, generation):
                    if resources.connected and resources.mic_track is not None:
                        break
                    await asyncio.sleep(0.01)
                if not self._talkback_request_is_current(resources, generation):
                    return
                await activate_ring_camera_speaker(resources.device, resources.session_id)
                if self._talkback_request_is_current(resources, generation):
                    set_error = getattr(resources.mic_track, "set_on_capture_error", None)
                    if set_error is not None:
                        set_error(lambda: self._talkback_failed(resources, generation))
                    await resources.mic_track.start_capture()
                    if self._talkback_request_is_current(resources, generation):
                        self._talkback_outcome(resources, generation, "active")
                    else:
                        await resources.mic_track.stop_capture()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.debug("Microphone activation failed: %s", exc)
            if self._talkback_request_is_current(resources, generation):
                with contextlib.suppress(Exception):
                    await resources.mic_track.stop_capture()
            self._talkback_failed(resources, generation)

    def _talkback_failed(self, resources: _StreamResources, generation: int) -> None:
        if self._talkback_request_is_current(resources, generation):
            resources.talkback_requested = False
            self._talkback_outcome(resources, generation, "failed")

    def _talkback_outcome(self, resources, generation: int, state: str) -> None:
        def deliver():
            if (
                self._is_current_resources(resources)
                and resources.talkback_generation == generation
            ):
                if state in {"starting", "active"} and not resources.talkback_requested:
                    return GLib.SOURCE_REMOVE
                callback = resources.talkback_callback
                if callback is not None:
                    callback(state)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def stop_talking(self) -> None:
        """Stop sending microphone audio.  Safe to call from GTK thread."""
        resources = self._resources
        if resources is None:
            return
        resources.talkback_generation += 1
        resources.talkback_requested = False
        self._talkback_outcome(resources, resources.talkback_generation, "stopped")
        if resources.mic_track is None:
            return
        self._submit(
            resources.client,
            resources.mic_track.stop_capture(),
            "Microphone stop",
        )

    def _talkback_request_is_current(
        self,
        resources: _StreamResources,
        generation: int,
    ) -> bool:
        return (
            self._is_current_resources(resources)
            and self._privacy_current(resources, "talkback")
            and resources.talkback_requested
            and resources.talkback_generation == generation
        )

    def has_current_frame(self) -> bool:
        resources = self._resources
        return (
            resources is not None
            and self._last_frame_rgb is not None
            and self._privacy_current(resources)
        )

    def get_current_frame_png(self, viewport=None) -> bytes | None:
        """Return the most recently decoded video frame as PNG bytes, or None."""
        arr = self._last_frame_rgb
        resources = self._resources
        if arr is None or resources is None or not self._privacy_current(resources):
            return None
        try:
            import io

            from PIL import Image

            img = Image.fromarray(arr)
            if viewport is not None:
                width, height = img.size
                left, top, right, bottom = viewport
                box = (
                    round(left * width),
                    round(top * height),
                    round(right * width),
                    round(bottom * height),
                )
                if box[2] > box[0] and box[3] > box[1]:
                    img = img.crop(box).resize(img.size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue() if self._privacy_current(resources) else None
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
            if not self._is_current_resources(resources) or not self._privacy_current(resources):
                return

            self._spawn_resource_task(resources, self._watch_privacy(resources), "Camera privacy")

            pc = RTCPeerConnection(
                RTCConfiguration(iceServers=[RTCIceServer(urls=resources.device.get_ice_servers())])
            )
            resources.pc = pc
            if not self._is_current_resources(resources):
                return

            pc.addTransceiver("video", direction="recvonly")
            resources.mic_track = MicrophoneTrack(
                capture_allowed=lambda: (
                    self._privacy_current(resources, "talkback") and resources.talkback_requested
                )
            )
            pc.addTransceiver(resources.mic_track, direction="sendrecv")

            def is_current_stream() -> bool:
                return (
                    self._is_current_resources(resources)
                    and self._privacy_current(resources)
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

    def _privacy_current(self, resources, purpose="live") -> bool:
        lease = resources.privacy_lease
        return (
            lease is not None
            and resources.client._preview_is_current()
            and lease.policy.valid(lease)
            and lease.policy.allowed(resources.device, purpose)
        )

    def _privacy_changed(self, resources) -> None:
        if self._is_current_resources(resources) and not self._privacy_current(resources):
            # GStreamer state changes are thread-safe. Flush queued audio and
            # video at revocation before GTK gets its next idle callback.
            self._last_frame_rgb = None
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
            resources.talkback_requested = False
            audio = getattr(self, "_audio_pipeline", None)
            if audio is not None:
                audio.set_state(Gst.State.NULL)
            GLib.idle_add(self._hide_private_frame, resources.token)
            if getattr(resources.client, "_stopped", False):
                # RingClient._async_close owns the retained resource teardown.
                return
            self._submit(
                resources.client,
                self._terminate_resources(
                    resources,
                    "failed",
                    resources.privacy_lease.policy.view(resources.device).message,
                ),
                "Camera privacy revocation",
            )

    def _hide_private_frame(self, token):
        if self._is_current_stream_token(token):
            self._video_picture.set_paintable(None)
        return GLib.SOURCE_REMOVE

    async def _watch_privacy(self, resources):
        try:
            while self._is_current_resources(resources):
                self._privacy_changed(resources)
                if self._privacy_current(resources):
                    await self._check_video_deadline(resources, time.monotonic())
                await asyncio.sleep(0.1)
        finally:
            if not self._privacy_current(resources):
                with contextlib.suppress(asyncio.CancelledError):
                    await resources.cleanup()

    async def _check_video_deadline(self, resources, now: float):
        if resources.recovery_at is not None:
            if now - resources.recovery_at >= _VIDEO_RECOVERY_TIMEOUT:
                await self._terminate_resources(
                    resources, "failed", "Camera video timed out. Start the stream to try again."
                )
            return
        deadline = (
            resources.admitted_at + _FIRST_FRAME_TIMEOUT
            if resources.last_frame_at is None
            else resources.last_frame_at + _VIDEO_STALL_TIMEOUT
        )
        if now < deadline:
            return

        resources.recovery_at = now
        GLib.idle_add(self._on_recovering, resources)
        # The pinned aiortc receiver routes a Picture Loss Indication to a recently
        # observed video source. No new Ring session or automatic reconnect is needed.
        if resources.pc is None:
            return
        try:
            for receiver in resources.pc.getReceivers():
                if getattr(receiver.track, "kind", None) == "video":
                    sources = receiver.getSynchronizationSources()
                    if sources:
                        async with asyncio.timeout(1):
                            await receiver._send_rtcp_pli(sources[0].source)
                        break
        except Exception as exc:
            _log.debug("Video keyframe request failed: %s", exc)

    def _on_recovering(self, resources) -> bool:
        if self._is_current_resources(resources) and resources.recovery_at is not None:
            self._set_status("Waiting for the camera to resume video…")
            if self._on_stream_state_changed_cb is not None:
                self._on_stream_state_changed_cb("recovering")
        return GLib.SOURCE_REMOVE

    async def _receive_frames(self, track, resources: _StreamResources) -> None:
        import numpy as np

        _log.debug("Video frame receiver started")
        # Per-stream state lives in locals (not on self) so a freshly-started
        # receiver can't race a still-running old one over caps/PTS.
        video_dimensions = None
        clock = FrameClock()
        terminal_state = "stopped"
        terminal_message = "Stream ended"
        try:
            while self._is_current_resources(resources) and self._privacy_current(resources):
                frame = await track.recv()
                if not self._is_current_resources(resources) or not self._privacy_current(
                    resources
                ):
                    break
                rgb = frame.to_ndarray(format="rgb24", threads=1)
                h, w = rgb.shape[:2]

                # Store the un-padded frame for screenshots.
                resources.privacy_lease.policy.apply(
                    resources.privacy_lease, lambda rgb=rgb: setattr(self, "_last_frame_rgb", rgb)
                )

                # GStreamer requires RGB row stride to be a multiple of 4 bytes.
                w_caps = (w + 3) & ~3
                if w_caps != w:
                    padded = np.zeros((h, w_caps, 3), dtype=np.uint8)
                    padded[:, :w, :] = rgb
                    raw: bytes = padded.tobytes()
                else:
                    raw = rgb.tobytes()

                if video_dimensions != (w_caps, h):
                    caps = Gst.Caps.from_string(
                        f"video/x-raw,format=RGB,width={w_caps},height={h},framerate=0/1"
                    )
                    self._video_appsrc.set_property("caps", caps)
                    video_dimensions = (w_caps, h)
                    if w_caps != w:
                        _log.debug("Video stream: %dx%d (padded to %dx%d)", w, h, w_caps, h)
                    else:
                        _log.debug("Video stream: %dx%d", w, h)

                timestamp, discontinuity = clock.stamp(frame, Gst.SECOND // 30)
                buffer = Gst.Buffer.new_wrapped(raw)
                buffer.pts = timestamp
                buffer.dts = timestamp
                buffer.duration = Gst.CLOCK_TIME_NONE
                if discontinuity:
                    buffer.set_flags(Gst.BufferFlags.DISCONT)
                result = None

                def push(buffer=buffer):
                    nonlocal result
                    result = self._video_appsrc.emit("push-buffer", buffer)

                if not resources.privacy_lease.policy.apply(resources.privacy_lease, push):
                    break
                if not gst_push_succeeded(result):
                    _log.debug("Stopping video receiver after appsrc push result: %s", result)
                    terminal_state = "failed"
                    terminal_message = "Video pipeline stopped accepting frames"
                    break
                accepted_at = time.monotonic()
                if timings.enabled:
                    timings.video_buffer(
                        resources.admitted_at, resources.last_frame_at, accepted_at
                    )
                resources.last_frame_at = accepted_at
                if not resources.connected or resources.recovery_at is not None:
                    resources.recovery_at = None
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
                if not self._privacy_current(resources):
                    self._privacy_changed(resources)
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
        audio_format = None
        clock = FrameClock()
        audio_push_logged = False
        failed_output = None
        try:
            while self._is_current_resources(resources) and self._privacy_current(resources):
                frame = await track.recv()
                if not self._is_current_resources(resources):
                    break
                if not self._privacy_current(resources, "audio"):
                    continue
                output = self._audio_appsrc
                if output is None or output is failed_output:
                    continue
                raw, channels, rate, samples = audio_frame_to_s16le_interleaved(frame)

                if audio_format != (output, channels, rate):
                    caps_str = (
                        "audio/x-raw,format=S16LE,layout=interleaved,"
                        f"channels={channels},rate={rate}"
                    )
                    output.set_property("caps", Gst.Caps.from_string(caps_str))
                    audio_format = (output, channels, rate)
                    _log.debug("Ring audio stream: S16LE interleaved %dch %dHz", channels, rate)

                duration = samples * Gst.SECOND // rate if rate > 0 else Gst.SECOND // 50
                timestamp, discontinuity = clock.stamp(frame, duration)
                buffer = Gst.Buffer.new_wrapped(raw)
                buffer.pts = timestamp
                buffer.dts = timestamp
                buffer.duration = duration
                if discontinuity:
                    buffer.set_flags(Gst.BufferFlags.DISCONT)
                result = None

                def push(buffer=buffer, output=output):
                    nonlocal result
                    result = output.emit("push-buffer", buffer)

                if not resources.privacy_lease.policy.apply(resources.privacy_lease, push):
                    break
                if self._audio_appsrc is not output:
                    continue
                if not audio_push_logged:
                    _log.debug("Ring audio push result: %s", result.value_nick)
                    audio_push_logged = True
                if not gst_push_succeeded(result):
                    failed_output = output
                    GLib.idle_add(self._audio_push_failed, resources.token, output)

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
            except (concurrent.futures.CancelledError, asyncio.CancelledError):
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
        if self._resources is None or not self._privacy_current(self._resources):
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
        self._stop_audio_output()
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
