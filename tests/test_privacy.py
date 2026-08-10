"""Tests for local privacy cleanup helpers."""

import logging
from logging import FileHandler

from halo_gtk import privacy


def test_clear_preview_cache_removes_preview_and_thumbnail_dirs(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    preview_file = cache_dir / "previews" / "front-door.png"
    thumbnail_file = cache_dir / "thumbnails" / "garage.png"
    preview_file.parent.mkdir(parents=True)
    thumbnail_file.parent.mkdir(parents=True)
    preview_file.write_text("preview")
    thumbnail_file.write_text("thumbnail")

    monkeypatch.setattr(privacy, "CACHE_DIR", cache_dir)

    removed = privacy.clear_preview_cache()

    assert removed == 4
    assert not (cache_dir / "previews").exists()
    assert not (cache_dir / "thumbnails").exists()


def test_clear_preview_cache_ignores_missing_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(privacy, "CACHE_DIR", tmp_path / "cache")

    assert privacy.clear_preview_cache() == 0


def test_clear_debug_logs_truncates_active_and_rotated_logs(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    active_log = state_dir / "halo-gtk.log"
    rotated_log = state_dir / "halo-gtk.log.1"
    other_log = state_dir / "other.log"
    active_log.write_text("active")
    rotated_log.write_text("rotated")
    other_log.write_text("other")

    handler = FileHandler(active_log)
    logging.getLogger().addHandler(handler)
    monkeypatch.setattr(privacy, "STATE_DIR", state_dir)
    try:
        touched = privacy.clear_debug_logs()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()

    assert touched == 2
    assert active_log.read_text() == ""
    assert rotated_log.read_text() == ""
    assert other_log.read_text() == "other"
