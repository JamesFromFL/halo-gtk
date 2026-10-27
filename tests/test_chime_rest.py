"""Chime-only fixed routes/payloads and honest single-attempt write outcomes."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from halo_gtk.chime_rest import ChimeRestError, RingChimeRestGateway
from halo_gtk.ring_json import RingJsonTransport
from tests.test_alarm_rest import Response, fake_auth
from tests.test_ring_json import until


async def test_fixed_inventory_health_and_minimal_volume_sound_json():
    auth, calls, _ = fake_auth(
        Response(b'{"chimes":[]}'),
        Response(b'{"device_health":{}}'),
        Response(),
        Response(),
        Response(),
    )
    gateway = RingChimeRestGateway(auth)
    assert await gateway.read_inventory() == {"chimes": []}
    assert await gateway.read_health(123) == {"device_health": {}}
    assert (await gateway.set_legacy_volume(123, 0)).accepted
    assert (await gateway.request_test_tone(123, "ding")).accepted
    assert (await gateway.request_test_tone(123, "motion")).accepted
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", "https://api.ring.com/clients_api/ring_devices"),
        ("GET", "https://api.ring.com/clients_api/chimes/123/health"),
        ("PUT", "https://api.ring.com/clients_api/chimes/123/"),
        ("POST", "https://api.ring.com/clients_api/chimes/123/play_sound"),
        ("POST", "https://api.ring.com/clients_api/chimes/123/play_sound"),
    ]
    assert calls[2][2]["json"] == {"chime": {"settings": {"volume": 0}}}
    assert calls[3][2]["json"] == {"kind": "ding"} and calls[4][2]["json"] == {"kind": "motion"}
    for _method, _url, options in calls:
        assert "params" not in options and options["allow_redirects"] is False
        assert options["auto_decompress"] is False and options["ssl"].check_hostname
        assert options["headers"]["hardware_id"] == "fixture-hardware"


@pytest.mark.parametrize("api_id", [True, False, 0, -1, "123", "../1", 1.0, None, 2**63])
async def test_invalid_identity_cannot_widen_any_device_route(api_id):
    auth, calls, _ = fake_auth()
    gateway = RingChimeRestGateway(auth)
    for invoke in (
        lambda: gateway.read_health(api_id),
        lambda: gateway.set_legacy_volume(api_id, 2),
        lambda: gateway.request_test_tone(api_id, "ding"),
    ):
        with pytest.raises(ChimeRestError, match="invalid-request"):
            await invoke()
    assert not calls


@pytest.mark.parametrize("value", [True, False, -1, 12, "2", 2.0, None, float("nan")])
async def test_volume_requires_reportable_legacy_integer_range(value):
    auth, calls, _ = fake_auth()
    with pytest.raises(ChimeRestError, match="invalid-request"):
        await RingChimeRestGateway(auth).set_legacy_volume(123, value)
    assert not calls


@pytest.mark.parametrize("kind", ["", "alarm", "ding?kind=motion", 1, None, ["ding"]])
async def test_sound_has_only_two_explicit_kinds_and_no_implicit_default(kind):
    auth, calls, _ = fake_auth()
    with pytest.raises(ChimeRestError, match="invalid-request"):
        await RingChimeRestGateway(auth).request_test_tone(123, kind)
    assert not calls


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "redirect-not-allowed"),
        (401, "authentication-required"),
        (403, "permission-denied"),
        (429, "rate-limited"),
        (500, "request-failed"),
    ],
)
@pytest.mark.parametrize("operation", ["volume", "tone"])
async def test_write_http_failure_is_never_replayed_or_reencoded(status, code, operation):
    response = Response(
        b"private application detail", status=status, headers={"Retry-After": "900"}
    )
    auth, calls, _ = fake_auth(response)
    gateway = RingChimeRestGateway(auth)
    marked = []
    invoke = gateway.set_legacy_volume if operation == "volume" else gateway.request_test_tone
    with pytest.raises(ChimeRestError) as error:
        await invoke(
            123, 2 if operation == "volume" else "ding", on_admitted=lambda: marked.append(True)
        )
    assert error.value.code == str(error.value) == code
    assert error.value.status == status and error.value.possibly_sent
    assert error.value.retry_after == 300
    assert len(calls) == len(marked) == 1 and response.closed
    assert auth.async_refresh_tokens.await_count == 0


async def test_guard_runs_after_shared_slot_wait_and_rejection_does_not_mark_or_send():
    release, marked = asyncio.Event(), []
    first = Response(gate=release)
    auth, calls, _ = fake_auth(first)
    core = RingJsonTransport(auth)
    gateway = RingChimeRestGateway(auth, transport=core)
    hold = asyncio.create_task(gateway.read_inventory())
    await until(lambda: calls)
    current = [True]

    def guard():
        if not current[0]:
            raise ChimeRestError("superseded")

    write = asyncio.create_task(
        gateway.request_test_tone(
            123, "ding", admission=guard, on_admitted=lambda: marked.append(True)
        )
    )
    await until(lambda: len(core._background) == 1)
    current[0] = False
    release.set()
    await hold
    with pytest.raises(ChimeRestError, match="superseded") as error:
        await write
    assert not error.value.possibly_sent and not marked and len(calls) == 1


async def test_client_admission_rechecks_retirement_for_queued_read():
    release = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=release))
    core = RingJsonTransport(auth)
    current = [True]

    def admission():
        if not current[0]:
            raise ChimeRestError("session-changed")

    gateway = RingChimeRestGateway(auth, transport=core, admission=admission)
    occupied = asyncio.create_task(core.request_json("https://api.ring.com/occupied"))
    await until(lambda: len(calls) == 1)
    read = asyncio.create_task(gateway.read_inventory())
    await until(lambda: len(core._background) == 1)
    current[0] = False
    release.set()
    await occupied
    with pytest.raises(ChimeRestError, match="session-changed") as error:
        await read
    assert not error.value.possibly_sent and len(calls) == 1


@pytest.mark.parametrize(
    "status,body",
    [(200, b'{"error":"unverified application field"}'), (202, b"not JSON"), (204, b"")],
)
async def test_two_xx_receipt_is_only_http_acceptance_and_does_not_invent_ack(status, body):
    auth, calls, _ = fake_auth(Response(body, status=status))
    receipt = await RingChimeRestGateway(auth).request_test_tone(123, "motion")
    assert receipt.status == status and receipt.accepted and receipt.body_complete
    assert receipt.body_error is None and len(calls) == 1
    assert not hasattr(receipt, "played")


@pytest.mark.parametrize(
    "failure,code",
    [
        ("overflow", "response-too-large"),
        ("encoding", "unsupported-encoding"),
        ("timeout", "timeout"),
        ("close", "network-unavailable"),
    ],
)
async def test_two_xx_with_unusable_reply_retains_acceptance_and_never_resends(failure, code):
    response = Response(
        b"x" * (65537 if failure == "overflow" else 1),
        status=202,
        headers={"Content-Encoding": "gzip"} if failure == "encoding" else None,
        gate=asyncio.Event() if failure == "timeout" else None,
    )
    if failure == "close":

        class ClosingResponse(Response):
            async def __aexit__(self, *args):
                await super().__aexit__(*args)
                raise aiohttp.ClientConnectionError("private close failure")

        response = ClosingResponse(status=202)
    auth, calls, _ = fake_auth(response)
    receipt = await RingChimeRestGateway(auth).request_test_tone(123, "ding", timeout=0.01)
    assert receipt.accepted and receipt.status == 202 and not receipt.body_complete
    assert receipt.body_error == code and len(calls) == 1 and response.closed


async def test_timeout_after_admission_before_response_is_possibly_sent_once():
    async def request(_url):
        await asyncio.Event().wait()

    auth, calls, _ = fake_auth(request=request)
    marks = []
    with pytest.raises(ChimeRestError, match="timeout") as error:
        await RingChimeRestGateway(auth).request_test_tone(
            123, "ding", timeout=0.01, on_admitted=lambda: marks.append(True)
        )
    assert error.value.possibly_sent and len(calls) == len(marks) == 1


async def test_cancel_after_post_closes_response_without_second_request():
    response = Response(gate=asyncio.Event())
    auth, calls, _ = fake_auth(response)
    gateway = RingChimeRestGateway(auth)
    marks = []
    task = asyncio.create_task(
        gateway.request_test_tone(123, "ding", on_admitted=lambda: marks.append(True))
    )
    await until(lambda: calls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == len(marks) == 1 and response.closed
    assert gateway._transport._active == 0 and not gateway._requests


async def test_read_top_level_and_json_limits_are_typed_feature_errors():
    auth, calls, _ = fake_auth(Response(b"[]"), Response(b'{"duplicate":1,"duplicate":2}'))
    gateway = RingChimeRestGateway(auth)
    for invoke in (gateway.read_inventory, lambda: gateway.read_health(123)):
        with pytest.raises(ChimeRestError, match="invalid-response"):
            await invoke()
    assert len(calls) == 2


async def test_wrapper_cannot_share_another_accounts_transport():
    auth, _, _ = fake_auth()
    other, _, _ = fake_auth()
    with pytest.raises(ValueError, match="another Auth"):
        RingChimeRestGateway(auth, transport=RingJsonTransport(other))
