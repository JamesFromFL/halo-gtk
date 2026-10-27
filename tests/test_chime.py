"""Chime contract parsing uses public fixtures and synthetic data only."""

from dataclasses import FrozenInstanceError

import pytest

from halo_gtk.chime import (
    ChimeConnectionStatus,
    ChimeIntent,
    ChimeSchemaError,
    parse_chime_health,
    parse_chime_inventory,
)


def row(**changes):
    return {
        "id": 10,
        "description": "Hall",
        "kind": "chime",
        "device_id": "private-hardware-id",
        "owned": True,
        "alerts": {"connection": "online"},
        "settings": {"volume": 2},
        **changes,
    }


@pytest.mark.parametrize("volume", [None, True, False, -1, 12, 1.0, "2", {}, []])
def test_volume_unknown_is_never_zero(volume):
    device = parse_chime_inventory({"chimes": [row(settings={"volume": volume})]})[0]
    assert device.volume is None
    assert not device.can_write


@pytest.mark.parametrize("volume", range(12))
def test_legacy_volume_range(volume):
    assert parse_chime_inventory({"chimes": [row(settings={"volume": volume})]})[0].volume == volume


@pytest.mark.parametrize("kind", ["chime", "chime_v2", "chime_pro", "chime_pro_v2", "future"])
def test_models_retain_wire_kind_without_guessing_generation(kind):
    device = parse_chime_inventory({"chimes": [row(kind=kind)]})[0]
    assert device.kind == kind
    assert "generation" not in device.model


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"chimes": None},
        {"chimes": {}},
        {"chimes": [None]},
        {"chimes": [row(id=True)]},
        {"chimes": [row(id="10")]},
        {"chimes": [row(), row()]},
        {"chimes": [row(id=i + 1) for i in range(129)]},
    ],
)
def test_invalid_complete_inventory_rejected(payload):
    with pytest.raises(ChimeSchemaError):
        parse_chime_inventory(payload)


def test_explicit_empty_and_maximum_complete_inventory():
    assert parse_chime_inventory({"chimes": []}) == ()
    assert len(parse_chime_inventory({"chimes": [row(id=i + 1) for i in range(128)]})) == 128


def test_selected_fields_are_bounded_literal_and_private():
    device = parse_chime_inventory(
        {
            "chimes": [
                row(
                    description="<b>Hall</b>\x00\n" + "x" * 200,
                    owned=1,
                    do_not_disturb={"seconds_left": True},
                )
            ]
        }
    )[0]
    assert len(device.name) == 160
    assert "<b>Hall</b>" in device.name
    assert "\x00" not in device.name
    assert device.owned is None
    assert device.snooze_seconds_left is None
    assert "private-hardware-id" not in repr(device)
    with pytest.raises(FrozenInstanceError):
        device.volume = 1


@pytest.mark.parametrize(
    "connection,expected",
    [
        ("online", "online"),
        ("offline", "offline"),
        (None, "unknown"),
        ([], "unknown"),
        ("connected", "unknown"),
    ],
)
def test_connection_requires_explicit_evidence(connection, expected):
    device = parse_chime_inventory({"chimes": [row(alerts={"connection": connection})]})[0]
    assert device.connection == ChimeConnectionStatus(expected)


def test_health_only_normalizes_selected_fields():
    health = parse_chime_health(
        {
            "device_health": {
                "id": 10,
                "wifi_name": "private-ssid",
                "latest_signal_strength": -39,
                "latest_signal_category": "good",
                "firmware": "1.2.3",
                "updated_at": "not-receipt-time",
            }
        },
        10,
        observed_at=100,
        observed_monotonic=200,
    )
    assert health.wifi_signal_strength == -39
    assert health.wifi_signal_category == "good"
    assert health.observed_at == 100
    assert health.observed_monotonic == 200
    assert "private-ssid" not in repr(health)
    with pytest.raises(ChimeSchemaError):
        parse_chime_health({"device_health": {"id": 11}}, 10, observed_at=1, observed_monotonic=1)


def test_intent_cancellation_is_local_and_thread_safe():
    intent = ChimeIntent(1, 10, 2, 3)
    assert not intent.cancelled
    intent.cancel()
    assert intent.cancelled
