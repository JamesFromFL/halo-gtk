"""Local schema and immutable evidence contracts; no SDK or network access."""

import threading
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from halo_gtk.light_group_models import (
    MAX_CACHED_GROUPS,
    MAX_CACHED_MEMBERS,
    LightGroupIntent,
    LightGroupPermission,
    LightGroupSchemaError,
    LightGroupsSnapshot,
    parse_group_report,
    parse_groups,
    parse_locations,
    validate_identifier,
)


def member(api_id=123, **changes):
    return {
        "doorbot_id": api_id,
        "location_id": "loc",
        "name": "Path Light",
        "type": "beams_ct200_transformer",
        "deleted_at": None,
        **changes,
    }


def group(group_id="group", **changes):
    return {
        "device_group_id": group_id,
        "location_id": "loc",
        "name": "Outside",
        "deleted_at": None,
        "devices": [member()],
        **changes,
    }


def report(**changes):
    return {
        "device_group_id": "group",
        "lights_on": False,
        "devices": [{"id": "123", "lights_on": True}],
        **changes,
    }


def test_existing_fixture_shapes_preserve_group_and_member_evidence_separately():
    locations = parse_locations({"user_locations": [{"location_id": "loc", "name": "Home"}]})
    groups = parse_groups({"device_groups": [group()]}, "loc")
    parsed = parse_group_report(report(), "loc", "group", members=groups[0].members)
    assert groups[0].member_count == 1 and groups[0].membership_complete
    assert groups[0].members[0].api_id == "123"
    assert groups[0].members[0].lights_on is None
    assert groups[0].report is None and not groups[0].can_write and groups[0].stale
    assert groups[0].permission == LightGroupPermission.UNKNOWN
    # Synthetic mixed input proves forwarding, never real Ring mixed semantics.
    assert parsed.report is False and parsed.members[0].lights_on is True
    location = replace(locations[0], groups=groups, complete=True, has_inventory=True)
    snapshot = LightGroupsSnapshot(7, locations=(location,))
    assert snapshot.find_group("loc", "group") is groups[0]
    assert snapshot.find_group("other", "group") is None
    assert replace(groups[0], membership_complete=False).member_count is None
    with pytest.raises(FrozenInstanceError):
        groups[0].name = "mutation"


def test_private_fields_do_not_enter_dtos_and_names_are_literal_bounded_text():
    row = group(
        name="<b>Outside</b>\u202e\n" + "x" * 200, hardware_id="private", mac_address="private"
    )
    row["devices"][0].update(hardware_id="private", mac_address="private", address="private")
    parsed = parse_groups({"device_groups": [row]}, "loc")[0]
    assert parsed.name.startswith("<b>Outside</b>") and len(parsed.name) == 160
    assert "\u202e" not in parsed.name and "\n" not in parsed.name
    assert "private" not in repr(asdict(parsed))
    assert not hasattr(parsed, "battery") and not hasattr(parsed, "connection")


@pytest.mark.parametrize(
    "value", [None, True, 1, "", ".", "..", "a\n", "a\x7f", "a\u202e", "\ud800", "a" * 129]
)
def test_opaque_identifier_rejects_unsafe_or_unbounded_input(value):
    with pytest.raises(LightGroupSchemaError, match="invalid-identifier"):
        validate_identifier(value)


def test_opaque_ids_preserve_unicode_reserved_characters_and_maximum_boundary():
    for value in (" a/雪?x=1#%2F ", "💡" * 128):
        assert validate_identifier(value) == value


@pytest.mark.parametrize("key", ["lights_on", "missing"])
@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_report_only_recognizes_explicit_json_bool(key, value):
    payload = report()
    payload.pop("lights_on")
    if key != "missing":
        payload[key] = value
    assert parse_group_report(payload, "loc", "group").report is None


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_member_unknown_report_is_not_filled_from_aggregate(value):
    parsed = parse_group_report(
        report(lights_on=True, devices=[{"id": "123", "lights_on": value}]), "loc", "group"
    )
    assert parsed.report is True and parsed.members[0].lights_on is None


@pytest.mark.parametrize("stamp", ["2026-10-27T12:30:00Z", "2026-10-27T12:30:00.123456+01:00"])
def test_valid_group_and_member_tombstones_are_excluded(stamp):
    payload = {
        "device_groups": [
            group("deleted", deleted_at=stamp),
            group(devices=[member(), member(456, deleted_at=stamp)]),
        ]
    }
    parsed = parse_groups(payload, "loc")
    assert len(parsed) == 1 and parsed[0].member_count == 1
    detail = parse_group_report(
        report(devices=[{"id": "123", "lights_on": True}, {"id": "456", "deleted_at": stamp}]),
        "loc",
        "group",
        members=parsed[0].members,
    )
    assert len(detail.members) == 1


@pytest.mark.parametrize(
    "stamp", [True, 0, "", "yesterday", "2026-02-30T00:00:00Z", "2026-10-27", "2026-10-27T00:00:00"]
)
@pytest.mark.parametrize("where", ["group", "member", "report"])
def test_bad_tombstone_never_becomes_active_membership(stamp, where):
    with pytest.raises(LightGroupSchemaError, match="invalid-deletion"):
        if where == "report":
            parse_group_report(report(devices=[{"id": "123", "deleted_at": stamp}]), "loc", "group")
        else:
            row = group()
            (row if where == "group" else row["devices"][0])["deleted_at"] = stamp
            parse_groups({"device_groups": [row]}, "loc")


def test_absent_tombstone_is_not_invented_deletion():
    row = group()
    row.pop("deleted_at")
    row["devices"][0].pop("deleted_at")
    assert parse_groups({"device_groups": [row]}, "loc")[0].member_count == 1


@pytest.mark.parametrize(
    "changes", [{"device_group_id": "other"}, {"location_id": "other"}, {"device_group_id": None}]
)
def test_report_scope_must_match_target(changes):
    with pytest.raises(LightGroupSchemaError):
        parse_group_report(report(**changes), "loc", "group")


@pytest.mark.parametrize("value", [True, 0, -1, 2**63, "123", 1.0, None])
def test_inventory_member_identity_is_positive_non_bool_integer(value):
    with pytest.raises(LightGroupSchemaError, match="invalid-member-id"):
        parse_groups({"device_groups": [group(devices=[member(value)])]}, "loc")


@pytest.mark.parametrize("value", [True, 123, "0", "01", "-1", "+1", "１", "1.0", str(2**63), None])
def test_report_member_identity_is_canonical_decimal_string(value):
    with pytest.raises(LightGroupSchemaError, match="invalid-member-id"):
        parse_group_report(report(devices=[{"id": value}]), "loc", "group")


def test_member_set_mismatch_is_not_fresh_membership_or_partial_join():
    members = parse_groups({"device_groups": [group()]}, "loc")[0].members
    for devices in ([], [{"id": "456"}], [{"id": "123"}, {"id": "456"}]):
        with pytest.raises(LightGroupSchemaError, match="membership-mismatch"):
            parse_group_report(report(devices=devices), "loc", "group", members=members)
    with pytest.raises(LightGroupSchemaError, match="membership-mismatch"):
        parse_group_report(
            report(), "loc", "group", members=(replace(members[0], location_id="other"),)
        )


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: parse_locations(
            {"user_locations": [{"location_id": "loc"}, {"location_id": "loc"}]}
        ),
        lambda: parse_groups({"device_groups": [group(), group()]}, "loc"),
        lambda: parse_groups({"device_groups": [group(devices=[member(), member()])]}, "loc"),
        lambda: parse_group_report(report(devices=[{"id": "123"}, {"id": "123"}]), "loc", "group"),
        lambda: parse_groups({"device_groups": [group(location_id="other")]}, "loc"),
        lambda: parse_groups(
            {"device_groups": [group(devices=[member(location_id="other")])]}, "loc"
        ),
    ],
)
def test_duplicates_and_foreign_scope_reject_whole_response(invoke):
    with pytest.raises(LightGroupSchemaError):
        invoke()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"device_groups": None},
        {"device_groups": (group(),)},
        {"device_groups": [group(), None]},
    ],
)
def test_invalid_or_partially_valid_list_is_not_an_authoritative_empty(payload):
    with pytest.raises(LightGroupSchemaError):
        parse_groups(payload, "loc")


@pytest.mark.parametrize(
    "hint",
    [
        {"has_more": True},
        {"has_more": 0},
        {"truncated": True},
        {"partial": "false"},
        {"next_page": "cursor"},
        {"total": 2},
        {"count": True},
        {"offset": 1},
        {"page": 2},
        {"pagination": {"unknown": True}},
        {"meta": {"has_more": True}},
        {"complete": False},
        {"is_truncated": True},
        {"has_next_page": True},
        {"next_page_url": "https://example.invalid/private"},
        {"limit": 100},
        {"total_pages": 2},
    ],
)
def test_pagination_or_truncation_never_proves_removal(hint):
    with pytest.raises(LightGroupSchemaError, match="incomplete-response"):
        parse_groups({"device_groups": [group()], **hint}, "loc")


def test_empty_and_explicit_complete_list_are_authoritative_and_scoped():
    assert parse_locations({"user_locations": []}) == ()
    assert parse_groups({"device_groups": []}, "loc") == ()
    parsed = parse_groups(
        {"device_groups": [group()], "has_more": False, "total": 1, "offset": 0}, "loc"
    )
    assert len(parsed) == 1
    other = group(location_id="other", devices=[member(location_id="other")])
    assert parse_groups({"device_groups": [other]}, "other")[0].group_id == parsed[0].group_id


def test_empty_list_with_conflicting_outer_location_cannot_remove_target_groups():
    with pytest.raises(LightGroupSchemaError, match="location-mismatch"):
        parse_groups({"device_groups": [], "location_id": "other"}, "loc")


def test_per_response_limits_reject_without_silent_truncation():
    assert MAX_CACHED_GROUPS == 512 and MAX_CACHED_MEMBERS == 8192
    assert (
        len(parse_locations({"user_locations": [{"location_id": str(i)} for i in range(16)]})) == 16
    )
    assert len(parse_groups({"device_groups": [group(str(i)) for i in range(128)]}, "loc")) == 128
    assert (
        parse_groups(
            {"device_groups": [group(devices=[member(i + 1) for i in range(128)])]}, "loc"
        )[0].member_count
        == 128
    )
    for call in (
        lambda: parse_locations({"user_locations": [{"location_id": str(i)} for i in range(17)]}),
        lambda: parse_groups({"device_groups": [group(str(i)) for i in range(129)]}, "loc"),
        lambda: parse_groups(
            {"device_groups": [group(devices=[member(i + 1) for i in range(129)])]}, "loc"
        ),
        lambda: parse_group_report(
            report(devices=[{"id": str(i + 1)} for i in range(129)]), "loc", "group"
        ),
    ):
        with pytest.raises(LightGroupSchemaError, match="response-limit"):
            call()


def test_intent_cancel_is_threadsafe_without_mutating_identity():
    intent = LightGroupIntent(2, "loc", "group", 7, 3)
    worker = threading.Thread(target=intent.cancel)
    worker.start()
    worker.join()
    assert intent.cancelled and intent.generation == 2 and intent.revision == 7
