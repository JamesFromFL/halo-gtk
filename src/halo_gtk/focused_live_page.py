"""Focused live camera monitoring page."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from halo_gtk import config as _cfg  # noqa: E402
from halo_gtk import device_commands, device_names, media_paths, theme_icons  # noqa: E402
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
        self._max_streams = 4
        self._session = None
        self._intercom_supported = False
        self._volume = 0.0
        self._previous_volume = 1.0
        self._zoom = 1.0
        self._drag_start_x = 0.0
        self._drag_start_y = 0.0
        self._theme_handler_id: int | None = None
        self._still_paintable = None
        self._live_ready_source_id: int | None = None

        self._build_ui()
        self._theme_handler_id = theme_icons.connect_theme_changed(self._refresh_status_icons)

    def do_unroot(self) -> None:
        self.leave()
        theme_icons.disconnect_theme_changed(self._theme_handler_id)
        self._theme_handler_id = None
        Gtk.Box.do_unroot(self)

    def _build_ui(self) -> None:
        top_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self.append(top_bar)

        self._network_icon = Gtk.Image(pixel_size=26, valign=Gtk.Align.CENTER)
        self._power_icon = Gtk.Image(pixel_size=26, valign=Gtk.Align.CENTER)
        top_bar.append(self._network_icon)
        top_bar.append(self._power_icon)

        top_bar.append(Gtk.Box(hexpand=True))

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

        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._video_view = ZoomPaintableView()
        self.append(self._video_view)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self._video_view.add_controller(drag)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self._video_view.add_controller(scroll)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        bottom_bar = Gtk.CenterBox(
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self.append(bottom_bar)

        left_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._mic_btn = Gtk.ToggleButton(
            icon_name="microphone-disabled-symbolic",
            tooltip_text="Microphone off",
        )
        self._mic_btn.connect("toggled", self._on_mic_toggled)
        left_controls.append(self._mic_btn)

        screenshot_btn = Gtk.Button(
            icon_name="camera-photo-symbolic",
            tooltip_text="Save screenshot",
        )
        screenshot_btn.connect("clicked", self._on_screenshot)
        left_controls.append(screenshot_btn)

        self._light_btn = Gtk.ToggleButton(tooltip_text="Light")
        self._light_icon = Gtk.Image(pixel_size=24)
        self._light_btn.set_child(self._light_icon)
        self._light_btn.connect("toggled", self._on_light_toggled)
        left_controls.append(self._light_btn)

        self._siren_btn = Gtk.Button(tooltip_text="Siren")
        self._siren_icon = Gtk.Image(pixel_size=24)
        self._siren_btn.set_child(self._siren_icon)
        self._siren_btn.connect("clicked", self._on_siren_clicked)
        left_controls.append(self._siren_btn)
        bottom_bar.set_start_widget(left_controls)

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
        bottom_bar.set_center_widget(play_box)

        right_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._volume_btn = Gtk.ToggleButton(
            icon_name="audio-volume-muted-symbolic",
            tooltip_text="Unmute",
        )
        self._volume_btn.connect("toggled", self._on_volume_toggled)
        right_controls.append(self._volume_btn)

        self._volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self._volume_scale.set_draw_value(False)
        self._volume_scale.set_size_request(140, -1)
        self._volume_scale.connect("value-changed", self._on_volume_changed)
        right_controls.append(self._volume_scale)

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
        right_controls.append(zoom_box)
        bottom_bar.set_end_widget(right_controls)

        self._set_controls_sensitive(False)

    def show_device(
        self,
        device,
        *,
        max_streams: int = 4,
        initial_snapshot: bytes | None = None,
    ) -> None:
        self._device = device
        self._max_streams = max_streams
        self._cancel_live_ready_poll()
        if initial_snapshot:
            self._set_still_paintable(initial_snapshot)
        else:
            self._load_snapshot_still(device)
        self._refresh_status_icons()
        self._refresh_device_capability_controls()
        self._set_volume_state(self._start_volume())
        self._set_zoom(1.0)
        self._start_session()

    def leave(self) -> None:
        if self._device is not None:
            get_live_session_manager().release(self._device.id, owner=FOCUSED_OWNER)
        self._session = None
        self._reset_microphone_control()
        self._set_controls_sensitive(False)
        self._cancel_live_ready_poll()
        self._clear_video_box()

    def _start_session(self) -> None:
        if self._device is None:
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
        if self._session.get_current_frame_png() is not None:
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
            png = self._session.get_current_frame_png()
            if png:
                self._set_still_paintable(png)
            elif self._still_paintable is not None:
                self._video_view.set_paintable(self._still_paintable)
        # Release only this view's ownership; if Live Monitoring still owns the
        # session the underlying stream keeps running (don't stop_device()).
        get_live_session_manager().release(self._device.id, owner=FOCUSED_OWNER)
        self._session = None
        self._set_controls_sensitive(False)

    def _clear_video_box(self) -> None:
        self._video_view.set_paintable(None)
        self._video_view.reset_pan()
        self._still_paintable = None

    def _show_session_paintable(self) -> None:
        paintable = self._session.get_paintable() if self._session is not None else None
        self._video_view.set_paintable(paintable)
        self._video_view.reset_pan()

    def _set_still_paintable(self, png_bytes: bytes) -> bool:
        texture = _texture_from_png(png_bytes)
        if texture is None:
            return False
        self._still_paintable = texture
        self._video_view.set_paintable(texture)
        self._video_view.reset_pan()
        return True

    def _load_snapshot_still(self, device) -> None:
        device_id = getattr(device, "id", None)

        def worker() -> None:
            png_bytes = None
            try:
                from halo_gtk.ring_client import get_client

                client = get_client()
                if client is not None and client.is_authenticated:
                    png_bytes = client.snapshot_for_device(device)
                    if not png_bytes:
                        png_bytes = client.last_event_frame_for_device(device)
            except Exception as exc:
                _log.debug("Focused still snapshot failed for %s: %s", device.name, exc)
            if png_bytes:
                GLib.idle_add(self._set_loaded_still, device_id, bytes(png_bytes))

        threading.Thread(target=worker, daemon=True).start()

    def _set_loaded_still(self, device_id: int | None, png_bytes: bytes) -> bool:
        if self._device is None or getattr(self._device, "id", None) != device_id:
            return GLib.SOURCE_REMOVE
        # Do not overwrite a live stream that already has its first frame.
        if self._session is not None and self._session.get_current_frame_png() is not None:
            return GLib.SOURCE_REMOVE
        self._set_still_paintable(png_bytes)
        return GLib.SOURCE_REMOVE

    def _wait_for_live_frame(self) -> None:
        self._cancel_live_ready_poll()
        self._live_ready_source_id = GLib.timeout_add(100, self._promote_live_when_ready)

    def _promote_live_when_ready(self) -> bool:
        if self._session is None:
            self._live_ready_source_id = None
            return GLib.SOURCE_REMOVE
        if self._session.get_current_frame_png() is None:
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
        self._play_btn.set_sensitive(not sensitive)
        self._stop_btn.set_sensitive(sensitive)
        self._mic_btn.set_sensitive(sensitive and self._intercom_supported)
        self._volume_btn.set_sensitive(sensitive)
        self._volume_scale.set_sensitive(sensitive)

    def _refresh_status_icons(self) -> None:
        if self._device is None:
            return
        for widget, status in (
            (self._network_icon, camera_network_status(self._device)),
            (self._power_icon, camera_power_status(self._device)),
        ):
            try:
                widget.set_from_paintable(Gdk.Texture.new_from_filename(str(status.icon_path)))
            except GLib.Error as exc:
                _log.debug("Focused status icon load failed: %s", exc)
            widget.set_tooltip_text(status.tooltip)
        self._refresh_light_icon()

    def _refresh_device_capability_controls(self) -> None:
        has_light = device_commands.has_capability(self._device, "light")
        has_siren = device_commands.has_capability(self._device, "siren")
        self._intercom_supported = supports_ring_camera_intercom(self._device)
        self._mic_btn.set_visible(self._intercom_supported)
        self._reset_microphone_control()
        self._light_btn.set_visible(has_light)
        self._siren_btn.set_visible(has_siren)
        if has_light:
            self._light_btn.handler_block_by_func(self._on_light_toggled)
            self._light_btn.set_active(
                device_commands.command_state.light_enabled(_ring_client(), self._device)
            )
            self._light_btn.handler_unblock_by_func(self._on_light_toggled)
            self._refresh_light_icon()
        if has_siren:
            self._load_icon(self._siren_icon, theme_icons.icon_path("controls", "siren.png"))

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

    def _on_mic_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._session is None:
            return
        if btn.get_active():
            btn.set_icon_name("audio-input-microphone-symbolic")
            btn.set_tooltip_text("Microphone on")
            self._session.start_talking()
        else:
            btn.set_icon_name("microphone-disabled-symbolic")
            btn.set_tooltip_text("Microphone off")
            self._session.stop_talking()

    def _on_screenshot(self, *_args) -> None:
        if self._session is None:
            return
        png = self._session.get_current_frame_png()
        if png is None:
            return
        zoomed_png = self._video_view.zoomed_frame_png(png)
        if zoomed_png is not None:
            png = zoomed_png
        camera_name = device_names.display_name(self._device) if self._device is not None else None
        dest = media_paths.snapshot_dir(camera_name)
        media_paths.write_unique_bytes(
            dest,
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
            ".png",
            png,
        )
        if media_paths.should_open_after_save():
            media_paths.open_folder(dest)

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
        setter = getattr(self._device, "async_set_light", None)
        client = _ring_client()
        if client is None or not callable(setter):
            self._finish_light_command(
                device,
                requested_active,
                RuntimeError("Light control is unavailable"),
            )
            return
        btn.set_sensitive(False)
        try:
            command = setter(requested_active)
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
        if error is None:
            device_commands.command_state.record_light_success(
                client,
                device,
                requested_active,
            )
        if self._device is device:
            self._light_btn.set_sensitive(True)
        if error is not None and self._device is device:
            self._light_btn.handler_block_by_func(self._on_light_toggled)
            self._light_btn.set_active(not requested_active)
            self._light_btn.handler_unblock_by_func(self._on_light_toggled)
            self._refresh_light_icon()
        if error is not None:
            action = "turn on" if requested_active else "turn off"
            self._present_device_command_error(f"Could not {action} light", device, error)
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
        dialog.connect("response", self._on_siren_confirmed)
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Window) else self)

    def _on_siren_confirmed(self, _dialog, response: str) -> None:
        if response == "activate":
            self._set_siren(30)

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
        dialog = Adw.AlertDialog(
            heading=heading,
            body=(
                f"{device_names.display_name(device)} did not accept the command.\n\n"
                f"{redact_text(error)}"
            ),
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

    def _reset_microphone_control(self) -> None:
        if not hasattr(self, "_mic_btn"):
            return
        self._mic_btn.handler_block_by_func(self._on_mic_toggled)
        self._mic_btn.set_active(False)
        self._mic_btn.handler_unblock_by_func(self._on_mic_toggled)
        self._mic_btn.set_icon_name("microphone-disabled-symbolic")
        self._mic_btn.set_tooltip_text("Microphone off")

    def _refresh_light_icon(self) -> None:
        if not hasattr(self, "_light_icon"):
            return
        path = (
            theme_icons.icon_path("controls", "light-on.png")
            if self._light_btn.get_active()
            else theme_icons.themed_icon_path("controls", "light-off")
        )
        self._load_icon(self._light_icon, path)

    @staticmethod
    def _load_icon(widget: Gtk.Image, path) -> None:
        try:
            widget.set_from_paintable(Gdk.Texture.new_from_filename(str(path)))
        except GLib.Error as exc:
            _log.debug("Focused control icon load failed for %s: %s", path, exc)


def _texture_from_png(png_bytes: bytes):
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_bytes))
    except GLib.Error as exc:
        _log.debug("Focused still texture decode failed: %s", exc)
        return None


def _ring_client():
    from halo_gtk.ring_client import get_client

    return get_client()
