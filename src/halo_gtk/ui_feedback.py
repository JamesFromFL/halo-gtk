"""Small GTK-main-context helpers for operation outcomes."""

from __future__ import annotations


def show_toast(widget, message: str) -> None:
    root = widget.get_root()
    if root is not None and callable(getattr(root, "show_toast", None)):
        root.show_toast(message)
