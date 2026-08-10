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
            hexpand=True,
            vexpand=True,
        )
        self._build_ui()

    def _build_ui(self) -> None:
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            hexpand=True,
            vexpand=True,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )

        icon = Gtk.Image.new_from_icon_name("security-high-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("dim-label")
        content.append(icon)

        title = Gtk.Label(
            label="Ring Alarm isn't available in Halo yet",
            justify=Gtk.Justification.CENTER,
            wrap=True,
        )
        title.add_css_class("title-2")
        content.append(title)

        description = Gtk.Label(
            label=(
                "Halo cannot arm, disarm, or monitor your Ring Alarm. "
                "Use the Ring app for alarm control and emergency alerts."
            ),
            justify=Gtk.Justification.CENTER,
            wrap=True,
            max_width_chars=64,
        )
        description.add_css_class("dim-label")
        content.append(description)

        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=520)
        clamp.set_child(content)
        self.append(clamp)

    def refresh(self) -> None:
        return
