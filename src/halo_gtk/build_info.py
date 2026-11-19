"""Diagnostic provenance for the package loaded by this process."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

from halo_gtk import APP_ID


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _read_small(path, limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError("unavailable metadata")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversized metadata")
    return data


def _managed_revision(release):
    revision_prefix, lock_prefix, _ = release.name.split("-")
    try:
        manifest = json.loads(_read_small(release / "release.json", 65536))
        if not isinstance(manifest, dict):
            return None
        revision = manifest.get("revision")
        digest = manifest.get("lock")
        if (
            manifest.get("format") == 1
            and manifest.get("data_format") == 1
            and manifest.get("validated") is True
            and manifest.get("release") == release.name
            and _hex(revision, 40)
            and revision.startswith(revision_prefix)
            and _hex(digest, 64)
            and digest.startswith(lock_prefix)
            and hashlib.sha256(
                _read_small(release / "source/uv.lock", 16 * 1024 * 1024)
            ).hexdigest()
            == digest
        ):
            return revision
    except (OSError, ValueError):
        pass
    return None


def _checkout_metadata(checkout):
    # Do not let the caller's Git environment select a different repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(checkout),
                *args,
            ],
            env=env,
            stderr=subprocess.DEVNULL,
            timeout=1,
        ).strip()

    rows = {"Install profile": "Checkout", "Source revision": "unavailable"}
    try:
        if Path(os.fsdecode(git("rev-parse", "--show-toplevel"))) != checkout:
            return rows
        revision = git("rev-parse", "--verify", "HEAD").decode("ascii")
        if not (_hex(revision, 40) or _hex(revision, 64)):
            return rows
        changed = git(
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--",
            "src",
            "pyproject.toml",
            "uv.lock",
        )
        if changed:
            rows["Checkout HEAD"] = revision
            rows["Checkout source"] = "local changes"
        else:
            rows["Source revision"] = revision
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return rows


def _build_metadata(module, prefix, flatpak_info):
    if flatpak_info.is_file():
        rows = {"Install profile": "unverified", "Source revision": "unavailable"}
        try:
            info = configparser.ConfigParser(interpolation=None)
            info.read_string(_read_small(flatpak_info, 65536).decode())
            if info.get("Application", "name", fallback="") == APP_ID and module.is_relative_to(
                "/app"
            ):
                rows["Install profile"] = "Flatpak"
                deployment = info.get("Instance", "app-commit", fallback="")
                if _hex(deployment, 64):
                    rows["Flatpak deployment commit"] = deployment
        except (OSError, ValueError, configparser.Error):
            pass
        return rows
    release = prefix.parent
    if (
        prefix.name == ".venv"
        and module.is_relative_to(prefix)
        and release.parent.name == "releases"
        and release.parent.parent.name == "managed"
        and release.parent.parent.parent.name == "halo-gtk"
        and re.fullmatch(r"[0-9a-f]{12}-[0-9a-f]{12}-[0-9a-f]{12}", release.name)
    ):
        return {
            "Install profile": "Managed",
            "Source revision": _managed_revision(release) or "unavailable",
        }
    if module.parent.name == "halo_gtk" and module.parent.parent.name == "src":
        checkout = module.parents[2]
        if (checkout / ".git").exists() and (checkout / "pyproject.toml").is_file():
            return _checkout_metadata(checkout)
    return {"Install profile": "Python package", "Source revision": "unavailable"}


@cache
def build_metadata():
    return _build_metadata(
        Path(__file__).resolve(), Path(sys.prefix).resolve(), Path("/.flatpak-info")
    )
