"""User autostart integration."""

from __future__ import annotations

import shutil
from pathlib import Path

from halo_gtk import APP_ID, storage_paths
from halo_gtk.atomic_io import atomic_write_text

AUTOSTART_DIR = storage_paths.autostart_dir()
AUTOSTART_FILE = AUTOSTART_DIR / f"{APP_ID}.desktop"


def is_enabled() -> bool:
    return AUTOSTART_FILE.exists()


def set_enabled(enabled: bool, *, background: bool = False) -> None:
    if enabled:
        _write_desktop_file(background=background)
    else:
        AUTOSTART_FILE.unlink(missing_ok=True)


def _write_desktop_file(*, background: bool) -> None:
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    exec_path = shutil.which("halo-gtk") or str(Path.home() / ".local" / "bin" / "halo-gtk")
    # Quote the executable per the Desktop Entry spec so an install path with
    # spaces doesn't silently break the Exec line.
    quoted = '"' + exec_path.replace('"', '\\"') + '"'
    exec_command = f"{quoted} --background" if background else quoted
    content = "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=Halo",
            "Comment=Start Halo",
            f"Exec={exec_command}",
            f"Icon={APP_ID}",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "",
        ]
    )
    atomic_write_text(
        AUTOSTART_FILE,
        content,
        prefix=f".{AUTOSTART_FILE.name}-",
        mode=0o644,
    )
