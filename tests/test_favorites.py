from __future__ import annotations

import contextlib
import multiprocessing
import threading
from datetime import UTC, datetime
from pathlib import Path

from halo_gtk import favorites


class _Device:
    id = 101
    name = "Front Yard Floodlight Cam"


def _event() -> dict:
    return {
        "id": 202,
        "kind": "motion",
        "created_at": datetime(2026, 5, 20, 20, 10, tzinfo=UTC),
        "_device": _Device(),
    }


def _redirect_favorites(monkeypatch, tmp_path):
    root = tmp_path / "halo-gtk" / "favorites"
    monkeypatch.setattr(favorites, "FAVORITES_DIR", root)
    monkeypatch.setattr(favorites, "FAVORITE_VIDEOS_DIR", root / "videos")
    monkeypatch.setattr(favorites, "FAVORITE_THUMBNAILS_DIR", root / "thumbnails")
    monkeypatch.setattr(favorites, "FAVORITES_INDEX", root / "index.json")
    return root


def _add_favorite_in_process(root_value: str, event_id: int, simultaneous_reads) -> None:
    root = Path(root_value)
    favorites.FAVORITES_DIR = root
    favorites.FAVORITE_VIDEOS_DIR = root / "videos"
    favorites.FAVORITE_THUMBNAILS_DIR = root / "thumbnails"
    favorites.FAVORITES_INDEX = root / "index.json"
    original_read_index = favorites._read_index

    def fake_download(_url, destination):
        Path(destination).write_bytes(b"video")

    def synchronized_read_index():
        data = original_read_index()
        with contextlib.suppress(threading.BrokenBarrierError):
            simultaneous_reads.wait(timeout=1)
        return data

    favorites.download_https = fake_download
    favorites._read_index = synchronized_read_index
    favorites.add_favorite(
        {**_event(), "id": event_id},
        "https://example.test/video.mp4",
    )


def test_add_favorite_downloads_clip_and_indexes_metadata(monkeypatch, tmp_path):
    _redirect_favorites(monkeypatch, tmp_path)

    def fake_download(_url, destination):
        Path(destination).write_bytes(b"video")

    monkeypatch.setattr(favorites, "download_https", fake_download)

    entry = favorites.add_favorite(
        _event(),
        "https://example.test/video.mp4",
        thumbnail_bytes=b"png",
    )

    assert favorites.is_favorited(_event()) is True
    assert entry["favorite_id"] == "101-202"
    assert entry["camera_name"] == "Front Yard Floodlight Cam"
    assert entry["event_kind"] == "motion"
    assert entry["video_path"].endswith("_front-yard-floodlight-cam_motion_202.mp4")
    assert entry["thumbnail_path"].endswith(".png")

    events = favorites.favorite_events()
    assert events[0]["_favorite_id"] == "101-202"
    assert events[0]["camera_name"] == "Front Yard Floodlight Cam"
    assert events[0]["_metadata_missing"] is False


def test_index_entries_prunes_missing_media(monkeypatch, tmp_path):
    root = _redirect_favorites(monkeypatch, tmp_path)
    favorites.FAVORITE_VIDEOS_DIR.mkdir(parents=True)
    present = favorites.FAVORITE_VIDEOS_DIR / "present.mp4"
    present.write_bytes(b"video")
    missing = favorites.FAVORITE_VIDEOS_DIR / "missing.mp4"
    root.mkdir(parents=True, exist_ok=True)
    favorites.FAVORITES_INDEX.write_text(
        f"""
        {{
          "version": 1,
          "favorites": [
            {{"favorite_id": "present", "video_path": "{present}"}},
            {{"favorite_id": "missing", "video_path": "{missing}"}}
          ]
        }}
        """,
        encoding="utf-8",
    )

    entries = favorites.index_entries()

    assert [entry["favorite_id"] for entry in entries] == ["present"]
    assert "missing" not in favorites.FAVORITES_INDEX.read_text(encoding="utf-8")


def test_favorite_events_shows_orphaned_clip_as_missing_metadata(monkeypatch, tmp_path):
    _redirect_favorites(monkeypatch, tmp_path)
    favorites.FAVORITE_VIDEOS_DIR.mkdir(parents=True)
    orphan = favorites.FAVORITE_VIDEOS_DIR / "orphan.mp4"
    orphan.write_bytes(b"video")

    events = favorites.favorite_events()

    assert events[0]["camera_name"] == "Missing Metadata"
    assert events[0]["_metadata_missing"] is True
    assert events[0]["_missing_metadata_filename"] == "orphan.mp4"


def test_is_favorited_checks_index_not_media_presence(monkeypatch, tmp_path):
    root = _redirect_favorites(monkeypatch, tmp_path)
    favorites._invalidate_favorite_ids_cache()
    root.mkdir(parents=True, exist_ok=True)
    favorites.FAVORITES_INDEX.write_text(
        '{"version": 1, "favorites": [{"favorite_id": "101-202", "video_path": "/no/such.mp4"}]}',
        encoding="utf-8",
    )

    # is_favorited() is the cheap per-row check: it reflects the index, and does
    # not stat/prune media (that stays index_entries()' responsibility).
    assert favorites.is_favorited(_event()) is True


def test_remove_favorite_deletes_indexed_files(monkeypatch, tmp_path):
    _redirect_favorites(monkeypatch, tmp_path)

    def fake_download(_url, destination):
        Path(destination).write_bytes(b"video")

    monkeypatch.setattr(favorites, "download_https", fake_download)
    entry = favorites.add_favorite(
        _event(),
        "https://example.test/video.mp4",
        thumbnail_bytes=b"png",
    )
    video_path = entry["video_path"]
    thumbnail_path = entry["thumbnail_path"]

    assert favorites.remove_favorite("101-202") is True

    assert favorites.index_entries() == []
    assert not Path(video_path).exists()
    assert not Path(thumbnail_path).exists()


def test_concurrent_favorite_adds_preserve_both_index_entries(monkeypatch, tmp_path):
    _redirect_favorites(monkeypatch, tmp_path)

    def fake_download(_url, destination):
        Path(destination).write_bytes(b"video")

    original_read_index = favorites._read_index
    simultaneous_reads = threading.Barrier(2)

    def synchronized_read_index():
        data = original_read_index()
        with contextlib.suppress(threading.BrokenBarrierError):
            simultaneous_reads.wait(timeout=0.2)
        return data

    monkeypatch.setattr(favorites, "download_https", fake_download)
    monkeypatch.setattr(favorites, "_read_index", synchronized_read_index)
    events = [
        {**_event(), "id": 202},
        {**_event(), "id": 203},
    ]
    errors = []

    def add(event):
        try:
            favorites.add_favorite(event, "https://example.test/video.mp4")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(event,)) for event in events]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert {entry["favorite_id"] for entry in favorites.index_entries()} == {
        "101-202",
        "101-203",
    }


def test_cross_process_favorite_adds_preserve_both_index_entries(monkeypatch, tmp_path):
    root = _redirect_favorites(monkeypatch, tmp_path)
    process_context = multiprocessing.get_context("spawn")
    simultaneous_reads = process_context.Barrier(2)
    processes = [
        process_context.Process(
            target=_add_favorite_in_process,
            args=(str(root), event_id, simultaneous_reads),
        )
        for event_id in (202, 203)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0, 0]
    assert {entry["favorite_id"] for entry in favorites.index_entries()} == {
        "101-202",
        "101-203",
    }


def test_remove_favorite_does_not_delete_paths_outside_managed_roots(monkeypatch, tmp_path):
    root = _redirect_favorites(monkeypatch, tmp_path)
    outside_video = tmp_path / "outside.mp4"
    outside_thumbnail = tmp_path / "outside.png"
    outside_video.write_bytes(b"keep")
    outside_thumbnail.write_bytes(b"keep")
    root.mkdir(parents=True, exist_ok=True)
    favorites.FAVORITES_INDEX.write_text(
        f"""
        {{
          "version": 1,
          "favorites": [
            {{
              "favorite_id": "corrupt",
              "video_path": "{outside_video}",
              "thumbnail_path": "{outside_thumbnail}"
            }}
          ]
        }}
        """,
        encoding="utf-8",
    )

    assert favorites.remove_favorite("corrupt") is True
    assert outside_video.read_bytes() == b"keep"
    assert outside_thumbnail.read_bytes() == b"keep"
    assert favorites.index_entries() == []
