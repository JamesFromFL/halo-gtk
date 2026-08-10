"""Tests for the user-local installer and uninstaller."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def test_desktop_install_and_uninstall_in_isolated_home(tmp_path):
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    checkout = tmp_path / "checkout"
    fake_bin.mkdir()
    home.mkdir()
    shutil.copytree(REPO_ROOT / "scripts", checkout / "scripts")
    shutil.copytree(
        REPO_ROOT / "data",
        checkout / "data",
        ignore=shutil.ignore_patterns("gschemas.compiled"),
    )

    uv_log = tmp_path / "uv.log"
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

with Path(os.environ["UV_CALL_LOG"]).open("a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")

if len(sys.argv) > 1 and sys.argv[1] == "venv":
    bin_dir = Path(sys.argv[-1]) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    python.chmod(0o755)
""",
    )
    _write_executable(fake_bin / "pgrep", "#!/bin/sh\nexit 1\n")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "UV_CALL_LOG": str(uv_log),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
        }
    )

    subprocess.run(
        [checkout / "scripts/install.sh"],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=30,
    )

    launcher = home / ".local/bin/halo-gtk"
    desktop_file = home / ".local/share/applications/io.github.JamesFromFL.HaloGtk.desktop"
    schema = home / ".local/share/glib-2.0/schemas/io.github.JamesFromFL.HaloGtk.gschema.xml"
    icon = home / ".local/share/icons/hicolor/256x256/apps/io.github.JamesFromFL.HaloGtk.png"
    assert launcher.is_file()
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
    assert f'exec "{checkout}/.venv/bin/halo-gtk" "$@"' in launcher.read_text(encoding="utf-8")
    assert f'Exec="{home}/.local/bin/halo-gtk"' in desktop_file.read_text(encoding="utf-8")
    assert schema.is_file()
    assert icon.is_file()
    assert uv_log.read_text(encoding="utf-8").splitlines() == [
        f"venv --system-site-packages {checkout}/.venv",
        f"--project {checkout} lock --check",
        f"--project {checkout} sync --frozen --no-dev",
    ]

    preserved = home / ".config/halo-gtk/settings.json"
    preserved.parent.mkdir(parents=True)
    preserved.write_text('{"preserved":true}\n', encoding="utf-8")
    autostart = home / ".config/autostart/io.github.JamesFromFL.HaloGtk.desktop"
    autostart.parent.mkdir(parents=True)
    autostart.write_text("[Desktop Entry]\n", encoding="utf-8")
    subprocess.run(
        [checkout / "scripts/uninstall.sh"],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=30,
    )

    assert not launcher.exists()
    assert not desktop_file.exists()
    assert not schema.exists()
    assert not icon.exists()
    assert not autostart.exists()
    assert preserved.read_text(encoding="utf-8") == '{"preserved":true}\n'


def test_installer_validates_live_media_runtime_contract():
    installer = (REPO_ROOT / "scripts/install.sh").read_text(encoding="utf-8")

    for namespace in ("Adw", "GdkPixbuf", "GioUnix", "Graphene", "Gst", "Gtk", "Notify", "Secret"):
        assert f"require_gi_namespace {namespace} " in installer

    for element in (
        "appsink",
        "appsrc",
        "audioconvert",
        "audioresample",
        "capsfilter",
        "gtk4paintablesink",
        "playbin",
        "pulsesrc",
        "queue",
        "videoconvert",
        "volume",
    ):
        assert f"require_gst_element {element} " in installer

    assert "pulsesink pipewiresink autoaudiosink" in installer
