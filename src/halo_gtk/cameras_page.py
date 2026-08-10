"""Cameras page — deterministic camera grid with size modes and drag-to-reorder."""

from __future__ import annotations

import io
import logging
import threading
import time

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

from halo_gtk import config as _cfg  # noqa: E402
from halo_gtk import device_commands, device_names, live_layouts  # noqa: E402
from halo_gtk.camera_settings import CameraSettingsWindow  # noqa: E402
from halo_gtk.live_sessions import (  # noqa: E402
    LIVE_MONITORING_OWNER,
    StreamLimitExceeded,
    get_live_session_manager,
)
from halo_gtk.log_redaction import redact_text  # noqa: E402
from halo_gtk.network_status import camera_network_status  # noqa: E402
from halo_gtk.power_status import camera_power_status  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402
from halo_gtk.ring_events import normalized_event_kind  # noqa: E402
from halo_gtk.ring_media import supports_ring_camera_intercom  # noqa: E402

_log = logging.getLogger(__name__)

# Families that support snapshot capture.
_SNAPSHOT_FAMILIES = frozenset({"doorbots", "authorized_doorbots", "stickup_cams"})

# Default tile dimensions (16:9) before any snapshot is loaded.
_DEFAULT_NATIVE_W = 320
_DEFAULT_NATIVE_H = 180
_FIXED_TILE_W = 16.0
_FIXED_TILE_H = 9.0
_PREVIEW_BLUR_REFERENCE_SHORT_EDGE = 360
_PREVIEW_BLUR_REFERENCE_RADIUS = 9
_PREVIEW_BLUR_PASSES = 3
_PREVIEW_BLUR_BRIGHTNESS = 0.75
_MOTION_OFF_TEXT = "Motion Detection Off"
_MOTION_OFF_FONT_RATIO = 0.18
_MOTION_OFF_MAX_WIDTH_RATIO = 0.86
_MOTION_OFF_MAX_HEIGHT_RATIO = 0.26
_MOTION_OFF_STROKE_RATIO = 0.035

# Density presets intentionally keep the persisted size names semantic. Saved
# layouts therefore remain compatible when users choose a denser camera wall.
_DENSITY_PRESETS: dict[str, dict[str, int]] = {
    "balanced": {"small": 4, "medium": 2, "large": 1},
    "dense": {"small": 5, "medium": 3, "large": 2},
}
_SIZE_MODES = _DENSITY_PRESETS["balanced"]
_DEFAULT_LIVE_MONITORING_MAX_STREAMS = 4
_EXPERIMENTAL_LIVE_MONITORING_MAX_STREAMS = 6
_MAX_EXCEEDED_TOOLTIP = "Exceeded Camera Maximum for Live Monitoring"

# Module-level drag state — safe for same-process DnD.
_dnd_src_id: int | None = None


def _density_columns(mode: str) -> int:
    preset = str(_cfg.load().get("camera_grid_density_preset", "balanced"))
    columns = _DENSITY_PRESETS.get(preset, _DENSITY_PRESETS["balanced"])
    return columns.get(mode, columns["medium"])


def _safe_device_text(device, attribute: str, fallback: str) -> str:
    try:
        value = getattr(device, attribute)
    except Exception:
        return fallback
    text = str(value or "").strip()
    return text or fallback


def _reorder_ids(order: list[int], available_ids: set[int], src_id: int, dst_id: int) -> list[int]:
    """Return *order* with src moved to dst, limited to currently available devices."""
    ordered = [did for did in order if did in available_ids]
    if src_id not in ordered or dst_id not in ordered:
        return ordered

    src_idx = ordered.index(src_id)
    dst_idx = ordered.index(dst_id)
    ordered.pop(src_idx)
    ordered.insert(dst_idx, src_id)
    return ordered


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _apply_motion_off_overlay(png_bytes: bytes) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

        img = Image.open(io.BytesIO(_apply_preview_blur(png_bytes))).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        font, font_size = _fit_motion_off_font(draw, ImageFont, w, h)
        bbox = draw.textbbox((0, 0), _MOTION_OFF_TEXT, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = (w - tw) // 2, (h - th) // 2
        stroke = max(1, round(font_size * _MOTION_OFF_STROKE_RATIO))
        for dx, dy in ((-stroke, -stroke), (stroke, -stroke), (-stroke, stroke), (stroke, stroke)):
            draw.text((x + dx, y + dy), _MOTION_OFF_TEXT, fill=(0, 0, 0), font=font)
        draw.text((x, y), _MOTION_OFF_TEXT, fill=(255, 255, 255), font=font)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("motion overlay failed: %s", exc)
        return png_bytes


def _motion_off_base_font_size(width: int, height: int) -> int:
    return max(12, round(min(width, height) * _MOTION_OFF_FONT_RATIO))


def _fit_motion_off_font(draw, image_font_module, width: int, height: int):
    font_path = _sans_bold_font_path()
    size = _motion_off_base_font_size(width, height)
    max_width = width * _MOTION_OFF_MAX_WIDTH_RATIO
    max_height = height * _MOTION_OFF_MAX_HEIGHT_RATIO

    while size > 12:
        font = _load_motion_off_font(image_font_module, font_path, size)
        bbox = draw.textbbox((0, 0), _MOTION_OFF_TEXT, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= max_width and text_height <= max_height:
            return font, size
        size -= 2
    return image_font_module.load_default(), 12


def _load_motion_off_font(image_font_module, font_path: str | None, size: int):
    if font_path:
        try:
            return image_font_module.truetype(font_path, size)
        except Exception:
            pass
    return image_font_module.load_default()


def _sans_bold_font_path() -> str | None:
    try:
        import subprocess  # noqa: PLC0415

        result = subprocess.run(
            ["fc-match", "--format=%{file}", "sans-serif:bold"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        font_path = result.stdout.strip()
        return font_path or None
    except Exception:
        return None


def _preview_blur_radius(width: int, height: int) -> float:
    short_edge = max(1, min(width, height))
    return max(
        2.0,
        _PREVIEW_BLUR_REFERENCE_RADIUS * short_edge / _PREVIEW_BLUR_REFERENCE_SHORT_EDGE,
    )


def _apply_preview_blur(png_bytes: bytes) -> bytes:
    """Return a consistently blurred preview image for inactive camera tiles."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        radius = _preview_blur_radius(*img.size)
        for _ in range(_PREVIEW_BLUR_PASSES):
            img = img.filter(ImageFilter.GaussianBlur(radius=radius))
        img = ImageEnhance.Brightness(img).enhance(_PREVIEW_BLUR_BRIGHTNESS)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("preview blur failed: %s", exc)
        return png_bytes


def _make_dark_placeholder() -> bytes:
    """1280×720 near-black placeholder for motion-off cameras with no event frame."""
    try:
        from PIL import Image

        img = Image.new("RGB", (1280, 720), color=(26, 26, 26))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        _log.debug("dark placeholder creation failed: %s", exc)
        return b""


_TIMER_CSS_PROVIDER: Gtk.CssProvider | None = None


def _get_timer_css_provider() -> Gtk.CssProvider:
    global _TIMER_CSS_PROVIDER
    if _TIMER_CSS_PROVIDER is None:
        _TIMER_CSS_PROVIDER = Gtk.CssProvider()
        _TIMER_CSS_PROVIDER.load_from_string(
            ".snapshot-timer {"
            " background-color: rgba(0,0,0,0.5);"
            " border-radius: 4px;"
            " padding: 2px 6px;"
            " color: white;"
            "}"
        )
    return _TIMER_CSS_PROVIDER


# ---------------------------------------------------------------------------
# AspectBox — custom Gtk.Widget that enforces a fixed h:w ratio via measure()
# ---------------------------------------------------------------------------


class AspectBox(Gtk.Widget):
    """A single-child widget that enforces a height-for-width aspect ratio.

    The parent grid queries each child's natural size via measure() before it
    decides row heights. By returning a fixed h = w * _h_ratio from
    do_measure(VERTICAL, for_size=allocated_width) we guarantee all tiles in a
    row share the same height regardless of the image's native resolution.

    The inner Gtk.Picture is managed via set_parent / unparent (GTK4 custom
    widget pattern) and receives the full allocation in do_size_allocate.
    """

    def __init__(self) -> None:
        super().__init__()
        self._h_ratio: float = _FIXED_TILE_H / _FIXED_TILE_W
        self._picture = Gtk.Picture(
            content_fit=Gtk.ContentFit.CONTAIN,
            can_shrink=True,
            hexpand=True,
            vexpand=True,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.FILL,
        )
        self._picture.set_parent(self)

    # ------------------------------------------------------------------
    # GTK4 vfunc overrides
    # ------------------------------------------------------------------

    def do_dispose(self) -> None:
        child = self._picture
        self._picture = None  # type: ignore[assignment]
        if child is not None:
            child.unparent()
        Gtk.Widget.do_dispose(self)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        """Return (minimum, natural, min_baseline, nat_baseline).

        HORIZONTAL: can shrink to 1 px; the grid controls the width.
        VERTICAL:   fixed height = for_size * _h_ratio so all row tiles
                    report the same height to the parent layout.
        """
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (1, 1, -1, -1)
        # VERTICAL
        if for_size <= 0:
            return (1, 1, -1, -1)
        fixed_h = max(1, int(for_size * self._h_ratio))
        return (fixed_h, fixed_h, -1, -1)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if self._picture is not None:
            self._picture.allocate(width, height, baseline, None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_ratio(self, h_ratio: float) -> None:
        """Update the height:width ratio and trigger a remeasure."""
        self._h_ratio = max(h_ratio, 0.01)
        self.queue_resize()

    def set_paintable(self, paintable) -> None:
        if self._picture is not None:
            self._picture.set_paintable(paintable)


# ---------------------------------------------------------------------------
# Camera tile widget
# ---------------------------------------------------------------------------


class CameraTile(Gtk.Frame):
    """A single camera tile in the camera grid."""

    def __init__(
        self,
        device,
        on_reorder,
        on_activate,
        on_camera_settings,
        is_layout_editing,
        *,
        live_monitoring: bool = False,
        on_light_toggle=None,
        on_siren_toggle=None,
        on_stream_status_changed=None,
    ) -> None:
        super().__init__()
        self.device = device
        self._on_reorder = on_reorder
        self._on_activate = on_activate
        self._on_camera_settings = on_camera_settings
        self._is_layout_editing = is_layout_editing
        self._live_monitoring = live_monitoring
        self._intercom_supported = live_monitoring and supports_ring_camera_intercom(device)
        self._on_light_toggle = on_light_toggle
        self._on_siren_toggle = on_siren_toggle
        self._on_stream_status_changed = on_stream_status_changed
        self._native_w = _DEFAULT_NATIVE_W
        self._native_h = _DEFAULT_NATIVE_H
        self._size_mode = "medium"
        self._live_session = None
        self._live_active = False
        self._stream_start_failed = False
        self._volume = 0.0
        self._previous_volume = 1.0
        self.set_focusable(True)
        self.set_accessible_role(Gtk.AccessibleRole.BUTTON)
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"Open {device_names.display_name(device)}"],
        )
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.START)

        # Snapshot age timer state.
        self._snapshot_loaded_at: float = 0.0
        self._motion_detection_off: bool = False
        self._show_snapshot_timer = not live_monitoring
        self._timer_source_id: int | None = None

        self.add_css_class("card")
        self.add_css_class("camera-tile")
        self.set_overflow(Gtk.Overflow.HIDDEN)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(box)

        # AspectBox enforces the tile height via GTK4 measure().
        # The Gtk.Picture lives inside AspectBox, not directly in the tree.
        self.aspect_box = AspectBox()
        self.aspect_box.add_css_class("camera-picture")

        # Timer badge floats over the aspect_box.
        self._timer_label = Gtk.Label(
            css_classes=["caption", "numeric", "snapshot-timer"],
            halign=Gtk.Align.START,
            valign=Gtk.Align.END,
            margin_start=8,
            margin_bottom=8,
            visible=False,
        )
        self._timer_label.get_style_context().add_provider(
            _get_timer_css_provider(), Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        picture_overlay = Gtk.Overlay(hexpand=True)
        picture_overlay.set_child(self.aspect_box)
        picture_overlay.add_overlay(self._timer_label)
        self._live_container = Gtk.Box(hexpand=True, vexpand=True, visible=False)
        picture_overlay.add_overlay(self._live_container)

        self._stream_state_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=5,
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
            margin_top=8,
            margin_start=8,
            visible=self._live_monitoring,
        )
        self._stream_state_box.add_css_class("camera-overlay")
        self._stream_state_icon = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
        self._stream_state_label = Gtk.Label(
            label="Stream stopped",
            ellipsize=Pango.EllipsizeMode.END,
        )
        self._stream_state_box.append(self._stream_state_icon)
        self._stream_state_box.append(self._stream_state_label)
        picture_overlay.add_overlay(self._stream_state_box)
        box.append(picture_overlay)

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        footer.add_css_class("camera-footer")
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        self._name_label = Gtk.Label(
            label=device_names.display_name(device),
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        self._name_label.add_css_class("camera-name")
        self._model_label = Gtk.Label(
            label=_safe_device_text(device, "model", "Ring camera"),
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        self._model_label.add_css_class("dim-label")
        title_copy.append(self._name_label)
        title_copy.append(self._model_label)
        title_row.append(title_copy)

        if not self._live_monitoring:
            self._settings_btn = Gtk.Button(
                icon_name="preferences-system-symbolic",
                tooltip_text="Camera Settings",
                css_classes=["flat"],
                valign=Gtk.Align.CENTER,
            )
            self._settings_btn.connect("clicked", self._on_settings_clicked)
            title_row.append(self._settings_btn)
            self._open_btn = Gtk.Button(
                icon_name="view-fullscreen-symbolic",
                tooltip_text="Open live view",
                css_classes=["flat"],
                valign=Gtk.Align.CENTER,
            )
            self._open_btn.connect("clicked", lambda *_args: self._on_activate(self.device))
            title_row.append(self._open_btn)
            self._next_icon = None
        else:
            self._settings_btn = None
            self._open_btn = None
            self._next_icon = Gtk.Image.new_from_icon_name("go-next-symbolic")
            title_row.append(self._next_icon)
        footer.append(title_row)

        self._network_icon = Gtk.Image(pixel_size=16, valign=Gtk.Align.CENTER)
        self._power_icon = Gtk.Image(pixel_size=16, valign=Gtk.Align.CENTER)
        self._network_value = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._power_value = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        motion_text = (
            "Motion alerts on"
            if getattr(device, "motion_detection", True)
            else "Motion detection off"
        )
        self._motion_value = Gtk.Label(
            label=motion_text,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        self._details_grid = Gtk.Grid(
            column_spacing=14,
            row_spacing=7,
            column_homogeneous=True,
            visible=not self._live_monitoring,
        )
        self._details_grid.add_css_class("camera-details")
        self._details_grid.attach(
            self._detail_item(self._power_icon, self._power_value), 0, 0, 1, 1
        )
        self._details_grid.attach(
            self._detail_item(self._network_icon, self._network_value), 1, 0, 1, 1
        )
        motion_icon = Gtk.Image.new_from_icon_name("preferences-system-notifications-symbolic")
        motion_icon.set_pixel_size(16)
        motion_icon.add_css_class("dim-label")
        self._details_grid.attach(self._detail_item(motion_icon, self._motion_value), 0, 1, 2, 1)
        footer.append(self._details_grid)
        box.append(footer)

        self._update_power_status()
        self._update_network_status()
        if self._live_monitoring:
            self._live_controls = self._build_live_monitoring_controls()
        else:
            self._live_controls = None

        # Start the 1-second tick; it is a no-op while the timer label is hidden.
        self._timer_source_id = GLib.timeout_add_seconds(1, self._update_timer)

        # Hover state.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.add_css_class("activatable"))
        motion.connect("leave", lambda *_: self.remove_css_class("activatable"))
        self.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect("released", self._on_click_released)
        self.add_controller(click)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

        # Drag source — set module-level ID so the drop target can read it.
        drag_src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag_src.connect("prepare", self._on_drag_prepare)
        drag_src.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag_src)

        # Drop target — accepts a string (the source device ID).
        drop_tgt = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        drop_tgt.connect("drop", self._on_drop)
        self.add_controller(drop_tgt)

    @staticmethod
    def _detail_item(icon: Gtk.Image, label: Gtk.Label) -> Gtk.Widget:
        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        icon.add_css_class("dim-label")
        item.append(icon)
        label.set_hexpand(True)
        item.append(label)
        return item

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.add_css_class("selected")
        else:
            self.remove_css_class("selected")
        self.update_state([Gtk.AccessibleState.SELECTED], [int(selected)])

    # ------------------------------------------------------------------
    # Ratio update — called by CamerasPage when mode or snapshot changes
    # ------------------------------------------------------------------

    def update_ratio(self, mode: str) -> None:
        """Update the measured display box for the active size mode."""
        self._size_mode = mode
        if _density_columns(mode) > 1:
            self.aspect_box.set_ratio(_FIXED_TILE_H / _FIXED_TILE_W)
        else:
            self.aspect_box.set_ratio(self._native_h / self._native_w)
        self._details_grid.set_visible(not self._live_monitoring and mode != "small")

    def set_compact_presentation(self, compact: bool) -> None:
        """Reduce tile chrome when the configured column count is physically tight."""
        self._model_label.set_visible(not compact)
        self._details_grid.set_visible(
            not compact and not self._live_monitoring and self._size_mode != "small"
        )
        for button in (self._settings_btn, self._open_btn):
            if button is not None:
                button.set_visible(not compact)
        if self._next_icon is not None:
            self._next_icon.set_visible(not compact)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def set_snapshot(self, png_bytes: bytes, *, motion_detection_off: bool = False) -> None:
        """Update the thumbnail from raw PNG bytes (GTK main thread)."""
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(png_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is not None:
                # Use the pixbuf's own dimensions instead of a second PIL decode
                # on the GTK main thread.
                self._native_w = pixbuf.get_width()
                self._native_h = pixbuf.get_height()
                self.aspect_box.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        except Exception as exc:
            _log.debug("CameraTile.set_snapshot failed for %s: %s", self.device.name, exc)

        self._motion_detection_off = motion_detection_off
        if motion_detection_off or not self._show_snapshot_timer:
            self._timer_label.set_visible(False)
        else:
            self._snapshot_loaded_at = time.monotonic()
            self._timer_label.set_visible(True)
            self._update_timer()

    # ------------------------------------------------------------------
    # Snapshot age timer
    # ------------------------------------------------------------------

    def _update_timer(self) -> bool:
        if self._live_monitoring:
            self._refresh_stream_state()
            return GLib.SOURCE_CONTINUE
        if not self._timer_label.get_visible():
            return GLib.SOURCE_CONTINUE
        elapsed = int(time.monotonic() - self._snapshot_loaded_at)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        if h > 0:
            text = f"{h}h {m}m {s}s"
        elif m > 0:
            text = f"{m}m {s}s"
        else:
            text = f"{s}s"
        self._timer_label.set_label(text)
        return GLib.SOURCE_CONTINUE

    def cleanup(self) -> None:
        self.stop_live_monitoring()
        if self._timer_source_id is not None:
            GLib.source_remove(self._timer_source_id)
            self._timer_source_id = None

    def refresh_display_name(self) -> None:
        display_name = device_names.display_name(self.device)
        self._name_label.set_label(display_name)
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"Open {display_name}"],
        )

    def _update_power_status(self) -> None:
        status = camera_power_status(self.device)
        self._power_icon.set_from_icon_name(status.icon_name)
        self._power_icon.set_tooltip_text(status.tooltip)
        self._power_value.set_label(status.tooltip)

    def _update_network_status(self) -> None:
        status = camera_network_status(self.device)
        self._network_icon.set_from_icon_name(status.icon_name)
        self._network_icon.set_tooltip_text(status.tooltip)
        self._network_value.set_label(status.tooltip)

    def _on_settings_clicked(self, *_):
        if self._on_camera_settings is not None:
            self._on_camera_settings(self.device)

    # ------------------------------------------------------------------
    # Live monitoring controls
    # ------------------------------------------------------------------

    def _build_live_monitoring_controls(self) -> Gtk.Widget:
        controls = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            halign=Gtk.Align.FILL,
        )

        mic_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        mic_content.set_halign(Gtk.Align.CENTER)
        mic_content.append(Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic"))
        mic_content.append(Gtk.Label(label="Talk"))
        self._mic_btn = Gtk.ToggleButton(child=mic_content, tooltip_text="Talk through camera")
        self._mic_btn.add_css_class("command-button")
        self._mic_btn.set_hexpand(True)
        self._mic_btn.set_sensitive(False)
        self._mic_btn.set_visible(self._intercom_supported)
        self._mic_btn.connect("toggled", self._on_mic_toggled)
        controls.append(self._mic_btn)

        self._volume_btn = Gtk.ToggleButton(
            icon_name="audio-volume-muted-symbolic",
            tooltip_text="Unmute",
            css_classes=["flat"],
        )
        self._volume_btn.set_active(False)
        self._volume_btn.set_sensitive(False)
        self._volume_btn.connect("toggled", self._on_volume_toggled)
        controls.append(self._volume_btn)

        self._volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self._volume_scale.set_value(0.0)
        self._volume_scale.set_draw_value(False)
        self._volume_scale.set_size_request(70, -1)
        self._volume_scale.set_hexpand(True)
        self._volume_scale.set_sensitive(False)
        self._volume_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Camera volume"],
        )
        self._volume_scale.connect("value-changed", self._on_volume_changed)
        controls.append(self._volume_scale)

        if device_commands.has_capability(self.device, "light"):
            light_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            light_content.set_halign(Gtk.Align.CENTER)
            self._light_icon = Gtk.Image(pixel_size=20)
            light_content.append(self._light_icon)
            light_content.append(Gtk.Label(label="Light"))
            self._light_btn = Gtk.ToggleButton(child=light_content, tooltip_text="Camera light")
            self._light_btn.add_css_class("command-button")
            self._light_btn.set_hexpand(True)
            self._light_btn.set_active(
                device_commands.command_state.light_enabled(get_client(), self.device)
            )
            self._light_btn.connect("toggled", self._on_light_toggled)
            self._refresh_light_icon()
            controls.append(self._light_btn)
        else:
            self._light_btn = None

        if device_commands.has_capability(self.device, "siren"):
            siren_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            siren_content.set_halign(Gtk.Align.CENTER)
            self._siren_icon = Gtk.Image.new_from_icon_name("alarm-symbolic")
            self._siren_icon.set_pixel_size(20)
            siren_content.append(self._siren_icon)
            siren_content.append(Gtk.Label(label="Siren…"))
            self._siren_btn = Gtk.Button(child=siren_content, tooltip_text="Camera siren")
            self._siren_btn.add_css_class("command-button")
            self._siren_btn.add_css_class("siren-button")
            self._siren_btn.set_hexpand(True)
            self._siren_btn.connect("clicked", self._on_siren_clicked)
            controls.append(self._siren_btn)
        else:
            self._siren_btn = None

        return controls

    def start_live_monitoring(self, volume: float = 0.0, *, max_streams: int | None = None) -> bool:
        if not self._live_monitoring:
            return False
        if self._live_active:
            session = get_live_session_manager().session_for(self.device.id)
            if session is not None and session.active and LIVE_MONITORING_OWNER in session.owners:
                return True
            self._live_active = False
        self._stream_start_failed = False
        self._set_volume_state(volume)
        try:
            self._live_session = get_live_session_manager().acquire(
                self.device,
                owner=LIVE_MONITORING_OWNER,
                volume=volume,
                max_streams=max_streams,
            )
        except StreamLimitExceeded:
            raise
        except Exception as exc:
            _log.debug("Failed to start monitoring stream for %s: %s", self.device.name, exc)
            self._stream_start_failed = True
            self._set_stream_state("Stream failed", "dialog-error-symbolic", "offline")
            return False
        self._live_session.attach_to(self._live_container)
        self._live_active = True
        self._set_stream_state("Connecting", "media-record-symbolic", "live")
        self._set_stream_controls_sensitive(True)
        return True

    def attach_existing_live_session(self) -> None:
        if self._live_session is None:
            session = get_live_session_manager().session_for(self.device.id)
            if session is None:
                self.mark_live_monitoring_stopped()
                return
            self._live_session = session
        self._set_volume_state(self._live_session.volume)
        self._live_session.attach_to(self._live_container)
        self._live_active = True
        self._live_container.set_visible(True)
        self._set_stream_state("Live", "media-record-symbolic", "live")
        self._set_stream_controls_sensitive(True)

    def mark_live_monitoring_stopped(self) -> None:
        self.stop_talking()
        self._stream_start_failed = False
        self._live_session = None
        self._live_container.set_visible(False)
        self._live_active = False
        self._set_stream_state("Stream stopped", "media-playback-stop-symbolic", "snapshot")
        self._set_stream_controls_sensitive(False)

    def stop_live_monitoring(self) -> None:
        if not self._live_monitoring:
            return
        self.stop_talking()
        get_live_session_manager().release(self.device.id, owner=LIVE_MONITORING_OWNER)
        self.mark_live_monitoring_stopped()

    def stop_talking(self) -> None:
        """Stop talkback owned by this tile before its control is hidden or released."""
        if not hasattr(self, "_mic_btn"):
            return
        was_active = self._mic_btn.get_active()
        if was_active and self._live_session is not None:
            try:
                self._live_session.stop_talking()
            except Exception as exc:
                _log.debug("Failed to stop camera talkback: %s", exc)
        self._mic_btn.handler_block_by_func(self._on_mic_toggled)
        self._mic_btn.set_active(False)
        self._mic_btn.handler_unblock_by_func(self._on_mic_toggled)

    def _set_stream_controls_sensitive(self, sensitive: bool) -> None:
        if not hasattr(self, "_mic_btn"):
            return
        self._mic_btn.set_sensitive(sensitive and self._intercom_supported)
        self._volume_btn.set_sensitive(sensitive)
        self._volume_scale.set_sensitive(sensitive)

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
        self._volume_scale.set_value(volume)
        if self._live_session is not None:
            self._live_session.set_volume(volume)

    def _on_mic_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self._live_session is None:
            return
        if btn.get_active():
            self._live_session.start_talking()
        else:
            self._live_session.stop_talking()

    def _on_volume_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            restored = self._previous_volume if self._previous_volume > 0 else 1.0
            self._volume = restored
            self._volume_scale.set_value(restored)
            btn.set_icon_name("audio-volume-high-symbolic")
            btn.set_tooltip_text("Mute")
        else:
            self._previous_volume = max(self._volume_scale.get_value(), 0.05)
            self._volume = 0.0
            self._volume_scale.set_value(0.0)
            btn.set_icon_name("audio-volume-muted-symbolic")
            btn.set_tooltip_text("Unmute")
        if self._live_session is not None:
            self._live_session.set_volume(self._volume)

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        self._volume = scale.get_value()
        if self._live_session is not None:
            self._live_session.set_volume(self._volume)
        if self._volume > 0 and hasattr(self, "_volume_btn"):
            self._volume_btn.handler_block_by_func(self._on_volume_toggled)
            self._volume_btn.set_active(True)
            self._volume_btn.handler_unblock_by_func(self._on_volume_toggled)
            self._volume_btn.set_icon_name("audio-volume-high-symbolic")
            self._volume_btn.set_tooltip_text("Mute")

    def _on_light_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._refresh_light_icon()
        if self._on_light_toggle is not None:
            self._on_light_toggle(self.device, btn.get_active())

    def _on_siren_clicked(self, *_args) -> None:
        if self._on_siren_toggle is not None:
            self._on_siren_toggle(self.device)

    def _refresh_light_icon(self) -> None:
        if not hasattr(self, "_light_icon"):
            return
        self._light_icon.set_from_icon_name("display-brightness-symbolic")
        self._light_btn.set_tooltip_text(
            "Turn camera light off" if self._light_btn.get_active() else "Turn camera light on"
        )

    def _set_stream_state(self, label: str, icon_name: str, style: str) -> None:
        changed = self._stream_state_label.get_label() != label
        self._stream_state_label.set_label(label)
        self._stream_state_icon.set_from_icon_name(icon_name)
        for css_class in ("state-live", "state-snapshot", "state-offline"):
            self._stream_state_box.remove_css_class(css_class)
        self._stream_state_box.add_css_class(f"state-{style}")
        if changed and self._on_stream_status_changed is not None:
            self._on_stream_status_changed(self.device, label)

    def _refresh_stream_state(self) -> None:
        session = get_live_session_manager().session_for(self.device.id)
        owned = session is not None and LIVE_MONITORING_OWNER in session.owners
        if owned and session.active:
            state = getattr(session.state, "value", str(session.state))
            if state == "active":
                self._set_stream_state("Live", "media-record-symbolic", "live")
            else:
                self._set_stream_state("Connecting", "media-record-symbolic", "live")
        elif owned:
            self.stop_talking()
            self._live_active = False
            self._set_stream_controls_sensitive(False)
            self._set_stream_state("Stream unavailable", "dialog-error-symbolic", "offline")
        elif self._stream_start_failed:
            self.stop_talking()
            self._live_active = False
            self._set_stream_controls_sensitive(False)
            self._set_stream_state("Stream failed", "dialog-error-symbolic", "offline")
        else:
            self.stop_talking()
            self._live_active = False
            self._set_stream_controls_sensitive(False)
            self._set_stream_state("Stream stopped", "media-playback-stop-symbolic", "snapshot")

    # ------------------------------------------------------------------
    # Drag source
    # ------------------------------------------------------------------

    def _on_drag_prepare(self, source, x, y):
        if not self._is_layout_editing():
            return None
        global _dnd_src_id
        _dnd_src_id = self.device.id
        return Gdk.ContentProvider.new_for_value(str(self.device.id))

    def _on_drag_begin(self, source, drag) -> None:
        paintable = Gtk.WidgetPaintable.new(self)
        source.set_icon(paintable, 0, 0)

    # ------------------------------------------------------------------
    # Drop target
    # ------------------------------------------------------------------

    def _on_drop(self, target, value, x, y) -> bool:
        if not self._is_layout_editing():
            return False
        global _dnd_src_id
        src_id = _dnd_src_id
        _dnd_src_id = None
        if src_id is None or src_id == self.device.id:
            return False
        self._on_reorder(src_id, self.device.id)
        return True

    def _on_click_released(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float
    ) -> None:
        if n_press == 1 and not self._is_layout_editing():
            self._on_activate(self.device)

    def _on_key_pressed(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        if keyval in {Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space}:
            if not self._is_layout_editing():
                self._on_activate(self.device)
            return True
        return False


# ---------------------------------------------------------------------------
# Cameras page
# ---------------------------------------------------------------------------


class CamerasPage(Gtk.Box):
    """Camera grid for the dashboard or multi-camera monitoring page."""

    def __init__(
        self,
        on_open_live_focus,
        *,
        on_open_history=None,
        show_monitoring_controls: bool = False,
        grid_size_config_key: str = "camera_grid_size",
        camera_order_config_key: str = "camera_order",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._on_open_live_focus = on_open_live_focus
        self._on_open_history = on_open_history
        self._show_monitoring_controls = show_monitoring_controls
        self._grid_size_config_key = grid_size_config_key
        self._camera_order_config_key = camera_order_config_key

        # device_id → CameraTile
        self._cards: dict[int, CameraTile] = {}
        self._devices: list = []
        # device_id → GLib source_id for 30-second fallback refresh.
        self._refresh_timers: dict[int, int] = {}
        # device_id → last good PNG bytes; survives grid rebuilds for cache-first display.
        self._snapshot_cache: dict[int, bytes] = {}
        self._snapshot_generation = 0
        self._snapshot_inflight_generations: dict[int, int] = {}
        self._snapshot_pending_events: dict[int, object | None] = {}
        self._snapshot_restart_timer_ids: set[int] = set()
        self._snapshot_updates_active = False
        self._event_client = None
        self._devices_request_generation = 0
        self._layout_editing = False
        self._monitoring_active = False
        self._active_monitoring_stream_ids: set[int] = set()
        self._selected_device_id: int | None = None

        cfg = _cfg.load()
        default_size = (
            live_layouts.DEFAULT_LAYOUT_SIZE if self._show_monitoring_controls else "medium"
        )
        self._size_mode: str = cfg.get(self._grid_size_config_key, default_size)
        if self._size_mode not in _SIZE_MODES:
            self._size_mode = default_size
        self._order: list[int] = cfg.get(self._camera_order_config_key, [])
        self._hidden_camera_ids: set[int] = set(cfg.get("live_monitoring_hidden_camera_ids", []))
        self._layout_records: list[dict] = []
        self._current_saved_layout_id: str | None = None
        self._current_layout_label = live_layouts.DEFAULT_LAYOUT_NAME
        self._build_ui()

    def do_unroot(self) -> None:
        self.stop_live_monitoring()
        self.deactivate_snapshot_updates()
        self._clear_grid()
        Gtk.Box.do_unroot(self)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        grid_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            hexpand=True,
            vexpand=True,
        )
        if self._show_monitoring_controls:
            grid_box.add_css_class("monitor-canvas")
            grid_box.set_margin_top(18)
            grid_box.set_margin_bottom(18)
            grid_box.set_margin_start(20)
            grid_box.set_margin_end(20)

        if self._show_monitoring_controls:
            title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            self._layout_status_label = Gtk.Label(
                label="Default layout",
                xalign=0,
                hexpand=True,
            )
            self._layout_status_label.add_css_class("heading")
            self._layout_status_label.add_css_class("dim-label")
            title_row.append(self._layout_status_label)
            self._inspector_toggle = Gtk.ToggleButton(
                icon_name="sidebar-show-right-symbolic",
                tooltip_text="Show selected camera",
                active=True,
            )
            self._inspector_toggle.add_css_class("flat")
            self._inspector_toggle.connect("toggled", self._on_inspector_toggled)
            title_row.append(self._inspector_toggle)
            grid_box.append(title_row)
            grid_box.append(self._build_monitoring_toolbar())
        else:
            toolbar = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
                halign=Gtk.Align.END,
            )
            toolbar.append(Gtk.Label(label="Density", css_classes=["dim-label"]))
            toolbar.append(self._build_density_controls())
            self._layout_btn = self._build_reorder_button()
            toolbar.append(self._layout_btn)
            grid_box.append(toolbar)

        self._status_page = Adw.StatusPage(
            icon_name="camera-video-symbolic",
            title="No cameras",
            description="Sign in to see your Ring cameras.",
            vexpand=True,
        )
        self._status_page.set_visible(True)
        grid_box.append(self._status_page)

        self._scroll = Gtk.ScrolledWindow(
            vexpand=True,
            visible=False,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        grid_box.append(self._scroll)

        self._grid = Gtk.Grid(
            column_spacing=16,
            row_spacing=16,
            margin_top=2,
            margin_bottom=2,
            margin_start=2,
            margin_end=2,
            hexpand=True,
            vexpand=False,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.START,
        )
        self._grid.add_css_class("camera-grid")
        self._apply_size_mode_layout(self._size_mode)
        self._scroll.set_child(self._grid)

        if not self._show_monitoring_controls:
            self.append(grid_box)
            return

        self._monitor_split = Adw.OverlaySplitView(
            hexpand=True,
            vexpand=True,
            sidebar_position=Gtk.PackType.END,
            min_sidebar_width=320,
            max_sidebar_width=380,
            sidebar_width_fraction=0.31,
            collapsed=False,
            show_sidebar=True,
        )
        self._monitor_split.set_content(grid_box)
        self._monitor_split.set_sidebar(self._build_monitoring_inspector())
        self._monitor_split.connect("notify::show-sidebar", self._sync_inspector_toggle)
        self.append(self._monitor_split)

    @property
    def inspector_split_view(self) -> Adw.OverlaySplitView | None:
        return getattr(self, "_monitor_split", None)

    def _build_density_controls(self) -> Gtk.Widget:
        size_box = Gtk.Box(css_classes=["linked"], spacing=0)
        self._size_btns: dict[str, Gtk.ToggleButton] = {}
        first_btn = None
        for mode, label in (("small", "S"), ("medium", "M"), ("large", "L")):
            columns = _density_columns(mode)
            noun = "camera" if columns == 1 else "cameras"
            btn = Gtk.ToggleButton(
                label=label,
                tooltip_text=f"{mode.title()} - {columns} {noun} per row",
            )
            btn.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"{mode.title()} density, {columns} {noun} per row"],
            )
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.set_active(mode == self._size_mode)
            btn.connect("toggled", self._on_size_toggled, mode)
            size_box.append(btn)
            self._size_btns[mode] = btn
        return size_box

    def _refresh_density_control_labels(self) -> None:
        for mode, button in self._size_btns.items():
            columns = _density_columns(mode)
            noun = "camera" if columns == 1 else "cameras"
            button.set_tooltip_text(f"{mode.title()} - {columns} {noun} per row")
            button.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"{mode.title()} density, {columns} {noun} per row"],
            )

    def _build_reorder_button(self) -> Gtk.ToggleButton:
        button = Gtk.ToggleButton(
            icon_name="view-list-symbolic",
            tooltip_text="Edit camera layout",
        )
        button.add_css_class("flat")
        button.connect("toggled", self._on_layout_toggled)
        return button

    def _build_monitoring_toolbar(self) -> Gtk.Widget:
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls.add_css_class("monitor-controls")

        layout_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        layout_row.append(Gtk.Label(label="Layout", xalign=0))
        self._layout_menu_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self._layout_popover = Gtk.Popover(child=self._layout_menu_box)
        self._layout_select_btn = Gtk.MenuButton(
            label=live_layouts.DEFAULT_LAYOUT_NAME,
            tooltip_text="Choose saved layout",
            popover=self._layout_popover,
            hexpand=True,
        )
        layout_row.append(self._layout_select_btn)
        self._save_layout_btn = Gtk.Button(
            icon_name="document-save-symbolic", tooltip_text="Save current layout"
        )
        self._save_layout_btn.connect("clicked", self._on_save_live_layout)
        layout_row.append(self._save_layout_btn)
        self._rename_layout_btn = Gtk.Button(
            icon_name="document-edit-symbolic",
            tooltip_text="Rename saved layout",
            visible=False,
        )
        self._rename_layout_btn.connect("clicked", self._on_rename_live_layout)
        layout_row.append(self._rename_layout_btn)
        self._delete_layout_btn = Gtk.Button(
            icon_name="edit-delete-symbolic",
            tooltip_text="Delete saved layout",
            visible=False,
        )
        self._delete_layout_btn.connect("clicked", self._on_delete_live_layout)
        layout_row.append(self._delete_layout_btn)
        controls.append(layout_row)

        stream_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._camera_filter_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        filter_popover = Gtk.Popover(child=self._camera_filter_box)
        self._camera_filter_btn = Gtk.MenuButton(
            label="Cameras",
            icon_name="camera-video-symbolic",
            tooltip_text="Choose cameras to display",
            popover=filter_popover,
            hexpand=True,
            halign=Gtk.Align.START,
        )
        stream_row.append(self._camera_filter_btn)
        self._play_btn = Gtk.Button(
            label="Start all",
            icon_name="media-playback-start-symbolic",
            tooltip_text="Start live monitoring",
        )
        self._play_btn.connect("clicked", self._on_monitoring_play)
        stream_row.append(self._play_btn)
        self._stop_btn = Gtk.Button(
            icon_name="media-playback-stop-symbolic",
            tooltip_text="Stop live monitoring",
            sensitive=False,
        )
        self._stop_btn.connect("clicked", self._on_monitoring_stop)
        stream_row.append(self._stop_btn)
        controls.append(stream_row)

        display_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        display_row.set_halign(Gtk.Align.END)
        display_row.append(Gtk.Label(label="Density", css_classes=["dim-label"]))
        display_row.append(self._build_density_controls())
        self._layout_btn = self._build_reorder_button()
        display_row.append(self._layout_btn)
        controls.append(display_row)
        return controls

    def _build_monitoring_inspector(self) -> Gtk.Widget:
        inspector = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=False,
            vexpand=True,
        )
        inspector.add_css_class("monitor-inspector")
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.add_css_class("inspector-content")
        scroller.set_child(content)
        inspector.append(scroller)

        self._selected_name = Gtk.Label(label="Select a camera", xalign=0, wrap=True)
        self._selected_name.add_css_class("title-2")
        content.append(self._selected_name)
        self._selected_model = Gtk.Label(label="Camera controls and health", xalign=0)
        self._selected_model.add_css_class("dim-label")
        content.append(self._selected_model)

        self._inspector_open_btn = Gtk.Button(
            label="Open camera",
            icon_name="view-fullscreen-symbolic",
            sensitive=False,
        )
        self._inspector_open_btn.add_css_class("suggested-action")
        self._inspector_open_btn.connect("clicked", self._on_inspector_open)
        content.append(self._inspector_open_btn)

        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        health_title = Gtk.Label(label="Camera health", xalign=0)
        health_title.add_css_class("heading")
        content.append(health_title)
        health = Gtk.Grid(column_spacing=16, row_spacing=9)
        self._selected_status = self._add_inspector_detail(health, 0, "Status")
        self._selected_network = self._add_inspector_detail(health, 1, "Network")
        self._selected_power = self._add_inspector_detail(health, 2, "Power")
        self._selected_motion = self._add_inspector_detail(health, 3, "Motion")
        content.append(health)

        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        controls_title = Gtk.Label(label="Audio and controls", xalign=0)
        controls_title.add_css_class("heading")
        content.append(controls_title)

        audio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        audio_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        incoming = Gtk.Label(label="Incoming audio", xalign=0)
        incoming.add_css_class("heading")
        audio_copy.append(incoming)
        audio_note = Gtk.Label(label="Hear sound from this camera", xalign=0, wrap=True)
        audio_note.add_css_class("dim-label")
        audio_copy.append(audio_note)
        audio_row.append(audio_copy)
        self._audio_controls_slot = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            valign=Gtk.Align.CENTER,
        )
        audio_row.append(self._audio_controls_slot)
        content.append(audio_row)

        command_grid = Gtk.Grid(column_homogeneous=True, column_spacing=8, row_spacing=8)
        self._talk_control_slot = Gtk.Box(hexpand=True)
        self._light_control_slot = Gtk.Box(hexpand=True)
        self._siren_control_slot = Gtk.Box(hexpand=True)
        command_grid.attach(self._talk_control_slot, 0, 0, 1, 1)
        command_grid.attach(self._light_control_slot, 1, 0, 1, 1)
        command_grid.attach(self._siren_control_slot, 0, 1, 2, 1)
        content.append(command_grid)

        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        navigation.add_css_class("linked")
        self._inspector_history_btn = Gtk.Button(
            label="History",
            icon_name="document-open-recent-symbolic",
            hexpand=True,
            sensitive=False,
        )
        self._inspector_history_btn.connect("clicked", self._on_inspector_history)
        navigation.append(self._inspector_history_btn)
        self._inspector_settings_btn = Gtk.Button(
            label="Settings",
            icon_name="preferences-system-symbolic",
            hexpand=True,
            sensitive=False,
        )
        self._inspector_settings_btn.connect("clicked", self._on_inspector_settings)
        navigation.append(self._inspector_settings_btn)
        content.append(navigation)
        return inspector

    @staticmethod
    def _add_inspector_detail(grid: Gtk.Grid, row: int, key: str) -> Gtk.Label:
        key_label = Gtk.Label(label=key, xalign=0)
        key_label.add_css_class("dim-label")
        grid.attach(key_label, 0, row, 1, 1)
        value = Gtk.Label(label="Not available", xalign=0, wrap=True, hexpand=True)
        grid.attach(value, 1, row, 1, 1)
        return value

    @staticmethod
    def _remove_all_children(container: Gtk.Box) -> None:
        while (child := container.get_first_child()) is not None:
            container.remove(child)

    @staticmethod
    def _append_reparented(container: Gtk.Box, widget: Gtk.Widget | None) -> None:
        if widget is None:
            return
        parent = widget.get_parent()
        if parent is container:
            return
        if parent is not None and hasattr(parent, "remove"):
            parent.remove(widget)
        container.append(widget)

    def _clear_inspector_controls(self) -> None:
        if not self._show_monitoring_controls or not hasattr(self, "_audio_controls_slot"):
            return
        for container in (
            self._audio_controls_slot,
            self._talk_control_slot,
            self._light_control_slot,
            self._siren_control_slot,
        ):
            self._remove_all_children(container)

    def _select_monitoring_camera(self, device, *, reveal: bool = True) -> None:
        if not self._show_monitoring_controls:
            return
        device_id = int(device.id)
        tile = self._cards.get(device_id)
        if tile is None:
            return
        previous_tile = self._cards.get(self._selected_device_id)
        if previous_tile is not None and previous_tile is not tile:
            previous_tile.stop_talking()
        self._selected_device_id = device_id
        for candidate_id, candidate in self._cards.items():
            candidate.set_selected(candidate_id == device_id)

        self._selected_name.set_label(device_names.display_name(device))
        self._selected_model.set_label(_safe_device_text(device, "model", "Ring camera"))
        self._inspector_open_btn.set_sensitive(True)
        self._inspector_settings_btn.set_sensitive(True)
        self._inspector_history_btn.set_sensitive(self._on_open_history is not None)

        self._refresh_monitoring_inspector()
        self._clear_inspector_controls()
        self._append_reparented(self._audio_controls_slot, tile._volume_btn)
        self._append_reparented(self._audio_controls_slot, tile._volume_scale)
        self._append_reparented(self._talk_control_slot, tile._mic_btn)
        self._append_reparented(self._light_control_slot, tile._light_btn)
        self._append_reparented(self._siren_control_slot, tile._siren_btn)
        self._talk_control_slot.set_visible(tile._mic_btn.get_visible())
        self._light_control_slot.set_visible(tile._light_btn is not None)
        self._siren_control_slot.set_visible(tile._siren_btn is not None)
        if reveal and self._monitor_split.get_collapsed():
            self._monitor_split.set_show_sidebar(True)

    def _refresh_monitoring_inspector(self) -> None:
        if not self._show_monitoring_controls or self._selected_device_id is None:
            return
        tile = self._cards.get(self._selected_device_id)
        if tile is None:
            return
        session = get_live_session_manager().session_for(self._selected_device_id)
        if session is not None and session.active:
            state = getattr(session.state, "value", str(session.state))
            status = "Live" if state == "active" else "Connecting"
        else:
            status = tile._stream_state_label.get_label()
        self._selected_status.set_label(status)
        self._selected_network.set_label(camera_network_status(tile.device).tooltip)
        self._selected_power.set_label(camera_power_status(tile.device).tooltip)
        self._selected_motion.set_label(
            "Motion alerts on"
            if getattr(tile.device, "motion_detection", True)
            else "Motion detection off"
        )

    def _show_empty_monitoring_inspector(self) -> None:
        if not self._show_monitoring_controls or not hasattr(self, "_selected_name"):
            return
        self._clear_inspector_controls()
        self._selected_name.set_label("Select a camera")
        self._selected_model.set_label("Camera controls and health")
        for label in (
            self._selected_status,
            self._selected_network,
            self._selected_power,
            self._selected_motion,
        ):
            label.set_label("Not available")
        self._inspector_open_btn.set_sensitive(False)
        self._inspector_history_btn.set_sensitive(False)
        self._inspector_settings_btn.set_sensitive(False)

    def _on_inspector_toggled(self, button: Gtk.ToggleButton) -> None:
        if hasattr(self, "_monitor_split") and (
            self._monitor_split.get_show_sidebar() != button.get_active()
        ):
            self._monitor_split.set_show_sidebar(button.get_active())

    def _sync_inspector_toggle(self, split_view: Adw.OverlaySplitView, _param) -> None:
        if not split_view.get_show_sidebar():
            tile = self._selected_tile()
            if tile is not None:
                tile.stop_talking()
        if self._inspector_toggle.get_active() != split_view.get_show_sidebar():
            self._inspector_toggle.set_active(split_view.get_show_sidebar())

    def _selected_tile(self) -> CameraTile | None:
        if self._selected_device_id is None:
            return None
        return self._cards.get(self._selected_device_id)

    def _on_inspector_open(self, *_args) -> None:
        if (tile := self._selected_tile()) is not None:
            self._show_live(tile.device)

    def _on_inspector_history(self, *_args) -> None:
        if (tile := self._selected_tile()) is not None and self._on_open_history is not None:
            self._on_open_history(int(tile.device.id))

    def _on_inspector_settings(self, *_args) -> None:
        if (tile := self._selected_tile()) is not None:
            self._show_camera_settings(tile.device)

    # ------------------------------------------------------------------
    # Live monitoring controls
    # ------------------------------------------------------------------

    def _on_monitoring_play(self, *_args) -> None:
        if not self._can_start_live_monitoring():
            self._update_monitoring_buttons()
            return
        self._start_visible_monitoring_streams()
        self._update_monitoring_buttons()
        self._refresh_monitoring_inspector()

    def _on_monitoring_stop(self, *_args) -> None:
        self._monitoring_active = False
        self._stop_monitoring_streams()
        self._update_monitoring_buttons()
        self._refresh_monitoring_inspector()

    def _update_monitoring_buttons(self) -> None:
        if not self._show_monitoring_controls:
            return
        can_start = self._can_start_live_monitoring()
        self._play_btn.set_sensitive(not self._monitoring_active and can_start)
        self._play_btn.set_tooltip_text(
            "Start live monitoring" if can_start else _MAX_EXCEEDED_TOOLTIP
        )
        self._stop_btn.set_sensitive(self._monitoring_active)

    def _live_monitoring_start_volume(self) -> float:
        return 1.0 if _cfg.load().get("live_monitoring_unmute_on_start", False) else 0.0

    def _start_visible_monitoring_streams(self) -> None:
        if not self._show_monitoring_controls:
            return
        if not self._can_start_live_monitoring():
            self.stop_live_monitoring()
            return
        start_volume = self._live_monitoring_start_volume()
        for tile in self._cards.values():
            if not self._reserve_live_monitoring_stream(tile.device.id):
                _log.error(
                    "Live Monitoring stream gate denied %s after reaching max; "
                    "stopping all streams",
                    tile.device.name,
                )
                self.stop_live_monitoring()
                return
            try:
                started = tile.start_live_monitoring(
                    start_volume,
                    max_streams=self._max_live_monitoring_streams(),
                )
                if not started:
                    self._active_monitoring_stream_ids.discard(int(tile.device.id))
            except StreamLimitExceeded:
                _log.error(
                    "Live Monitoring stream manager denied %s after reaching max; "
                    "stopping all streams",
                    tile.device.name,
                )
                self.stop_live_monitoring()
                return
        self._sync_monitoring_activity()

    def _sync_monitoring_activity(self) -> None:
        manager = get_live_session_manager()
        self._active_monitoring_stream_ids = {
            device_id
            for device_id in self._cards
            if (
                (session := manager.session_for(device_id)) is not None
                and session.active
                and LIVE_MONITORING_OWNER in session.owners
            )
        }
        self._monitoring_active = bool(self._active_monitoring_stream_ids)

    def _stop_monitoring_streams(self) -> None:
        if not self._show_monitoring_controls:
            return
        for tile in self._cards.values():
            tile.stop_live_monitoring()
        self._active_monitoring_stream_ids.clear()

    def _max_live_monitoring_streams(self) -> int:
        if _cfg.load().get("live_monitoring_allow_six_streams", False):
            return _EXPERIMENTAL_LIVE_MONITORING_MAX_STREAMS
        return _DEFAULT_LIVE_MONITORING_MAX_STREAMS

    def _can_start_live_monitoring(self) -> bool:
        if not self._show_monitoring_controls:
            return True
        return len(self._current_visible_ids()) <= self._max_live_monitoring_streams()

    def _reserve_live_monitoring_stream(self, device_id: int) -> bool:
        max_streams = self._max_live_monitoring_streams()
        manager = get_live_session_manager()
        active_ids = {
            existing_id
            for existing_id in self._cards
            if (
                (session := manager.session_for(existing_id)) is not None
                and session.active
                and LIVE_MONITORING_OWNER in session.owners
            )
        }
        self._active_monitoring_stream_ids = active_ids

        # Reusing a focused session does not consume another stream slot.
        existing = manager.session_for(device_id)
        if existing is not None and existing.active:
            active_ids.add(device_id)
            return True
        if manager.active_count() >= max_streams:
            return False
        active_ids.add(device_id)
        return True

    # ------------------------------------------------------------------
    # Saved Live Monitoring layouts
    # ------------------------------------------------------------------

    def _available_device_ids(self) -> set[int]:
        return {device.id for device in self._devices}

    def _alphabetical_device_ids(self) -> list[int]:
        return [
            device.id
            for device in sorted(self._devices, key=lambda d: device_names.display_name(d))
        ]

    def _complete_order(self, order: list[int] | None = None) -> list[int]:
        available = self._available_device_ids()
        base_order = self._order if order is None else order
        ordered: list[int] = []
        seen: set[int] = set()
        for device_id in base_order:
            if device_id not in available or device_id in seen:
                continue
            ordered.append(device_id)
            seen.add(device_id)

        missing = [
            device
            for device in sorted(self._devices, key=lambda d: device_names.display_name(d))
            if device.id not in seen
        ]
        ordered.extend(device.id for device in missing)
        return ordered

    def _current_visible_ids(self) -> list[int]:
        return [
            device_id
            for device_id in self._complete_order()
            if device_id not in self._hidden_camera_ids
        ]

    def _default_visible_ids(self) -> list[int]:
        return self._alphabetical_device_ids()[: self._max_live_monitoring_streams()]

    def _is_default_live_layout(self) -> bool:
        if not self._show_monitoring_controls:
            return False
        if self._size_mode != live_layouts.DEFAULT_LAYOUT_SIZE:
            return False
        if not self._devices:
            return True
        return (
            self._complete_order() == self._alphabetical_device_ids()
            and self._current_visible_ids() == self._default_visible_ids()
        )

    def _layout_is_default_equivalent(self, layout: dict) -> bool:
        if layout.get("size") != live_layouts.DEFAULT_LAYOUT_SIZE:
            return False
        available = self._available_device_ids()
        saved_visible = [
            device_id for device_id in layout.get("visible", []) if device_id in available
        ]
        if saved_visible != self._default_visible_ids():
            return False
        return self._complete_order(layout.get("order", [])) == self._alphabetical_device_ids()

    def _live_layout_matches(self, layout: dict) -> bool:
        if self._size_mode != layout.get("size"):
            return False
        if self._complete_order(layout.get("order", [])) != self._complete_order():
            return False
        available = self._available_device_ids()
        saved_visible = {
            device_id for device_id in layout.get("visible", []) if device_id in available
        }
        return saved_visible == set(self._current_visible_ids())

    def _refresh_live_layout_state(self) -> None:
        if not self._show_monitoring_controls:
            return

        loaded_layouts = live_layouts.load_layouts()
        self._layout_records = [
            layout for layout in loaded_layouts if not self._layout_is_default_equivalent(layout)
        ]
        if self._devices and len(self._layout_records) != len(loaded_layouts):
            live_layouts.save_layouts(self._layout_records)
        self._current_saved_layout_id = None
        if self._is_default_live_layout():
            label = live_layouts.DEFAULT_LAYOUT_NAME
        else:
            matched = next(
                (layout for layout in self._layout_records if self._live_layout_matches(layout)),
                None,
            )
            if matched is None:
                label = live_layouts.next_custom_name(self._layout_records)
            else:
                self._current_saved_layout_id = matched["id"]
                label = matched["name"]

        self._current_layout_label = label
        self._layout_select_btn.set_label(label)
        if hasattr(self, "_layout_status_label"):
            count = len(self._current_visible_ids())
            noun = "camera" if count == 1 else "cameras"
            self._layout_status_label.set_label(f"{label} layout - {count} {noun}")
        is_default = label == live_layouts.DEFAULT_LAYOUT_NAME
        has_saved_match = self._current_saved_layout_id is not None
        self._save_layout_btn.set_sensitive(not is_default)
        self._rename_layout_btn.set_visible(has_saved_match)
        self._delete_layout_btn.set_visible(has_saved_match)
        self._rebuild_live_layout_menu()

    def _rebuild_live_layout_menu(self) -> None:
        if not self._show_monitoring_controls:
            return

        while (child := self._layout_menu_box.get_first_child()) is not None:
            self._layout_menu_box.remove(child)

        default_btn = Gtk.Button(label=live_layouts.DEFAULT_LAYOUT_NAME, css_classes=["flat"])
        default_btn.connect("clicked", self._on_select_default_live_layout)
        self._layout_menu_box.append(default_btn)

        for layout in self._layout_records:
            btn = Gtk.Button(label=layout["name"], css_classes=["flat"])
            btn.connect("clicked", self._on_select_saved_live_layout, layout["id"])
            self._layout_menu_box.append(btn)

    def _on_select_default_live_layout(self, *_args) -> None:
        self._layout_popover.popdown()
        self._size_mode = live_layouts.DEFAULT_LAYOUT_SIZE
        self._order = self._alphabetical_device_ids()
        self._hidden_camera_ids = self._available_device_ids() - set(self._default_visible_ids())
        self._set_size_button_state()
        self._persist_live_monitoring_state(default_order=True)
        self._populate_devices(self._devices)

    def _on_select_saved_live_layout(self, _button: Gtk.Button, layout_id: str) -> None:
        self._layout_popover.popdown()
        layout = next(
            (item for item in live_layouts.load_layouts() if item.get("id") == layout_id),
            None,
        )
        if layout is None:
            self._refresh_live_layout_state()
            return

        self._size_mode = layout.get("size", live_layouts.DEFAULT_LAYOUT_SIZE)
        if self._size_mode not in _SIZE_MODES:
            self._size_mode = live_layouts.DEFAULT_LAYOUT_SIZE

        self._order = self._complete_order(layout.get("order", []))
        visible_ids = {device_id for device_id in layout.get("visible", [])}
        available_ids = self._available_device_ids()
        self._hidden_camera_ids = available_ids - visible_ids
        self._set_size_button_state()
        self._persist_live_monitoring_state()
        self._populate_devices(self._devices)

    def _set_size_button_state(self) -> None:
        if not hasattr(self, "_size_btns"):
            return
        button = self._size_btns.get(self._size_mode)
        if button is not None and not button.get_active():
            button.set_active(True)

    def _persist_live_monitoring_state(self, *, default_order: bool = False) -> None:
        cfg = _cfg.load()
        cfg[self._grid_size_config_key] = self._size_mode
        cfg[self._camera_order_config_key] = [] if default_order else self._complete_order()
        cfg["live_monitoring_hidden_camera_ids"] = sorted(self._hidden_camera_ids)
        _cfg.save(cfg)

    def _current_live_layout_payload(self) -> tuple[str, list[int], list[int]]:
        return (
            self._size_mode,
            self._complete_order(),
            self._current_visible_ids(),
        )

    def _on_save_live_layout(self, *_args) -> None:
        if self._is_default_live_layout():
            return
        fallback_name = (
            live_layouts.next_custom_name(self._layout_records)
            if self._current_layout_label == live_layouts.DEFAULT_LAYOUT_NAME
            else self._current_layout_label
        )
        self._present_layout_name_dialog(
            heading="Save Layout",
            default_name=fallback_name,
            initial_text="",
            on_save=lambda text: self._save_live_layout(text, fallback_name),
        )

    def _save_live_layout(self, text: str, fallback_name: str) -> None:
        size, order, visible = self._current_live_layout_payload()
        if self._current_saved_layout_id is not None:
            updated = live_layouts.update_layout(
                self._current_saved_layout_id,
                name=text or fallback_name,
                size=size,
                order=order,
                visible=visible,
            )
            if updated is not None:
                self._current_saved_layout_id = updated["id"]
        else:
            saved = live_layouts.save_new_layout(
                name=text,
                fallback_name=fallback_name,
                size=size,
                order=order,
                visible=visible,
            )
            self._current_saved_layout_id = saved["id"]
        self._refresh_live_layout_state()

    def _on_rename_live_layout(self, *_args) -> None:
        if self._current_saved_layout_id is None:
            return
        current_name = self._current_layout_label
        self._present_layout_name_dialog(
            heading="Rename Layout",
            default_name=current_name,
            initial_text=current_name,
            on_save=lambda text: self._rename_live_layout(text, current_name),
        )

    def _rename_live_layout(self, text: str, current_name: str) -> None:
        if self._current_saved_layout_id is None:
            return
        if not text.strip() or text.strip() == current_name:
            return
        live_layouts.update_layout(self._current_saved_layout_id, name=text)
        self._refresh_live_layout_state()

    def _on_delete_live_layout(self, *_args) -> None:
        if self._current_saved_layout_id is None:
            return

        dialog = Adw.AlertDialog(
            heading="Delete Layout?",
            body=f'Delete saved layout "{self._current_layout_label}"?',
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_live_layout_confirmed)
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Window) else self)

    def _on_delete_live_layout_confirmed(self, _dialog, response: str) -> None:
        if response != "delete" or self._current_saved_layout_id is None:
            return
        live_layouts.delete_layout(self._current_saved_layout_id)
        self._current_saved_layout_id = None
        self._refresh_live_layout_state()

    def _present_layout_name_dialog(
        self,
        *,
        heading: str,
        default_name: str,
        initial_text: str,
        on_save,
    ) -> None:
        dialog = Adw.AlertDialog(
            heading=heading,
            body="Give this custom layout a nickname?",
        )
        entry = Gtk.Entry(
            placeholder_text=default_name,
            max_length=live_layouts.MAX_LAYOUT_NAME_LENGTH,
        )
        if initial_text:
            entry.set_text(initial_text)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def _on_response(_dialog, response: str) -> None:
            if response == "save":
                on_save(entry.get_text())

        dialog.connect("response", _on_response)
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Window) else self)

    def _rebuild_camera_filter_menu(self, devices: list) -> None:
        if not self._show_monitoring_controls:
            return

        while (child := self._camera_filter_box.get_first_child()) is not None:
            self._camera_filter_box.remove(child)

        visible_count = sum(1 for device in devices if device.id not in self._hidden_camera_ids)
        max_streams = self._max_live_monitoring_streams()
        counter = Gtk.Label(
            label=f"{visible_count}/{max_streams} Cameras Selected",
            halign=Gtk.Align.START,
            css_classes=["caption", "dim-label"],
            margin_bottom=4,
        )
        self._camera_filter_box.append(counter)

        for device in devices:
            check = Gtk.CheckButton(label=device_names.display_name(device))
            check.set_active(device.id not in self._hidden_camera_ids)
            if not check.get_active() and visible_count >= max_streams:
                check.set_sensitive(False)
            check.connect("toggled", self._on_camera_filter_toggled, device.id)
            self._camera_filter_box.append(check)

        if visible_count == len(devices):
            self._camera_filter_btn.set_label("All Cameras")
        elif visible_count == 1:
            self._camera_filter_btn.set_label("1 Camera")
        else:
            self._camera_filter_btn.set_label(f"{visible_count} Cameras")
        self._update_monitoring_buttons()

    def _on_camera_filter_toggled(self, button: Gtk.CheckButton, device_id: int) -> None:
        if button.get_active():
            if len(self._current_visible_ids()) >= self._max_live_monitoring_streams():
                button.handler_block_by_func(self._on_camera_filter_toggled)
                button.set_active(False)
                button.handler_unblock_by_func(self._on_camera_filter_toggled)
                return
            self._hidden_camera_ids.discard(device_id)
        else:
            self._hidden_camera_ids.add(device_id)

        cfg = _cfg.load()
        cfg["live_monitoring_hidden_camera_ids"] = sorted(self._hidden_camera_ids)
        _cfg.save(cfg)

        self._populate_devices(self._devices)
        self._refresh_live_layout_state()

    def _visible_devices(self, devices: list) -> list:
        if not self._show_monitoring_controls:
            return devices
        return [device for device in devices if device.id not in self._hidden_camera_ids]

    # ------------------------------------------------------------------
    # Size mode
    # ------------------------------------------------------------------

    def _on_size_toggled(self, button: Gtk.ToggleButton, mode: str) -> None:
        if not button.get_active():
            return
        self._size_mode = mode
        self._apply_size_mode_layout(mode)
        for tile in self._cards.values():
            tile.update_ratio(mode)
        cfg = _cfg.load()
        cfg[self._grid_size_config_key] = mode
        _cfg.save(cfg)
        self._refresh_live_layout_state()

    def _on_layout_toggled(self, button: Gtk.ToggleButton) -> None:
        self._layout_editing = button.get_active()
        if self._layout_editing:
            button.set_icon_name("document-save-symbolic")
            button.set_tooltip_text("Save camera layout")
        else:
            button.set_icon_name("view-list-symbolic")
            button.set_tooltip_text("Edit camera layout")
            self._save_camera_order()

        for tile in self._cards.values():
            tile.set_tooltip_text(
                "Drag to reorder camera layout" if self._layout_editing else "Open live view"
            )

    def _apply_size_mode_layout(self, mode: str) -> None:
        self._grid.set_column_homogeneous(_density_columns(mode) > 1)
        self._grid.set_row_homogeneous(False)
        self._rebuild_grid()

    # ------------------------------------------------------------------
    # Drag-and-drop reorder
    # ------------------------------------------------------------------

    def _on_reorder(self, src_id: int, dst_id: int) -> None:
        """Move the tile for *src_id* to the position of *dst_id*."""
        self._order = _reorder_ids(self._order, set(self._cards), src_id, dst_id)
        self._rebuild_grid()

    def _save_camera_order(self) -> None:
        cfg = _cfg.load()
        if self._show_monitoring_controls:
            cfg[self._camera_order_config_key] = self._complete_order()
        else:
            cfg[self._camera_order_config_key] = [did for did in self._order if did in self._cards]
        _cfg.save(cfg)
        self._refresh_live_layout_state()

    # ------------------------------------------------------------------
    # Public refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-fetch device list and repopulate the grid."""
        self._refresh_density_control_labels()
        client = get_client()
        if client is None or not client.is_authenticated:
            self.deactivate_snapshot_updates()
            self._show_signed_out_state()
            return

        self._activate_snapshot_updates(client)

        self._devices_request_generation += 1
        generation = self._devices_request_generation
        self._status_page.set_title("Loading…")
        self._status_page.set_description("")
        self._status_page.set_visible(True)
        self._scroll.set_visible(False)
        self._clear_grid()

        threading.Thread(
            target=self._fetch_and_populate,
            args=(generation,),
            daemon=True,
        ).start()

    def _show_signed_out_state(self) -> None:
        self._devices_request_generation += 1
        self._layout_editing = False
        self._layout_btn.set_active(False)
        self._clear_grid()
        self._snapshot_cache.clear()
        self._monitoring_active = False
        self._update_monitoring_buttons()
        self._show_empty_monitoring_inspector()
        self._status_page.set_title("Not signed in")
        self._status_page.set_description(
            "You are not signed into your Ring account. To view camera feeds, please sign in"
            " to your Ring account."
        )
        self._status_page.set_visible(True)
        self._scroll.set_visible(False)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_and_populate(self, generation: int) -> None:
        client = get_client()
        try:
            devices = client.refresh_devices(_SNAPSHOT_FAMILIES)
            GLib.idle_add(self._populate_devices, devices, generation)
        except Exception as exc:
            GLib.idle_add(self._show_fetch_error, str(exc), generation)

    def _populate_devices(self, devices: list, generation: int | None = None) -> bool:
        if generation is not None and generation != self._devices_request_generation:
            return GLib.SOURCE_REMOVE

        previous_selected_id = self._selected_device_id
        self._clear_grid()

        if not devices:
            self._status_page.set_title("No cameras found")
            self._status_page.set_description("No Ring cameras are linked to your account.")
            self._status_page.set_visible(True)
            self._scroll.set_visible(False)
            self._devices = []
            self._order = []
            self._show_empty_monitoring_inspector()
            self._refresh_live_layout_state()
            return GLib.SOURCE_REMOVE

        self._status_page.set_visible(False)
        self._scroll.set_visible(True)

        known = {did: i for i, did in enumerate(self._order)}
        ordered = sorted(
            devices,
            key=lambda d: (known.get(d.id, len(self._order)), device_names.display_name(d)),
        )
        self._devices = ordered
        if self._show_monitoring_controls and not self._order and not self._hidden_camera_ids:
            self._hidden_camera_ids = self._available_device_ids() - set(
                self._default_visible_ids()
            )
            self._persist_live_monitoring_state(default_order=True)
        self._rebuild_camera_filter_menu(ordered)

        visible_devices = self._visible_devices(ordered)
        if not visible_devices:
            if self._show_monitoring_controls:
                self._monitoring_active = False
                self._update_monitoring_buttons()
            self._status_page.set_title("No cameras selected")
            self._status_page.set_description(
                "Use the camera selector to choose cameras for Live Monitoring."
            )
            self._status_page.set_visible(True)
            self._scroll.set_visible(False)
            self._order = [device.id for device in ordered]
            self._show_empty_monitoring_inspector()
            self._refresh_live_layout_state()
            return GLib.SOURCE_REMOVE

        for device in visible_devices:
            tile = CameraTile(
                device,
                on_reorder=self._on_reorder,
                on_activate=self._on_tile_activated,
                on_camera_settings=self._show_camera_settings,
                is_layout_editing=lambda: self._layout_editing,
                live_monitoring=self._show_monitoring_controls,
                on_light_toggle=self._on_tile_light_toggle,
                on_siren_toggle=self._on_tile_siren_toggle,
                on_stream_status_changed=self._on_tile_stream_status_changed,
            )
            tile.set_tooltip_text(
                "Drag to reorder camera layout" if self._layout_editing else "Open live view"
            )
            tile.update_ratio(self._size_mode)
            self._cards[device.id] = tile

            cached = self._snapshot_cache.get(device.id)
            if cached is not None:
                self._set_card_snapshot(device.id, cached)

            self._queue_snapshot_load(device, restart_timer=True)

        self._order = [device.id for device in ordered]
        self._rebuild_grid()
        self._refresh_live_layout_state()
        if self._show_monitoring_controls:
            selected = next(
                (device for device in visible_devices if int(device.id) == previous_selected_id),
                visible_devices[0],
            )
            self._select_monitoring_camera(selected, reveal=False)
        if self._show_monitoring_controls and (
            self._monitoring_active or _cfg.load().get("live_monitoring_autostart", False)
        ):
            if self._can_start_live_monitoring():
                self._monitoring_active = True
                self._start_visible_monitoring_streams()
            else:
                self._monitoring_active = False
            self._update_monitoring_buttons()
            self._refresh_monitoring_inspector()

        return GLib.SOURCE_REMOVE

    def _show_fetch_error(self, message: str, generation: int | None = None) -> bool:
        if generation is not None and generation != self._devices_request_generation:
            return GLib.SOURCE_REMOVE

        self._status_page.set_title("Failed to load cameras")
        self._status_page.set_description(message)
        self._status_page.set_visible(True)
        self._scroll.set_visible(False)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Snapshot loading
    # ------------------------------------------------------------------

    def _queue_snapshot_load(
        self,
        device,
        event=None,
        *,
        restart_timer: bool = False,
    ) -> None:
        if not self._snapshot_updates_active:
            return
        device_id = int(device.id)
        generation = self._snapshot_generation
        if restart_timer:
            self._snapshot_restart_timer_ids.add(device_id)
        if device_id in self._snapshot_inflight_generations:
            if event is not None or device_id not in self._snapshot_pending_events:
                self._snapshot_pending_events[device_id] = event
            return

        self._snapshot_inflight_generations[device_id] = generation
        threading.Thread(
            target=self._load_snapshot_guarded,
            args=(device, event, generation),
            daemon=True,
        ).start()

    def _load_snapshot_guarded(self, device, event=None, generation: int = 0) -> None:
        try:
            self._load_snapshot(device, event, generation)
        finally:
            GLib.idle_add(self._finish_snapshot_load, int(device.id), generation)

    def _load_snapshot(self, device, event=None, generation: int | None = None) -> None:
        client = get_client()
        if client is None:
            return

        if self._show_monitoring_controls:
            png_bytes: bytes | None = None
            if event is not None:
                try:
                    png_bytes = client.event_preview_for_device(device, event)
                except Exception as exc:
                    _log.debug("Event preview fetch failed for %s: %s", device.name, exc)
            try:
                if not png_bytes:
                    png_bytes = client.snapshot_for_device(device)
            except Exception as exc:
                _log.debug("Snapshot fetch failed for %s: %s", device.name, exc)
            if not png_bytes:
                png_bytes = client.last_event_frame_for_device(device)
            base = bytes(png_bytes) if png_bytes else _make_dark_placeholder()
            img = _apply_preview_blur(base)
            if img:
                self._queue_card_snapshot_update(device.id, img, False, generation)
            return

        if not getattr(device, "motion_detection", True):
            png_bytes = None
            if event is not None:
                try:
                    png_bytes = client.event_preview_for_device(device, event)
                except Exception as exc:
                    _log.debug("Event preview fetch failed for %s: %s", device.name, exc)
            if not png_bytes:
                png_bytes = client.last_event_frame_for_device(device)
            base = bytes(png_bytes) if png_bytes else _make_dark_placeholder()
            img = _apply_motion_off_overlay(base)
            if img:
                self._queue_card_snapshot_update(device.id, img, True, generation)
            return

        png_bytes: bytes | None = None
        try:
            if event is not None:
                png_bytes = client.event_preview_for_device(device, event)
            if not png_bytes:
                png_bytes = client.snapshot_for_device(device)
        except Exception as exc:
            _log.debug("Snapshot fetch failed for %s: %s", device.name, exc)

        if png_bytes:
            self._queue_card_snapshot_update(device.id, bytes(png_bytes), False, generation)
        else:
            _log.debug("No snapshot available for %s", device.name)

    def _queue_card_snapshot_update(
        self,
        device_id: int,
        png_bytes: bytes,
        motion_off: bool,
        generation: int | None,
    ) -> None:
        # All callers pass a generation; _set_card_snapshot treats None as
        # "always apply", so this single call covers both.
        GLib.idle_add(self._set_card_snapshot, device_id, png_bytes, motion_off, generation)

    def _set_card_snapshot(
        self,
        device_id: int,
        png_bytes: bytes,
        motion_off: bool = False,
        generation: int | None = None,
    ) -> bool:
        if generation is not None and generation != self._snapshot_generation:
            return GLib.SOURCE_REMOVE

        self._snapshot_cache[device_id] = png_bytes
        tile = self._cards.get(device_id)
        if tile is not None:
            tile.set_snapshot(png_bytes, motion_detection_off=motion_off)
            # Native dimensions are now known — update the AspectBox ratio.
            tile.update_ratio(self._size_mode)
        return GLib.SOURCE_REMOVE

    def _finish_snapshot_load(self, device_id: int, generation: int) -> bool:
        if (
            generation != self._snapshot_generation
            or self._snapshot_inflight_generations.get(device_id) != generation
        ):
            return GLib.SOURCE_REMOVE

        self._snapshot_inflight_generations.pop(device_id, None)
        pending_marker = object()
        pending_event = self._snapshot_pending_events.pop(device_id, pending_marker)
        restart_timer = device_id in self._snapshot_restart_timer_ids
        if pending_event is not pending_marker:
            tile = self._cards.get(device_id)
            if tile is not None:
                self._queue_snapshot_load(
                    tile.device,
                    pending_event,
                    restart_timer=restart_timer,
                )
                return GLib.SOURCE_REMOVE

        self._snapshot_restart_timer_ids.discard(device_id)
        if restart_timer and device_id in self._cards:
            self._start_refresh_timer(device_id)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # FCM event → snapshot refresh
    # ------------------------------------------------------------------

    def _on_ring_event(self, event) -> None:
        if not self._snapshot_updates_active:
            return
        kind = normalized_event_kind(event)
        if kind not in ("ding", "motion"):
            return
        try:
            device_id = int(getattr(event, "doorbot_id", None))
        except (TypeError, ValueError):
            return
        if device_id not in self._cards:
            return
        tile = self._cards[device_id]
        _log.debug("Refreshing snapshot for %s after %s event", tile.device.name, kind)
        self._cancel_refresh_timer(device_id)
        self._queue_snapshot_load(tile.device, event, restart_timer=True)

    # ------------------------------------------------------------------
    # Tile activation → live stream
    # ------------------------------------------------------------------

    def _on_tile_activated(self, device) -> None:
        if self._show_monitoring_controls:
            self._select_monitoring_camera(device)
        else:
            self._show_live(device)

    def _on_tile_stream_status_changed(self, device, status: str) -> None:
        if self._selected_device_id == int(device.id):
            self._selected_status.set_label(status)
        if status in {"Stream failed", "Stream unavailable", "Stream stopped"}:
            self._sync_monitoring_activity()
            self._update_monitoring_buttons()

    def _show_live(self, device) -> None:
        tile = self._cards.get(int(device.id))
        if tile is not None:
            tile.stop_talking()
        if self._show_monitoring_controls and not _cfg.load().get(
            "live_monitoring_keep_streams_in_focus", False
        ):
            self._stop_monitoring_streams_except(device.id)
            self._update_monitoring_buttons()
        self._on_open_live_focus(
            device,
            "cameras" if self._show_monitoring_controls else "dashboard",
            self._snapshot_cache.get(device.id),
        )

    def _on_tile_light_toggle(self, device, active: bool) -> None:
        client = get_client()
        setter = getattr(device, "async_set_light", None)
        device_id = int(device.id)
        tile = self._cards.get(device_id)
        if client is None or not callable(setter):
            self._finish_tile_light_command(
                device,
                active,
                RuntimeError("Light control is unavailable"),
                tile,
            )
            return
        if tile is not None and tile._light_btn is not None:
            tile._light_btn.set_sensitive(False)
        try:
            command = setter(active)
        except Exception as exc:
            self._finish_tile_light_command(device, active, exc, tile)
            return
        device_commands.submit_observed(
            client,
            command,
            lambda error: self._finish_tile_light_command(
                device,
                active,
                error,
                tile,
                client=client,
            ),
        )

    def _finish_tile_light_command(
        self,
        device,
        requested_active: bool,
        error: Exception | None,
        expected_tile=None,
        *,
        client=None,
    ) -> bool:
        if error is None:
            device_commands.command_state.record_light_success(
                client,
                device,
                requested_active,
            )
        tile = self._cards.get(int(device.id))
        if expected_tile is not None and tile is not expected_tile:
            tile = None
        if tile is not None and tile._light_btn is not None:
            tile._light_btn.set_sensitive(True)
            if error is not None:
                tile._light_btn.handler_block_by_func(tile._on_light_toggled)
                tile._light_btn.set_active(not requested_active)
                tile._light_btn.handler_unblock_by_func(tile._on_light_toggled)
                tile._refresh_light_icon()
        if error is not None:
            action = "turn on" if requested_active else "turn off"
            self._present_device_command_error(
                f"Could not {action} light",
                device,
                error,
            )
        return GLib.SOURCE_REMOVE

    def _on_tile_siren_toggle(self, device) -> None:
        client = get_client()
        if device_commands.command_state.siren_active(client, device):
            self._set_siren(device, 0, client=client)
            return

        dialog = Adw.AlertDialog(
            heading="Activate Siren?",
            body=f"Activate siren for {device_names.display_name(device)}?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("activate", "Activate")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("activate", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_siren_confirmed, device)
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Window) else self)

    def _on_siren_confirmed(self, _dialog, response: str, device) -> None:
        if response == "activate":
            self._set_siren(device, 30)

    def _set_siren(self, device, duration: int, *, client=None) -> None:
        if client is None:
            client = get_client()
        setter = getattr(device, "async_set_siren", None)
        tile = self._cards.get(int(device.id))
        if client is None or not callable(setter):
            self._finish_tile_siren_command(
                device,
                duration,
                RuntimeError("Siren control is unavailable"),
                tile,
            )
            return
        if tile is not None and tile._siren_btn is not None:
            tile._siren_btn.set_sensitive(False)
        try:
            command = setter(duration)
        except Exception as exc:
            self._finish_tile_siren_command(device, duration, exc, tile)
            return
        device_commands.submit_observed(
            client,
            command,
            lambda error: self._finish_tile_siren_command(
                device,
                duration,
                error,
                tile,
                client=client,
            ),
        )

    def _finish_tile_siren_command(
        self,
        device,
        duration: int,
        error: Exception | None,
        expected_tile=None,
        *,
        client=None,
    ) -> bool:
        if error is None:
            device_commands.command_state.record_siren_success(client, device, duration)
        tile = self._cards.get(int(device.id))
        if expected_tile is not None and tile is not expected_tile:
            tile = None
        if tile is not None and tile._siren_btn is not None:
            tile._siren_btn.set_sensitive(True)
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

    def _show_camera_settings(self, device) -> None:
        root = self.get_root()
        app = root.get_application() if isinstance(root, Gtk.Window) else None
        window = CameraSettingsWindow(
            device,
            application=app,
            on_nickname_changed=self._refresh_device_names,
        )
        if isinstance(root, Gtk.Window):
            window.set_transient_for(root)
        window.present()

    def _refresh_device_names(self) -> None:
        for tile in self._cards.values():
            tile.refresh_display_name()

    def stop_live_monitoring(self) -> None:
        """Force-stop grid monitoring streams."""
        self._monitoring_active = False
        self._stop_monitoring_streams()
        self._update_monitoring_buttons()

    def _stop_monitoring_streams_except(self, keep_device_id: int) -> None:
        if not self._show_monitoring_controls:
            return
        for device_id, tile in self._cards.items():
            if device_id != keep_device_id:
                tile.stop_live_monitoring()
        self._active_monitoring_stream_ids = {
            device_id
            for device_id in self._active_monitoring_stream_ids
            if device_id == keep_device_id
        }

    def reattach_live_monitoring_sessions(self) -> None:
        """Reattach running sessions to grid tiles after leaving focused view."""
        if not self._show_monitoring_controls:
            return
        manager = get_live_session_manager()
        active_ids: set[int] = set()
        for device_id, tile in self._cards.items():
            session = manager.session_for(device_id)
            if session is not None and session.active and LIVE_MONITORING_OWNER in session.owners:
                tile.attach_existing_live_session()
                active_ids.add(device_id)
            else:
                tile.mark_live_monitoring_stopped()
        self._active_monitoring_stream_ids = active_ids
        self._monitoring_active = bool(active_ids)
        self._update_monitoring_buttons()

    def resume_after_focused(self) -> None:
        """Resume event updates without rebuilding and disconnecting reused streams."""
        client = get_client()
        if client is None or not client.is_authenticated:
            self.refresh()
            return
        self._activate_snapshot_updates(client)
        for device_id in self._cards:
            self._start_refresh_timer(device_id)
        self._refresh_monitoring_inspector()
        self._update_monitoring_buttons()

    def enforce_live_monitoring_limits(self) -> None:
        """Stop monitoring if current state exceeds the active stream cap."""
        if not self._show_monitoring_controls:
            return
        if not get_live_session_manager().enforce_limit(self._max_live_monitoring_streams()):
            self._monitoring_active = False
            self._active_monitoring_stream_ids.clear()
            self._update_monitoring_buttons()
            return
        manager = get_live_session_manager()
        active_ids = {
            device_id
            for device_id in self._cards
            if (
                (session := manager.session_for(device_id)) is not None
                and session.active
                and LIVE_MONITORING_OWNER in session.owners
            )
        }
        self._active_monitoring_stream_ids = active_ids
        self._monitoring_active = bool(active_ids)
        if len(active_ids) > self._max_live_monitoring_streams() or (
            self._monitoring_active and not self._can_start_live_monitoring()
        ):
            _log.warning("Live Monitoring exceeded active max; stopping all streams")
            self.stop_live_monitoring()
        else:
            self._update_monitoring_buttons()

    def on_page_hidden(self) -> None:
        """Apply page-exit live stream policy before navigating away."""
        self.deactivate_snapshot_updates()
        for tile in self._cards.values():
            tile.stop_talking()
        if self._show_monitoring_controls and not _cfg.load().get(
            "live_monitoring_continue_on_page_exit", False
        ):
            self.stop_live_monitoring()

    # ------------------------------------------------------------------
    # Fallback refresh timers
    # ------------------------------------------------------------------

    def _start_refresh_timer(self, device_id: int) -> None:
        if not self._snapshot_updates_active:
            return
        self._cancel_refresh_timer(device_id)
        source_id = GLib.timeout_add_seconds(30, self._fallback_refresh, device_id)
        self._refresh_timers[device_id] = source_id

    def _cancel_refresh_timer(self, device_id: int) -> None:
        source_id = self._refresh_timers.pop(device_id, None)
        if source_id is not None:
            GLib.source_remove(source_id)

    def _cancel_all_refresh_timers(self) -> None:
        for source_id in self._refresh_timers.values():
            GLib.source_remove(source_id)
        self._refresh_timers.clear()

    def _fallback_refresh(self, device_id: int) -> bool:
        if not self._snapshot_updates_active:
            self._refresh_timers.pop(device_id, None)
            return GLib.SOURCE_REMOVE
        tile = self._cards.get(device_id)
        if tile is None:
            self._refresh_timers.pop(device_id, None)
            return GLib.SOURCE_REMOVE
        _log.debug("Fallback snapshot refresh for %s", tile.device.name)
        self._refresh_timers.pop(device_id, None)  # clear stale id before re-arming
        self._queue_snapshot_load(tile.device, restart_timer=True)
        return GLib.SOURCE_REMOVE

    def _activate_snapshot_updates(self, client) -> None:
        if self._event_client is not client:
            if self._event_client is not None:
                self._event_client.remove_event_callback(self._on_ring_event)
            client.add_event_callback(self._on_ring_event)
            self._event_client = client
        self._snapshot_updates_active = True

    def deactivate_snapshot_updates(self) -> None:
        """Stop hidden-page timers, event callbacks, and stale snapshot results."""
        self._snapshot_updates_active = False
        if self._event_client is not None:
            self._event_client.remove_event_callback(self._on_ring_event)
            self._event_client = None
        self._devices_request_generation += 1
        self._snapshot_generation += 1
        self._cancel_all_refresh_timers()
        self._snapshot_inflight_generations.clear()
        self._snapshot_pending_events.clear()
        self._snapshot_restart_timer_ids.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_grid(self) -> None:
        self._snapshot_generation += 1
        self._cancel_all_refresh_timers()
        self._snapshot_inflight_generations.clear()
        self._snapshot_pending_events.clear()
        self._snapshot_restart_timer_ids.clear()
        self._clear_inspector_controls()
        for tile in self._cards.values():
            tile.cleanup()
        self._cards.clear()
        self._selected_device_id = None
        self._active_monitoring_stream_ids.clear()
        while (child := self._grid.get_first_child()) is not None:
            self._grid.remove(child)
        self._show_empty_monitoring_inspector()

    def _rebuild_grid(self) -> None:
        while (child := self._grid.get_first_child()) is not None:
            self._grid.remove(child)

        children_per_line = _density_columns(self._size_mode)
        ordered_ids = [did for did in self._order if did in self._cards]

        for index, device_id in enumerate(ordered_ids):
            tile = self._cards[device_id]
            tile.update_ratio(self._size_mode)
            self._grid.attach(tile, index % children_per_line, index // children_per_line, 1, 1)
        self._update_tile_compactness(self._scroll.get_allocated_width())

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        self._update_tile_compactness(self._scroll.get_allocated_width())

    def _update_tile_compactness(self, grid_width: int) -> None:
        if grid_width <= 0:
            return
        columns = _density_columns(self._size_mode)
        available = max(1, grid_width - 4 - (16 * max(0, columns - 1)))
        compact = self._size_mode == "small" or available / columns < 180
        for tile in self._cards.values():
            tile.set_compact_presentation(compact)
