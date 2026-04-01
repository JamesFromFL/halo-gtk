"""Home page — app branding and information panel."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from halo_gtk import APP_ID  # noqa: E402

_ICON_PNG_FALLBACK = (
    __file__,  # resolved at runtime relative to package location
)


class HomePage(Gtk.Box):
    """Centered branding shown when the app first opens."""

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            spacing=20,
            margin_top=48,
            margin_bottom=48,
            margin_start=32,
            margin_end=32,
        )
        self._build_ui()

    def _build_ui(self) -> None:
        title = Gtk.Label(
            label="Halo",
            css_classes=["title-1"],
            halign=Gtk.Align.CENTER,
        )
        self.append(title)

        icon = self._make_icon()
        self.append(icon)

        description = Gtk.Label(
            label="A Linux native desktop client for Ring home security.",
            css_classes=["body"],
            halign=Gtk.Align.CENTER,
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=50,
        )
        self.append(description)

        notice_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            halign=Gtk.Align.CENTER,
        )
        self.append(notice_box)

        notice = Gtk.Label(
            label=(
                "Halo is in early development. If you find any issues or have any suggestions,"
                " please report them on the project's GitHub page."
            ),
            css_classes=["dim-label", "caption"],
            halign=Gtk.Align.CENTER,
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=56,
        )
        notice_box.append(notice)

        github_btn = Gtk.LinkButton(
            uri="https://github.com/JamesFromFL/halo-gtk",
            label="GitHub page",
            halign=Gtk.Align.CENTER,
            css_classes=["caption"],
        )
        notice_box.append(github_btn)

    @staticmethod
    def _make_icon() -> Gtk.Image:
        """Load the app icon from the hicolor theme, falling back to the bundled PNG."""
        from pathlib import Path

        # Prefer the installed theme icon so it scales with the icon theme.
        icon = Gtk.Image.new_from_icon_name(APP_ID)
        icon.set_pixel_size(128)
        icon.set_halign(Gtk.Align.CENTER)

        # If the theme doesn't have the icon yet (dev/uninstalled run), load the
        # PNG directly from the source tree relative to this file.
        display = icon.get_display()
        theme = Gtk.IconTheme.get_for_display(display) if display is not None else None
        if theme is not None and not theme.has_icon(APP_ID):
            pkg_dir = Path(__file__).parent.parent.parent  # src/halo_gtk/../../.. = repo root
            png = pkg_dir / "data" / "icons" / "hicolor" / "128x128" / "apps" / f"{APP_ID}.png"
            if png.exists():
                icon = Gtk.Image.new_from_file(str(png))
                icon.set_pixel_size(128)
                icon.set_halign(Gtk.Align.CENTER)

        return icon
