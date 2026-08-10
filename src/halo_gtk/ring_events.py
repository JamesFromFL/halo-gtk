"""Normalize Ring event names across history and push payload generations."""

from __future__ import annotations

from typing import Any

from ring_doorbell.const import (
    KIND_DING,
    KIND_MOTION,
    PUSH_ACTION_DING,
    PUSH_ACTION_MOTION,
    PUSH_NOTIFICATION_DING,
    PUSH_NOTIFICATION_MOTION,
)

_MOTION_KINDS = frozenset(
    str(kind).casefold() for kind in (KIND_MOTION, PUSH_ACTION_MOTION, PUSH_NOTIFICATION_MOTION)
)
_DING_KINDS = frozenset(
    str(kind).casefold() for kind in (KIND_DING, PUSH_ACTION_DING, PUSH_NOTIFICATION_DING)
)


def normalized_event_kind(event_or_kind: Any) -> str:
    """Return Halo's stable event kind for a Ring object, mapping, or raw name."""
    if isinstance(event_or_kind, dict):
        value = event_or_kind.get("kind")
    elif isinstance(event_or_kind, str):
        value = event_or_kind
    else:
        value = getattr(event_or_kind, "kind", None)
    kind = str(value or "").casefold()
    if kind in _MOTION_KINDS:
        return KIND_MOTION
    if kind in _DING_KINDS:
        return KIND_DING
    return kind
