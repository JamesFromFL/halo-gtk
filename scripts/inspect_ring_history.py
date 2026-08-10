#!/usr/bin/env python3
"""Print sanitized recent Ring history fields for event classifier work."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from halo_gtk.ring_client import init_client_from_cache  # noqa: E402

_INTERESTING_KEY_PARTS = (
    "answer",
    "answered",
    "miss",
    "person",
    "people",
    "human",
    "vehicle",
    "car",
    "package",
    "parcel",
    "motion",
    "linked",
    "source",
    "cv",
    "smart",
    "detect",
    "detection",
    "kind",
    "type",
    "subtype",
    "category",
    "tags",
    "label",
    "event",
)
_SENSITIVE_KEY_PARTS = (
    "url",
    "token",
    "authorization",
    "auth",
    "jwt",
    "secret",
    "password",
    "share",
    "recording",
    "snapshot",
    "address",
    "latitude",
    "longitude",
    "lat",
    "lng",
)
_SCALAR_TYPES = (str, int, float, bool, type(None))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect sanitized Ring event history fields for icon/event mapping."
    )
    parser.add_argument("--limit", type=int, default=10, help="events per camera to inspect")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="print sanitized nested interesting fields, not only top-level fields",
    )
    args = parser.parse_args()

    client = init_client_from_cache()
    if client is None or not client.is_authenticated:
        print("No saved Ring session found. Sign in with Halo first, then rerun this script.")
        return 1

    try:
        devices, events = client.event_history(limit=args.limit)
    finally:
        client.stop()

    print(f"Devices with history access: {len(devices)}")
    print(f"Events inspected: {len(events)}")
    print()

    for idx, event in enumerate(events, start=1):
        _print_event(idx, event, deep=args.deep)

    return 0


def _print_event(index: int, event: dict[str, Any], *, deep: bool) -> None:
    device = event.get("_device")
    camera_name = getattr(device, "name", None) or "Unknown camera"

    print(f"Event {index}")
    print(f"  camera: {camera_name}")
    print(f"  kind: {_scalar(event.get('kind'))}")
    print(f"  created_at: {_scalar(event.get('created_at'))}")
    print(f"  top_level_keys: {json.dumps(sorted(_safe_keys(event)))}")

    top_level = {
        key: _redacted_scalar(value)
        for key, value in event.items()
        if key != "_device" and _is_interesting_key(key) and _is_safe_scalar_key(key, value)
    }
    if top_level:
        print("  interesting_top_level:")
        for key in sorted(top_level):
            print(f"    {key}: {top_level[key]}")

    nested_paths = list(_interesting_paths(event)) if deep else []
    if nested_paths:
        print("  interesting_nested:")
        for path, value in nested_paths[:80]:
            print(f"    {path}: {_redacted_scalar(value)}")
        if len(nested_paths) > 80:
            print(f"    ... {len(nested_paths) - 80} more")
    print()


def _interesting_paths(value: Any, *, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    if depth > 5:
        return []

    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "_device" or _is_sensitive_key(key):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, _SCALAR_TYPES) and _is_interesting_key(key):
                found.append((path, item))
            elif isinstance(item, dict | list):
                found.extend(_interesting_paths(item, prefix=path, depth=depth + 1))
    elif isinstance(value, list):
        for idx, item in enumerate(value[:20]):
            path = f"{prefix}[{idx}]"
            if isinstance(item, _SCALAR_TYPES):
                if _is_interesting_key(prefix):
                    found.append((path, item))
            elif isinstance(item, dict | list):
                found.extend(_interesting_paths(item, prefix=path, depth=depth + 1))
    return found


def _safe_keys(event: dict[str, Any]) -> list[str]:
    return [key for key in event if key != "_device" and not _is_sensitive_key(key)]


def _is_safe_scalar_key(key: str, value: Any) -> bool:
    return isinstance(value, _SCALAR_TYPES) and not _is_sensitive_key(key)


def _is_interesting_key(key: str) -> bool:
    lower = key.lower()
    return any(part in lower for part in _INTERESTING_KEY_PARTS)


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return any(part in lower for part in _SENSITIVE_KEY_PARTS)


def _redacted_scalar(value: Any) -> str:
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return "<redacted-url>"
        if len(value) > 120:
            return value[:117] + "..."
        return value
    return _scalar(value)


def _scalar(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _SCALAR_TYPES):
        return str(value)
    return f"<{type(value).__name__}>"


if __name__ == "__main__":
    raise SystemExit(main())
