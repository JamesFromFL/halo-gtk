"""Configured filesystem destinations for saved Halo media."""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

import gi

gi.require_version("GioUnix", "2.0")

from gi.repository import Gio, GioUnix  # noqa: E402

from halo_gtk import config as _config  # noqa: E402

DEFAULT_SNAPSHOT_DIR = Path.home() / "Pictures" / "halo-gtk"
DEFAULT_VIDEO_DIR = Path.home() / "Videos" / "halo-gtk"
_FILE_MANAGER_DESKTOP_IDS = (
    "org.gnome.Nautilus.desktop",
    "org.kde.dolphin.desktop",
    "org.xfce.thunar.desktop",
    "thunar.desktop",
    "pcmanfm.desktop",
    "pcmanfm-qt.desktop",
    "nemo.desktop",
    "caja.desktop",
)


def snapshot_dir(camera_name: str | None = None) -> Path:
    """Return the configured snapshot directory, optionally scoped to a camera."""
    cfg = _config.load()
    path = _configured_path(cfg.get("snapshot_dir"), DEFAULT_SNAPSHOT_DIR)
    return _camera_scoped_path(path, camera_name, cfg)


def video_dir(camera_name: str | None = None) -> Path:
    """Return the configured recording download directory, optionally scoped to a camera."""
    cfg = _config.load()
    path = _configured_path(cfg.get("video_dir"), DEFAULT_VIDEO_DIR)
    return _camera_scoped_path(path, camera_name, cfg)


def should_open_after_save() -> bool:
    return bool(_config.load().get("open_folder_after_save", False))


def write_unique_bytes(directory: Path, stem: str, suffix: str, payload: bytes) -> Path:
    """Write *payload* to a new file without replacing an existing save."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _validate_filename_parts(stem, suffix)

    for index in range(1, 1_000_000):
        path = directory / _numbered_filename(stem, suffix, index)
        try:
            with path.open("xb") as handle:
                _write_all(handle, payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            continue
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        return path

    raise OSError("Could not allocate a unique media filename")


def open_folder(path: Path) -> None:
    folder = path.resolve()
    uri = folder.as_uri()
    for desktop_id in _FILE_MANAGER_DESKTOP_IDS:
        app = GioUnix.DesktopAppInfo.new(desktop_id)
        if app is None:
            continue
        app.launch_uris([uri], None)
        return
    Gio.AppInfo.launch_default_for_uri(uri, None)


def _configured_path(value, default: Path) -> Path:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else default


def _camera_scoped_path(path: Path, camera_name: str | None, cfg: dict) -> Path:
    if camera_name and cfg.get("create_camera_subfolders", False):
        return path / _safe_folder_name(camera_name)
    return path


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:\0]+", "-", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Camera"


def _validate_filename_parts(stem: str, suffix: str) -> None:
    if not stem or Path(stem).name != stem:
        raise ValueError("Media filename stem must be a non-empty basename")
    if not suffix.startswith(".") or Path(suffix).name != suffix:
        raise ValueError("Media filename suffix must be a file extension")


def _numbered_filename(stem: str, suffix: str, index: int) -> str:
    return f"{stem}{suffix}" if index == 1 else f"{stem}_{index}{suffix}"


def _write_all(handle, payload: bytes) -> None:
    handle.write(payload)
