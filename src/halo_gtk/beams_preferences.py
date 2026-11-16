"""Private local Beams opt-in and finite default duration; saving never actuates."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass

from halo_gtk import storage_paths
from halo_gtk.alarm.beams import ON_DURATIONS
from halo_gtk.atomic_io import atomic_write_text
from halo_gtk.keypad_preferences import _key, _unique_object


@dataclass(frozen=True, slots=True)
class BeamsPreference:
    enabled: bool = False
    duration: int = 60
    revision: int = 0


class BeamsPreferences:
    def __init__(self, path=None):
        self.path = path or storage_paths.config_dir() / "beams-settings.json"
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
            values = payload["lights"]
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
                    or set(value) != {"enabled", "duration", "revision"}
                    or type(value["enabled"]) is not bool
                    or type(value["duration"]) is not int
                    or value["duration"] not in ON_DURATIONS
                    or type(value["revision"]) is not int
                    or not 0 <= value["revision"] < 2**53
                ):
                    return
                parsed[key] = BeamsPreference(**value)
                if value["revision"] >= 2**53 - 2:
                    parsed[key] = BeamsPreference(False, value["duration"], value["revision"])
            self._values = parsed
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def get(self, account, location_id, asset_id, zid):
        key = _key(account, location_id, asset_id, zid)
        with self._lock:
            self._load()
            return self._values.get(key, BeamsPreference())

    def set(self, account, location_id, asset_id, zid, *, enabled, duration):
        if type(enabled) is not bool or type(duration) is not int or duration not in ON_DURATIONS:
            raise ValueError("Invalid lighting preference")
        key = _key(account, location_id, asset_id, zid)
        with self._lock:
            self._load()
            if key not in self._values and len(self._values) >= 512:
                raise ValueError("Lighting preference limit reached")
            previous = self._values.get(key, BeamsPreference())
            if previous.revision >= 2**53 - 2:
                self._values[key] = BeamsPreference(False, previous.duration, previous.revision)
                raise ValueError("Lighting preference revision limit reached")
            value = BeamsPreference(enabled, duration, previous.revision + 1)
            updated = {**self._values, key: value}
            try:
                atomic_write_text(
                    self.path,
                    json.dumps(
                        {
                            "schema": 1,
                            "lights": {key: asdict(item) for key, item in updated.items()},
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    prefix=".beams-",
                    mode=0o600,
                )
            except OSError:
                if not enabled:
                    self._values = updated
                raise
            self._values = updated
            return value


preferences = BeamsPreferences()
