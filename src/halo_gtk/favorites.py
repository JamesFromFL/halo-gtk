"""Local Favorites archive for preserved Ring event clips."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import re
import tempfile
import threading
import time
import weakref
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from halo_gtk import device_names, storage_paths
from halo_gtk.atomic_io import atomic_write_json
from halo_gtk.http_download import async_download_https, download_https

DATA_DIR = storage_paths.data_dir()
FAVORITES_DIR = DATA_DIR / "favorites"
FAVORITE_VIDEOS_DIR = FAVORITES_DIR / "videos"
FAVORITE_THUMBNAILS_DIR = FAVORITES_DIR / "thumbnails"
FAVORITES_INDEX = FAVORITES_DIR / "index.json"

# Cache the set of favorited ids (keyed by index mtime) for the cheap per-row
# is_favorited() check while building history lists. Unlike index_entries() this
# does not prune missing media. Invalidated whenever the index is rewritten.
_ids_lock = threading.Lock()
_cached_ids: set[str] | None = None
_cached_ids_key: tuple[str, float | None] | None = None
_index_lock = threading.RLock()
_async_locks = weakref.WeakKeyDictionary()
_async_locks_guard = threading.Lock()

_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}

_snapshot_lock = threading.Lock()
_snapshot_key: str | None = None
_snapshot_events: tuple = ()
_snapshot_ids: frozenset[str] = frozenset()
_snapshot_updated = 0.0
_snapshot_loading = False
_snapshot_callbacks: list = []
_snapshot_generation = 0
_snapshot_retry_after = 0.0


def cached_favorite_events() -> list[dict[str, Any]]:
    """Return copied immutable-cache entries without filesystem work on GTK."""
    request_reconcile()
    with _snapshot_lock:
        return [dict(event) for event in _snapshot_events]


def cached_is_favorited(event: dict[str, Any]) -> bool:
    request_reconcile()
    with _snapshot_lock:
        return favorite_id_for_event(event) in _snapshot_ids


def request_reconcile(callback=None, *, force: bool = False) -> None:
    """Coalesce background scans; a mutation during a scan forces a fresh pass."""
    global _snapshot_key, _snapshot_loading, _snapshot_events, _snapshot_ids
    global _snapshot_updated, _snapshot_generation, _snapshot_retry_after
    key = str(FAVORITES_INDEX)
    ready = []
    start = False
    with _snapshot_lock:
        if _snapshot_key != key:
            _snapshot_key, _snapshot_events, _snapshot_ids = key, (), frozenset()
            _snapshot_updated = 0.0
            _snapshot_retry_after = 0.0
            _snapshot_generation += 1
        if force:
            _snapshot_generation += 1
        if callback is not None:
            reference = (
                weakref.WeakMethod(callback) if getattr(callback, "__self__", None) else callback
            )
            if reference not in _snapshot_callbacks:
                _snapshot_callbacks.append(reference)
        if _snapshot_loading:
            return
        now = time.monotonic()
        if not force and now - _snapshot_updated < 2:
            ready = list(_snapshot_callbacks)
            _snapshot_callbacks.clear()
        elif now < _snapshot_retry_after:
            return
        else:
            _snapshot_loading = True
            start = True
    if ready:
        _dispatch_snapshot_callbacks(ready)
        return
    if not start:
        return

    def worker():
        global _snapshot_events, _snapshot_ids, _snapshot_updated, _snapshot_loading
        global _snapshot_retry_after
        while True:
            with _snapshot_lock:
                generation = _snapshot_generation
            try:
                events = favorite_events()
                snapshot = tuple(MappingProxyType(dict(event)) for event in events)
                ids = frozenset(
                    str(event["_favorite_id"]) for event in events if event.get("_favorite_id")
                )
            except Exception:
                # Keep the last good snapshot on transient filesystem failures.
                snapshot = ids = None
            with _snapshot_lock:
                if generation != _snapshot_generation:
                    continue
                now = time.monotonic()
                if snapshot is not None:
                    _snapshot_events, _snapshot_ids = snapshot, ids
                    _snapshot_updated = now
                    _snapshot_retry_after = 0.0
                else:
                    _snapshot_retry_after = now + 2
                _snapshot_loading = False
                callbacks = list(_snapshot_callbacks)
                _snapshot_callbacks.clear()
            _dispatch_snapshot_callbacks(callbacks)
            return

    try:
        threading.Thread(target=worker, name="halo-favorites-cache", daemon=True).start()
    except RuntimeError:
        with _snapshot_lock:
            _snapshot_loading = False
            _snapshot_retry_after = time.monotonic() + 2


def _dispatch_snapshot_callbacks(callbacks):
    if not callbacks:
        return
    from gi.repository import GLib

    for reference in callbacks:

        def deliver(reference=reference):
            callback = reference() if isinstance(reference, weakref.WeakMethod) else reference
            if callback is not None:
                callback()
            return False

        GLib.idle_add(deliver)


def favorite_id_for_event(event: dict[str, Any]) -> str | None:
    """Return the stable favorite id for a Ring history event."""
    event_id = event.get("id")
    device_id = _event_device_id(event)
    if event_id is None or device_id is None:
        return None
    return f"{device_id}-{event_id}"


def is_favorited(event: dict[str, Any]) -> bool:
    """Return whether *event* has a local favorite archive (cheap index check)."""
    favorite_id = favorite_id_for_event(event)
    if favorite_id is None:
        return False
    return favorite_id in _favorited_ids()


def _favorited_ids() -> set[str]:
    """Return the indexed favorite ids, cached by index mtime.

    Does not prune missing media (that is index_entries()'s job) — this is the
    per-row membership check used while building history lists.
    """
    try:
        mtime = FAVORITES_INDEX.stat().st_mtime
    except OSError:
        mtime = None
    key = (str(FAVORITES_INDEX), mtime)
    global _cached_ids, _cached_ids_key
    with _ids_lock:
        if _cached_ids is not None and _cached_ids_key == key:
            return set(_cached_ids)
        data = _read_index()
        favorites_list = data.get("favorites", []) if isinstance(data, dict) else []
        ids = {
            str(entry.get("favorite_id"))
            for entry in favorites_list
            if isinstance(entry, dict) and entry.get("favorite_id")
        }
        _cached_ids = ids
        _cached_ids_key = key
        return set(_cached_ids)


def _invalidate_favorite_ids_cache() -> None:
    global _cached_ids, _cached_ids_key
    with _ids_lock:
        _cached_ids = None
        _cached_ids_key = None


def index_entries() -> list[dict[str, Any]]:
    """Return valid indexed favorites, pruning entries whose media disappeared."""
    with _index_transaction():
        return _index_entries_locked()


def _index_entries_locked() -> list[dict[str, Any]]:
    data = _read_index()
    entries = data.get("favorites", []) if isinstance(data, dict) else []
    valid_entries = []
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            changed = True
            continue
        try:
            video_path = Path(str(entry.get("video_path", ""))).expanduser()
            present = video_path.is_file()
        except (OSError, ValueError):
            present = False
        if present:
            valid_entries.append(entry)
        else:
            changed = True

    if changed:
        _write_index(valid_entries)

    return valid_entries


def favorite_events() -> list[dict[str, Any]]:
    """Return indexed favorites plus local video files with missing metadata."""
    indexed = [_entry_to_event(entry) for entry in index_entries()]
    indexed_paths = {
        Path(str(entry.get("_local_video_path", ""))).resolve()
        for entry in indexed
        if entry.get("_local_video_path")
    }

    orphaned = []
    if FAVORITE_VIDEOS_DIR.is_dir():
        for path in sorted(FAVORITE_VIDEOS_DIR.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in _VIDEO_EXTENSIONS:
                continue
            if path.resolve() in indexed_paths:
                continue
            orphaned.append(_missing_metadata_event(path))

    return sorted(indexed + orphaned, key=_event_sort_key, reverse=True)


def add_favorite(
    event: dict[str, Any],
    recording_url: str,
    *,
    thumbnail_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Download *recording_url* into the local archive and index it."""
    staging_path = _new_staging_path(event)
    try:
        download_https(recording_url, staging_path)
        return _publish_favorite(event, staging_path, thumbnail_bytes)
    finally:
        staging_path.unlink(missing_ok=True)


async def async_add_favorite(event, recording_url, *, thumbnail_bytes=None, control=None):
    """App-owned archive save with cancellable transport and a final index fence."""
    staging_path = _new_staging_path(event)
    try:
        await async_download_https(recording_url, staging_path, control=control, publish=False)
        async with _async_index_transaction(control):
            if control is not None:
                control.begin_commit()
            entry = _write_favorite_locked(event, staging_path, thumbnail_bytes)
        request_reconcile(force=True)
        return entry
    finally:
        staging_path.unlink(missing_ok=True)


def _new_staging_path(event) -> Path:
    if favorite_id_for_event(event) is None:
        raise ValueError("Cannot favorite an event without device and event ids")
    FAVORITE_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    FAVORITE_THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=FAVORITE_VIDEOS_DIR, prefix=".download-", suffix=".part", delete=False
    ) as temporary:
        return Path(temporary.name)


def _publish_favorite(event, staging_path, thumbnail_bytes):
    with _index_transaction():
        return _write_favorite_locked(event, staging_path, thumbnail_bytes)


def _write_favorite_locked(event, staging_path, thumbnail_bytes):
    favorite_id = favorite_id_for_event(event)
    video_path = FAVORITE_VIDEOS_DIR / _video_filename(event)
    staging_path.replace(video_path)
    thumbnail_path = ""
    if thumbnail_bytes:
        thumb = FAVORITE_THUMBNAILS_DIR / f"{video_path.stem}.png"
        thumb.write_bytes(thumbnail_bytes)
        thumbnail_path = str(thumb)
    entry = {
        "favorite_id": favorite_id,
        "event_id": str(event.get("id")),
        "device_id": str(_event_device_id(event)),
        "camera_name": _event_camera_name(event),
        "event_kind": str(event.get("kind") or "event"),
        "created_at": _event_created_at(event).isoformat(),
        "video_path": str(video_path),
        "thumbnail_path": thumbnail_path,
        "filename": video_path.name,
    }
    entries = [item for item in _index_entries_locked() if item.get("favorite_id") != favorite_id]
    entries.append(entry)
    _write_index(entries)
    return entry


def _removal_id(event_or_id):
    return (event_or_id if isinstance(event_or_id, str) else event_or_id.get("_favorite_id")) or (
        favorite_id_for_event(event_or_id) if isinstance(event_or_id, dict) else None
    )


def remove_favorite(event_or_id: dict[str, Any] | str) -> bool:
    """Remove a favorite from the index and delete its archived files."""
    favorite_id = _removal_id(event_or_id)
    if favorite_id is None:
        return False
    with _index_transaction():
        return _remove_favorite_locked(favorite_id)


async def async_remove_favorite(event_or_id, *, control=None):
    favorite_id = _removal_id(event_or_id)
    if favorite_id is None:
        return False
    async with _async_index_transaction(control):
        if control is not None:
            control.begin_commit()
        removed = _remove_favorite_locked(favorite_id)
    request_reconcile(force=True)
    return removed


def _remove_favorite_locked(favorite_id):
    removed = False
    remaining = []
    for entry in _index_entries_locked():
        if entry.get("favorite_id") != favorite_id:
            remaining.append(entry)
            continue
        removed = True
        _delete_managed_file(entry.get("video_path"), FAVORITE_VIDEOS_DIR)
        _delete_managed_file(entry.get("thumbnail_path"), FAVORITE_THUMBNAILS_DIR)
    if removed:
        _write_index(remaining)
    return removed


@contextlib.contextmanager
def _index_transaction():
    """Blocking API for background readers and legacy synchronous callers."""
    with _index_lock:
        FAVORITES_DIR.mkdir(parents=True, exist_ok=True)
        with (FAVORITES_DIR / ".index.lock").open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextlib.asynccontextmanager
async def _async_index_transaction(control=None):
    """Yield while waiting for thread/process locks; never stall other saves."""
    loop = asyncio.get_running_loop()
    with _async_locks_guard:
        reference = _async_locks.get(loop)
        lock = reference() if reference is not None else None
        if lock is None:
            lock = asyncio.Lock()
            _async_locks[loop] = weakref.ref(lock)
    async with lock:
        while not _index_lock.acquire(blocking=False):
            if control is not None:
                control.checkpoint()
            await asyncio.sleep(0.01)
        try:
            FAVORITES_DIR.mkdir(parents=True, exist_ok=True)
            with (FAVORITES_DIR / ".index.lock").open("a+b") as lock_file:
                while True:
                    if control is not None:
                        control.checkpoint()
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.01)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            _index_lock.release()


def _read_index() -> dict[str, Any]:
    if not FAVORITES_INDEX.is_file():
        return {"version": 1, "favorites": []}
    try:
        data = json.loads(FAVORITES_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {"version": 1, "favorites": []}
    if not isinstance(data, dict) or not isinstance(data.get("favorites"), list):
        return {"version": 1, "favorites": []}
    return data


def _write_index(entries: list[dict[str, Any]]) -> None:
    payload = {"version": 1, "favorites": entries}
    atomic_write_json(FAVORITES_INDEX, payload, prefix=".index-")
    _invalidate_favorite_ids_cache()


def _entry_to_event(entry: dict[str, Any]) -> dict[str, Any]:
    created_at = _parse_datetime(entry.get("created_at"))
    return {
        "id": entry.get("event_id"),
        "kind": entry.get("event_kind") or "event",
        "created_at": created_at,
        "device_id": entry.get("device_id"),
        "camera_name": device_names.display_name_for_id(
            entry.get("device_id"),
            entry.get("camera_name") or "Unknown camera",
        ),
        "_favorite_id": entry.get("favorite_id"),
        "_local_video_path": entry.get("video_path"),
        "_local_thumbnail_path": entry.get("thumbnail_path"),
        "_is_local_favorite": True,
        "_metadata_missing": False,
    }


def _missing_metadata_event(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "id": None,
        "kind": "favorite",
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        "camera_name": "Missing Metadata",
        "_local_video_path": str(path),
        "_is_local_favorite": True,
        "_metadata_missing": True,
        "_missing_metadata_filename": path.name,
    }


def _event_sort_key(event: dict[str, Any]) -> datetime:
    created_at = event.get("created_at")
    return created_at if isinstance(created_at, datetime) else datetime.fromtimestamp(0, tz=UTC)


def _event_created_at(event: dict[str, Any]) -> datetime:
    created_at = event.get("created_at")
    if isinstance(created_at, datetime):
        return created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    return datetime.now(tz=UTC)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.fromtimestamp(0, tz=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.fromtimestamp(0, tz=UTC)


def _event_device_id(event: dict[str, Any]) -> str | int | None:
    device = event.get("_device")
    return getattr(device, "id", None) or event.get("device_id") or event.get("doorbot_id")


def _event_camera_name(event: dict[str, Any]) -> str:
    device = event.get("_device")
    if device is not None:
        return device_names.display_name(device)
    return device_names.display_name_for_id(
        event.get("device_id") or event.get("doorbot_id"),
        event.get("camera_name") or "Unknown camera",
    )


def _video_filename(event: dict[str, Any]) -> str:
    created = _event_created_at(event).strftime("%Y-%m-%d_%H-%M-%S")
    camera = _safe_filename(_event_camera_name(event))
    kind = _safe_filename(str(event.get("kind") or "event"))
    event_id = _safe_filename(str(event.get("id") or "unknown"))
    device_id = _safe_filename(str(_event_device_id(event)))
    return f"{created}_{device_id}_{camera}_{kind}_{event_id}.mp4"


def _safe_filename(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip(".-")
    return cleaned or "unknown"


def _delete_managed_file(value: Any, root: Path) -> None:
    """Delete only a file whose resolved parent remains within *root*."""
    if not value:
        return
    path = Path(value).expanduser()
    try:
        if not path.parent.resolve().is_relative_to(root.resolve()):
            return
        if path.is_file() or path.is_symlink():
            path.unlink()
    except OSError:
        pass
