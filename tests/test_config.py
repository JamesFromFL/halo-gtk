"""Tests for config load/save round-trip."""


def test_load_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("halo_gtk.config.CONFIG_FILE", tmp_path / "config.json")
    from halo_gtk import config

    cfg = config.load()
    assert cfg["show_notifications"] is True
    assert cfg["poll_interval_seconds"] == 30


def test_save_and_reload(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("halo_gtk.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("halo_gtk.config.CONFIG_FILE", cfg_file)
    from halo_gtk import config

    config.save({"start_minimised": True, "show_notifications": False, "poll_interval_seconds": 60})
    loaded = config.load()
    assert loaded["start_minimised"] is True
    assert loaded["show_notifications"] is False
    assert loaded["poll_interval_seconds"] == 60
