"""Fake-HTTP Light Groups route, admission, ownership and no-replay contracts."""

import asyncio
from urllib.parse import quote

import pytest

from halo_gtk.chime_rest import RingChimeRestGateway
from halo_gtk.light_groups_rest import LightGroupsRestError, RingLightGroupsRestGateway
from halo_gtk.ring_json import RingJsonTransport
from tests.test_alarm_rest import Response, fake_auth
from tests.test_ring_json import until


async def test_exact_routes_and_on_off_json_with_sdk_metadata_and_tls():
    auth, calls, _ = fake_auth(*(Response() for _ in range(7)))
    gateway = RingLightGroupsRestGateway(auth)
    await gateway.read_locations()
    await gateway.read_groups("loc")
    await gateway.read_report("loc", "group")
    for duration in (60, 300, 900):
        assert (await gateway.set_lights("loc", "group", True, duration_seconds=duration)).accepted
    assert (await gateway.set_lights("loc", "group", False)).accepted
    base = "https://api.ring.com/groups/v1/locations/loc/groups"
    assert [(method, url) for method, url, _ in calls[:3]] == [
        ("GET", "https://api.ring.com/devices/v1/locations"),
        ("GET", base),
        ("GET", base + "/group/devices"),
    ]
    for index, duration in enumerate((60, 300, 900), 3):
        assert calls[index][:2] == ("POST", base + "/group/devices")
        assert calls[index][2]["json"] == {
            "lights_on": {"enabled": True, "duration_seconds": duration}
        }
    assert calls[6][2]["json"] == {"lights_on": {"enabled": False}}
    for _, _, options in calls:
        assert options["headers"]["hardware_id"] == "fixture-hardware"
        assert options["headers"]["Authorization"] == "Bearer fixture-only"
        assert options["allow_redirects"] is False and options["auto_decompress"] is False
        assert options["ssl"].check_hostname and "params" not in options


async def test_opaque_unicode_ids_are_exact_encoded_single_segments():
    location, group = " a/雪?query=1#fragment ", "%2F/💡"
    auth, calls, _ = fake_auth(Response())
    await RingLightGroupsRestGateway(auth).read_report(location, group)
    assert calls[0][1] == (
        "https://api.ring.com/groups/v1/locations/"
        + quote(location, safe="")
        + "/groups/"
        + quote(group, safe="")
        + "/devices"
    )


@pytest.mark.parametrize(
    "value", [None, True, 1, "", ".", "..", "x\n", "x\u202e", "\ud800", "x" * 129]
)
async def test_bad_ids_cannot_widen_detail_or_post_to_list_route(value):
    auth, calls, _ = fake_auth()
    gateway = RingLightGroupsRestGateway(auth)
    for call in (
        lambda: gateway.read_groups(value),
        lambda: gateway.read_report("loc", value),
        lambda: gateway.read_report(value, "group"),
        lambda: gateway.set_lights("loc", value, False),
    ):
        with pytest.raises(LightGroupsRestError, match="invalid-request"):
            await call()
    assert not calls


@pytest.mark.parametrize(
    "on,duration",
    [
        (1, 60),
        (None, 60),
        ("false", None),
        (True, None),
        (True, True),
        (True, -1),
        (True, 0),
        (True, 60.0),
        (True, "60"),
        (True, 301),
        (False, 60),
    ],
)
async def test_strict_bool_presets_and_duration_omission(on, duration):
    auth, calls, _ = fake_auth()
    with pytest.raises(LightGroupsRestError, match="invalid-request"):
        await RingLightGroupsRestGateway(auth).set_lights(
            "loc", "group", on, duration_seconds=duration
        )
    assert not calls


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "redirect-not-allowed"),
        (401, "authentication-required"),
        (403, "permission-denied"),
        (429, "rate-limited"),
        (500, "request-failed"),
        (504, "request-failed"),
    ],
)
async def test_post_failure_never_replays_or_changes_encoding(status, code):
    response = Response(b"private detail", status=status, headers={"Retry-After": "900"})
    auth, calls, _ = fake_auth(response)
    marks = []
    with pytest.raises(LightGroupsRestError) as caught:
        await RingLightGroupsRestGateway(auth).set_lights(
            "loc", "group", False, on_admitted=lambda: marks.append(True)
        )
    assert caught.value.code == str(caught.value) == code
    assert caught.value.possibly_sent and caught.value.status == status
    assert caught.value.retry_after == 300 and len(calls) == len(marks) == 1
    assert auth.async_refresh_tokens.await_count == 0 and response.closed


@pytest.mark.parametrize("write", [False, True])
async def test_client_guard_rechecks_after_background_slot_before_any_handoff(write):
    release, current, marks = asyncio.Event(), [True], []
    auth, calls, _ = fake_auth(Response(gate=release))
    core = RingJsonTransport(auth)
    hold = asyncio.create_task(core.request_json("https://api.ring.com/hold"))
    await until(lambda: calls)

    def guard():
        if not current[0]:
            raise LightGroupsRestError("session-changed")

    gateway = RingLightGroupsRestGateway(auth, transport=core, admission=guard)
    coroutine = (
        gateway.set_lights("loc", "group", False, on_admitted=lambda: marks.append(True))
        if write
        else gateway.read_report("loc", "group")
    )
    pending = asyncio.create_task(coroutine)
    await until(lambda: core._background)
    current[0] = False
    release.set()
    await hold
    with pytest.raises(LightGroupsRestError, match="session-changed") as caught:
        await pending
    assert len(calls) == 1 and not marks and not caught.value.possibly_sent


async def test_command_admission_is_synchronous_after_auth_and_before_mark_request():
    auth, calls, _ = fake_auth(Response())
    ordered = []

    def admission():
        assert not calls
        ordered.append("check")

    def marked():
        assert ordered == ["check"] and not calls
        ordered.append("mark")

    await RingLightGroupsRestGateway(auth).set_lights(
        "loc", "group", False, admission=admission, on_admitted=marked
    )
    assert ordered == ["check", "mark"] and len(calls) == 1

    async def invalid():
        pytest.fail("Must not execute an async admission")

    with pytest.raises(LightGroupsRestError, match="invalid-admission") as caught:
        await RingLightGroupsRestGateway(auth).set_lights("loc", "group", False, admission=invalid)
    assert not caught.value.possibly_sent and len(calls) == 1


async def test_read_replay_is_safe_single_refresh_with_client_recheck():
    response = Response(status=401)
    auth, calls, _ = fake_auth(response, Response())
    retired = [False]

    async def refresh():
        assert response.closed
        retired[0] = True
        return {}

    auth.async_refresh_tokens = refresh

    def guard():
        if retired[0]:
            raise LightGroupsRestError("session-changed")

    with pytest.raises(LightGroupsRestError, match="session-changed"):
        await RingLightGroupsRestGateway(auth, admission=guard).read_locations()
    assert len(calls) == 1


async def test_timeout_and_cancellation_before_admission_send_nothing():
    release = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=release))
    core = RingJsonTransport(auth)
    hold = asyncio.create_task(core.request_json("https://api.ring.com/hold"))
    await until(lambda: calls)
    gateway = RingLightGroupsRestGateway(auth, transport=core)
    with pytest.raises(LightGroupsRestError, match="timeout") as caught:
        await gateway.set_lights("loc", "group", False, timeout=0.01)
    assert not caught.value.possibly_sent and len(calls) == 1
    pending = asyncio.create_task(gateway.read_groups("loc"))
    await until(lambda: core._background)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not core._background and len(calls) == 1
    release.set()
    await hold


async def test_cancel_possibly_sent_and_feature_close_do_not_close_other_feature():
    response = Response(gate=asyncio.Event())
    auth, calls, _ = fake_auth(response, Response())
    core = RingJsonTransport(auth)
    gateway = RingLightGroupsRestGateway(auth, transport=core)
    marks = []
    pending = asyncio.create_task(
        gateway.set_lights("loc", "group", False, on_admitted=lambda: marks.append(True))
    )
    await until(lambda: calls)
    await gateway.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert marks == [True] and response.closed and not core._closed
    assert await RingChimeRestGateway(auth, transport=core).read_inventory() == {}
    assert len(calls) == 2
    with pytest.raises(LightGroupsRestError, match="closed"):
        await gateway.read_locations()


@pytest.mark.parametrize(
    "body,headers,code",
    [
        (b"x" * 65537, {}, "response-too-large"),
        (b"x", {"Content-Encoding": "gzip"}, "unsupported-encoding"),
    ],
)
async def test_two_xx_incomplete_reply_is_accepted_without_second_post(body, headers, code):
    auth, calls, _ = fake_auth(Response(body, status=202, headers=headers))
    receipt = await RingLightGroupsRestGateway(auth).set_lights("loc", "group", False)
    assert receipt.accepted and not receipt.body_complete and receipt.body_error == code
    assert len(calls) == 1


async def test_arbitrary_callback_error_is_sanitized_and_cross_auth_core_rejected():
    auth, calls, _ = fake_auth()
    other, _, _ = fake_auth()
    with pytest.raises(ValueError, match="another Auth"):
        RingLightGroupsRestGateway(other, transport=RingJsonTransport(auth))

    def fail():
        raise RuntimeError("private signed URL")

    with pytest.raises(LightGroupsRestError, match="^admission-failed$"):
        await RingLightGroupsRestGateway(auth, admission=fail).read_locations()
    assert not calls
