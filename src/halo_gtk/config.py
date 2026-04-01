"""Application configuration — persistent settings via GSettings or a JSON fallback."""

from __future__ import annotations

import json
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "halo-gtk"
CONFIG_FILE = CONFIG_DIR / "config.json"

_DEFAULTS: dict = {
    "start_minimised": False,
    "show_notifications": True,
    "poll_interval_seconds": 30,
}


def load() -> dict:
    if not CONFIG_FILE.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return {**_DEFAULTS, **data}
    except Exception as exc:
        _log.warning("Failed to load config: %s", exc)
        return dict(_DEFAULTS)


def save(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
