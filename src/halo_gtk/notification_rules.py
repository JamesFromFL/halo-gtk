"""Local notification schedules; never changes device or Ring alert settings."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime

MAX_DEVICE_RULES = 512
SNOOZE_MINUTES = (0, 15, 30, 60, 120)


def device_key(family: str, identity: object) -> str | None:
    if family not in {"camera", "intercom", "alarm"} or identity is None:
        return None
    value = str(identity)
    if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        return None
    return f"{family}:{value}"


def alarm_device_key(location: str, asset: str | None, zid: str | None) -> str | None:
    if not asset or not zid:
        return None
    identity = json.dumps([location, asset, zid], separators=(",", ":"))
    return device_key("alarm", hashlib.sha256(identity.encode()).hexdigest())


def _minute(value: object, default: int) -> int:
    return value if type(value) is int and 0 <= value < 1440 else default


def _expiry(value: object) -> float:
    return (
        float(value)
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value < 4102444800
        else 0.0
    )


def normalize(value: object) -> dict:
    value = value if isinstance(value, dict) else {}
    devices = {}
    raw = value.get("devices", {})
    if isinstance(raw, dict):
        for key, rule in list(raw.items())[:MAX_DEVICE_RULES]:
            if not isinstance(key, str) or ":" not in key or not isinstance(rule, dict):
                continue
            family, identity = key.split(":", 1)
            if device_key(family, identity) != key:
                continue
            devices[key] = {
                "enabled": rule.get("enabled") is not False,
                "motion": rule.get("motion") is not False,
                "ding": rule.get("ding") is not False,
                "snoozed_until": _expiry(rule.get("snoozed_until")),
            }
    return {
        "quiet_enabled": value.get("quiet_enabled") is True,
        "quiet_start": _minute(value.get("quiet_start"), 22 * 60),
        "quiet_end": _minute(value.get("quiet_end"), 7 * 60),
        "snoozed_until": _expiry(value.get("snoozed_until")),
        "devices": devices,
    }


def allowed(
    config: dict,
    key: str | None,
    kind: str | None,
    *,
    critical: bool = False,
    now: float | None = None,
    local_time: datetime | None = None,
) -> bool:
    """Critical Alarm events bypass these rules, but not the master preference."""
    if critical:
        return True
    now = time.time() if now is None else now
    rules = normalize(config.get("notification_rules"))
    device = rules["devices"].get(key, {})
    if not device.get("enabled", True) or device.get(kind) is False:
        return False
    # A bad clock or edited timestamp cannot leave notifications muted for years.
    for expiry in (rules["snoozed_until"], device.get("snoozed_until", 0)):
        if now < expiry <= now + 2 * 3600:
            return False
    if rules["quiet_enabled"]:
        local_time = local_time or datetime.fromtimestamp(now).astimezone()
        minute = local_time.hour * 60 + local_time.minute
        start, end = rules["quiet_start"], rules["quiet_end"]
        if (start < end and start <= minute < end) or (
            start > end and (minute >= start or minute < end)
        ):
            return False
    return True


def snooze_until(minutes: int, *, now: float | None = None) -> float:
    if type(minutes) is not int or minutes not in SNOOZE_MINUTES:
        raise ValueError("Choose a supported notification snooze duration")
    return (time.time() if now is None else now) + minutes * 60 if minutes else 0.0


def parse_time(text: str) -> int:
    """Use an explicit local 24-hour clock, independent of locale parsing."""
    parts = text.strip().split(":")
    if len(parts) != 2 or any(len(p) != 2 or not p.isascii() or not p.isdigit() for p in parts):
        raise ValueError("Enter the time as HH:MM, using the 24-hour clock")
    hour, minute = map(int, parts)
    if hour > 23 or minute > 59:
        raise ValueError("Enter a time between 00:00 and 23:59")
    return hour * 60 + minute


def format_time(minute: int) -> str:
    return f"{minute // 60:02}:{minute % 60:02}"
