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


def test_profile_metadata_checkout_clean_modified_and_irrelevant_cwd(tmp_path, monkeypatch):
    import subprocess

    from halo_gtk.build_info import _build_metadata

    source = tmp_path / "checkout"
    module = source / "src/halo_gtk/build_info.py"
    module.parent.mkdir(parents=True)
    module.write_text("# source")
    (source / "pyproject.toml").write_text('[project]\nname = "halo-gtk"')

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args]).decode().strip()

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    head = git("rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    assert _build_metadata(module, tmp_path, tmp_path / "no-flatpak-info") == {
        "Install profile": "Checkout",
        "Source revision": head,
    }
    module.write_text("private camera name and a signed URL must never enter metadata")
    dirty = _build_metadata(module, tmp_path, tmp_path / "missing")
    assert dirty == {
        "Install profile": "Checkout",
        "Checkout HEAD": head,
        "Checkout source": "local changes",
        "Source revision": "unavailable",
    }


def test_managed_metadata_binds_loaded_package_manifest_and_lock(tmp_path):
    import hashlib
    import json

    from halo_gtk.build_info import _build_metadata

    lock = b"offline fixture"
    digest = hashlib.sha256(lock).hexdigest()
    sha = "a" * 40
    name = f"{sha[:12]}-{digest[:12]}-cccccccccccc"
    release = tmp_path / "halo-gtk/managed/releases" / name
    (release / "source").mkdir(parents=True)
    (release / "source/uv.lock").write_bytes(lock)
    (release / "release.json").write_text(
        json.dumps(
            {
                "release": name,
                "revision": sha,
                "lock": digest,
                "validated": True,
                "format": 1,
                "data_format": 1,
                "private": "name@example.invalid https://signed.invalid/token",
            }
        )
    )
    prefix = release / ".venv"
    module = prefix / "lib/python3/site-packages/halo_gtk/build_info.py"
    assert _build_metadata(module, prefix, tmp_path / "none") == {
        "Install profile": "Managed",
        "Source revision": sha,
    }
    (release / "source/uv.lock").write_text("changed")
    assert _build_metadata(module, prefix, tmp_path / "none")["Source revision"] == "unavailable"
    assert (
        _build_metadata(tmp_path / "different.py", prefix, tmp_path / "none")["Install profile"]
        == "Python package"
    )


def test_flatpak_export_accepts_only_installed_halo_identity_and_deployment_hash(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from halo_gtk import APP_ID
    from halo_gtk.build_info import _build_metadata

    info = tmp_path / "flatpak-info"
    module = Path("/app/lib/python3/site-packages/halo_gtk/build_info.py")
    commit = "0123456789abcdef" * 4

    def write(app_id, value):
        info.write_text(
            f"[Application]\nname={app_id}\n[Instance]\napp-commit={value}\n"
            "private=contextual-secret\n"
        )

    write(APP_ID, commit)
    monkeypatch.setattr(
        diagnostics, "build_metadata", lambda: _build_metadata(module, Path("/app"), info)
    )
    monkeypatch.setattr(privacy, "STATE_DIR", tmp_path / "absent")
    with zipfile.ZipFile(diagnostics.export_debug_logs(tmp_path / "archive")) as archive:
        report = archive.read("diagnostics.txt").decode()
    assert "Install profile: Flatpak" in report and f"Flatpak deployment commit: {commit}" in report
    assert "Source revision: unavailable" in report and "contextual-secret" not in report
    assert all(f"{label}: " in report for label in ("GTK", "GStreamer", "aiortc", "PyAV"))
    write(APP_ID, "signed-url")
    assert "Flatpak deployment commit" not in _build_metadata(module, Path("/app"), info)
    write("other.app", commit)
    assert _build_metadata(module, Path("/app"), info)["Install profile"] == "unverified"


def test_timing_opt_in_bounds_export_and_immediate_purge(tmp_path, monkeypatch):
    from halo_gtk.stream_timing import SAMPLE_LIMIT, StreamTiming

    now = [10.0]
    timing = StreamTiming(clock=lambda: now[0])
    monkeypatch.setattr(diagnostics, "timings", timing)
    monkeypatch.setattr(privacy, "STATE_DIR", tmp_path / "absent")

    def export():
        with zipfile.ZipFile(diagnostics.export_debug_logs(tmp_path / "test")) as bundle:
            return bundle.read("diagnostics.txt").decode()

    timing.video_buffer(12, None, 13)
    assert "Stream timing" not in export()
    timing.set_enabled(True)
    timing.video_buffer(9, None, 13)
    assert "first video buffer: 0 samples" in export()
    timing.video_buffer(10, None, 11.0)
    for i in range(SAMPLE_LIMIT * 2):
        start = 12.0 + i
        timing.video_buffer(10, start, start + 0.020)
    timing.video_buffer(10, 10, float("inf"))
    report = export()
    assert "first video buffer: 1 sample, median 1000 ms, p95 1000 ms" in report
    assert (
        f"Interval between video buffers: {SAMPLE_LIMIT} samples, median 20 ms, p95 20 ms" in report
    )
    timing.set_enabled(False)
    assert "Stream timing" not in export()
    now[0] = 400
    timing.set_enabled(True)
    timing.video_buffer(15, 16, 405)
    assert all("0 samples" in line for line in timing.summary()[1:])


def test_settings_timing_switch_and_log_clear_take_effect_immediately(monkeypatch):
    from types import SimpleNamespace

    from halo_gtk.settings_page import SettingsPage
    from halo_gtk.stream_timing import StreamTiming

    timing = StreamTiming(clock=lambda: 0)
    monkeypatch.setattr(diagnostics, "timings", timing)
    monkeypatch.setattr(privacy, "clear_debug_logs", lambda: False)
    page = SimpleNamespace(_refreshing=False, _set_advanced_message=lambda _: None)
    switch = SimpleNamespace(get_active=lambda: True)
    SettingsPage._on_timing_toggled(page, switch, None)
    timing.video_buffer(0, None, 1)
    assert "first video buffer: 1 sample" in timing.summary()[1]
    SettingsPage._clear_debug_log(page)
    assert timing.enabled and "0 samples" in timing.summary()[1]
    switch.get_active = lambda: False
    SettingsPage._on_timing_toggled(page, switch, None)
    assert not timing.enabled and timing.summary() == []
