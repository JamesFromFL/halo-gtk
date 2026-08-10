"""Tests for Halo's XDG storage path helpers."""

from __future__ import annotations


def test_storage_paths_respect_xdg_environment(monkeypatch, tmp_path):
    from halo_gtk import storage_paths

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert storage_paths.config_dir() == tmp_path / "config" / "halo-gtk"
    assert storage_paths.data_dir() == tmp_path / "data" / "halo-gtk"
    assert storage_paths.cache_dir() == tmp_path / "cache" / "halo-gtk"
    assert storage_paths.state_dir() == tmp_path / "state" / "halo-gtk"
    assert storage_paths.autostart_dir() == tmp_path / "config" / "autostart"
