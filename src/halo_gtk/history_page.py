"""Event History page — Adw.NavigationSplitView with event list and GStreamer player."""

from __future__ import annotations

import logging
import re
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GLib, Gst, Gtk, Pango  # noqa: E402

from halo_gtk import activity_icons, device_names, favorites, media_paths  # noqa: E402
from halo_gtk import config as _cfg  # noqa: E402
from halo_gtk.http_download import download_https  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402
from halo_gtk.ring_events import normalized_event_kind  # noqa: E402
from halo_gtk.zoom_view import (  # noqa: E402
    ZOOM_MAX,
    ZOOM_MIN,
    ZOOM_STEP,
    ZoomPaintableView,
)

_log = logging.getLogger(__name__)

_ALL_FILTERS = [
    ("answered_ring", "Answered Ring", "event", True),
    ("missed_ring", "Missed Ring", "event", True),
    ("answered_knock", "Answered Knock", "event", False),
    ("missed_knock", "Missed Knock", "event", False),
    ("motion", "Motion", "event", True),
    ("animal", "Animal Detected", "event", False),
    ("sound", "Sound Event", "event", False),
    ("door_intrusion", "Door Intrusion", "event", False),
    ("door_unlocked", "Door Unlocked", "event", False),
    ("live_view", "Live View", "event", True),
    ("key_delivery", "Key Delivery", "event", False),
    ("linked_alarm", "Linked Alarm", "event", False),
    ("linked_motion", "Linked Motion", "event", True),
    ("alexa_greetings", "Alexa Greetings", "event", False),
    ("quick_replies", "Quick Replies", "event", False),
    ("package", "Package Detected", "event", True),
    ("critical", "Critical Event", "event", False),
    ("favorite", "Favorited", "tag", True),
    ("person", "Person Detected", "tag", True),
    ("persistent_visitor", "Persistent Visitor", "tag", False),
    ("third_party_reviewed", "Third-Party App Reviewed", "tag", False),
    ("guard_reviewed", "Guard Reviewed", "tag", False),
    ("guard_intervened", "Guard Intervened", "tag", False),
    ("important", "Important", "tag", False),
    ("static_image", "Static Image", "tag", False),
    ("vehicle", "Vehicle Detected", "tag", True),
]

_VISIBLE_FILTERS = {key: label for key, label, _section, visible in _ALL_FILTERS if visible}

_HISTORY_PAGE_LIMIT = 50
_HISTORY_DEVICE_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})
_MIN_UTC_DATETIME = datetime.min.replace(tzinfo=UTC)

_BACKEND_KIND_FILTERS = {
    "answered_ring": "ding",
    "missed_ring": "ding",
    "motion": "motion",
    "package": "motion",
    "person": "motion",
    "vehicle": "motion",
    "live_view": "on_demand",
    "linked_motion": "on_demand_link",
}


def _normalized_event_datetime(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        return value.astimezone(UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _event_sort_key(event: dict) -> datetime:
    return _normalized_event_datetime(event.get("created_at")) or _MIN_UTC_DATETIME


def _event_datetime_label(dt: object) -> str:
    dt = _normalized_event_datetime(dt)
    if dt is None:
        return ""
    local_dt = dt.astimezone()
    return local_dt.strftime("%A, %B %d, %I:%M %p").replace(" 0", " ").replace(", 0", ", ")


def _event_list_time_parts(dt: object) -> tuple[str, str]:
    dt = _normalized_event_datetime(dt)
    if dt is None:
        return "", ""
    local_dt = dt.astimezone()
    today = datetime.now().astimezone().date()
    event_date = local_dt.date()
    if event_date == today:
        date_label = "Today"
    elif event_date == today - timedelta(days=1):
        date_label = "Yesterday"
    else:
        date_label = local_dt.strftime("%a, %b %d").replace(" 0", " ")
    return date_label, _time_label(local_dt)


def _time_label(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _set_activity_image(image: Gtk.Image, event: dict | None) -> None:
    try:
        texture = Gdk.Texture.new_from_filename(str(activity_icons.activity_icon_path(event)))
    except GLib.Error as exc:
        _log.debug("Failed to load activity icon: %s", exc)
        return
    image.set_from_paintable(texture)


def _filter_icon_event(key: str) -> dict:
    return {
        "answered_ring": {"kind": "ding", "answered": True},
        "missed_ring": {"kind": "ding", "answered": False},
        "motion": {"kind": "motion"},
        "live_view": {"kind": "on_demand"},
        "linked_motion": {"kind": "on_demand_link"},
        "package": {"kind": "motion", "cv_properties": {"detection_type": "package_delivery"}},
        "favorite": {"kind": "favorite", "_is_local_favorite": True},
        "person": {"kind": "motion", "cv_properties": {"detection_type": "human"}},
        "vehicle": {"kind": "motion", "cv_properties": {"detection_type": "vehicle"}},
    }.get(key, {"kind": "motion"})


def _devices_with_history_events(devices: list, events: list[dict]) -> list:
    event_devices = {
        id(event.get("_device")) for event in events if event.get("_device") is not None
    }
    return [device for device in devices if id(device) in event_devices]


def _history_filter_devices(devices: list) -> list:
    """Return devices that belong in the camera event-history selector."""
    camera_devices = [
        device
        for device in devices
        if (getattr(device, "family", None) or "other") in _HISTORY_DEVICE_FAMILIES
    ]
    if camera_devices:
        return camera_devices
    return devices


def _event_matches_device(event: dict, device) -> bool:
    event_device = event.get("_device")
    if event_device is not None:
        return event_device is device

    event_device_id = event.get("device_id")
    device_id = getattr(device, "id", None)
    if event_device_id is None or device_id is None:
        return False
    return str(event_device_id) == str(device_id)


def _merge_favorite_events(remote_events: list[dict], local_events: list[dict]) -> list[dict]:
    """Merge favorites once, preferring an archived local copy when available."""
    merged: list[dict] = []
    positions: dict[str, int] = {}
    for event in remote_events:
        favorite_id = favorites.favorite_id_for_event(event)
        if favorite_id is None:
            merged.append(event)
            continue
        if favorite_id in positions:
            continue
        positions[favorite_id] = len(merged)
        merged.append(event)

    for event in local_events:
        favorite_id = event.get("_favorite_id") or favorites.favorite_id_for_event(event)
        if favorite_id is None:
            merged.append(event)
            continue
        key = str(favorite_id)
        index = positions.get(key)
        if index is None:
            positions[key] = len(merged)
            merged.append(event)
        else:
            merged[index] = event
    return merged


# ---------------------------------------------------------------------------
# GStreamer-based video player widget
# ---------------------------------------------------------------------------


class _VideoPlayer(Gtk.Box):
    """Simple GStreamer playbin player with gtk4paintablesink."""

    def __init__(
        self,
        *,
        on_previous=None,
        on_next=None,
        on_finished=None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_previous = on_previous
        self._on_next = on_next
        self._on_finished = on_finished
        self._duration_ns: int = -1
        self._poll_id: int | None = None
        self._seeking = False
        self._muted = False
        self._last_volume = 1.0
        self._fullscreen_window: Gtk.Window | None = None
        self._zoom = 1.0
        self._drag_start_x = 0.0
        self._drag_start_y = 0.0
        # Initialise to None so _build_ui() can safely check even if
        # _build_pipeline() returns early due to a missing GStreamer element.
        self._player: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._bus_handlers: list[int] = []
        self._paintable = None
        self._build_pipeline()
        self._build_ui()

    def _build_pipeline(self) -> None:
        self._player = Gst.ElementFactory.make("playbin", "player")
        if self._player is None:
            _log.warning("playbin not available")
            return

        # Connect gtk4paintablesink directly as the video sink.  Avoid wrapping
        # it in a videorate bin — the hard 30 fps cap caused playbin to stall on
        # recordings whose timestamps don't align with the pipeline clock,
        # resulting in the first frame being displayed but never advanced.
        # playbin's own internal queue and decodebin handle frame pacing.
        video_sink = Gst.ElementFactory.make("gtk4paintablesink", "vsink")
        if video_sink is None:
            _log.warning("gtk4paintablesink not available")
            return

        video_sink.set_property("sync", True)
        self._paintable = video_sink.get_property("paintable")
        self._player.set_property("video-sink", video_sink)

        self._bus = self._player.get_bus()
        self._bus.add_signal_watch()
        self._bus_handlers.extend(
            [
                self._bus.connect("message::eos", self._on_eos),
                self._bus.connect("message::error", self._on_bus_error),
                self._bus.connect("message::duration-changed", self._on_duration_changed),
            ]
        )

    def _build_ui(self) -> None:
        overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self.append(overlay)

        if self._paintable is not None:
            self._video_view = ZoomPaintableView()
            self._video_view.set_paintable(self._paintable)

            drag = Gtk.GestureDrag()
            drag.connect("drag-begin", self._on_drag_begin)
            drag.connect("drag-update", self._on_drag_update)
            self._video_view.add_controller(drag)

            scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            scroll.connect("scroll", self._on_scroll)
            self._video_view.add_controller(scroll)

            video_widget = self._video_view
        else:
            self._video_view = None
            video_widget = Gtk.Label(label="Video unavailable")

        overlay.set_child(video_widget)

        self._placeholder = Gtk.Label(
            label="Select an event to begin playback",
            css_classes=["dim-label", "title-4"],
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        overlay.add_overlay(self._placeholder)

        # Playback progress.
        progress_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=6,
            margin_start=12,
            margin_end=12,
        )
        self.append(progress_row)

        self._scrubber = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.001)
        self._scrubber.set_draw_value(False)
        self._scrubber.set_hexpand(True)
        # GTK4: use GestureClick instead of the removed button-press/release-event signals.
        press_gesture = Gtk.GestureClick.new()
        press_gesture.connect("pressed", lambda *_: setattr(self, "_seeking", True))
        press_gesture.connect("released", lambda *_: self._on_scrubber_released())
        self._scrubber.add_controller(press_gesture)
        self._scrubber.connect("change-value", self._on_scrubber_changed)
        progress_row.append(self._scrubber)

        self._time_label = Gtk.Label(
            label="0:00 / --:--",
            css_classes=["caption", "dim-label"],
            xalign=1,
            width_request=88,
        )
        progress_row.append(self._time_label)

        # Playback controls.
        controls_row = Gtk.CenterBox(
            orientation=Gtk.Orientation.HORIZONTAL,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self.append(controls_row)

        zoom_box = Gtk.Box(css_classes=["linked"], spacing=0, halign=Gtk.Align.START)
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
        controls_row.set_start_widget(zoom_box)

        # Centred playback buttons.
        ctrl_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.CENTER,
            hexpand=True,
        )
        controls_row.set_center_widget(ctrl_box)

        def _btn(icon, tip, cb):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", cb)
            ctrl_box.append(b)
            return b

        _btn("media-skip-backward-symbolic", "Previous event", self._on_previous_clicked)
        self._play_btn = _btn("media-playback-start-symbolic", "Play / Pause", self._on_play_pause)
        _btn("media-skip-forward-symbolic", "Next event", self._on_next_clicked)

        # Volume control, right-aligned.
        vol_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.END,
        )
        self._mute_btn = Gtk.Button(
            icon_name="audio-volume-high-symbolic",
            tooltip_text="Mute",
            css_classes=["flat"],
        )
        self._mute_btn.connect("clicked", self._on_mute_clicked)
        vol_box.append(self._mute_btn)
        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self._vol_scale.set_value(1.0)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.set_size_request(80, -1)
        self._vol_scale.connect("value-changed", self._on_volume_changed)
        vol_box.append(self._vol_scale)

        self._fullscreen_btn = Gtk.Button(
            icon_name="view-fullscreen-symbolic",
            tooltip_text="Fullscreen",
            css_classes=["flat"],
        )
        self._fullscreen_btn.connect("clicked", self._on_fullscreen_clicked)
        vol_box.append(self._fullscreen_btn)
        controls_row.set_end_widget(vol_box)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_url(self, url: str) -> None:
        """Load and begin playing *url*."""
        self.load_uri(url)

    def load_file(self, path: Path) -> None:
        """Load and begin playing a local media file."""
        self.load_uri(path.resolve().as_uri())

    def load_uri(self, uri: str) -> None:
        """Load and begin playing *uri*."""
        self._stop_poll()
        self._duration_ns = -1
        self._scrubber.set_value(0)
        self._update_time_label(0)

        if self._player is None:
            return

        self._player.set_state(Gst.State.NULL)
        self._player.set_property("uri", uri)
        self._player.set_state(Gst.State.PLAYING)
        self._play_btn.set_icon_name("media-playback-pause-symbolic")
        self._placeholder.set_visible(False)
        # Poll position every 500 ms to drive the scrubber during playback.
        self._start_poll()

    def stop(self) -> None:
        self._stop_poll()
        if self._player is not None:
            self._player.set_state(Gst.State.NULL)
        self._play_btn.set_icon_name("media-playback-start-symbolic")
        self._scrubber.set_value(0)
        self._update_time_label(0)

    def deactivate(self) -> None:
        """Stop playback and close any detached fullscreen presentation."""
        self.stop()
        fullscreen_window = self._fullscreen_window
        if fullscreen_window is not None:
            self._fullscreen_window = None
            fullscreen_window.close()
            self.set_fullscreened(False)

    def do_unroot(self) -> None:
        self.deactivate()
        if self._video_view is not None:
            self._video_view.set_paintable(None)
        self._detach_bus_watch()
        Gtk.Box.do_unroot(self)

    def _detach_bus_watch(self) -> None:
        if self._bus is None:
            self._bus_handlers.clear()
            return
        if not self._bus_handlers:
            return
        for handler_id in self._bus_handlers:
            with suppress(TypeError, ValueError):
                self._bus.disconnect(handler_id)
        self._bus_handlers.clear()
        with suppress(Exception):
            self._bus.remove_signal_watch()

    def set_fullscreened(self, fullscreened: bool) -> None:
        if fullscreened:
            self._fullscreen_btn.set_icon_name("view-restore-symbolic")
            self._fullscreen_btn.set_tooltip_text("Exit fullscreen")
        else:
            self._fullscreen_btn.set_icon_name("view-fullscreen-symbolic")
            self._fullscreen_btn.set_tooltip_text("Fullscreen")

    def get_current_frame_png(self) -> bytes | None:
        """Grab the current video frame as PNG bytes via GStreamer sample."""
        if self._player is None:
            return None
        try:
            sample = self._player.emit("convert-sample", Gst.Caps.from_string("image/png"))
            if sample is None:
                return None
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return None
            data = bytes(info.data)
            buf.unmap(info)
            if self._video_view is not None:
                return self._video_view.zoomed_frame_png(data)
            return data
        except Exception as exc:
            _log.debug("Frame capture failed: %s", exc)
            return None

    def _set_zoom(self, value: float) -> None:
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, round(value, 1)))
        self._zoom_label.set_label(f"{round(self._zoom * 100)}%")
        if self._video_view is not None:
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
        if self._video_view is None:
            return
        self._drag_start_x, self._drag_start_y = self._video_view.pan_origin()

    def _on_drag_update(self, _gesture, offset_x: float, offset_y: float) -> None:
        if self._video_view is None or self._zoom <= 1.0:
            return
        self._video_view.set_pan(self._drag_start_x + offset_x, self._drag_start_y + offset_y)

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        value = scale.get_value()
        if value > 0:
            self._last_volume = value
            self._muted = False
        if self._player is not None:
            # playbin exposes a native `volume` property (0.0 = muted, 1.0 = 100%).
            self._player.set_property("volume", value)
            self._player.set_property("mute", self._muted or value <= 0)
        self._update_mute_button()

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------

    def _on_play_pause(self, *_) -> None:
        if self._player is None:
            return
        _, state, _ = self._player.get_state(0)
        if state == Gst.State.PLAYING:
            self._player.set_state(Gst.State.PAUSED)
            self._play_btn.set_icon_name("media-playback-start-symbolic")
            self._stop_poll()
        else:
            self._player.set_state(Gst.State.PLAYING)
            self._play_btn.set_icon_name("media-playback-pause-symbolic")
            self._start_poll()

    def _on_previous_clicked(self, *_) -> None:
        if self._on_previous is not None:
            self._on_previous()

    def _on_next_clicked(self, *_) -> None:
        if self._on_next is not None:
            self._on_next()

    def _on_fullscreen_clicked(self, *_) -> None:
        if self._fullscreen_window is not None:
            self._fullscreen_window.close()
            return
        if self._paintable is None:
            return

        root = self.get_root()
        window = Gtk.Window(title="Halo Video", decorated=False)
        if isinstance(root, Gtk.Window):
            window.set_transient_for(root)
            if app := root.get_application():
                window.set_application(app)

        picture = Gtk.Picture(
            paintable=self._paintable,
            content_fit=Gtk.ContentFit.CONTAIN,
            hexpand=True,
            vexpand=True,
        )
        window.set_child(picture)
        window.connect("close-request", self._on_fullscreen_close)

        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self._on_fullscreen_key_pressed)
        window.add_controller(key_controller)

        self._fullscreen_window = window
        self.set_fullscreened(True)
        window.fullscreen()
        window.present()

    def _on_fullscreen_close(self, *_):
        self._fullscreen_window = None
        self.set_fullscreened(False)
        return False

    def _on_fullscreen_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape and self._fullscreen_window is not None:
            self._fullscreen_window.close()
            return True
        return False

    def _on_mute_clicked(self, *_) -> None:
        self._muted = not self._muted
        if not self._muted and self._vol_scale.get_value() <= 0:
            self._vol_scale.set_value(self._last_volume or 1.0)
        if self._player is not None:
            self._player.set_property("mute", self._muted)
            self._player.set_property("volume", self._vol_scale.get_value())
        self._update_mute_button()

    def _update_mute_button(self) -> None:
        muted = self._muted or self._vol_scale.get_value() <= 0
        self._mute_btn.set_icon_name(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        )
        self._mute_btn.set_tooltip_text("Unmute" if muted else "Mute")

    def _seek_to_ns(self, ns: int) -> None:
        if self._player is not None:
            self._player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, ns)

    def _get_position_ns(self) -> int:
        if self._player is None:
            return 0
        ok, pos = self._player.query_position(Gst.Format.TIME)
        return pos if ok else 0

    def _on_scrubber_changed(self, scale, scroll, value) -> bool:
        return False

    def _on_scrubber_released(self) -> None:
        self._seeking = False
        if self._duration_ns > 0:
            frac = self._scrubber.get_value()
            self._seek_to_ns(int(frac * self._duration_ns))

    # ------------------------------------------------------------------
    # GStreamer bus messages
    # ------------------------------------------------------------------

    def _on_eos(self, *_) -> None:
        GLib.idle_add(self._handle_eos)

    def _handle_eos(self) -> bool:
        self._stop_poll()
        self._scrubber.set_value(1.0)
        self._update_time_label(self._duration_ns if self._duration_ns > 0 else 0)
        self._play_btn.set_icon_name("media-playback-start-symbolic")
        if self._on_finished is not None:
            self._on_finished()
        return GLib.SOURCE_REMOVE

    def _on_bus_error(self, bus, msg) -> None:
        err, _ = msg.parse_error()
        _log.warning("GStreamer player error: %s", err)

    def _on_duration_changed(self, *_) -> None:
        if self._player is None:
            return
        ok, dur = self._player.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            self._duration_ns = dur
            self._update_time_label(self._get_position_ns())

    # ------------------------------------------------------------------
    # Position polling — drives the scrubber during playback
    # ------------------------------------------------------------------

    def _start_poll(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add(500, self._poll_position)

    def _stop_poll(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def _poll_position(self) -> bool:
        """Query pipeline position every 500 ms and advance the scrubber."""
        if self._player is None:
            return GLib.SOURCE_REMOVE
        if self._seeking:
            return GLib.SOURCE_CONTINUE

        # Try to learn duration if it wasn't available at load time
        # (common for HTTP streams where Content-Length is returned after buffering).
        if self._duration_ns <= 0:
            ok, dur = self._player.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                self._duration_ns = dur

        ok, pos = self._player.query_position(Gst.Format.TIME)
        if ok and self._duration_ns > 0:
            self._scrubber.set_value(pos / self._duration_ns)
            self._update_time_label(pos)

        return GLib.SOURCE_CONTINUE

    def _update_time_label(self, position_ns: int) -> None:
        total = self._duration_ns if self._duration_ns > 0 else None
        self._time_label.set_label(f"{self._format_time(position_ns)} / {self._format_time(total)}")

    @staticmethod
    def _format_time(value_ns: int | None) -> str:
        if value_ns is None or value_ns < 0:
            return "--:--"
        total_seconds = max(0, round(value_ns / Gst.SECOND))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


# ---------------------------------------------------------------------------
# History page
# ---------------------------------------------------------------------------


class HistoryPage(Gtk.Box):
    """Adw.NavigationSplitView — event list on left, player on right."""

    def __init__(self, on_title_change=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_title_change = on_title_change
        self._events: list = []
        self._devices: list = []
        self._device_options: list = []
        self._selected_device_id: int | None = None  # None = all cameras
        self._selected_device_index = 0
        self._pending_event_id: int | None = None
        self._current_event = None
        self._active_filter_keys: set[str] = set()
        self._staged_filter_keys: set[str] = set()
        self._filter_buttons: dict[str, Gtk.CheckButton] = {}
        self._history_seen_keys: set[tuple[int | None, int]] = set()
        self._history_older_than_by_device: dict[int, int] = {}
        self._history_exhausted_device_ids: set[int] = set()
        self._history_loading = False
        self._history_exhausted = False
        self._history_request_generation = 0
        self._playback_request_generation = 0
        self._build_ui()

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        """Collapse the NavigationSplitView when the page is narrower than 700 px."""
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        should_collapse = width < 700
        if self._nav_split.get_collapsed() != should_collapse:
            self._nav_split.set_collapsed(should_collapse)

    def do_unroot(self) -> None:
        self._history_request_generation += 1
        self._playback_request_generation += 1
        self._history_loading = False
        self._player.stop()
        Gtk.Box.do_unroot(self)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._nav_split = Adw.NavigationSplitView(
            hexpand=True,
            vexpand=True,
            sidebar_width_fraction=0.3,
        )
        self.append(self._nav_split)

        # --- Sidebar navigation page ---
        self._sidebar_page = Adw.NavigationPage(title="Event History")
        self._nav_split.set_sidebar(self._sidebar_page)

        self._sidebar_stack = Gtk.Stack(hexpand=True, vexpand=True)
        self._sidebar_page.set_child(self._sidebar_stack)

        event_sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._sidebar_stack.add_named(event_sidebar, "events")

        controls_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=8,
            margin_start=8,
            margin_end=8,
            margin_bottom=8,
        )
        event_sidebar.append(controls_row)

        self._device_filter_popover = Gtk.Popover()
        self._device_filter_popover.set_size_request(280, -1)
        self._device_filter_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._device_filter_list.connect("row-activated", self._on_device_filter_row_activated)
        self._device_filter_list.set_margin_top(6)
        self._device_filter_list.set_margin_bottom(6)
        self._device_filter_popover.set_child(self._device_filter_list)

        self._device_filter_label = Gtk.Label(
            label="All Cameras",
            xalign=0,
            hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
        )
        device_filter_child = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        device_filter_child.append(Gtk.Image(icon_name="camera-video-symbolic"))
        device_filter_child.append(self._device_filter_label)
        device_filter_child.append(Gtk.Image(icon_name="pan-down-symbolic"))

        self._device_filter_button = Gtk.MenuButton(
            child=device_filter_child,
            popover=self._device_filter_popover,
            hexpand=True,
        )
        controls_row.append(self._device_filter_button)

        filter_btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        filter_btn_box.append(Gtk.Image(icon_name="view-filter-symbolic"))
        filter_btn_box.append(Gtk.Label(label="Filter", css_classes=["caption"]))
        self._filter_panel_button = Gtk.Button(
            child=filter_btn_box,
            tooltip_text="Filter events",
            css_classes=["flat"],
        )
        self._filter_panel_button.connect("clicked", self._show_filter_panel)
        controls_row.append(self._filter_panel_button)

        # Event list.
        scroll = Gtk.ScrolledWindow(vexpand=True)
        event_sidebar.append(scroll)

        self._event_list = Gtk.ListBox(
            css_classes=["navigation-sidebar"],
            selection_mode=Gtk.SelectionMode.SINGLE,
            vexpand=True,
        )
        self._event_list.connect("row-activated", self._on_event_selected)
        scroll.set_child(self._event_list)

        self._list_placeholder = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="No events",
            description="No recorded events found.",
        )
        self._event_list.set_placeholder(self._list_placeholder)

        filter_sidebar = self._build_filter_sidebar()
        self._sidebar_stack.add_named(filter_sidebar, "filters")
        self._sidebar_stack.set_visible_child_name("events")

        # --- Content navigation page ---
        content_page = Adw.NavigationPage(title="Event Playback")
        self._nav_split.set_content(content_page)

        content_toolbar = Adw.ToolbarView()

        content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        content_toolbar.set_content(content_box)
        content_page.set_child(content_toolbar)

        # Player widget.
        self._player = _VideoPlayer(
            on_previous=self._on_previous_event,
            on_next=self._on_next_event,
            on_finished=self._on_playback_finished,
        )
        content_box.append(self._player)

        self._details_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_start=16,
            margin_end=16,
            margin_bottom=14,
            valign=Gtk.Align.CENTER,
        )
        content_box.append(self._details_row)

        self._event_icon = Gtk.Image(pixel_size=38, valign=Gtk.Align.CENTER)
        self._details_row.append(self._event_icon)

        event_text = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            valign=Gtk.Align.CENTER,
        )
        self._details_row.append(event_text)

        self._event_type_label = Gtk.Label(
            xalign=0,
            css_classes=["heading"],
            ellipsize=Pango.EllipsizeMode.END,
        )
        event_text.append(self._event_type_label)

        self._event_detail_label = Gtk.Label(
            xalign=0,
            css_classes=["dim-label", "caption"],
            ellipsize=Pango.EllipsizeMode.END,
        )
        event_text.append(self._event_detail_label)

        actions_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        self._details_row.append(actions_box)

        def _action_btn(icon, tip, cb):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", cb)
            return b

        actions_box.append(
            _action_btn("camera-photo-symbolic", "Save screenshot", self._on_screenshot)
        )
        self._favorite_btn = _action_btn("non-starred-symbolic", "Favorite", self._on_favorite)
        actions_box.append(self._favorite_btn)
        actions_box.append(_action_btn("share-symbolic", "Copy recording URL", self._on_share))
        actions_box.append(
            _action_btn(
                "document-save-symbolic", "Download to ~/Videos/halo-gtk/", self._on_download
            )
        )
        actions_box.append(_action_btn("edit-delete-symbolic", "Delete event", self._on_delete))
        self._clear_event_details()

    def _build_filter_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=8,
            margin_start=12,
            margin_end=12,
            margin_bottom=12,
            hexpand=True,
            vexpand=True,
        )

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        back_btn.connect("clicked", self._hide_filter_panel)
        title_row.append(back_btn)
        title_row.append(
            Gtk.Label(
                label="Filter Events",
                xalign=0,
                hexpand=True,
                css_classes=["title-3"],
            )
        )
        sidebar.append(title_row)
        sidebar.append(Gtk.Separator())

        clear_btn = Gtk.Button(label="Clear filters", halign=Gtk.Align.END, css_classes=["flat"])
        clear_btn.connect("clicked", self._clear_filters)
        sidebar.append(clear_btn)

        apply_btn = Gtk.Button(label="Filter Events", css_classes=["suggested-action"])
        apply_btn.connect("clicked", self._apply_filters)
        sidebar.append(apply_btn)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        sidebar.append(scroll)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        scroll.set_child(content)

        event_filters = [item for item in _ALL_FILTERS if item[2] == "event" and item[3]]
        tag_filters = [item for item in _ALL_FILTERS if item[2] == "tag" and item[3]]
        content.append(self._build_filter_section("Event Type", event_filters))
        content.append(self._build_filter_section("Event Tags", tag_filters))
        return sidebar

    def _build_filter_section(
        self,
        title: str,
        filters: list[tuple[str, str, str, bool]],
    ) -> Gtk.Box:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.append(Gtk.Label(label=title, xalign=0, hexpand=True, css_classes=["heading"]))
        select_all = Gtk.Button(label="Select All", css_classes=["flat"])
        select_all.connect("clicked", self._select_all_filters, [item[0] for item in filters])
        header.append(select_all)
        section.append(header)

        for key, label, _section, _visible in filters:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_bottom(2)
            image = Gtk.Image(pixel_size=24)
            _set_activity_image(image, _filter_icon_event(key))
            row.append(image)

            check = Gtk.CheckButton(label=label, hexpand=True)
            check.set_active(key in self._staged_filter_keys)
            check.connect("toggled", self._on_filter_toggled, key)
            self._filter_buttons[key] = check
            row.append(check)
            section.append(row)

        return section

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_page_hidden(self) -> None:
        """Stop playback and reject delayed history work after page exit."""
        self._history_request_generation += 1
        self._playback_request_generation += 1
        self._history_loading = False
        self._pending_event_id = None
        self._player.deactivate()

    def refresh(
        self,
        filter_device_id: int | None = None,
        select_event_id: int | None = None,
    ) -> None:
        """Load event history.  Optionally pre-select *filter_device_id*."""
        client = get_client()
        if client is None or not client.is_authenticated:
            self._show_signed_out_state()
            return

        if filter_device_id is not None:
            self._selected_device_id = filter_device_id
        if select_event_id is not None:
            self._pending_event_id = select_event_id

        self._list_placeholder.set_title("Loading…")
        self._list_placeholder.set_description("")
        self._clear_event_list()
        self._reset_history_pagination()

        self._start_history_fetch(append=False)

    def _show_signed_out_state(self) -> None:
        self._history_request_generation += 1
        self._playback_request_generation += 1
        self._history_loading = False
        self._devices = []
        self._device_options = []
        self._events = []
        self._selected_device_id = None
        self._pending_event_id = None
        self._current_event = None
        self._player.stop()
        self._clear_event_details()
        self._nav_split.set_show_content(False)
        self._clear_event_list()
        self._selected_device_index = 0
        self._active_filter_keys.clear()
        self._staged_filter_keys.clear()
        self._rebuild_device_filter()
        self._sync_filter_buttons()

        self._list_placeholder.set_title("Not signed in")
        self._list_placeholder.set_description(
            "Sign in to your Ring account to view event history."
        )
        if self._on_title_change is not None:
            self._on_title_change(None)

    def show_event_by_id(self, device_id: int, event_id: int) -> None:
        """Load history for a device and play the requested event."""
        self.refresh(filter_device_id=device_id, select_event_id=event_id)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _start_history_fetch(self, *, append: bool) -> None:
        if append:
            if self._history_loading or self._history_exhausted:
                return
            generation = self._history_request_generation
        else:
            self._history_request_generation += 1
            generation = self._history_request_generation

        device_ids = self._selected_history_device_ids()
        if append:
            candidate_ids = (
                device_ids
                if device_ids is not None
                else {int(device.id) for device in self._devices}
            )
            device_ids = set(candidate_ids) - set(self._history_exhausted_device_ids)
            if not device_ids:
                GLib.idle_add(self._finish_history_exhausted, generation)
                return

        self._history_loading = True
        backend_kind = self._backend_history_kind()
        older_than = dict(self._history_older_than_by_device) if append else None
        threading.Thread(
            target=self._fetch_history,
            kwargs={
                "append": append,
                "generation": generation,
                "device_ids": device_ids,
                "backend_kind": backend_kind,
                "older_than": older_than,
            },
            daemon=True,
        ).start()

    def _fetch_history(
        self,
        *,
        append: bool,
        generation: int,
        device_ids: set[int] | None,
        backend_kind: str | None,
        older_than: dict[int, int] | None,
    ) -> None:
        client = get_client()
        if client is None:
            GLib.idle_add(self._show_fetch_error, "Not signed in", generation)
            return
        try:
            devices, events = client.event_history(
                limit=_HISTORY_PAGE_LIMIT,
                kind=backend_kind,
                older_than=older_than,
                device_ids=device_ids,
                enforce_limit=backend_kind is not None,
            )
            events.sort(key=_event_sort_key, reverse=True)
            GLib.idle_add(
                self._populate_events,
                devices,
                events,
                append,
                device_ids,
                generation,
            )
        except Exception as exc:
            GLib.idle_add(self._show_fetch_error, str(exc), generation)

    def _populate_events(
        self,
        devices: list,
        events: list,
        append: bool,
        requested_device_ids: set[int] | None,
        generation: int,
    ) -> bool:
        if generation != self._history_request_generation:
            return GLib.SOURCE_REMOVE
        self._history_loading = False
        if not append:
            self._events = []
            self._history_seen_keys.clear()
            self._history_older_than_by_device.clear()
            self._history_exhausted_device_ids.clear()
            self._history_exhausted = False

        new_events = []
        seen_this_batch: set[int] = set()
        for event in events:
            device = event.get("_device")
            device_id = int(device.id) if device is not None else None
            event_id = event.get("id")
            if event_id is None:
                continue
            if device_id is not None:
                seen_this_batch.add(device_id)
            key = (device_id, int(event_id))
            if key in self._history_seen_keys:
                continue
            self._history_seen_keys.add(key)
            new_events.append(event)
            if device_id is not None:
                self._history_older_than_by_device[device_id] = int(event_id)

        if requested_device_ids is not None:
            self._history_exhausted_device_ids.update(requested_device_ids - seen_this_batch)

        if (append and not new_events) or not events:
            self._history_exhausted = True

        self._events.extend(new_events)
        self._events.sort(key=_event_sort_key, reverse=True)
        self._devices = _devices_with_history_events(devices, self._events)
        if (
            not append
            and (
                self._selected_device_index == 0
                or self._selected_device_id is not None
                or not self._device_options
            )
            or not self._device_options
        ):
            self._device_options = _history_filter_devices(devices)
        self._clear_event_list()
        self._rebuild_device_filter()

        # If we have a pre-selected device, find its index.
        if self._selected_device_id is not None:
            for i, dev in enumerate(self._device_options):
                if dev.id == self._selected_device_id:
                    self._selected_device_index = i + 1
                    self._update_device_filter_label()
                    break
            self._selected_device_id = None

        self._fill_event_rows()
        return GLib.SOURCE_REMOVE

    def _finish_history_exhausted(self, generation: int | None = None) -> bool:
        if generation is not None and generation != self._history_request_generation:
            return GLib.SOURCE_REMOVE
        self._history_loading = False
        self._history_exhausted = True
        self._fill_event_rows()
        return GLib.SOURCE_REMOVE

    def _fill_event_rows(self) -> None:
        self._clear_event_list()
        visible_events = self._visible_events()
        if not visible_events:
            if self._selected_device_index == len(self._device_options) + 1:
                self._list_placeholder.set_title("Alarm and Sensors")
                self._list_placeholder.set_description("Ring Alarm support is not wired yet.")
            elif self._active_filter_keys:
                self._list_placeholder.set_title("No matching events")
                self._list_placeholder.set_description("No events match the current filters.")
            else:
                self._list_placeholder.set_title("No events")
                self._list_placeholder.set_description("No recorded events found.")
            self._player.stop()
            self._current_event = None
            self._clear_event_details()
            self._update_favorite_button()
            if (
                not self._history_exhausted
                and self._selected_device_index != len(self._device_options) + 1
            ):
                self._event_list.append(self._make_load_older_row())
            return

        pending_row = None
        for ev in visible_events:
            row = self._make_event_row(ev)
            self._event_list.append(row)
            if self._pending_event_id is not None and int(ev.get("id", -1)) == int(
                self._pending_event_id
            ):
                pending_row = row

        if not self._history_exhausted:
            self._event_list.append(self._make_load_older_row())

        if pending_row is not None:
            self._pending_event_id = None
            self._event_list.select_row(pending_row)
            self._on_event_selected(self._event_list, pending_row)

    def _make_event_row(self, event: dict) -> Adw.ActionRow:
        device = event.get("_device")
        created_at = event.get("created_at")
        metadata_missing = bool(event.get("_metadata_missing"))

        if metadata_missing:
            title = "Missing Metadata"
            subtitle = event.get("_missing_metadata_filename") or "Unknown local clip"
        else:
            title = (
                device_names.display_name(device)
                if device is not None
                else device_names.display_name_for_id(
                    event.get("device_id"),
                    event.get("camera_name", "Unknown camera"),
                )
            )
            subtitle = activity_icons.activity_label(event)

        row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
        row._event_data = event  # type: ignore[attr-defined]

        icon = Gtk.Image()
        icon.set_pixel_size(30)
        _set_activity_image(icon, event)
        row.add_prefix(icon)

        if created_at is not None:
            date_text, time_text = _event_list_time_parts(created_at)
            ts_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                valign=Gtk.Align.CENTER,
                halign=Gtk.Align.END,
                spacing=3,
            )
            ts_box.append(
                Gtk.Label(
                    label=date_text,
                    xalign=1,
                    css_classes=["dim-label", "caption"],
                )
            )
            ts_box.append(
                Gtk.Label(
                    label=time_text,
                    xalign=1,
                    css_classes=["dim-label", "caption"],
                )
            )
            row.add_suffix(ts_box)

        return row

    def _make_load_older_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        button = Gtk.Button(
            label="Load Older Events",
            halign=Gtk.Align.CENTER,
            margin_top=10,
            margin_bottom=10,
        )
        button.set_sensitive(not self._history_loading)
        button.connect("clicked", self._on_load_older_clicked)
        row.set_child(button)
        return row

    def _show_fetch_error(self, message: str, generation: int | None = None) -> bool:
        if generation is not None and generation != self._history_request_generation:
            return GLib.SOURCE_REMOVE
        self._history_loading = False
        self._list_placeholder.set_title("Failed to load events")
        self._list_placeholder.set_description(message)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Event selection → playback
    # ------------------------------------------------------------------

    def _on_event_selected(self, list_box: Gtk.ListBox, row) -> None:
        if row is None:
            return
        event = getattr(row, "_event_data", None)
        if event is None:
            return
        self._current_event = event
        self._update_favorite_button()
        self._update_event_details(event)
        self._nav_split.set_show_content(True)

        if self._on_title_change is not None:
            device = event.get("_device")
            camera_name = (
                device_names.display_name(device)
                if device is not None
                else device_names.display_name_for_id(
                    event.get("device_id"),
                    event.get("camera_name", "Unknown camera"),
                )
            )
            self._on_title_change(camera_name)

        self._playback_request_generation += 1
        generation = self._playback_request_generation
        threading.Thread(
            target=self._load_and_play,
            args=(event, generation),
            daemon=True,
        ).start()

    def _load_and_play(self, event: dict, generation: int) -> None:
        client = get_client()
        local_path = event.get("_local_video_path")
        if local_path:
            GLib.idle_add(self._load_file_if_current, Path(local_path), generation)
            return

        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            url = client.recording_url(device, event_id)
            if url:
                GLib.idle_add(self._load_url_if_current, url, generation)
        except Exception as exc:
            _log.debug("Failed to get recording URL for event %s: %s", event_id, exc)

    def _load_file_if_current(self, path: Path, generation: int) -> bool:
        if generation == self._playback_request_generation:
            self._player.load_file(path)
        return GLib.SOURCE_REMOVE

    def _load_url_if_current(self, url: str, generation: int) -> bool:
        if generation == self._playback_request_generation:
            self._player.load_url(url)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Action callbacks
    # ------------------------------------------------------------------

    def _on_favorite(self, *_) -> None:
        ev = self._current_event
        if ev is None:
            return
        if ev.get("_metadata_missing"):
            _log.debug("Cannot remove local favorite with missing metadata from Favorite button")
            return
        if ev.get("_is_local_favorite") or favorites.is_favorited(ev):
            self._remove_local_favorite(ev)
            return

        thumbnail = self._player.get_current_frame_png()
        threading.Thread(target=self._archive_favorite, args=(ev, thumbnail), daemon=True).start()

    def _archive_favorite(self, event: dict, thumbnail: bytes | None) -> None:
        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            url = client.recording_url(device, event_id)
            if not url:
                return
            favorites.add_favorite(event, url, thumbnail_bytes=thumbnail)
            GLib.idle_add(self._after_favorite_change)
        except Exception as exc:
            _log.debug("Favorite archive failed: %s", exc)

    def _remove_local_favorite(self, event: dict) -> None:
        was_local = bool(event.get("_is_local_favorite"))
        if event.get("_metadata_missing"):
            local_path = event.get("_local_video_path")
            try:
                if local_path:
                    Path(local_path).unlink(missing_ok=True)
            except OSError as exc:
                _log.debug("Failed to remove local favorite file: %s", exc)
                return
            self._current_event = None
            self._player.stop()
            self._clear_event_details()
            self._after_favorite_change()
            return

        if favorites.remove_favorite(event):
            if was_local:
                self._current_event = None
                self._player.stop()
                self._clear_event_details()
            self._after_favorite_change()

    def _after_favorite_change(self) -> bool:
        self._update_favorite_button()
        self._fill_event_rows()
        return GLib.SOURCE_REMOVE

    def _on_share(self, *_) -> None:
        """Copy the recording URL to the clipboard."""
        ev = self._current_event
        if ev is None:
            return
        if local_path := ev.get("_local_video_path"):
            self._do_copy_clipboard(Path(local_path).resolve().as_uri())
            return
        threading.Thread(target=self._copy_url_to_clipboard, args=(ev,), daemon=True).start()

    def _copy_url_to_clipboard(self, event: dict) -> None:
        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            url = client.recording_url(device, event_id)
            if url:
                GLib.idle_add(self._do_copy_clipboard, url)
        except Exception as exc:
            _log.debug("Failed to fetch URL for share: %s", exc)

    def _do_copy_clipboard(self, text: str) -> bool:
        display = self.get_display()
        if display is not None:
            clipboard = display.get_clipboard()
            clipboard.set(text)
        return GLib.SOURCE_REMOVE

    def _on_download(self, *_) -> None:
        ev = self._current_event
        if ev is None:
            return
        if local_path := ev.get("_local_video_path"):
            self._open_folder(Path(local_path).parent)
            return
        threading.Thread(target=self._download_recording, args=(ev,), daemon=True).start()

    def _download_recording(self, event: dict) -> None:
        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            url = client.recording_url(device, event_id)
            if not url:
                return
            dest = media_paths.video_dir(device_names.display_name(device))
            dest.mkdir(parents=True, exist_ok=True)
            kind = re.sub(r"[^A-Za-z0-9._-]+", "-", str(event.get("kind") or "event")).strip("-.")
            fname = dest / (f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{kind or 'event'}.mp4")
            _log.debug("Downloading recording to %s", fname)
            fname = download_https(url, fname, replace_existing=False)
            _log.debug("Download complete: %s", fname)
            if media_paths.should_open_after_save():
                GLib.idle_add(self._open_folder, dest)
        except Exception as exc:
            _log.debug("Download failed: %s", exc)

    def _on_screenshot(self, *_) -> None:
        png = self._player.get_current_frame_png()
        if png is None:
            _log.debug("No frame available for screenshot")
            return
        device = self._current_event.get("_device") if self._current_event is not None else None
        camera_name = device_names.display_name(device) if device is not None else None
        dest = media_paths.snapshot_dir(camera_name)
        fname = media_paths.write_unique_bytes(
            dest,
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
            ".png",
            png,
        )
        _log.debug("Screenshot saved to %s", fname)
        if media_paths.should_open_after_save():
            self._open_folder(dest)

    def _open_folder(self, path: Path) -> bool:
        try:
            media_paths.open_folder(path)
        except Exception as exc:
            _log.debug("Failed to open media folder: %s", exc)
        return GLib.SOURCE_REMOVE

    def _on_delete(self, *_) -> None:
        ev = self._current_event
        if ev is None:
            return
        if ev.get("_is_local_favorite"):
            self._confirm_remove_local_favorite(ev)
            return

        dialog = Adw.AlertDialog(
            heading="Delete this event?",
            body="The recording will be permanently deleted from Ring.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_confirmed, ev)
        dialog.present(self)

    def _confirm_remove_local_favorite(self, event: dict) -> None:
        dialog = Adw.AlertDialog(
            heading="Remove this favorite?",
            body="The archived local clip will be deleted from Halo favorites.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_remove_local_favorite_confirmed, event)
        dialog.present(self)

    def _on_remove_local_favorite_confirmed(self, _dialog, response: str, event: dict) -> None:
        if response == "remove":
            self._remove_local_favorite(event)

    def _on_delete_confirmed(self, dialog, response: str, event: dict) -> None:
        if response != "delete":
            return
        threading.Thread(target=self._do_delete, args=(event,), daemon=True).start()

    def _do_delete(self, event: dict) -> None:
        client = get_client()
        device = event.get("_device")
        event_id = event.get("id")
        if client is None or device is None or event_id is None:
            return
        try:
            client.delete_recording(device, event_id)
            _log.debug("Deleted event %s", event_id)
            GLib.idle_add(self._after_delete, event)
        except Exception as exc:
            _log.debug("Delete failed: %s", exc)

    def _after_delete(self, event: dict) -> bool:
        self._events = [e for e in self._events if e.get("id") != event.get("id")]
        self._current_event = None
        self._player.stop()
        self._clear_event_details()
        self._update_favorite_button()
        self._fill_event_rows()
        return GLib.SOURCE_REMOVE

    def _on_previous_event(self) -> None:
        self._select_adjacent_event(-1)

    def _on_next_event(self) -> None:
        self._select_adjacent_event(1)

    def _on_playback_finished(self) -> None:
        if not _cfg.load().get("event_history_next_auto_play", False):
            return
        self._select_adjacent_event(1)

    def _select_adjacent_event(self, offset: int) -> None:
        row = self._event_list.get_selected_row()
        if row is None:
            return
        target = self._event_list.get_row_at_index(row.get_index() + offset)
        if target is None:
            return
        self._event_list.select_row(target)
        self._on_event_selected(self._event_list, target)

    # ------------------------------------------------------------------
    # Sidebar filters
    # ------------------------------------------------------------------

    def _rebuild_device_filter(self) -> None:
        self._clear_device_filter_rows()
        self._device_filter_list.append(self._make_device_filter_row("All Cameras", 0))
        for index, device in enumerate(self._device_options, start=1):
            self._device_filter_list.append(
                self._make_device_filter_row(device_names.display_name(device), index)
            )
        self._device_filter_list.append(
            self._make_device_filter_row("Alarm, Sensors, and More", len(self._device_options) + 1)
        )
        self._update_device_filter_label()

    def _make_device_filter_row(self, label: str, index: int) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True)
        row._device_filter_index = index  # type: ignore[attr-defined]
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=10,
            margin_end=10,
        )
        box.append(Gtk.Image(icon_name="camera-video-symbolic"))
        box.append(
            Gtk.Label(
                label=label,
                xalign=0,
                hexpand=True,
                ellipsize=Pango.EllipsizeMode.END,
            )
        )
        check = Gtk.Image(icon_name="object-select-symbolic")
        check.set_visible(index == self._selected_device_index)
        box.append(check)
        row.set_child(box)
        row.connect("activate", self._on_device_filter_row_activate)
        return row

    def _on_device_filter_row_activated(self, _list_box, row: Gtk.ListBoxRow) -> None:
        self._activate_device_filter_index(getattr(row, "_device_filter_index", 0))

    def _on_device_filter_row_activate(self, row: Gtk.ListBoxRow) -> None:
        self._activate_device_filter_index(getattr(row, "_device_filter_index", 0))

    def _activate_device_filter_index(self, index: int) -> None:
        if index == self._selected_device_index:
            self._device_filter_popover.popdown()
            return
        self._selected_device_index = index
        self._device_filter_popover.popdown()
        self._update_device_filter_label()
        self._rebuild_device_filter()
        self._reset_current_event()
        if self._selected_device_index == len(self._device_options) + 1:
            self._fill_event_rows()
            return
        self._reload_history()

    def _update_device_filter_label(self) -> None:
        if self._selected_device_index == 0:
            label = "All Cameras"
        elif self._selected_device_index == len(self._device_options) + 1:
            label = "Alarm, Sensors, and More"
        elif 0 <= self._selected_device_index - 1 < len(self._device_options):
            label = device_names.display_name(self._device_options[self._selected_device_index - 1])
        else:
            self._selected_device_index = 0
            label = "All Cameras"
        self._device_filter_label.set_label(label)

    def _clear_device_filter_rows(self) -> None:
        while (row := self._device_filter_list.get_first_child()) is not None:
            self._device_filter_list.remove(row)

    def _show_filter_panel(self, *_):
        self._staged_filter_keys = set(self._active_filter_keys)
        self._sync_filter_buttons()
        self._sidebar_page.set_title("Filter Events")
        self._sidebar_stack.set_visible_child_name("filters")

    def _hide_filter_panel(self, *_):
        self._staged_filter_keys = set(self._active_filter_keys)
        self._sync_filter_buttons()
        self._sidebar_page.set_title("Event History")
        self._sidebar_stack.set_visible_child_name("events")

    def _apply_filters(self, *_):
        self._active_filter_keys = set(self._staged_filter_keys)
        self._reset_current_event()
        self._reload_history()
        self._hide_filter_panel()

    def _clear_filters(self, *_):
        self._staged_filter_keys.clear()
        self._active_filter_keys.clear()
        self._sync_filter_buttons()
        self._reset_current_event()
        self._reload_history()

    def _select_all_filters(self, _button, keys: list[str]) -> None:
        self._staged_filter_keys.update(keys)
        self._sync_filter_buttons()

    def _on_filter_toggled(self, button: Gtk.CheckButton, key: str) -> None:
        if button.get_active():
            self._staged_filter_keys.add(key)
        else:
            self._staged_filter_keys.discard(key)

    def _sync_filter_buttons(self) -> None:
        for key, button in self._filter_buttons.items():
            button.set_active(key in self._staged_filter_keys)

    def _reset_current_event(self) -> None:
        self._playback_request_generation += 1
        self._current_event = None
        self._player.stop()
        self._clear_event_details()
        self._update_favorite_button()

    def _reload_history(self) -> None:
        self._list_placeholder.set_title("Loading…")
        self._list_placeholder.set_description("")
        self._clear_event_list()
        self._reset_history_pagination()
        self._start_history_fetch(append=False)

    def _reset_history_pagination(self) -> None:
        self._history_seen_keys.clear()
        self._history_older_than_by_device.clear()
        self._history_exhausted_device_ids.clear()
        self._history_exhausted = False

    def _on_load_older_clicked(self, *_args) -> None:
        self._start_history_fetch(append=True)

    def _backend_history_kind(self) -> str | None:
        kinds = {
            _BACKEND_KIND_FILTERS[key]
            for key in self._active_filter_keys
            if key in _BACKEND_KIND_FILTERS
        }
        if len(kinds) == 1:
            return next(iter(kinds))
        return None

    def _selected_history_device_ids(self) -> set[int] | None:
        if self._selected_device_index > 0 and self._selected_device_index - 1 < len(
            self._device_options
        ):
            return {int(self._device_options[self._selected_device_index - 1].id)}
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_event_list(self) -> None:
        while (row := self._event_list.get_first_child()) is not None:
            self._event_list.remove(row)

    def _visible_events(self) -> list:
        if self._selected_device_index == len(self._device_options) + 1:
            return []

        filter_device = None
        if self._selected_device_index > 0 and self._selected_device_index - 1 < len(
            self._device_options
        ):
            filter_device = self._device_options[self._selected_device_index - 1]

        events = [
            e
            for e in self._events
            if filter_device is None or _event_matches_device(e, filter_device)
        ]
        if "favorite" in self._active_filter_keys:
            favorite_events = favorites.favorite_events()
            if filter_device is not None:
                favorite_events = [
                    event
                    for event in favorite_events
                    if _event_matches_device(event, filter_device)
                ]
            events = _merge_favorite_events(events, favorite_events)
        if not self._active_filter_keys:
            return events
        return [event for event in events if self._event_matches_active_filters(event)]

    def _event_matches_active_filters(self, event: dict) -> bool:
        return any(self._event_matches_filter(event, key) for key in self._active_filter_keys)

    def _event_matches_filter(self, event: dict, key: str) -> bool:
        activity_key = activity_icons.activity_key(event)
        if key == "favorite":
            return bool(event.get("_is_local_favorite")) or favorites.is_favorited(event)
        if key == "motion":
            return normalized_event_kind(event) == "motion" and activity_key == "motion"
        return activity_key == key

    def _update_event_details(self, event: dict) -> None:
        self._details_row.set_sensitive(True)
        _set_activity_image(self._event_icon, event)
        self._event_type_label.set_label(activity_icons.activity_label(event))

        if event.get("_metadata_missing"):
            detail = event.get("_missing_metadata_filename") or "Unknown local clip"
        else:
            device = event.get("_device")
            camera_name = (
                device_names.display_name(device)
                if device is not None
                else device_names.display_name_for_id(
                    event.get("device_id"),
                    event.get("camera_name", "Unknown camera"),
                )
            )
            when = _event_datetime_label(event.get("created_at"))
            detail = f"{camera_name} • {when}" if camera_name and when else camera_name or when
        self._event_detail_label.set_label(detail)

    def _clear_event_details(self) -> None:
        self._details_row.set_sensitive(False)
        _set_activity_image(self._event_icon, None)
        self._event_type_label.set_label("No Event Selected")
        self._event_detail_label.set_label("Select an event to begin playback")

    def _update_favorite_button(self) -> None:
        event = self._current_event
        if event is None or event.get("_metadata_missing"):
            self._favorite_btn.set_sensitive(False)
            self._favorite_btn.set_icon_name("non-starred-symbolic")
            self._favorite_btn.set_tooltip_text("Favorite")
            return

        self._favorite_btn.set_sensitive(True)
        if event.get("_is_local_favorite") or favorites.is_favorited(event):
            self._favorite_btn.set_icon_name("starred-symbolic")
            self._favorite_btn.set_tooltip_text("Remove from Favorites")
        else:
            self._favorite_btn.set_icon_name("non-starred-symbolic")
            self._favorite_btn.set_tooltip_text("Add to Favorites")
