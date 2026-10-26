"""Small GTK-main-context helpers for operation outcomes."""

from __future__ import annotations


def show_toast(widget, message: str) -> None:
    root = widget.get_root()
    if root is not None and callable(getattr(root, "show_toast", None)):
        root.show_toast(message)


def microphone_state(button, toggled_callback, state: str, *, update_icon: bool = True) -> None:
    active = state in {"starting", "active"}
    button.handler_block_by_func(toggled_callback)
    try:
        button.set_active(active)
    finally:
        button.handler_unblock_by_func(toggled_callback)
    if update_icon:
        button.set_icon_name(
            "audio-input-microphone-symbolic" if active else "microphone-disabled-symbolic"
        )
    button.set_tooltip_text(
        {
            "starting": "Starting microphone…",
            "active": "Microphone on",
            "stopped": "Microphone off",
            "failed": "Microphone unavailable. Check your input device and try again.",
        }[state]
    )
