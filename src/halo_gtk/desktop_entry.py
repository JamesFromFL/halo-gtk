"""Escaping for freedesktop Desktop Entry command arguments."""

from __future__ import annotations


def quote_exec_argument(value: str) -> str:
    """Quote one literal Exec argument, including Desktop Entry string escapes."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError("Desktop command arguments must be non-empty without control characters")
    escaped = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    # Backslash escaping is applied twice: first by the desktop key-value
    # parser, then by the Exec command parser.
    return '"' + escaped.replace("\\", "\\\\") + '"'
