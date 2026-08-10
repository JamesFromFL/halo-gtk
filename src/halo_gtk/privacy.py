"""Privacy-focused local data cleanup helpers."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from halo_gtk import storage_paths

CACHE_DIR = storage_paths.cache_dir()
STATE_DIR = storage_paths.state_dir()
PREVIEW_CACHE_NAMES = ("previews", "thumbnails")
LOG_GLOB = "halo-gtk.log*"


def clear_preview_cache() -> int:
    """Delete local cached preview/thumbnail files and return removed item count."""
    removed = 0
    for name in PREVIEW_CACHE_NAMES:
        removed += _remove_path(CACHE_DIR / name)
    return removed


def clear_debug_logs() -> int:
    """Truncate Halo debug logs and return the number of log files touched."""
    touched = 0
    active_paths: set[Path] = set()

    for handler in logging.getLogger().handlers:
        base_filename = getattr(handler, "baseFilename", None)
        if not base_filename:
            continue

        path = Path(base_filename)
        if not _is_halo_log(path):
            continue

        handler.acquire()
        try:
            handler.flush()
            stream = getattr(handler, "stream", None)
            if stream is not None:
                stream.seek(0)
                stream.truncate()
            else:
                path.write_text("")
            active_paths.add(path.resolve())
            touched += 1
        finally:
            handler.release()

    for path in STATE_DIR.glob(LOG_GLOB):
        if not path.is_file() or path.resolve() in active_paths:
            continue
        path.write_text("")
        touched += 1

    return touched


def _remove_path(path: Path) -> int:
    if not path.exists():
        return 0

    if path.is_file() or path.is_symlink():
        path.unlink()
        return 1

    count = 1 + sum(1 for _ in path.rglob("*"))
    shutil.rmtree(path)
    return count


def _is_halo_log(path: Path) -> bool:
    return path.name.startswith("halo-gtk.log") and path.parent == STATE_DIR
