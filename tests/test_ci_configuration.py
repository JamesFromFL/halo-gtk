"""Security checks for continuous-integration configuration."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_USE = re.compile(r"^\s*-\s+uses:\s+\S+@(\S+)", re.MULTILINE)
FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def test_ci_actions_are_pinned_and_workflow_permissions_are_read_only():
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    revisions = ACTION_USE.findall(workflow)

    assert revisions
    assert all(FULL_COMMIT_SHA.fullmatch(revision) for revision in revisions)
    assert "\npermissions:\n  contents: read\n" in workflow


def test_dependabot_tracks_github_action_updates():
    config = (REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")

    assert "package-ecosystem: github-actions" in config
    assert "interval: monthly" in config


def test_release_toolchain_and_private_patch_dependencies_are_pinned():
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        config = tomllib.load(file)

    assert config["project"]["requires-python"] == ">=3.11,<3.15"
    assert config["tool"]["uv"]["required-version"] == "==0.11.24"
    assert config["build-system"]["requires"] == ["hatchling==1.31.0"]
    assert [
        dependency
        for dependency in config["project"]["dependencies"]
        if dependency.startswith("aiortc")
    ] == ["aiortc==1.14.0"]


def test_ci_checks_lock_freshness_and_the_installed_wheel():
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    setup_count = workflow.count("uses: astral-sh/setup-uv@")

    assert setup_count == 4
    assert workflow.count('version: "0.11.24"') == setup_count
    assert workflow.count("uv lock --check") == setup_count
    assert "uv build --out-dir dist" in workflow
    assert "--requirements /tmp/halo-runtime-requirements.txt" in workflow
    assert "uv pip check --python /tmp/halo-artifact-venv/bin/python" in workflow
    assert "scripts/ci_import_check.py --installed" in workflow


def test_desktop_categories_match_the_camera_viewer():
    desktop_file = (REPO_ROOT / "data/io.github.JamesFromFL.HaloGtk.desktop").read_text(
        encoding="utf-8"
    )

    assert "Categories=AudioVideo;Video;" in desktop_file
    assert ";Security;" not in desktop_file
