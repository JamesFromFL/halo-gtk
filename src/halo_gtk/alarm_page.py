"""Ring Alarm page placeholder."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk  # noqa: E402


class AlarmPage(Gtk.Box):
    """Dedicated area for future Ring Alarm support."""

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=24,
            margin_bottom=32,
            margin_start=24,
            margin_end=24,
            hexpand=True,
            vexpand=True,
        )
        self._build_ui()

    def _build_ui(self) -> None:
        heading = Gtk.Label(
            label="Alarm",
            css_classes=["title-1"],
            halign=Gtk.Align.START,
        )
        self.append(heading)

        status = Adw.StatusPage(
            icon_name="security-high-symbolic",
            title="Ring Alarm Support",
            description=(
                "Alarm state, sensors, and confirmed arm/disarm controls will live here "
                "after the alarm backend is added."
            ),
            vexpand=True,
        )
        self.append(status)

    def refresh(self) -> None:
        return
