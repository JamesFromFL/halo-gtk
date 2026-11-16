"""Offline dependency selection and sandbox capability regression checks."""

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "flatpak_dependencies", ROOT / "scripts/flatpak_dependencies.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def artifact(filename):
    return {
        "url": f"https://files.pythonhosted.org/packages/{filename}",
        "hash": "sha256:" + "a" * 64,
    }


def test_runtime_wheel_selection_obeys_abi_platform_and_markers():
    lock = {
        "package": [
            {
                "name": "example",
                "version": "1.0",
                "wheels": [
                    artifact("example-1.0-cp314-cp314-manylinux_2_28_x86_64.whl"),
                    artifact("example-1.0-cp313-cp313-manylinux_2_43_x86_64.whl"),
                    artifact("example-1.0-cp313-cp313-manylinux_2_28_aarch64.whl"),
                    artifact("example-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"),
                    artifact("example-1.0-py3-none-any.whl"),
                ],
            }
        ]
    }
    result = module.dependency_module(
        lock, "example==1.0\nwindows-only==1; sys_platform == 'win32'\n"
    )
    assert result["sources"][0]["url"].endswith("cp313-cp313-manylinux_2_28_x86_64.whl")
    assert result["sources"][-1]["contents"] == "example==1.0\n"


@pytest.mark.parametrize(
    "problem", ["version", "foreign-url", "digest", "no-compatible-wheel", "duplicate"]
)
def test_unqualified_source_is_rejected(problem):
    wheel = artifact("example-1.0-py3-none-any.whl")
    package = {"name": "example", "version": "1.0", "wheels": [wheel]}
    requirements = "example==1.0"
    if problem == "version":
        requirements = "example>=1.0"
    elif problem == "foreign-url":
        wheel["url"] = wheel["url"].replace("files.pythonhosted.org", "example.test")
    elif problem == "digest":
        wheel["hash"] = "sha256:bad"
    elif problem == "duplicate":
        requirements += "\nexample==1.0"
    else:
        package["wheels"] = [artifact("example-1.0-cp314-cp314-win_amd64.whl")]
    with pytest.raises(ValueError):
        module.dependency_module({"package": [package]}, requirements)


def test_committed_flatpak_artifacts_match_locked_versions_and_hashes():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    committed = json.loads((ROOT / "packaging/flatpak/python-dependencies.json").read_text())
    assert module.dependency_module(lock, committed["sources"][-1]["contents"]) == committed
    command = committed["build-commands"][-1]
    assert "--ignore-installed" in command and "--no-index" in command


def test_sandbox_autostart_never_writes_or_removes_a_host_style_entry(tmp_path, monkeypatch):
    from halo_gtk import autostart

    path = tmp_path / "existing.desktop"
    path.write_text("unchanged")
    monkeypatch.setattr(autostart, "AUTOSTART_FILE", path)
    monkeypatch.setattr(autostart, "is_supported", lambda: False)
    assert not autostart.is_enabled()
    with pytest.raises(OSError, match="Flatpak"):
        autostart.set_enabled(True)
    autostart.set_enabled(False)
    assert path.read_text() == "unchanged"


def test_sandbox_settings_callbacks_cannot_save_fake_startup_success(monkeypatch):
    from halo_gtk import settings_page

    page = settings_page.SettingsPage.__new__(settings_page.SettingsPage)
    page._refreshing = False
    refreshed = []
    page.refresh = lambda: refreshed.append(True)
    monkeypatch.setattr(settings_page.autostart, "is_supported", lambda: False)
    monkeypatch.setattr(settings_page._config, "save", lambda *_: pytest.fail("Unexpected save"))
    page._on_autostart_toggled(None, None)
    page._on_autostart_background_toggled(None, None)
    assert refreshed == [True, True]
