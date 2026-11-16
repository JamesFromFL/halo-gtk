"""Bounded optional copies of explicitly retrieved cloud Alarm history.

The owner must enforce current-session and authoritative location access before
calling this store. Nothing here authenticates, fetches or controls a device.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from halo_gtk import storage_paths
from halo_gtk.alarm.history import AlarmHistoryEvent, AlarmHistoryPage, AlarmHistoryStatus
from halo_gtk.secret_store import valid_history_profile

DAYS = (0, 7, 30)
MAX_LOCATIONS = 20
MAX_EVENTS = 500
MAX_BYTES = 1024 * 1024
MAX_PROFILES = 16
SAVED_COVERAGE = (
    "Saved copies of explicitly requested Ring cloud history. Coverage is incomplete; "
    "row identities and distinct occurrences are unverified. This is not current Alarm state."
)
_LOCATION_FILE = re.compile(r"[0-9a-f]{64}\.json\Z")
_TEMPORARY_FILE = re.compile(r"\.history-[a-z0-9_]{8}\Z")
_FIELDS = {
    "key",
    "location_id",
    "kind",
    "title",
    "occurred_at",
    "retrieved_at",
    "zid",
    "asset_id",
    "event_id",
    "actor",
    "impulse_types",
}


class LocalHistoryError(RuntimeError):
    """Saved history is unavailable without changing live Alarm behavior."""


def _text(value, limit, *, optional=False):
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise LocalHistoryError("Invalid saved history text")
    return value


def _timestamp(value, *, optional=False):
    if value is None and optional:
        return None
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 946684800 <= value < 4102444800
    ):
        raise LocalHistoryError("Invalid saved history timestamp")
    return datetime.fromtimestamp(value, UTC)


def _record(event: AlarmHistoryEvent) -> dict:
    def timestamp(value):
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise LocalHistoryError("History timestamps require a timezone")
        return value.timestamp()

    result = {key: getattr(event, key) for key in _FIELDS}
    result["retrieved_at"] = timestamp(event.retrieved_at)
    result["occurred_at"] = timestamp(event.occurred_at)
    result["impulse_types"] = list(event.impulse_types)
    _event(result)
    return result


def _content_key(record: dict) -> str:
    # Parser keys include an offset position, not a verified Ring event ID.
    # Use content only for retention cohorts, never as proof of an occurrence.
    stable = {key: value for key, value in record.items() if key not in {"key", "retrieved_at"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _event(value) -> AlarmHistoryEvent:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise LocalHistoryError("Invalid saved history event schema")
    if value["kind"] not in {"mode", "alarm", "other"}:
        raise LocalHistoryError("Invalid saved history category")
    impulses = value["impulse_types"]
    if (
        not isinstance(impulses, list)
        or len(impulses) > 32
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", item)
            for item in impulses
        )
    ):
        raise LocalHistoryError("Invalid saved history impulse types")
    return AlarmHistoryEvent(
        key=_text(value["key"], 256),
        location_id=_text(value["location_id"], 512),
        kind=value["kind"],
        title=_text(value["title"], 256),
        occurred_at=_timestamp(value["occurred_at"], optional=True),
        retrieved_at=_timestamp(value["retrieved_at"]),
        zid=_text(value["zid"], 512, optional=True),
        asset_id=_text(value["asset_id"], 512, optional=True),
        event_id=_text(value["event_id"], 512, optional=True),
        actor=_text(value["actor"], 256, optional=True),
        impulse_types=tuple(impulses),
        provenance="Saved Ring cloud history",
        experimental=True,
    )


def _directory(path: Path, *, private=False):
    if not path.is_absolute():
        raise LocalHistoryError("Local history requires an absolute state directory")
    for parent in reversed((path, *path.parents)):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise LocalHistoryError("Local history path contains a symlink or non-directory")
        else:
            parent.mkdir(mode=0o700)
    if path.stat().st_uid != os.getuid() or (private and stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise LocalHistoryError("Local history directory is not private and owned")


def _read(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_BYTES
        ):
            raise LocalHistoryError("Local history file is not private, owned and bounded")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise LocalHistoryError("Saved history exceeds its size limit")
        try:
            return json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise LocalHistoryError("Saved history file is invalid") from exc
    finally:
        os.close(fd)


def _sync(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, value, *, guard=contextlib.nullcontext):
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True).encode()
    if len(encoded) > MAX_BYTES:
        raise LocalHistoryError("Saved history exceeds its size limit")
    _directory(path.parent, private=True)
    if path.exists() or path.is_symlink():
        _read(path)
    fd, temporary = tempfile.mkstemp(prefix=".history-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        with guard():
            os.replace(temporary, path)
        _sync(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class LocalAlarmHistory:
    def __init__(self, profile: str, *, root: Path | None = None, clock=time.time):
        if not valid_history_profile(profile):
            raise LocalHistoryError("Saved history has no bound account")
        self.root = root or storage_paths.state_dir() / "alarm-history"
        self.profile = profile
        self.clock = clock
        self.path = self.root / profile

    @contextlib.contextmanager
    def _locked(self, *, guard=contextlib.nullcontext):
        _directory(self.root, private=True)
        fd = os.open(self.root / "store.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise LocalHistoryError("Local history lock is not private and owned")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LocalHistoryError("Local history is busy; try again") from exc
            profiles = [path for path in self.root.iterdir() if valid_history_profile(path.name)]
            if not self.path.exists() and len(profiles) >= MAX_PROFILES:
                raise LocalHistoryError("Local history profile storage limit reached")
            _directory(self.path, private=True)
            # A crash can leave a prepared event copy before atomic replacement.
            # The exclusive store lock proves no cooperating writer owns it now.
            changed = False
            for path in self.path.iterdir():
                if not _TEMPORARY_FILE.fullmatch(path.name):
                    continue
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise LocalHistoryError("Temporary history file is not private and owned")
                with guard():
                    path.unlink()
                changed = True
            if changed:
                _sync(self.path)
            yield
        finally:
            os.close(fd)

    def _location_path(self, location: str) -> Path:
        _text(location, 512)
        return self.path / (hashlib.sha256(location.encode()).hexdigest() + ".json")

    def _policy(self) -> int:
        value = _read(self.path / "policy.json")
        if value is None:
            return 0
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "days"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["days"]) is not int
            or value["days"] not in DAYS
        ):
            raise LocalHistoryError("Saved history retention policy is invalid")
        return value["days"]

    def preferences(self, *, guard=contextlib.nullcontext) -> dict:
        with self._locked(guard=guard):
            self._prune(guard=guard)
            return {"available": True, "days": self._policy()}

    def set_retention(self, days: int, *, guard=contextlib.nullcontext) -> None:
        if type(days) is not int or days not in DAYS:
            raise LocalHistoryError("Choose Off, 7 days or 30 days")
        with self._locked(guard=guard):
            self._prune(guard=guard)
            _write(self.path / "policy.json", {"version": 1, "days": days}, guard=guard)

    def _entries(self, path: Path, location: str | None = None):
        value = _read(path)
        if value is None:
            return [], False
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "location", "entries", "limited"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or not isinstance(value["entries"], list)
            or len(value["entries"]) > MAX_EVENTS
            or type(value["limited"]) is not bool
        ):
            raise LocalHistoryError("Saved history location schema is invalid")
        source = _text(value["location"], 512)
        if self._location_path(source) != path or (location is not None and source != location):
            raise LocalHistoryError("Saved history location does not match its file")
        entries = []
        seen = set()
        now = self.clock()
        for item in value["entries"]:
            if not isinstance(item, dict) or set(item) != {"event", "expires"}:
                raise LocalHistoryError("Invalid saved history retention record")
            event = _event(item["event"])
            expires = _timestamp(item["expires"]).timestamp()
            received = event.retrieved_at.timestamp()
            if (
                event.location_id != source
                or event.key in seen
                or received > now + 300
                or not 0 < expires - received <= 30 * 86400
            ):
                raise LocalHistoryError("Invalid saved history event ownership or expiry")
            seen.add(event.key)
            if expires > now:
                entries.append(item)
        return entries, value["limited"]

    def _locations(self):
        result = [path for path in self.path.iterdir() if _LOCATION_FILE.fullmatch(path.name)]
        if len(result) > MAX_LOCATIONS:
            raise LocalHistoryError("Saved history location limit exceeded")
        return result

    def _prune(self, *, guard=contextlib.nullcontext):
        for path in self._locations():
            value = _read(path)
            entries, limited = self._entries(path)
            if not entries:
                with guard():
                    path.unlink()
                _sync(self.path)
            elif len(entries) != len(value["entries"]):
                _write(path, {**value, "entries": entries, "limited": limited}, guard=guard)

    def save(self, page: AlarmHistoryPage, *, guard=contextlib.nullcontext) -> None:
        if (
            not isinstance(page, AlarmHistoryPage)
            or page.from_cache
            or page.status not in {AlarmHistoryStatus.READY, AlarmHistoryStatus.PARTIAL}
            or not page.events
        ):
            return
        with self._locked(guard=guard):
            self._prune(guard=guard)
            days = self._policy()
            if not days:
                return
            path = self._location_path(page.location_id)
            if not path.exists() and len(self._locations()) >= MAX_LOCATIONS:
                raise LocalHistoryError("Saved history location limit reached")
            entries, limited = self._entries(path, page.location_id)
            by_key = {item["event"]["key"]: item for item in entries}
            cohorts = {}
            for item in entries:
                cohorts.setdefault(_content_key(item["event"]), []).append(item)
            incoming = {}
            seen = set()
            now = self.clock()
            if len(page.events) > 1000:
                raise LocalHistoryError("Too many source events for one saved history page")
            for event in page.events:
                record = _record(event)
                received = event.retrieved_at.timestamp()
                if (
                    event.location_id != page.location_id
                    or received > now + 300
                    or event.key in seen
                ):
                    raise LocalHistoryError(
                        "History response has invalid location, time or repeated row keys"
                    )
                seen.add(event.key)
                incoming.setdefault(_content_key(record), []).append(record)
            for cohort, records in incoming.items():
                previous = cohorts.get(cohort, [])
                # Retain at most the largest observed same-page multiplicity.
                # Shifted offsets must not add copies or extend their expiry.
                if previous:
                    limited |= {item["event"]["key"] for item in previous} != {
                        record["key"] for record in records
                    }
                for record in records:
                    if (
                        record["key"] in by_key
                        and _content_key(by_key[record["key"]]["event"]) != cohort
                    ):
                        raise LocalHistoryError("Ambiguous saved history row key")
                extra = max(0, len(records) - len(previous))
                new_records = [record for record in records if record["key"] not in by_key][:extra]
                for record in new_records:
                    if previous:
                        received = min(item["event"]["retrieved_at"] for item in previous)
                        expires = min(item["expires"] for item in previous)
                        record = {**record, "retrieved_at": received}
                    else:
                        expires = record["retrieved_at"] + days * 86400
                    if expires > now:
                        by_key[record["key"]] = {"event": record, "expires": expires}
            values = sorted(
                by_key.values(),
                key=lambda item: (item["event"]["retrieved_at"], item["event"]["key"]),
                reverse=True,
            )
            limited = limited or page.truncated or len(values) > MAX_EVENTS
            value = {
                "version": 1,
                "location": page.location_id,
                "entries": values[:MAX_EVENTS],
                "limited": limited,
            }
            while len(json.dumps(value).encode()) > MAX_BYTES and value["entries"]:
                value["entries"].pop()
                value["limited"] = True
            _write(path, value, guard=guard)

    def load(self, location: str, *, guard=contextlib.nullcontext) -> AlarmHistoryPage:
        with self._locked(guard=guard):
            self._prune(guard=guard)
            entries, limited = self._entries(self._location_path(location), location)
            events = tuple(_event(item["event"]) for item in entries)
            retrieved = max((event.retrieved_at for event in events), default=None)
            return AlarmHistoryPage(
                location,
                events=events,
                status=AlarmHistoryStatus.PARTIAL if events else AlarmHistoryStatus.EMPTY,
                truncated=limited,
                from_cache=True,
                stale=True,
                retrieved_at=retrieved,
                message="Saved Alarm history; these copies are not a fresh Ring request."
                if events
                else "No unexpired saved Alarm history for this location.",
                coverage=SAVED_COVERAGE,
            )

    def clear(self, location: str | None = None, *, guard=contextlib.nullcontext) -> None:
        """Explicit clear/revocation may remove a corrupt owned file without parsing it."""
        with self._locked(guard=guard):
            paths = [self._location_path(location)] if location is not None else self._locations()
            for path in paths:
                if not path.exists() and not path.is_symlink():
                    continue
                info = path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise LocalHistoryError("Refusing to clear a file without private ownership")
                with guard():
                    path.unlink()
            _sync(self.path)

    def retain_locations(self, locations: set[str], *, guard=contextlib.nullcontext) -> None:
        """Remove copies absent from an authoritative account location inventory."""
        allowed = {self._location_path(location) for location in locations}
        with self._locked(guard=guard):
            changed = False
            for path in self._locations():
                if path not in allowed:
                    info = path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        raise LocalHistoryError(
                            "Saved history removal requires an owned private file"
                        )
                    with guard():
                        path.unlink()
                    changed = True
            if changed:
                _sync(self.path)

    def annotate_save(
        self, page: AlarmHistoryPage, *, guard=contextlib.nullcontext
    ) -> AlarmHistoryPage:
        """Keep a successful cloud result usable when optional local storage fails."""
        try:
            self.save(page, guard=guard)
        except (LocalHistoryError, OSError, ValueError):
            return replace(page, message=page.message + " Local history could not be saved.")
        return page
