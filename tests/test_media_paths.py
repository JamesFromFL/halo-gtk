"""Tests for configured media save directories."""

import pytest

from halo_gtk import media_paths


def test_snapshot_dir_uses_configured_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media_paths._config,
        "load",
        lambda: {
            "snapshot_dir": str(tmp_path / "snapshots"),
            "create_camera_subfolders": False,
        },
    )

    assert media_paths.snapshot_dir() == tmp_path / "snapshots"


def test_video_dir_can_scope_to_camera_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media_paths._config,
        "load",
        lambda: {
            "video_dir": str(tmp_path / "videos"),
            "create_camera_subfolders": True,
        },
    )

    assert media_paths.video_dir("Front / Door: Camera") == tmp_path / "videos" / (
        "Front - Door- Camera"
    )


def test_empty_config_uses_defaults(monkeypatch):
    monkeypatch.setattr(
        media_paths._config,
        "load",
        lambda: {
            "snapshot_dir": "",
            "video_dir": "",
            "create_camera_subfolders": False,
        },
    )

    assert media_paths.snapshot_dir() == media_paths.DEFAULT_SNAPSHOT_DIR
    assert media_paths.video_dir() == media_paths.DEFAULT_VIDEO_DIR


def test_should_open_after_save(monkeypatch):
    monkeypatch.setattr(media_paths._config, "load", lambda: {"open_folder_after_save": True})

    assert media_paths.should_open_after_save() is True


def test_write_unique_bytes_never_reuses_existing_name(tmp_path):
    (tmp_path / "2026-07-25_12-00-00.png").write_bytes(b"existing")

    second = media_paths.write_unique_bytes(
        tmp_path,
        "2026-07-25_12-00-00",
        ".png",
        b"second",
    )
    third = media_paths.write_unique_bytes(
        tmp_path,
        "2026-07-25_12-00-00",
        ".png",
        b"third",
    )

    assert second.name == "2026-07-25_12-00-00_2.png"
    assert third.name == "2026-07-25_12-00-00_3.png"
    assert second.read_bytes() == b"second"
    assert third.read_bytes() == b"third"


def test_write_unique_bytes_removes_partial_file_on_failure(monkeypatch, tmp_path):
    def fail_write(_handle, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(media_paths, "_write_all", fail_write)

    with pytest.raises(OSError, match="disk full"):
        media_paths.write_unique_bytes(tmp_path, "snapshot", ".png", b"png")

    assert list(tmp_path.iterdir()) == []
