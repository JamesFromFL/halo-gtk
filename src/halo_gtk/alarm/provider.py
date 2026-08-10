"""Provider-neutral contracts for Halo's Ring Alarm backend."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from itertools import islice
from typing import Any, Protocol

from halo_gtk.alarm.models import AlarmMode, AlarmWriteAuthorization

JsonObject = Mapping[str, Any]
_MAX_DISCOVERY_RECORDS = 1_024
_MAX_LOCATIONS = 128
_MAX_ASSET_HINTS = 128
_MAX_IDENTIFIER_LENGTH = 512
_MAX_NAME_LENGTH = 256


class AlarmRestGateway(Protocol):
    """Minimal authenticated REST surface needed by the Alarm transport."""

    async def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        json: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> JsonObject:
        """Return one decoded JSON object or raise a sanitized request error."""


class AlarmRequestError(RuntimeError):
    """Sanitized authenticated-request failure."""

    def __init__(
        self,
        code: str = "request-failed",
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class AlarmCommandSupersededError(RuntimeError):
    """Raised by a send-boundary guard when its client is no longer active."""


@dataclass(frozen=True, slots=True)
class AlarmLocationSeed:
    """Non-secret discovery data needed to request a location ticket."""

    location_id: str
    name: str = ""
    asset_ids: tuple[str, ...] = ()
    write_authorization: AlarmWriteAuthorization = AlarmWriteAuthorization.UNKNOWN

    def __post_init__(self) -> None:
        location_id = _validated_text(
            self.location_id,
            "location_id",
            _MAX_IDENTIFIER_LENGTH,
            allow_empty=False,
        )
        name = _validated_text(
            self.name,
            "name",
            _MAX_NAME_LENGTH,
            allow_empty=True,
        )
        if not isinstance(self.write_authorization, AlarmWriteAuthorization):
            raise ValueError("write_authorization must be an AlarmWriteAuthorization")
        if isinstance(self.asset_ids, str):
            raise ValueError("asset_ids must contain device identifiers")
        raw_asset_ids = tuple(self.asset_ids)
        if len(raw_asset_ids) > _MAX_ASSET_HINTS:
            raise ValueError("asset_ids contains too many values")
        asset_ids = tuple(
            dict.fromkeys(
                _validated_text(
                    value,
                    "asset_id",
                    _MAX_IDENTIFIER_LENGTH,
                    allow_empty=False,
                )
                for value in raw_asset_ids
            )
        )
        object.__setattr__(self, "location_id", location_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "asset_ids", asset_ids)


@unique
class AlarmCommandStatus(StrEnum):
    """Terminal outcome of a mode command."""

    CONFIRMED = "confirmed"
    NEEDS_BYPASS = "needs-bypass"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission-denied"
    STALE_REVISION = "stale-revision"
    TIMEOUT_UNKNOWN = "timeout-unknown"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class AlarmCommandResult:
    """Typed result returned without leaking Ring protocol details."""

    status: AlarmCommandStatus
    location_id: str
    requested_mode: AlarmMode
    confirmed_mode: AlarmMode = AlarmMode.UNKNOWN
    bypass_ids: tuple[str, ...] = ()
    code: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.status is AlarmCommandStatus.CONFIRMED


def location_seeds_from_base_stations(
    raw_base_stations: Mapping[object, object] | Iterable[object] | None,
    *,
    raw_locations: Mapping[object, object] | Iterable[object] | None = None,
) -> tuple[AlarmLocationSeed, ...]:
    """Group python-ring-doorbell base-station records by location.

    Ticket discovery is authoritative for asset UUIDs.  The device response is
    used only to identify Alarm locations; optional device IDs are retained as
    hints for sanitized fixtures and forward compatibility.
    """

    if raw_base_stations is None:
        return ()
    if isinstance(raw_base_stations, Mapping):
        records = raw_base_stations.values()
    else:
        records = raw_base_stations

    location_names = _location_names(raw_locations)
    grouped: dict[str, dict[str, Any]] = {}
    for value in islice(records, _MAX_DISCOVERY_RECORDS):
        if not isinstance(value, Mapping):
            continue
        location_id = _first_text(
            value,
            "location_id",
            "locationId",
            limit=_MAX_IDENTIFIER_LENGTH,
        )
        if location_id is None:
            continue
        if location_id not in grouped and len(grouped) >= _MAX_LOCATIONS:
            continue
        entry = grouped.setdefault(
            location_id,
            {
                "name": location_names.get(location_id, ""),
                "asset_ids": [],
                "write_authorization": AlarmWriteAuthorization.UNKNOWN,
            },
        )
        if not entry["name"]:
            entry["name"] = (
                _first_text(
                    value,
                    "location_name",
                    "locationName",
                    limit=_MAX_NAME_LENGTH,
                )
                or ""
            )
        asset_id = _first_text(
            value,
            "asset_id",
            "assetId",
            "device_id",
            "deviceId",
            limit=_MAX_IDENTIFIER_LENGTH,
        )
        if (
            asset_id is not None
            and asset_id not in entry["asset_ids"]
            and len(entry["asset_ids"]) < _MAX_ASSET_HINTS
        ):
            entry["asset_ids"].append(asset_id)
        if value.get("owned") is True:
            entry["write_authorization"] = AlarmWriteAuthorization.ALLOWED

    return tuple(
        AlarmLocationSeed(
            location_id=location_id,
            name=data["name"],
            asset_ids=tuple(data["asset_ids"]),
            write_authorization=data["write_authorization"],
        )
        for location_id, data in sorted(grouped.items())
    )


def _location_names(
    raw_locations: Mapping[object, object] | Iterable[object] | None,
) -> dict[str, str]:
    if raw_locations is None:
        return {}
    if isinstance(raw_locations, Mapping):
        nested = raw_locations.get("user_locations")
        if isinstance(nested, list | tuple):
            records = nested
        elif "location_id" in raw_locations or "locationId" in raw_locations:
            records = (raw_locations,)
        else:
            return {}
    else:
        records = raw_locations

    result: dict[str, str] = {}
    for value in islice(records, _MAX_DISCOVERY_RECORDS):
        if not isinstance(value, Mapping):
            continue
        location_id = _first_text(
            value,
            "location_id",
            "locationId",
            limit=_MAX_IDENTIFIER_LENGTH,
        )
        name = _first_text(value, "name", limit=_MAX_NAME_LENGTH)
        if (
            location_id is not None
            and name is not None
            and (location_id in result or len(result) < _MAX_LOCATIONS)
        ):
            result[location_id] = name
    return result


def _first_text(
    value: Mapping[object, object],
    *keys: str,
    limit: int,
) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            candidate = str(candidate)
        if (
            isinstance(candidate, str)
            and candidate.strip()
            and len(candidate.strip()) <= limit
            and not any(ord(character) < 32 for character in candidate)
        ):
            return candidate.strip()
    return None


def _validated_text(
    value: object,
    label: str,
    limit: int,
    *,
    allow_empty: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = value.strip()
    if (
        (not result and not allow_empty)
        or len(result) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return result
