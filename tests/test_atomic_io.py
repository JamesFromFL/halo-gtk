"""Tests for same-directory atomic JSON writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from halo_gtk.atomic_io import atomic_write_json, atomic_write_text


def test_atomic_write_json_replaces_destination_with_formatted_payload(tmp_path):
    destination = tmp_path / "nested" / "settings.json"

    atomic_write_json(destination, {"z": 1, "a": [2]}, prefix=".settings-")

    assert json.loads(destination.read_text(encoding="utf-8")) == {"a": [2], "z": 1}
    assert destination.read_text(encoding="utf-8").endswith("\n")
    assert not list(destination.parent.glob(".settings-*.tmp"))


def test_atomic_write_json_cleans_temporary_file_when_serialization_fails(tmp_path):
    destination = tmp_path / "settings.json"
    destination.write_text('{"preserved": true}\n', encoding="utf-8")

    with pytest.raises(TypeError):
        atomic_write_json(destination, {"invalid": object()}, prefix=".settings-")

    assert destination.read_text(encoding="utf-8") == '{"preserved": true}\n'
    assert not list(tmp_path.glob(".settings-*.tmp"))


def test_atomic_write_json_cleans_temporary_file_when_replace_fails(
    monkeypatch,
    tmp_path,
):
    destination = tmp_path / "settings.json"
    destination.write_text('{"preserved": true}\n', encoding="utf-8")
    real_replace = Path.replace

    def fail_destination_replace(path, target):
        if Path(target) == destination:
            raise OSError("replace failed")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_destination_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_json(destination, {"updated": True}, prefix=".settings-")

    assert destination.read_text(encoding="utf-8") == '{"preserved": true}\n'
    assert not list(tmp_path.glob(".settings-*.tmp"))


def test_atomic_write_text_applies_mode_and_cleans_temporary_file(tmp_path):
    destination = tmp_path / "autostart" / "halo.desktop"

    atomic_write_text(
        destination,
        "[Desktop Entry]\n",
        prefix=".halo.desktop-",
        mode=0o644,
    )

    assert destination.read_text(encoding="utf-8") == "[Desktop Entry]\n"
    assert destination.stat().st_mode & 0o777 == 0o644
    assert not list(destination.parent.glob(".halo.desktop-*.tmp"))
