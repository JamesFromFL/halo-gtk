"""Small same-directory atomic file-writing helpers."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(destination: Path, payload: Any, *, prefix: str) -> None:
    """Write *payload* as formatted JSON, replacing *destination* atomically."""
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write_text(destination, content, prefix=prefix)


def atomic_write_text(
    destination: Path,
    content: str,
    *,
    prefix: str,
    mode: int | None = None,
) -> None:
    """Write text through a same-directory temporary file and atomic replace."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=prefix,
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if mode is not None:
            temporary_path.chmod(mode)
        temporary_path.replace(destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def install_unique(source: Path, destination: Path) -> Path:
    for index in range(1, 1_000_000):
        candidate = _numbered_path(destination, index)
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            os.close(descriptor)
            os.replace(source, candidate)
        except BaseException:
            with contextlib.suppress(OSError):
                candidate.unlink()
            raise
        return candidate
    raise OSError("Could not allocate a unique download filename")


def _numbered_path(path: Path, index: int) -> Path:
    if index == 1:
        return path
    return path.with_name(f"{path.stem}_{index}{path.suffix}")
