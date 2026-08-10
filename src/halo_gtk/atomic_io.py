"""Small same-directory atomic file-writing helpers."""

from __future__ import annotations

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
