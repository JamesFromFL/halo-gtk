"""Focused live camera monitoring page."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import gi

from halo_gtk import camera_privacy

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from halo_gtk import camera_lighting, device_commands, device_names, media_paths  # noqa: E402
from halo_gtk import config as _cfg  # noqa: E402
from halo_gtk.camera_settings import CameraSettingsWindow  # noqa: E402
from halo_gtk.live_sessions import (  # noqa: E402
    FOCUSED_OWNER,
    StreamLimitExceeded,
    get_live_session_manager,
)
from halo_gtk.log_redaction import redact_text  # noqa: E402
from halo_gtk.network_status import camera_network_status  # noqa: E402
from halo_gtk.power_status import camera_power_status  # noqa: E402
from halo_gtk.ring_media import supports_ring_camera_intercom  # noqa: E402
from halo_gtk.zoom_view import (  # noqa: E402
    ZOOM_MAX,
    ZOOM_MIN,
    ZOOM_STEP,
    ZoomPaintableView,
)

_log = logging.getLogger(__name__)


class FocusedLivePage(Gtk.Box):
    """Large focused live monitor shared by Dashboard and Live Monitoring."""

    def __init__(self, *, on_history=None, on_nickname_changed=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_history = on_history
        self._on_nickname_changed = on_nickname_changed
        self._device = None
        self._view_generation = 0
        self._max_streams = 4
        self._session = None
        self._intercom_supported = False
        self._volume = 0.0
        self._screenshot_pending = False
        self._previous_volume = 1.0
        self._zoom = 1.0
        self._drag_start_x = 0.0
        self._drag_start_y = 0.0
        self._still_paintable = None
        self._still_privacy_lease = None
        self._live_ready_source_id: int | None = None

        self._build_ui()
        self._privacy_watch = camera_privacy.PrivacyWatch(self._refresh_privacy)

    def do_unroot(self) -> None:
        self._privacy_watch.close()
        self.leave()
        Gtk.Box.do_unroot(self)

    def _build_ui(self) -> None:
        top_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=24,
            margin_bottom=12,
            margin_start=24,
            margin_end=24,
        )
        self.append(top_bar)

        title_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True)
        self._title_label = Gtk.Label(label="Live camera", xalign=0, ellipsize=3)
        self._title_label.add_css_class("halo-page-heading")
        self._model_label = Gtk.Label(label="Ring camera", xalign=0, ellipsize=3)
        self._model_label.add_css_class("halo-page-subtitle")
        title_copy.append(self._title_label)
        title_copy.append(self._model_label)
        self._privacy_label = Gtk.Label(xalign=0, wrap=True, css_classes=["caption"])
        title_copy.append(self._privacy_label)
        self._stream_status_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        title_copy.append(self._stream_status_label)
        self._lighting_label = Gtk.Label(
            xalign=0, wrap=True, css_classes=["caption", "dim-label"], visible=False
        )
        title_copy.append(self._lighting_label)
        top_bar.append(title_copy)

        self._settings_btn = Gtk.Button(
            icon_name="preferences-system-symbolic",
            tooltip_text="Camera Settings",
            css_classes=["flat"],
        )
        self._settings_btn.connect("clicked", self._on_camera_settings)
        top_bar.append(self._settings_btn)

        history_btn = Gtk.Button(
            icon_name="document-open-recent-symbolic",
            tooltip_text="Event history for this camera",
            css_classes=["flat"],
        )
        history_btn.connect("clicked", self._on_history_clicked)
        top_bar.append(history_btn)

        self._video_view = ZoomPaintableView()

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self._video_view.add_controller(drag)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self._video_view.add_controller(scroll)

        monitor = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=6,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
            hexpand=True,
            vexpand=True,
        )
        self.append(monitor)

        controls = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            min_children_per_line=1,
            max_children_per_line=2,
            column_spacing=16,
            row_spacing=8,
            homogeneous=False,
            halign=Gtk.Align.CENTER,
        )
        controls.add_css_class("halo-focus-controls")

        transport_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        transport_controls.set_halign(Gtk.Align.CENTER)
        view_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        view_controls.set_halign(Gtk.Align.CENTER)
        controls.append(transport_controls)
        controls.append(view_controls)

        play_box = Gtk.Box(css_classes=["linked"], spacing=0)
        self._play_btn = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            tooltip_text="Start live stream",
        )
        self._play_btn.connect("clicked", self._on_play)
        play_box.append(self._play_btn)
        self._stop_btn = Gtk.Button(
            icon_name="media-playback-stop-symbolic",
            tooltip_text="Stop live stream",
        )
        self._stop_btn.connect("clicked", self._on_stop)
        play_box.append(self._stop_btn)
        transport_controls.append(play_box)

        self._volume_btn = Gtk.ToggleButton(
            icon_name="audio-volume-muted-symbolic",
            tooltip_text="Unmute",
        )
        self._volume_btn.connect("toggled", self._on_volume_toggled)
        transport_controls.append(self._volume_btn)

        self._volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self._volume_scale.set_draw_value(False)
        self._volume_scale.set_size_request(96, -1)
        self._volume_scale.set_tooltip_text("Incoming camera audio volume")
        self._volume_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Incoming camera audio volume"],
        )
        self._volume_scale.connect("value-changed", self._on_volume_changed)
        transport_controls.append(self._volume_scale)

        from halo_gtk.talk_button import HoldToTalkButton

        self._mic_btn = HoldToTalkButton(lambda: self._session, compact=True)
        transport_controls.append(self._mic_btn)

        screenshot_btn = self._screenshot_btn = Gtk.Button(
            icon_name="camera-photo-symbolic",
            tooltip_text="Save screenshot",
        )
        screenshot_btn.connect("clicked", self._on_screenshot)
        view_controls.append(screenshot_btn)

        zoom_box = Gtk.Box(css_classes=["linked"], spacing=0)
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text="Zoom out")
        zoom_out.connect("clicked", lambda *_: self._set_zoom(self._zoom - ZOOM_STEP))
        zoom_box.append(zoom_out)
        self._zoom_label = Gtk.Label(label="100%", width_request=54)
        zoom_box.append(self._zoom_label)
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text="Zoom in")
        zoom_in.connect("clicked", lambda *_: self._set_zoom(self._zoom + ZOOM_STEP))
        zoom_box.append(zoom_in)
        reset = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text="Reset zoom")
        reset.connect("clicked", lambda *_: self._set_zoom(1.0))
        zoom_box.append(reset)
        view_controls.append(zoom_box)

        self._light_btn = Gtk.ToggleButton(tooltip_text="Light")
        self._light_icon = Gtk.Image(pixel_size=16)
        self._light_btn.set_child(self._light_icon)
        self._light_btn.connect("toggled", self._on_light_toggled)
        view_controls.append(self._light_btn)

        self._siren_btn = Gtk.Button(tooltip_text="Siren")
        self._siren_icon = Gtk.Image.new_from_icon_name("alarm-symbolic")
        self._siren_icon.set_pixel_size(16)
        self._siren_btn.set_child(self._siren_icon)
        self._siren_btn.add_css_class("siren-button")
        self._siren_btn.connect("clicked", self._on_siren_clicked)
        view_controls.append(self._siren_btn)

        aspect = Gtk.AspectFrame(
            xalign=0.5,
            yalign=0.5,
            ratio=16 / 9,
            obey_child=False,
            hexpand=True,
            vexpand=True,
        )
        aspect.add_css_class("video-frame")
        aspect.add_css_class("halo-focus-canvas")
        aspect.set_overflow(Gtk.Overflow.HIDDEN)
        aspect.set_child(self._video_view)
        monitor.append(aspect)
        monitor.append(controls)

        health = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=1,
            max_children_per_line=3,
            column_spacing=18,
            row_spacing=8,
            hexpand=True,
        )
        health.add_css_class("health-strip")
        health.add_css_class("halo-focus-health")
        network_item, self._network_icon, self._network_value = self._build_health_item(
            "network-wireless-signal-good-symbolic"
        )
        power_item, self._power_icon, self._power_value = self._build_health_item(
            "ac-adapter-symbolic"
        )
        motion_item, self._motion_icon, self._motion_value = self._build_health_item(
            "preferences-system-notifications-symbolic"
        )
        for item in (network_item, power_item, motion_item):
            health.append(item)
        monitor.append(health)

        self._set_controls_sensitive(False)

    @staticmethod
    def _build_health_item(icon_name: str) -> tuple[Gtk.Box, Gtk.Image, Gtk.Label]:
        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        item.set_halign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class("dim-label")
        value = Gtk.Label(label="Not available", ellipsize=3)
        item.append(icon)
        item.append(value)
        return item, icon, value

    def show_device(
        self,
        device,
        *,
        max_streams: int = 4,
        initial_snapshot: camera_privacy.CameraPreview | None = None,
    ) -> None:
        previous = getattr(self, "_still_cancel_event", None)
        if previous is not None:
            previous.set()
        self._view_generation += 1
        self._device = device
        self._max_streams = max_streams
        self._title_label.set_label(device_names.display_name(device))
        try:
            model = str(device.model or "").strip()
        except Exception:
            model = ""
        self._model_label.set_label(model or "Ring camera")
        self._cancel_live_ready_poll()
        self._clear_video_box()
        if (
            isinstance(initial_snapshot, camera_privacy.CameraPreview)
            and initial_snapshot.lease.device is device
            and camera_privacy.lease_is_current(initial_snapshot.lease)
        ):
            self._still_privacy_lease = initial_snapshot.lease
            self._set_still_paintable(initial_snapshot.data)
        else:
            self._load_snapshot_still(device)
        self._refresh_privacy()
        self._refresh_status_icons()
        self._refresh_device_capability_controls()
        existing_session = get_live_session_manager().session_for(device.id)
        initial_volume = (
            existing_session.volume
            if existing_session is not None and existing_session.active
            else self._start_volume()
        )
        self._set_volume_state(initial_volume)
        self._set_zoom(1.0)
        self._start_session()

    def leave(self) -> None:
        cancel_event = getattr(self, "_still_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        self._view_generation += 1
        if self._device is not None:
            self._stop_talking()
            get_live_session_manager().release(self._device.id, owner=FOCUSED_OWNER)
        self._session = None
        self._set_controls_sensitive(False)
        self._cancel_live_ready_poll()
        self._clear_video_box()

    def _start_session(self) -> None:
        if self._device is None:
            return
        if camera_privacy.current_view(self._device).live_allowed is not True:
            self._refresh_privacy()
            return
        try:
            self._session = get_live_session_manager().acquire(
                self._device,
                owner=FOCUSED_OWNER,
                volume=self._volume,
                max_streams=self._max_streams,
            )
        except StreamLimitExceeded:
            _log.warning("Focused live view denied by stream cap")
            self._set_controls_sensitive(False)
            return
        except Exception as exc:
            _log.warning("Focused live view could not start: %s", exc)
            self._session = None
            self._stop_talking()
            self._set_controls_sensitive(False)
            return
        if self._session.has_current_frame():
            self._show_session_paintable()
        else:
            self._wait_for_live_frame()
        self._set_controls_sensitive(True)
        self._apply_zoom()

    def _stop_session(self) -> None:
        if self._device is None:
            return
        self._cancel_live_ready_poll()
        if self._session is not None:
            lease = camera_privacy.acquire_current(self._device, "preview")
            paintable = self._session.get_paintable()
            if lease is not None and paintable is not None and self._session.has_current_frame():
                self._still_paintable = paintable.get_current_image()
                self._still_privacy_lease = lease
            if camera_privacy.lease_is_current(self._still_privacy_lease):
                self._video_view.set_paintable(self._still_paintable)
            else:
                self._clear_video_box()
        # Release only this view's ownership; if Live Monitoring still owns the
        # session the underlying stream keeps running (don't stop_device()).
        self._stop_talking()
        get_live_session_manager().release(self._device.id, owner=FOCUSED_OWNER)
        self._session = None
        self._set_controls_sensitive(False)

    def _clear_video_box(self) -> None:
        self._video_view.set_paintable(None)
        self._video_view.reset_pan()
        self._still_paintable = None
        self._still_privacy_lease = None

    def _refresh_privacy(self) -> None:
        if self._device is None:
            return
        self._refresh_lighting()
        view = camera_privacy.current_view(self._device)
        self._privacy_label.set_label(view.message)
        self._privacy_label.set_visible(view.reason != "allowed")
        if self._still_paintable is not None and not camera_privacy.lease_is_current(
            self._still_privacy_lease
        ):
            self._clear_video_box()
        if view.live_allowed is not True:
            self._stop_talking()
            get_live_session_manager().release(self._device.id, owner=FOCUSED_OWNER)
            self._session = None
            self._clear_video_box()
            self._cancel_live_ready_poll()
        elif view.audio_allowed is not True:
            self._stop_talking()
        session = self._session
        if session is not None and session.get_paintable() is None:
            self._video_view.set_paintable(None)
        stream_status = {
            "waiting": "Waiting for the previous camera to close…",
            "recovering": "Waiting for the camera to resume video…",
            "failed": "Camera video unavailable. Select Play to try again.",
        }.get(session.state if session is not None else None)
        self._stream_status_label.set_label(stream_status or "")
        self._stream_status_label.set_visible(stream_status is not None)
        self._set_controls_sensitive(session is not None and session.active)

    def _show_session_paintable(self) -> None:
        paintable = self._session.get_paintable() if self._session is not None else None
        self._video_view.set_paintable(paintable)
        self._video_view.reset_pan()

    def _set_still_paintable(self, png_bytes: bytes) -> bool:
        if not camera_privacy.lease_is_current(self._still_privacy_lease):
            return False
        texture = _texture_from_png(png_bytes)
        if texture is None:
            return False
        self._still_paintable = texture
        self._video_view.set_paintable(texture)
        self._video_view.reset_pan()
        return True

    def _load_snapshot_still(self, device) -> None:
        lease = camera_privacy.acquire_current(device, "preview")
        if lease is None:
            return
        device_id = getattr(device, "id", None)
        generation = self._view_generation
        previous = getattr(self, "_still_cancel_event", None)
        if previous is not None:
            previous.set()
        cancel_event = self._still_cancel_event = threading.Event()

        def worker() -> None:
            png_bytes = None
            try:
                from halo_gtk.ring_client import get_client

                client = get_client()
                if client is not None and client.is_authenticated:
                    png_bytes = client.snapshot_for_device(device, cancel_event=cancel_event)
                    if not png_bytes and not cancel_event.is_set():
                        png_bytes = client.last_event_frame_for_device(
                            device, cancel_event=cancel_event
                        )
            except Exception as exc:
                _log.debug("Focused still snapshot failed for %s: %s", device.name, exc)
            if png_bytes and not cancel_event.is_set():
                GLib.idle_add(
                    self._set_loaded_still, device_id, bytes(png_bytes), generation, lease
                )

        threading.Thread(target=worker, daemon=True).start()

    def _set_loaded_still(
        self, device_id: int | None, png_bytes: bytes, generation: int, lease=None
    ) -> bool:
        if (
            generation != self._view_generation
            or self._device is None
            or getattr(self._device, "id", None) != device_id
            or not camera_privacy.lease_is_current(lease)
        ):
            return GLib.SOURCE_REMOVE
        # Do not overwrite a live stream that already has its first frame.
        if self._session is not None and self._session.has_current_frame():
            return GLib.SOURCE_REMOVE
        self._still_privacy_lease = lease
        self._set_still_paintable(png_bytes)
        return GLib.SOURCE_REMOVE

    def _wait_for_live_frame(self) -> None:
        self._cancel_live_ready_poll()
        self._live_ready_source_id = GLib.timeout_add(100, self._promote_live_when_ready)

    def _promote_live_when_ready(self) -> bool:
        if self._session is None:
            self._live_ready_source_id = None
            return GLib.SOURCE_REMOVE
        state = getattr(self._session.state, "value", str(self._session.state))
        if state in {"failed", "stopped"}:
            self._live_ready_source_id = None
            self._stop_talking()
            self._set_controls_sensitive(False)
            return GLib.SOURCE_REMOVE
        if not self._session.has_current_frame():
            return GLib.SOURCE_CONTINUE
        self._show_session_paintable()
        self._live_ready_source_id = None
        return GLib.SOURCE_REMOVE

    def _cancel_live_ready_poll(self) -> None:
        if self._live_ready_source_id is not None:
            GLib.source_remove(self._live_ready_source_id)
            self._live_ready_source_id = None

    def _start_volume(self) -> float:
        return 1.0 if _cfg.load().get("live_monitoring_unmute_on_start", False) else 0.0

    def _set_controls_sensitive(self, sensitive: bool) -> None:
        view = camera_privacy.current_view(self._device)
        self._play_btn.set_sensitive(not sensitive and view.live_allowed is True)
        self._stop_btn.set_sensitive(sensitive)
        audio = sensitive and view.live_allowed is True and view.audio_allowed is True
        self._mic_btn.set_sensitive(audio and self._intercom_supported)
        self._volume_btn.set_sensitive(audio)
        self._volume_scale.set_sensitive(audio)
        screenshot = getattr(self, "_screenshot_btn", None)
        if screenshot is not None:
            screenshot.set_sensitive(sensitive and view.preview_allowed is True)

    def _refresh_status_icons(self) -> None:
        if self._device is None:
            return
        network = camera_network_status(self._device)
        self._network_icon.set_from_icon_name(network.icon_name)
        self._network_icon.set_tooltip_text(network.tooltip)
        self._network_value.set_label(network.tooltip)

        power = camera_power_status(self._device)
        self._power_icon.set_from_icon_name(power.icon_name)
        self._power_icon.set_tooltip_text(power.tooltip)
        self._power_value.set_label(power.tooltip)
        motion_enabled = getattr(self._device, "motion_detection", True)
        self._motion_value.set_label(
            "Motion alerts on" if motion_enabled else "Motion detection off"
        )
        self._refresh_light_icon()

    def _refresh_device_capability_controls(self) -> None:
        has_light = device_commands.has_capability(self._device, "light")
        has_siren = device_commands.has_capability(self._device, "siren")
        self._intercom_supported = supports_ring_camera_intercom(self._device)
        self._mic_btn.set_visible(self._intercom_supported)
        self._stop_talking()
        self._light_btn.set_visible(has_light)
        self._siren_btn.set_visible(has_siren)
        if has_light:
            self._light_btn.handler_block_by_func(self._on_light_toggled)
            self._light_btn.set_active(
                camera_lighting.view_for(_ring_client(), self._device).button_active
            )
            self._light_btn.handler_unblock_by_func(self._on_light_toggled)
            self._refresh_light_icon()

    def _set_volume_state(self, value: float) -> None:
        volume = max(0.0, min(1.0, value))
        self._volume = volume
        if volume > 0:
            self._previous_volume = volume
        self._volume_btn.handler_block_by_func(self._on_volume_toggled)
        self._volume_btn.set_active(volume > 0)
        self._volume_btn.handler_unblock_by_func(self._on_volume_toggled)
        self._volume_btn.set_icon_name(
            "audio-volume-high-symbolic" if volume > 0 else "audio-volume-muted-symbolic"
        )
        self._volume_btn.set_tooltip_text("Mute" if volume > 0 else "Unmute")
        self._volume_scale.handler_block_by_func(self._on_volume_changed)
        self._volume_scale.set_value(volume)
        self._volume_scale.handler_unblock_by_func(self._on_volume_changed)
        if self._session is not None:
            self._session.set_volume(volume)

    def _on_volume_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._set_volume_state(self._previous_volume if btn.get_active() else 0.0)

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        self._set_volume_state(scale.get_value())

    def _on_screenshot(self, *_args) -> None:
        from halo_gtk.ui_feedback import show_toast

        session = self._session
        if session is None or self._screenshot_pending:
            return
        lease = camera_privacy.acquire_current(self._device, "preview")
        if lease is None:
            return
        viewport = None
        if self._zoom > 1.0:
            paintable = session.get_paintable()
            width, height = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
            if width > 0 and height > 0:
                left, top, right, bottom = self._video_view.source_viewport(width, height)
                viewport = left / width, top / height, right / width, bottom / height
        camera_name = device_names.display_name(self._device)
        generation = self._view_generation
        self._screenshot_pending = True

        def finish(message, folder=None):
            self._screenshot_pending = False
            if generation != self._view_generation or not camera_privacy.lease_is_current(lease):
                return GLib.SOURCE_REMOVE
            if message:
                show_toast(self, message)
            if folder is not None and media_paths.should_open_after_save():
                try:
                    media_paths.open_folder(folder)
                except Exception:
                    show_toast(self, "Screenshot saved, but the folder could not be opened.")
            return GLib.SOURCE_REMOVE

        def save():
            message, folder = None, None
            try:
                if camera_privacy.lease_is_current(lease):
                    png = session.get_current_frame_png(viewport)
                    if png and camera_privacy.lease_is_current(lease):
                        dest = media_paths.snapshot_dir(camera_name)
                        saved = media_paths.write_unique_bytes(
                            dest,
                            datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
                            ".png",
                            png,
                            publish=lambda commit: (
                                camera_privacy.lease_is_current(lease)
                                and lease.policy.apply(lease, commit)
                            ),
                        )
                        if saved is not None:
                            message, folder = "Screenshot saved", dest
            except OSError:
                message = "Could not save screenshot. Check free space and folder permissions."
            finally:
                GLib.idle_add(finish, message, folder)

        threading.Thread(target=save, daemon=True, name="halo-screenshot").start()

    def _on_play(self, *_args) -> None:
        self._start_session()

    def _on_stop(self, *_args) -> None:
        self._stop_session()

    def _on_light_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._refresh_light_icon()
        if self._device is None:
            return
        device = self._device
        requested_active = btn.get_active()
        client = _ring_client()
        command_builder = getattr(client, "camera_light_command", None)
        if client is None or not callable(command_builder):
            self._finish_light_command(
                device,
                requested_active,
                RuntimeError("Light control is unavailable"),
            )
            return
        btn.set_sensitive(False)
        try:
            command = command_builder(device, requested_active)
        except Exception as exc:
            self._finish_light_command(device, requested_active, exc)
            return
        device_commands.submit_observed(
            client,
            command,
            lambda error: self._finish_light_command(
                device,
                requested_active,
                error,
                client=client,
            ),
        )

    def _finish_light_command(
        self,
        device,
        requested_active: bool,
        error: Exception | None,
        *,
        client=None,
    ) -> bool:
        if client is not None and _ring_client() is not client:
            return GLib.SOURCE_REMOVE
        if self._device is device:
            self._light_btn.set_sensitive(True)
            self._light_btn.handler_block_by_func(self._on_light_toggled)
            self._light_btn.set_active(
                camera_lighting.view_for(client or _ring_client(), device).button_active
            )
            self._light_btn.handler_unblock_by_func(self._on_light_toggled)
            self._refresh_light_icon()
        if error is not None and self._device is device:
            action = "turn on" if requested_active else "turn off"
            self._present_device_command_error(
                "Light request uncertain"
                if getattr(error, "possibly_sent", False)
                else f"Could not {action} light",
                device,
                error,
            )
        return GLib.SOURCE_REMOVE

    def _on_siren_clicked(self, *_args) -> None:
        if self._device is None:
            return
        client = _ring_client()
        if device_commands.command_state.siren_active(client, self._device):
            self._set_siren(0, client=client)
            return
        dialog = Adw.AlertDialog(
            heading="Activate Siren?",
            body=f"Activate siren for {device_names.display_name(self._device)}?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("activate", "Activate")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("activate", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect(
            "response", self._on_siren_confirmed, self._device, client, self._view_generation
        )
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Window) else self)

    def _on_siren_confirmed(self, _dialog, response: str, device, client, generation: int) -> None:
        if (
            response == "activate"
            and generation == self._view_generation
            and self._device is device
            and client is not None
            and _ring_client() is client
        ):
            self._set_siren(30, client=client)

    def _set_siren(self, duration: int, *, client=None) -> None:
        if self._device is None:
            return
        setter = getattr(self._device, "async_set_siren", None)
        if client is None:
            client = _ring_client()
        device = self._device
        if client is None or not callable(setter):
            self._finish_siren_command(
                device,
                duration,
                RuntimeError("Siren control is unavailable"),
            )
            return
        self._siren_btn.set_sensitive(False)
        try:
            command = setter(duration)
        except Exception as exc:
            self._finish_siren_command(device, duration, exc)
            return
        device_commands.submit_observed(
            client,
            command,
            lambda error: self._finish_siren_command(
                device,
                duration,
                error,
                client=client,
            ),
        )

    def _finish_siren_command(
        self,
        device,
        duration: int,
        error: Exception | None,
        *,
        client=None,
    ) -> bool:
        if error is None:
            device_commands.command_state.record_siren_success(client, device, duration)
        if self._device is device:
            self._siren_btn.set_sensitive(True)
        if error is not None:
            heading = "Could not stop siren" if duration == 0 else "Could not activate siren"
            self._present_device_command_error(heading, device, error)
        return GLib.SOURCE_REMOVE

    def _present_device_command_error(self, heading: str, device, error: Exception) -> None:
        _log.warning("%s for %s: %s", heading, device.name, error)
        summary = device_commands.command_failure_summary(device_names.display_name(device), error)
        dialog = Adw.AlertDialog(
            heading=heading,
            body=(f"{summary}\n\n{redact_text(error)}"),
        )
        dialog.add_response("close", "Close")
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Window) else self)

    def _on_history_clicked(self, *_args) -> None:
        if self._device is not None and self._on_history is not None:
            self._on_history(self._device.id)

    def _on_camera_settings(self, *_args) -> None:
        if self._device is None:
            return
        root = self.get_root()
        app = root.get_application() if isinstance(root, Gtk.Window) else None
        window = CameraSettingsWindow(
            self._device,
            application=app,
            on_nickname_changed=self._on_nickname_changed,
        )
        if isinstance(root, Gtk.Window):
            window.set_transient_for(root)
        window.present()

    def _set_zoom(self, value: float) -> None:
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, round(value, 1)))
        self._zoom_label.set_label(f"{round(self._zoom * 100)}%")
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        self._video_view.set_zoom(self._zoom)
        if self._zoom <= 1.0:
            self._video_view.reset_pan()

    def _on_scroll(self, _controller, _dx: float, dy: float) -> bool:
        if dy < 0:
            self._set_zoom(self._zoom + ZOOM_STEP)
        elif dy > 0:
            self._set_zoom(self._zoom - ZOOM_STEP)
        return True

    def _on_drag_begin(self, _gesture, _x: float, _y: float) -> None:
        self._drag_start_x, self._drag_start_y = self._video_view.pan_origin()

    def _on_drag_update(self, _gesture, offset_x: float, offset_y: float) -> None:
        if self._zoom <= 1.0:
            return
        self._video_view.set_pan(self._drag_start_x + offset_x, self._drag_start_y + offset_y)

    def _stop_talking(self) -> None:
        if hasattr(self, "_mic_btn"):
            self._mic_btn.release()

    def _refresh_lighting(self) -> None:
        supported = device_commands.has_capability(self._device, "light")
        label = getattr(self, "_lighting_label", None)
        if label is not None:
            label.set_visible(supported)
        if not supported:
            return
        view = camera_lighting.view_for(_ring_client(), self._device)
        if label is not None:
            label.set_label(view.message)
        self._light_btn.handler_block_by_func(self._on_light_toggled)
        self._light_btn.set_active(view.button_active)
        self._light_btn.handler_unblock_by_func(self._on_light_toggled)
        self._light_btn.set_sensitive(view.phase not in {"pending", "accepted"})
        self._refresh_light_icon()

    def _refresh_light_icon(self) -> None:
        if not hasattr(self, "_light_icon"):
            return
        self._light_icon.set_from_icon_name("display-brightness-symbolic")
        self._light_btn.set_tooltip_text(
            (
                "Request camera light Off"
                if self._light_btn.get_active()
                else "Request camera light On"
            )
            + " · "
            + camera_lighting.view_for(_ring_client(), self._device).message
        )


def _texture_from_png(png_bytes: bytes):
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_bytes))
    except GLib.Error as exc:
        _log.debug("Focused still texture decode failed: %s", exc)
        return None


def _ring_client():
    from halo_gtk.ring_client import get_client

    return get_client()
