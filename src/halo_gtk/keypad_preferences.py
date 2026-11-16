"""Private, account/device-scoped permission for Halo's keypad LED controls.

This lock gates Halo writes only. It does not enforce a brightness value or
prevent Ring, a keypad user, or another application from changing the device.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass

from halo_gtk import storage_paths
from halo_gtk.atomic_io import atomic_write_text


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate settings key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class KeypadPreference:
    enabled: bool = False
    locked: bool = True
    revision: int = 0

    @property
    def allowed(self):
        return self.enabled and not self.locked


def _key(account, location_id, asset_id, zid):
    values = (account, location_id, asset_id, zid)
    if any(
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or any(ord(char) < 32 for char in value)
        for value in values
    ):
        raise ValueError("A known account and keypad identity are required")
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


class KeypadPreferences:
    def __init__(self, path=None):
        self.path = path or storage_paths.config_dir() / "keypad-settings.json"
        self._lock = threading.RLock()
        self._values = None

    def _load(self):
        if self._values is not None:
            return
        self._values = {}
        try:
            if self.path.stat().st_size > 128 * 1024:
                return
            payload = json.loads(
                self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
            )
            values = payload["keypads"]
            if (
                type(payload.get("schema")) is not int
                or payload["schema"] != 1
                or not isinstance(values, dict)
                or len(values) > 512
            ):
                return
            parsed = {}
            for key, value in values.items():
                if (
                    not isinstance(key, str)
                    or len(key) != 64
                    or any(char not in "0123456789abcdef" for char in key)
                    or not isinstance(value, dict)
                    or set(value) != {"enabled", "locked", "revision"}
                    or type(value["enabled"]) is not bool
                    or type(value["locked"]) is not bool
                    or type(value["revision"]) is not int
                    or not 0 <= value["revision"] < 2**53
                ):
                    return
                parsed[key] = (
                    KeypadPreference(False, True, value["revision"])
                    if value["revision"] >= 2**53 - 2
                    else KeypadPreference(**value)
                )
            self._values = parsed
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def get(self, account, location_id, asset_id, zid):
        key = _key(account, location_id, asset_id, zid)
        with self._lock:
            self._load()
            return self._values.get(key, KeypadPreference())

    def set(self, account, location_id, asset_id, zid, *, enabled, locked):
        if type(enabled) is not bool or type(locked) is not bool:
            raise ValueError("Keypad permission requires boolean values")
        key = _key(account, location_id, asset_id, zid)
        with self._lock:
            self._load()
            if key not in self._values and len(self._values) >= 512:
                raise ValueError("Keypad preference limit reached")
            previous = self._values.get(key, KeypadPreference())
            if previous.revision >= 2**53 - 2:
                self._values[key] = KeypadPreference(False, True, previous.revision)
                raise ValueError("Keypad preference revision limit reached")
            value = KeypadPreference(enabled, locked, previous.revision + 1)
            updated = dict(self._values, **{key: value})
            payload = {
                "schema": 1,
                "keypads": {
                    key: {"enabled": item.enabled, "locked": item.locked, "revision": item.revision}
                    for key, item in updated.items()
                },
            }
            try:
                atomic_write_text(
                    self.path,
                    json.dumps(payload, sort_keys=True) + "\n",
                    prefix=".keypad-",
                    mode=0o600,
                )
            except OSError:
                if not value.allowed:
                    self._values = updated
                raise
            self._values = updated
            return value


preferences = KeypadPreferences()
