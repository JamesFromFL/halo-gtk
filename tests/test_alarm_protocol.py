"""Tests for the bounded Ring Alarm CLAP protocol boundary."""

import json

import pytest

from halo_gtk.alarm.models import AlarmConnectionStatus, AlarmMode
from halo_gtk.alarm.protocol import (
    AlarmFrameTooLargeError,
    AlarmSchemaError,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    HubDisconnection,
    SessionInfo,
    UnknownMessage,
    encode_clap_frame,
    encode_device_list_request,
    encode_mode_command,
    parse_clap_frame,
)


def _frame(message, channel="message"):
    return json.dumps({"channel": channel, "msg": message})


def _document(zid="sensor-1", *, general=None, device=None):
    return {
        "general": {
            "v2": {
                "zid": zid,
                "name": "Front Door",
                "deviceType": "sensor.contact",
                **(general or {}),
            }
        },
        "device": {"v1": device or {}},
    }


def test_parse_device_list_flattens_general_then_device_and_freezes_data():
    message = parse_clap_frame(
        _frame(
            {
                "msg": "DeviceInfoDocGetList",
                "src": "asset-1",
                "seq": 4,
                "body": [_document(general={"faulted": False}, device={"faulted": True})],
            }
        )
    )

    assert isinstance(message, DeviceInfoDocList)
    assert message.asset_id == "asset-1"
    assert message.sequence == 4
    assert message.devices[0].zid == "sensor-1"
    assert message.devices[0].device_type == "sensor.contact"
    assert message.devices[0].faulted is True
    with pytest.raises(TypeError):
        message.devices[0].data["name"] = "Changed"


def test_parse_realtime_device_update():
    message = parse_clap_frame(
        _frame(
            {
                "msg": "DataUpdate",
                "datatype": "DeviceInfoDocType",
                "src": "asset-1",
                "body": [_document(device={"mode": "some"})],
            },
            channel="DataUpdate",
        )
    )

    assert isinstance(message, DeviceInfoDocUpdate)
    assert message.devices[0].mode == "some"


def test_parse_session_info_normalizes_connectivity_without_household_data():
    message = parse_clap_frame(
        _frame(
            {
                "msg": "SessionInfo",
                "datatype": "SessionInfoType",
                "body": [
                    {
                        "assetUuid": "asset-1",
                        "connectionStatus": "cell-backup",
                        "kind": "base_station_v1",
                        "doorbotId": 123,
                    },
                    {
                        "assetUuid": "asset-2",
                        "connectionStatus": "future-status",
                        "kind": "future-hub",
                    },
                ],
            },
            channel="DataUpdate",
        )
    )

    assert isinstance(message, SessionInfo)
    assert message.sessions[0].connection is AlarmConnectionStatus.CELLULAR_BACKUP
    assert message.sessions[1].connection is AlarmConnectionStatus.UNKNOWN
    assert not hasattr(message.sessions[0], "doorbot_id")


def test_parse_hub_disconnection_before_other_datatype_routing():
    message = parse_clap_frame(
        _frame(
            {
                "msg": "DataUpdate",
                "datatype": "HubDisconnectionEventType",
                "src": "asset-1",
                "body": [],
            },
            channel="DataUpdate",
        )
    )

    assert message == HubDisconnection(asset_id="asset-1")


def test_unknown_message_retains_metadata_but_not_raw_body():
    message = parse_clap_frame(
        _frame(
            {
                "msg": "FutureMessage",
                "datatype": "FutureType",
                "src": "asset-1",
                "body": [{"ticket": "do-not-retain"}],
            }
        )
    )

    assert isinstance(message, UnknownMessage)
    assert message.message_type == "FutureMessage"
    assert not hasattr(message, "body")
    assert "do-not-retain" not in repr(message)


def test_parser_rejects_oversize_deep_duplicate_and_nonstandard_json():
    with pytest.raises(AlarmFrameTooLargeError):
        parse_clap_frame("{}", max_frame_bytes=1)
    with pytest.raises(AlarmSchemaError, match="nesting"):
        parse_clap_frame('{"channel":"x","msg":{"msg":"x"}}', max_depth=2)
    with pytest.raises(AlarmSchemaError, match="duplicate JSON key"):
        parse_clap_frame('{"channel":"x","channel":"y","msg":{"msg":"x"}}')
    with pytest.raises(AlarmSchemaError, match="non-standard JSON number"):
        parse_clap_frame('{"channel":"x","msg":{"msg":"x","seq":NaN}}')


def test_parser_rejects_malformed_known_messages_and_duplicate_devices():
    with pytest.raises(AlarmSchemaError, match="body"):
        parse_clap_frame(_frame({"msg": "DeviceInfoDocGetList", "src": "asset-1"}))
    with pytest.raises(AlarmSchemaError, match="duplicate zids"):
        parse_clap_frame(
            _frame(
                {
                    "msg": "DeviceInfoDocGetList",
                    "src": "asset-1",
                    "body": [_document(), _document()],
                }
            )
        )


def test_device_list_request_matches_clap_envelope():
    assert encode_device_list_request("asset-1", 9) == {
        "channel": "message",
        "msg": {"msg": "DeviceInfoDocGetList", "dst": "asset-1", "seq": 9},
    }


@pytest.mark.parametrize(
    ("mode", "raw_mode"),
    [
        (AlarmMode.DISARMED, "none"),
        (AlarmMode.HOME, "some"),
        (AlarmMode.AWAY, "all"),
    ],
)
def test_mode_command_matches_device_info_set_envelope(mode, raw_mode):
    envelope = encode_mode_command(
        "asset-1",
        "panel-1",
        mode,
        12,
        bypass_zids=("sensor-1", "sensor-2"),
    )

    assert envelope == {
        "channel": "message",
        "msg": {
            "msg": "DeviceInfoSet",
            "datatype": "DeviceInfoSetType",
            "dst": "asset-1",
            "body": [
                {
                    "zid": "panel-1",
                    "command": {
                        "v1": [
                            {
                                "commandType": "security-panel.switch-mode",
                                "data": {
                                    "mode": raw_mode,
                                    "bypass": ["sensor-1", "sensor-2"],
                                },
                            }
                        ]
                    },
                }
            ],
            "seq": 12,
        },
    }
    assert json.loads(encode_clap_frame(envelope)) == envelope


def test_mode_command_omits_bypass_when_not_explicit_and_rejects_unknown_mode():
    envelope = encode_mode_command("asset-1", "panel-1", AlarmMode.HOME, 1)
    command_data = envelope["msg"]["body"][0]["command"]["v1"][0]["data"]

    assert command_data == {"mode": "some"}
    with pytest.raises(ValueError, match="mode"):
        encode_mode_command("asset-1", "panel-1", AlarmMode.UNKNOWN, 2)
    with pytest.raises(ValueError, match="iterable"):
        encode_mode_command("asset-1", "panel-1", AlarmMode.HOME, 2, "sensor-1")


def test_encoder_bounds_nested_tuples_and_rejects_non_string_object_keys():
    deep = "value"
    for _ in range(40):
        deep = (deep,)
    with pytest.raises(AlarmSchemaError, match="nesting"):
        encode_clap_frame({"value": deep})
    with pytest.raises(AlarmSchemaError, match="keys"):
        encode_clap_frame({"value": {1: "not-json-object-semantics"}})
