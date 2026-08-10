"""Central XDG storage paths for Halo local files."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "halo-gtk"


def _xdg_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else fallback


def config_home() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")


def data_home() -> Path:
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share")


def cache_home() -> Path:
    return _xdg_path("XDG_CACHE_HOME", Path.home() / ".cache")


def state_home() -> Path:
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state")


def config_dir() -> Path:
    return config_home() / APP_DIR_NAME


def data_dir() -> Path:
    return data_home() / APP_DIR_NAME


def cache_dir() -> Path:
    return cache_home() / APP_DIR_NAME


def state_dir() -> Path:
    return state_home() / APP_DIR_NAME


def autostart_dir() -> Path:
    return config_home() / "autostart"
