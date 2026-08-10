"""Tests for diagnostics export helpers."""

import zipfile

from halo_gtk import diagnostics, privacy


def test_export_debug_logs_writes_zip_with_logs(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "halo-gtk.log").write_text("active log")
    (state_dir / "halo-gtk.log.1").write_text("rotated log")
    (state_dir / "other.log").write_text("ignored")
    monkeypatch.setattr(privacy, "STATE_DIR", state_dir)

    archive = diagnostics.export_debug_logs(tmp_path / "diagnostics")

    assert archive == tmp_path / "diagnostics.zip"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "diagnostics.txt" in names
        assert "logs/halo-gtk.log" in names
        assert "logs/halo-gtk.log.1" in names
        assert "logs/other.log" not in names
        assert zf.read("logs/halo-gtk.log").decode() == "active log"


def test_export_debug_logs_handles_missing_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(privacy, "STATE_DIR", tmp_path / "missing")

    archive = diagnostics.export_debug_logs(tmp_path / "diagnostics.zip")

    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["diagnostics.txt"]
        assert "No Halo debug logs found" in zf.read("diagnostics.txt").decode()


def test_export_debug_logs_redacts_secrets_and_signed_urls(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "halo-gtk.log").write_text(
        "\n".join(
            [
                "GET https://api.ring.test/recording/1?token=signed-secret&expires=123",
                "Authorization: Bearer authorization-secret",
                'refresh_token: "refresh-secret"',
                "password=account-secret",
                "JWT eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2lnbmF0dXJl",
                "camera stream connected",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(privacy, "STATE_DIR", state_dir)

    archive = diagnostics.export_debug_logs(tmp_path / "diagnostics.zip")

    with zipfile.ZipFile(archive) as zf:
        exported = zf.read("logs/halo-gtk.log").decode()
    for secret in (
        "signed-secret",
        "authorization-secret",
        "refresh-secret",
        "account-secret",
        "cGF5bG9hZA",
    ):
        assert secret not in exported
    assert "https://api.ring.test/recording/1?<redacted>" in exported
    assert "camera stream connected" in exported
