"""Reusable clipped paintable view with zoom and pan support."""

from __future__ import annotations

import io
import logging
from contextlib import suppress

import gi

gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Graphene, Gtk  # noqa: E402

_log = logging.getLogger(__name__)

ZOOM_MIN = 1.0
ZOOM_MAX = 2.5
ZOOM_STEP = 0.1


class ZoomPaintableView(Gtk.Widget):
    """Clipped paintable viewer with local zoom/pan that never resizes parents."""

    def __init__(self) -> None:
        super().__init__(hexpand=True, vexpand=True)
        self._paintable = None
        self._paintable_handlers: list[int] = []
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0

    def set_paintable(self, paintable) -> None:
        self._disconnect_paintable()
        self._paintable = paintable
        self._connect_paintable()
        self.queue_draw()

    def do_unroot(self) -> None:
        self._disconnect_paintable()
        self._paintable = None
        Gtk.Widget.do_unroot(self)

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))
        self._clamp_pan()
        self.queue_draw()

    def reset_pan(self) -> None:
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.queue_draw()

    def pan_origin(self) -> tuple[float, float]:
        return self._pan_x, self._pan_y

    def set_pan(self, pan_x: float, pan_y: float) -> None:
        self._pan_x = pan_x
        self._pan_y = pan_y
        self._clamp_pan()
        self.queue_draw()

    def _connect_paintable(self) -> None:
        if self._paintable is None or not hasattr(self._paintable, "connect"):
            return
        for signal_name in ("invalidate-contents", "invalidate-size"):
            try:
                handler_id = self._paintable.connect(signal_name, self._on_paintable_invalidated)
            except TypeError:
                continue
            self._paintable_handlers.append(handler_id)

    def _disconnect_paintable(self) -> None:
        if self._paintable is None or not hasattr(self._paintable, "disconnect"):
            self._paintable_handlers.clear()
            return
        for handler_id in self._paintable_handlers:
            with suppress(TypeError):
                self._paintable.disconnect(handler_id)
        self._paintable_handlers.clear()

    def _on_paintable_invalidated(self, *_args) -> None:
        self._clamp_pan()
        self.queue_draw()

    def source_viewport(
        self, source_width: int, source_height: int
    ) -> tuple[float, float, float, float]:
        """Return the visible source-space rectangle for the current zoom/pan."""
        source_width = max(1, int(source_width))
        source_height = max(1, int(source_height))
        width = max(1, self.get_width())
        height = max(1, self.get_height())
        scale = min(width / source_width, height / source_height) * self._zoom
        scaled_width = source_width * scale
        scaled_height = source_height * scale
        x = (width - scaled_width) / 2 + self._pan_x
        y = (height - scaled_height) / 2 + self._pan_y

        left = max(0.0, (0.0 - x) / scale)
        top = max(0.0, (0.0 - y) / scale)
        right = min(float(source_width), (width - x) / scale)
        bottom = min(float(source_height), (height - y) / scale)
        if right <= left or bottom <= top:
            return 0.0, 0.0, float(source_width), float(source_height)
        return left, top, right, bottom

    def zoomed_frame_png(self, frame_png: bytes) -> bytes | None:
        """Crop a PNG frame to the current viewport and resize to frame dimensions."""
        if self._zoom <= 1.0:
            return frame_png
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(frame_png)).convert("RGB")
            width, height = image.size
            left, top, right, bottom = self.source_viewport(width, height)
            cropped = image.crop((round(left), round(top), round(right), round(bottom)))
            if cropped.size != image.size:
                cropped = cropped.resize(image.size, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            cropped.save(out, format="PNG")
            return out.getvalue()
        except Exception as exc:
            _log.debug("Zoomed frame capture failed: %s", exc)
            return None

    def _scaled_bounds(self) -> tuple[float, float]:
        paintable = self._paintable
        width = max(1, self.get_width())
        height = max(1, self.get_height())
        if paintable is None:
            return float(width), float(height)

        source_width = paintable.get_intrinsic_width()
        source_height = paintable.get_intrinsic_height()
        if source_width <= 0 or source_height <= 0:
            source_width = width
            source_height = height

        scale = min(width / source_width, height / source_height)
        return source_width * scale * self._zoom, source_height * scale * self._zoom

    def _clamp_pan(self) -> None:
        width = max(1, self.get_width())
        height = max(1, self.get_height())
        scaled_width, scaled_height = self._scaled_bounds()
        max_x = max(0.0, (scaled_width - width) / 2)
        max_y = max(0.0, (scaled_height - height) / 2)
        self._pan_x = max(-max_x, min(max_x, self._pan_x))
        self._pan_y = max(-max_y, min(max_y, self._pan_y))

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.Widget.do_size_allocate(self, width, height, baseline)
        self._clamp_pan()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        paintable = self._paintable
        width = max(1, self.get_width())
        height = max(1, self.get_height())
        rect = Graphene.Rect().init(0, 0, width, height)
        snapshot.push_clip(rect)
        if paintable is not None:
            scaled_width, scaled_height = self._scaled_bounds()
            x = (width - scaled_width) / 2 + self._pan_x
            y = (height - scaled_height) / 2 + self._pan_y
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x, y))
            paintable.snapshot(snapshot, scaled_width, scaled_height)
            snapshot.restore()
        snapshot.pop()
