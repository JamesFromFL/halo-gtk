"""Packaged icon lookup with a small theme-aware exception."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")

from gi.repository import Adw, Gio  # noqa: E402

ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"


def icon_path(category: str, filename: str) -> Path:
    """Return a packaged icon path for *category* and *filename*."""
    return ICON_DIR / category / filename


def theme_suffix() -> str:
    """Return the themed icon suffix matching the current Adwaita style."""
    if Gio.Application.get_default() is None:
        return "dark-theme"
    manager = Adw.StyleManager.get_default()
    if manager is None:
        return "dark-theme"
    return "dark-theme" if manager.get_dark() else "light-theme"


def themed_icon_path(category: str, stem: str) -> Path:
    """Return a packaged icon with a dark/light suffix."""
    return icon_path(category, f"{stem}-{theme_suffix()}.png")


def hardwired_power_icon_path() -> Path:
    """Return the hardwired power icon variant matching the current Adwaita style."""
    return themed_icon_path("power", "power-hardwired")


def connect_theme_changed(callback: Callable[[], None]) -> int:
    """Run *callback* when Adwaita switches between dark and light styles."""
    if Gio.Application.get_default() is None:
        return 0
    manager = Adw.StyleManager.get_default()
    if manager is None:
        return 0
    return manager.connect("notify::dark", lambda *_: callback())


def disconnect_theme_changed(handler_id: int | None) -> None:
    """Disconnect a handler returned by :func:`connect_theme_changed`."""
    if handler_id is None:
        return
    if Gio.Application.get_default() is None:
        return
    manager = Adw.StyleManager.get_default()
    if manager is None:
        return
    if manager.handler_is_connected(handler_id):
        manager.disconnect(handler_id)
