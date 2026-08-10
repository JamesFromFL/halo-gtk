"""Diagnostic export helpers."""

from __future__ import annotations

import logging
import platform
import zipfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from halo_gtk import APP_ID, APP_VERSION, privacy
from halo_gtk.log_redaction import redact_text


def export_debug_logs(destination: Path) -> Path:
    """Write Halo debug logs to a zip archive and return the archive path."""
    destination = destination.expanduser()
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)

    _flush_log_handlers()
    log_paths = _debug_log_paths()

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.txt", _diagnostics_text(log_paths))
        for path in log_paths:
            archive.writestr(
                f"logs/{path.name}",
                redact_text(path.read_text(encoding="utf-8", errors="replace")),
            )

    return destination


def _debug_log_paths() -> list[Path]:
    if not privacy.STATE_DIR.exists():
        return []
    return sorted(
        path
        for path in privacy.STATE_DIR.glob(privacy.LOG_GLOB)
        if path.is_file() and not path.is_symlink()
    )


def _flush_log_handlers() -> None:
    for handler in logging.getLogger().handlers:
        with suppress(Exception):
            handler.flush()


def _diagnostics_text(log_paths: list[Path]) -> str:
    lines = [
        f"Application ID: {APP_ID}",
        f"Application version: {APP_VERSION}",
        f"Generated: {datetime.now(tz=UTC).isoformat()}",
        f"Python: {platform.python_version()}",
        f"Platform: {platform.platform()}",
        "",
        "Included logs:",
    ]
    if log_paths:
        lines.extend(f"- {path.name}" for path in log_paths)
    else:
        lines.append("- No Halo debug logs found")
    return "\n".join(lines) + "\n"
