"""Immutable Light Group evidence; no aggregate-to-member state inference."""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

MAX_LOCATIONS = 16
MAX_GROUPS_PER_LOCATION = 128
MAX_CACHED_GROUPS = 512
MAX_MEMBERS_PER_GROUP = 128
MAX_CACHED_MEMBERS = 8192
ON_DURATIONS = (60, 300, 900)
_MAX_API_ID = 9_223_372_036_854_775_807
_DELETION_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})\Z"
)


class LightGroupsReadStatus(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    STALE = "stale"
    ERROR = "error"
    CLOSED = "closed"


class LightGroupPermission(StrEnum):
    UNKNOWN = "unknown"
    DENIED = "denied"


class LightGroupOperationStatus(StrEnum):
    PREPARING = "preparing"
    POSSIBLY_SENT = "possibly-sent"
    ACCEPTED = "accepted"
    REPORTED = "reported"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LightGroupIntent:
    generation: int
    location_id: str
    group_id: str
    revision: int
    identity_epoch: int
    _cancelled: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


@dataclass(frozen=True, slots=True)
class LightGroupMember:
    api_id: str
    location_id: str
    name: str
    kind: str
    lights_on: bool | None = None


@dataclass(frozen=True, slots=True)
class LightGroupMemberReport:
    api_id: str
    lights_on: bool | None = None


@dataclass(frozen=True, slots=True)
class LightGroupReport:
    location_id: str
    group_id: str
    report: bool | None
    members: tuple[LightGroupMemberReport, ...] = ()
    members_complete: bool = False


@dataclass(frozen=True, slots=True)
class LightGroupOperationSnapshot:
    operation_id: int
    status: LightGroupOperationStatus
    active: bool
    on: bool
    duration: int | None = None
    possibly_sent: bool = False
    code: str | None = None

    @property
    def requested_on(self) -> bool:
        return self.on

    @property
    def requested_duration(self) -> int | None:
        return self.duration


@dataclass(frozen=True, slots=True)
class LightGroupSnapshot:
    location_id: str
    group_id: str
    name: str
    members: tuple[LightGroupMember, ...] = ()
    membership_complete: bool = False
    revision: int = 1
    identity_epoch: int = 1
    report: bool | None = None
    stale: bool = True
    inventory_received_at: float | None = None
    inventory_monotonic: float | None = None
    report_received_at: float | None = None
    report_monotonic: float | None = None
    permission: LightGroupPermission = LightGroupPermission.UNKNOWN
    can_write: bool = False
    unavailable_reason: str | None = "refresh-required"
    operation: LightGroupOperationSnapshot | None = None
    status: LightGroupsReadStatus = LightGroupsReadStatus.UNKNOWN
    code: str | None = None
    # An absolute suspend-aware monotonic deadline, not a relative HTTP header.
    read_retry_after: float | None = None
    poll_allowed: bool = True
    read_failure_count: int = 0

    @property
    def member_count(self) -> int | None:
        return len(self.members) if self.membership_complete else None


@dataclass(frozen=True, slots=True)
class LightGroupLocationSnapshot:
    location_id: str
    name: str
    revision: int = 1
    identity_epoch: int = 1
    groups: tuple[LightGroupSnapshot, ...] = ()
    complete: bool = False
    has_inventory: bool = False
    status: LightGroupsReadStatus = LightGroupsReadStatus.UNKNOWN
    code: str | None = None
    inventory_received_at: float | None = None
    inventory_monotonic: float | None = None
    read_retry_after: float | None = None
    poll_allowed: bool = True
    read_failure_count: int = 0

    def find_group(self, group_id: str) -> LightGroupSnapshot | None:
        return next((group for group in self.groups if group.group_id == group_id), None)


@dataclass(frozen=True, slots=True)
class LightGroupsSnapshot:
    generation: int
    revision: int = 0
    locations: tuple[LightGroupLocationSnapshot, ...] = ()
    complete: bool = False
    has_inventory: bool = False
    status: LightGroupsReadStatus = LightGroupsReadStatus.UNKNOWN
    code: str | None = None
    inventory_received_at: float | None = None
    inventory_monotonic: float | None = None
    read_retry_after: float | None = None
    poll_allowed: bool = True
    read_failure_count: int = 0

    def find_location(self, location_id: str) -> LightGroupLocationSnapshot | None:
        return next((loc for loc in self.locations if loc.location_id == location_id), None)

    def find_group(self, location_id: str, group_id: str) -> LightGroupSnapshot | None:
        location = self.find_location(location_id)
        return location.find_group(group_id) if location is not None else None


@dataclass(frozen=True, slots=True)
class LightGroupResult:
    status: LightGroupOperationStatus
    location_id: str
    group_id: str
    operation_id: int | None = None
    code: str | None = None
    reported: bool | None = None

    @property
    def confirmed(self) -> bool:
        """Only a matching group report, never proof of every physical member."""
        return self.status == LightGroupOperationStatus.REPORTED


class LightGroupSchemaError(ValueError):
    """Static safe code only; never retain upstream raw objects or private text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_identifier(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value in {".", ".."}:
        raise LightGroupSchemaError("invalid-identifier")
    try:
        invalid = len(value.encode("utf-8")) > 512 or any(
            unicodedata.category(char) in {"Cc", "Cf"} for char in value
        )
    except UnicodeError:
        invalid = True
    if invalid:
        raise LightGroupSchemaError("invalid-identifier")
    return value


def _name(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    return text.strip()[:160] or fallback


def _kind(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise LightGroupSchemaError("invalid-member-kind")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise LightGroupSchemaError("invalid-member-kind")
    return value


def _api_id(value: object, *, report: bool = False) -> str:
    if report:
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdigit()
            or not 1 <= len(value) <= 19
            or value.startswith("0")
        ):
            raise LightGroupSchemaError("invalid-member-id")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_API_ID:
        raise LightGroupSchemaError("invalid-member-id")
    return str(value)


def _deleted(row: Mapping) -> bool:
    value = row.get("deleted_at")
    if value is None:
        return False
    if not isinstance(value, str) or len(value) > 80 or not _DELETION_TIMESTAMP.fullmatch(value):
        raise LightGroupSchemaError("invalid-deletion")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise LightGroupSchemaError("invalid-deletion") from None
    return True


def _complete(payload: Mapping, count: int, _depth: int = 0) -> None:
    """Recognize explicit truncation hints; unknown paging envelopes are unsafe."""
    for key in (
        "has_more",
        "hasMore",
        "has_next_page",
        "truncated",
        "is_truncated",
        "partial",
        "incomplete",
    ):
        if key in payload and payload[key] is not False:
            raise LightGroupSchemaError("incomplete-response")
    for key in ("complete", "is_complete"):
        if key in payload and payload[key] is not True:
            raise LightGroupSchemaError("incomplete-response")
    for key in (
        "next",
        "next_page",
        "next_page_token",
        "next_cursor",
        "cursor",
        "continuation_token",
        "next_page_url",
        "nextPage",
    ):
        if key in payload and payload[key] not in (None, ""):
            raise LightGroupSchemaError("incomplete-response")
    for key in ("total", "total_count", "total_entries", "total_results", "count"):
        if key in payload and (type(payload[key]) is not int or payload[key] != count):
            raise LightGroupSchemaError("incomplete-response")
    for key in ("offset", "page"):
        if key in payload and (
            type(payload[key]) is not int or payload[key] != (1 if key == "page" else 0)
        ):
            raise LightGroupSchemaError("incomplete-response")
    for key in ("pagination", "paging", "links"):
        if key in payload and payload[key] not in (None, {}, []):
            raise LightGroupSchemaError("incomplete-response")
    if any(
        key in payload for key in ("limit", "page_size", "pageSize", "total_pages", "totalPages")
    ):
        raise LightGroupSchemaError("incomplete-response")
    meta = payload.get("meta")
    if meta is not None:
        if not isinstance(meta, Mapping) or _depth >= 4:
            raise LightGroupSchemaError("incomplete-response")
        _complete(meta, count, _depth + 1)


def _rows(payload: object, key: str, maximum: int) -> list:
    if not isinstance(payload, Mapping) or not isinstance(payload.get(key), list):
        raise LightGroupSchemaError("invalid-response")
    rows = payload[key]
    if len(rows) > maximum:
        raise LightGroupSchemaError("response-limit")
    _complete(payload, len(rows))
    return rows


def _row(value: object) -> Mapping:
    if not isinstance(value, Mapping):
        raise LightGroupSchemaError("invalid-row")
    return value


def parse_locations(payload: object) -> tuple[LightGroupLocationSnapshot, ...]:
    result, ids = [], set()
    for value in _rows(payload, "user_locations", MAX_LOCATIONS):
        row = _row(value)
        location_id = validate_identifier(row.get("location_id"))
        if location_id in ids:
            raise LightGroupSchemaError("duplicate-location")
        ids.add(location_id)
        result.append(LightGroupLocationSnapshot(location_id, _name(row.get("name"), "Location")))
    return tuple(sorted(result, key=lambda location: location.location_id))


def parse_groups(payload: object, location_id: str) -> tuple[LightGroupSnapshot, ...]:
    location_id = validate_identifier(location_id)
    if (
        isinstance(payload, Mapping)
        and "location_id" in payload
        and validate_identifier(payload["location_id"]) != location_id
    ):
        raise LightGroupSchemaError("location-mismatch")
    result, ids = [], set()
    for value in _rows(payload, "device_groups", MAX_GROUPS_PER_LOCATION):
        row = _row(value)
        group_id = validate_identifier(row.get("device_group_id"))
        if group_id in ids:
            raise LightGroupSchemaError("duplicate-group")
        ids.add(group_id)
        if validate_identifier(row.get("location_id")) != location_id:
            raise LightGroupSchemaError("location-mismatch")
        if _deleted(row):
            continue
        members, member_ids = [], set()
        for member in _rows(row, "devices", MAX_MEMBERS_PER_GROUP):
            member = _row(member)
            api_id = _api_id(member.get("doorbot_id"))
            if api_id in member_ids:
                raise LightGroupSchemaError("duplicate-member")
            member_ids.add(api_id)
            if validate_identifier(member.get("location_id")) != location_id:
                raise LightGroupSchemaError("location-mismatch")
            kind = _kind(member.get("type"))
            if _deleted(member):
                continue
            members.append(
                LightGroupMember(api_id, location_id, _name(member.get("name"), "Device"), kind)
            )
        result.append(
            LightGroupSnapshot(
                location_id,
                group_id,
                _name(row.get("name"), "Light group"),
                members=tuple(sorted(members, key=lambda member: member.api_id)),
                membership_complete=True,
            )
        )
    return tuple(sorted(result, key=lambda group: group.group_id))


def parse_group_report(
    payload: object,
    location_id: str,
    group_id: str,
    *,
    members: tuple[LightGroupMember, ...] | None = None,
) -> LightGroupReport:
    location_id, group_id = validate_identifier(location_id), validate_identifier(group_id)
    row = _row(payload)
    if validate_identifier(row.get("device_group_id")) != group_id:
        raise LightGroupSchemaError("group-mismatch")
    if "location_id" in row and validate_identifier(row["location_id"]) != location_id:
        raise LightGroupSchemaError("location-mismatch")
    reported = row.get("lights_on")
    reported = reported if isinstance(reported, bool) else None
    reports, ids, seen = [], set(), set()
    for value in _rows(row, "devices", MAX_MEMBERS_PER_GROUP):
        value = _row(value)
        api_id = _api_id(value.get("id"), report=True)
        if api_id in seen:
            raise LightGroupSchemaError("duplicate-member")
        seen.add(api_id)
        if "location_id" in value and validate_identifier(value["location_id"]) != location_id:
            raise LightGroupSchemaError("location-mismatch")
        if _deleted(value):
            continue
        ids.add(api_id)
        light = value.get("lights_on")
        reports.append(LightGroupMemberReport(api_id, light if isinstance(light, bool) else None))
    if members is not None and (
        any(member.location_id != location_id for member in members)
        or ids != {member.api_id for member in members}
    ):
        raise LightGroupSchemaError("membership-mismatch")
    return LightGroupReport(
        location_id,
        group_id,
        reported,
        tuple(sorted(reports, key=lambda report: report.api_id)),
        True,
    )
