"""Pure validation and encoding for Ring's undocumented CLAP wire protocol.

The wire schema is based on behavioral documentation in ``ring-client-api``
at Koush's pinned commit ``516e96a24ec279168c246795e623b6bfdf58ec45``.
It is deliberately isolated here because Ring does not publish this protocol.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias

from halo_gtk.alarm.models import AlarmConnectionStatus, AlarmMode

MAX_FRAME_BYTES = 1_048_576
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000
MAX_DEVICE_DOCUMENTS = 4_096
MAX_IDENTIFIER_LENGTH = 512


class AlarmProtocolError(ValueError):
    """Base class for rejected CLAP frames and outbound messages."""


class AlarmFrameTooLargeError(AlarmProtocolError):
    """Raised when a CLAP frame exceeds the configured byte limit."""


class AlarmSchemaError(AlarmProtocolError):
    """Raised when a known CLAP message has an invalid schema."""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class DeviceDocument:
    """One immutable, flattened ``general.v2`` plus ``device.v1`` document."""

    zid: str
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        _validate_identifier(self.zid, "device zid")
        if not isinstance(self.data, Mapping):
            raise AlarmSchemaError("device data must be an object")
        data = dict(self.data)
        if data.get("zid") != self.zid:
            raise AlarmSchemaError("device document zid does not match its data")
        object.__setattr__(self, "data", _freeze_json(data))

    @property
    def device_type(self) -> str:
        value = self.data.get("deviceType")
        return value if isinstance(value, str) else ""

    @property
    def raw_type(self) -> str:
        return self.device_type

    @property
    def mode(self) -> str | None:
        value = self.data.get("mode")
        return value if isinstance(value, str) else None

    @property
    def faulted(self) -> bool | None:
        value = self.data.get("faulted")
        return value if isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class DeviceInfoDocList:
    """A complete device inventory returned by one asset."""

    asset_id: str
    devices: tuple[DeviceDocument, ...]
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class DeviceInfoDocUpdate:
    """One realtime patch batch for devices on an asset."""

    asset_id: str
    devices: tuple[DeviceDocument, ...]
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class AssetSessionInfo:
    """Connectivity reported for a CLAP asset."""

    asset_id: str
    connection: AlarmConnectionStatus
    kind: str = ""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """A location-level batch of asset session states."""

    sessions: tuple[AssetSessionInfo, ...]
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class HubDisconnection:
    """The server's instruction to discard and recreate a location socket."""

    asset_id: str | None = None
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class UnknownMessage:
    """Forward-compatible representation that intentionally retains no body."""

    channel: str
    message_type: str
    datatype: str
    asset_id: str | None = None
    sequence: int | None = None


ClapMessage: TypeAlias = (
    DeviceInfoDocList | DeviceInfoDocUpdate | SessionInfo | HubDisconnection | UnknownMessage
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AlarmSchemaError("CLAP object contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_nonstandard_number(value: str) -> None:
    raise AlarmSchemaError(f"non-standard JSON number: {value}")


def _validate_json_bounds(value: Any, max_depth: int) -> None:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise AlarmSchemaError("CLAP frame contains too many JSON values")
        if depth > max_depth:
            raise AlarmSchemaError("CLAP frame exceeds the JSON nesting limit")
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list | tuple):
            stack.extend((child, depth + 1) for child in item)


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlarmSchemaError(f"{label} must be a non-empty string")
    if len(value) > MAX_IDENTIFIER_LENGTH or any(ord(character) < 32 for character in value):
        raise AlarmSchemaError(f"{label} is invalid")
    return value


def _optional_string(value: object, label: str) -> str:
    if value is None:
        return ""
    if (
        not isinstance(value, str)
        or len(value) > MAX_IDENTIFIER_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise AlarmSchemaError(f"{label} must be a bounded string")
    return value


def _optional_sequence(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**53:
        raise AlarmSchemaError("message sequence must be a non-negative safe integer")
    return value


def _require_body(message: Mapping[str, Any]) -> list[Any]:
    body = message.get("body")
    if not isinstance(body, list):
        raise AlarmSchemaError("known CLAP message body must be an array")
    if len(body) > MAX_DEVICE_DOCUMENTS:
        raise AlarmSchemaError("known CLAP message body contains too many entries")
    return body


def _flatten_device_document(value: object) -> DeviceDocument:
    if not isinstance(value, dict):
        raise AlarmSchemaError("device document must be an object")

    flat: dict[str, Any] = {}
    saw_versioned_document = False
    for namespace, version in (("general", "v2"), ("device", "v1")):
        section = value.get(namespace)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise AlarmSchemaError(f"device document {namespace} must be an object")
        versioned = section.get(version)
        if versioned is None:
            continue
        if not isinstance(versioned, dict):
            raise AlarmSchemaError(f"device document {namespace}.{version} must be an object")
        flat.update(versioned)
        saw_versioned_document = True

    if not saw_versioned_document:
        raise AlarmSchemaError("device document has no supported versioned data")
    zid = _validate_identifier(flat.get("zid"), "device zid")
    return DeviceDocument(zid=zid, data=flat)


def _parse_device_documents(message: Mapping[str, Any]) -> tuple[DeviceDocument, ...]:
    documents = tuple(_flatten_device_document(value) for value in _require_body(message))
    zids = [document.zid for document in documents]
    if len(zids) != len(set(zids)):
        raise AlarmSchemaError("device document batch contains duplicate zids")
    return documents


def _parse_session_info(message: Mapping[str, Any], sequence: int | None) -> SessionInfo:
    sessions: list[AssetSessionInfo] = []
    seen_assets: set[str] = set()
    connection_map = {
        "online": AlarmConnectionStatus.ONLINE,
        "cell-backup": AlarmConnectionStatus.CELLULAR_BACKUP,
        "offline": AlarmConnectionStatus.OFFLINE,
        "unknown": AlarmConnectionStatus.UNKNOWN,
    }
    for value in _require_body(message):
        if not isinstance(value, dict):
            raise AlarmSchemaError("session entry must be an object")
        asset_id = _validate_identifier(value.get("assetUuid"), "session assetUuid")
        if asset_id in seen_assets:
            raise AlarmSchemaError("session batch contains duplicate asset ids")
        seen_assets.add(asset_id)
        raw_connection = value.get("connectionStatus")
        if not isinstance(raw_connection, str):
            raise AlarmSchemaError("session connectionStatus must be a string")
        sessions.append(
            AssetSessionInfo(
                asset_id=asset_id,
                connection=connection_map.get(raw_connection, AlarmConnectionStatus.UNKNOWN),
                kind=_optional_string(value.get("kind"), "session kind"),
            )
        )
    return SessionInfo(sessions=tuple(sessions), sequence=sequence)


def parse_clap_frame(
    frame: str | bytes,
    *,
    max_frame_bytes: int = MAX_FRAME_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
) -> ClapMessage:
    """Parse and validate one complete CLAP WebSocket text frame."""

    if (
        isinstance(max_frame_bytes, bool)
        or not isinstance(max_frame_bytes, int)
        or max_frame_bytes <= 0
    ):
        raise ValueError("max_frame_bytes must be positive")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth <= 0:
        raise ValueError("max_depth must be positive")
    if isinstance(frame, bytes):
        if len(frame) > max_frame_bytes:
            raise AlarmFrameTooLargeError("CLAP frame exceeds the byte limit")
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AlarmSchemaError("CLAP frame is not valid UTF-8") from exc
    elif isinstance(frame, str):
        try:
            encoded_size = len(frame.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise AlarmSchemaError("CLAP frame is not valid Unicode") from exc
        if encoded_size > max_frame_bytes:
            raise AlarmFrameTooLargeError("CLAP frame exceeds the byte limit")
        text = frame
    else:
        raise TypeError("CLAP frame must be text or bytes")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except AlarmProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AlarmSchemaError("CLAP frame is not valid JSON") from exc

    _validate_json_bounds(payload, max_depth)
    if not isinstance(payload, dict):
        raise AlarmSchemaError("CLAP frame root must be an object")

    channel = _optional_string(payload.get("channel"), "channel")
    message = payload.get("msg")
    if not isinstance(message, dict):
        raise AlarmSchemaError("CLAP frame msg must be an object")
    message_type = _optional_string(message.get("msg"), "message type")
    datatype = _optional_string(message.get("datatype"), "message datatype")
    sequence = _optional_sequence(message.get("seq"))
    source = message.get("src")
    if source is not None:
        source = _validate_identifier(source, "message source")

    if datatype == "HubDisconnectionEventType":
        return HubDisconnection(asset_id=source, sequence=sequence)
    if message_type == "DeviceInfoDocGetList":
        asset_id = _validate_identifier(source, "device-list source")
        return DeviceInfoDocList(
            asset_id=asset_id,
            devices=_parse_device_documents(message),
            sequence=sequence,
        )
    if channel == "DataUpdate" and datatype == "DeviceInfoDocType":
        asset_id = _validate_identifier(source, "device-update source")
        return DeviceInfoDocUpdate(
            asset_id=asset_id,
            devices=_parse_device_documents(message),
            sequence=sequence,
        )
    if message_type == "SessionInfo":
        return _parse_session_info(message, sequence)
    return UnknownMessage(
        channel=channel,
        message_type=message_type,
        datatype=datatype,
        asset_id=source,
        sequence=sequence,
    )


def _validate_outbound_sequence(sequence: int) -> None:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence < 2**53:
        raise ValueError("sequence must be a positive safe integer")


def _validate_outbound_identifier(value: str, label: str) -> str:
    try:
        return _validate_identifier(value, label)
    except AlarmSchemaError as exc:
        raise ValueError(str(exc)) from exc


def encode_device_list_request(asset_id: str, sequence: int) -> dict[str, Any]:
    """Build a ``DeviceInfoDocGetList`` request envelope."""

    asset_id = _validate_outbound_identifier(asset_id, "asset_id")
    _validate_outbound_sequence(sequence)
    return {
        "channel": "message",
        "msg": {
            "msg": "DeviceInfoDocGetList",
            "dst": asset_id,
            "seq": sequence,
        },
    }


def encode_mode_command(
    asset_id: str,
    panel_zid: str,
    mode: AlarmMode,
    sequence: int,
    bypass_zids: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one exact security-panel mode-switch command envelope."""

    asset_id = _validate_outbound_identifier(asset_id, "asset_id")
    panel_zid = _validate_outbound_identifier(panel_zid, "panel_zid")
    _validate_outbound_sequence(sequence)
    if not isinstance(mode, AlarmMode) or mode is AlarmMode.UNKNOWN:
        raise ValueError("mode must be DISARMED, HOME, or AWAY")

    raw_modes = {
        AlarmMode.DISARMED: "none",
        AlarmMode.HOME: "some",
        AlarmMode.AWAY: "all",
    }
    if isinstance(bypass_zids, str | bytes):
        raise ValueError("bypass_zids must be an iterable of identifiers")
    bypass = tuple(_validate_outbound_identifier(zid, "bypass zid") for zid in bypass_zids)
    if len(bypass) > 256:
        raise ValueError("no more than 256 bypass zids are allowed")
    if len(bypass) != len(set(bypass)):
        raise ValueError("bypass zids must be unique")

    command_data: dict[str, Any] = {"mode": raw_modes[mode]}
    if bypass:
        command_data["bypass"] = list(bypass)
    return {
        "channel": "message",
        "msg": {
            "msg": "DeviceInfoSet",
            "datatype": "DeviceInfoSetType",
            "dst": asset_id,
            "body": [
                {
                    "zid": panel_zid,
                    "command": {
                        "v1": [
                            {
                                "commandType": "security-panel.switch-mode",
                                "data": command_data,
                            }
                        ]
                    },
                }
            ],
            "seq": sequence,
        },
    }


def encode_clap_frame(payload: Mapping[str, Any]) -> str:
    """Serialize an already constructed outbound CLAP envelope."""

    if not isinstance(payload, Mapping):
        raise TypeError("CLAP payload must be a mapping")
    _validate_json_bounds(payload, MAX_JSON_DEPTH)
    try:
        encoded = json.dumps(
            _json_plain(payload),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except AlarmProtocolError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise AlarmSchemaError("outbound CLAP payload is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
        raise AlarmFrameTooLargeError("outbound CLAP frame exceeds the byte limit")
    return encoded


def _json_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AlarmSchemaError("outbound CLAP object keys must be strings")
            result[key] = _json_plain(item)
        return result
    if isinstance(value, list | tuple):
        return [_json_plain(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise AlarmSchemaError("outbound CLAP payload contains an unsupported value")
