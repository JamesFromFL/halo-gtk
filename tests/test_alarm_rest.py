"""Bounded native Alarm REST contracts: no credentials or live network."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import pytest
from ring_doorbell import Auth
from ring_doorbell.const import OAuth

from halo_gtk import ring_client
from halo_gtk.alarm import rest
from halo_gtk.alarm.rest import (
    DEVICES_ENDPOINT,
    HISTORY_ENDPOINT,
    LOCATIONS_ENDPOINT,
    TICKET_ENDPOINT,
    AlarmRestError,
    RingAlarmRestGateway,
)


def ticket_url(location="location/one"):
    return (
        TICKET_ENDPOINT
        + "?"
        + urlencode(
            {
                "locationID": location,
                "enableExtendedEmergencyCellUsage": "true",
                "requestedTransport": "ws",
            }
        )
    )


class Stream:
    def __init__(self, data, *, gate=None):
        self.data = data
        self.position = 0
        self.calls = []
        self.gate = gate

    async def read(self, size):
        self.calls.append(size)
        if self.gate is not None:
            await self.gate.wait()
        value = self.data[self.position : self.position + min(size, 13)]
        self.position += len(value)
        return value


class Response:
    def __init__(self, data=b"{}", *, status=200, headers=None, gate=None):
        self.content = Stream(data, gate=gate)
        self.status = status
        self.headers = headers or {}
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def read(self):
        pytest.fail("Alarm must never call SDK eager response.read")

    async def text(self):
        return self.content.data.decode()


def fake_auth(*responses, request=None):
    calls = []
    scopes = []

    class Client:
        def add_token(self, url, **kwargs):
            headers = dict(kwargs["headers"], Authorization="Bearer fixture-only")
            return url, headers, None

    @contextlib.contextmanager
    def scope():
        scopes.append("enter")
        try:
            yield
        finally:
            scopes.append("exit")

    async def send(method, url, **kwargs):
        calls.append((method, str(url), kwargs))
        return await request(str(url)) if request is not None else responses[len(calls) - 1]

    auth = SimpleNamespace(
        user_agent="fixture-user-agent",
        get_hardware_id=lambda: "fixture-hardware",
        _oauth_client=Client(),
        _session=SimpleNamespace(request=send),
        _halo_refresh_serialized=True,
        _halo_auth_request_scope=scope,
        async_refresh_tokens=AsyncMock(return_value={}),
    )
    return auth, calls, scopes


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [LOCATIONS_ENDPOINT, DEVICES_ENDPOINT, ticket_url()])
async def test_mapping_get_preserves_sdk_metadata_and_streams_with_tls(url):
    response = Response(b'{"value":{"answer":42}}')
    auth, calls, scopes = fake_auth(response)
    result = await RingAlarmRestGateway(auth).request_json(url)
    assert result == {"value": {"answer": 42}} and response.closed
    assert scopes == ["enter", "exit"]
    method, requested, options = calls[0]
    assert method == "GET" and requested == url
    assert options["headers"] == {
        "User-Agent": "fixture-user-agent",
        "hardware_id": "fixture-hardware",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": "Bearer fixture-only",
    }
    assert options["allow_redirects"] is False and options["auto_decompress"] is False
    assert options["ssl"].check_hostname and options["ssl"].verify_mode.name == "CERT_REQUIRED"
    assert response.content.calls and max(response.content.calls) <= 16 * 1024


@pytest.mark.asyncio
async def test_history_is_list_only_forced_scope_and_literal_opaque_location():
    response = Response(b'[{"msg":"DataUpdate","body":{"opaque":"event-id"}}]')
    auth, calls, _ = fake_auth(response)
    location = "location/one&accountId=other"
    result = await RingAlarmRestGateway(auth).request_history(location, limit=12, offset=24)
    assert result == [{"msg": "DataUpdate", "body": {"opaque": "event-id"}}]
    parsed = urlsplit(calls[0][1])
    assert parsed.scheme + "://" + parsed.netloc + parsed.path == HISTORY_ENDPOINT
    assert parse_qs(parsed.query) == {
        "accountId": [location],
        "category": ["alarm"],
        "limit": ["12"],
        "offset": ["24"],
        "maxLevel": ["50"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://api.ring.com/devices/v1/locations",
        "https://evil.invalid/devices/v1/locations",
        "https://user:password@api.ring.com/devices/v1/locations",
        LOCATIONS_ENDPOINT + "?redirect=elsewhere",
        LOCATIONS_ENDPOINT + "#fragment",
        "https://api.ring.com:444/devices/v1/locations",
        HISTORY_ENDPOINT,
        "https://app.ring.com/api/v1/clap/tickets?locationID=one",
        ticket_url() + "&locationID=other",
        ticket_url() + "&accountId=other",
        ticket_url().replace("requestedTransport=ws", "requestedTransport=poll"),
        ticket_url().replace(
            "enableExtendedEmergencyCellUsage=true", "enableExtendedEmergencyCellUsage=false"
        ),
    ],
)
async def test_allowlist_rejects_destination_route_and_query_widening(url):
    auth, calls, _ = fake_auth()
    with pytest.raises(AlarmRestError, match="endpoint-not-allowed"):
        await RingAlarmRestGateway(auth).request_json(url)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method,payload", [("POST", None), ("PUT", {}), ("GET", {})])
async def test_no_write_or_caller_json_can_reach_auth(method, payload):
    auth, calls, _ = fake_auth()
    with pytest.raises(AlarmRestError, match="operation-not-allowed"):
        await RingAlarmRestGateway(auth).request_json(
            LOCATIONS_ENDPOINT, method=method, json=payload
        )
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (301, "redirect-not-allowed"),
        (302, "redirect-not-allowed"),
        (403, "permission-denied"),
        (429, "rate-limited"),
        (503, "request-failed"),
    ],
)
async def test_http_errors_close_response_and_never_follow_or_report_body(status, code):
    response = Response(
        b"private upstream detail",
        status=status,
        headers={"Location": "https://evil.invalid", "Retry-After": "900"},
    )
    auth, calls, _ = fake_auth(response)
    with pytest.raises(AlarmRestError) as error:
        await RingAlarmRestGateway(auth).request_json(LOCATIONS_ENDPOINT)
    assert error.value.code == str(error.value) == code
    assert error.value.status == status and error.value.retry_after == 300
    assert response.closed and len(calls) == 1


@pytest.mark.asyncio
async def test_401_is_closed_before_one_refresh_and_no_third_attempt():
    first, second = Response(status=401), Response(status=401)
    auth, calls, scopes = fake_auth(first, second)

    async def refresh():
        assert first.closed
        return {}

    auth.async_refresh_tokens.side_effect = refresh
    with pytest.raises(AlarmRestError, match="authentication-required"):
        await RingAlarmRestGateway(auth).request_json(LOCATIONS_ENDPOINT)
    assert len(calls) == 2 and first.closed and second.closed
    assert auth.async_refresh_tokens.await_count == 1 and scopes == ["enter", "exit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,code",
    [
        (b'{"duplicate":1,"duplicate":2}', "invalid-response"),
        (b'{"value":NaN}', "invalid-response"),
        (b'{"value":1e1000}', "invalid-response"),
        (b'{"value":"bad\\u000a"}', "invalid-response"),
        (b'{"value":"bad\\ud800"}', "invalid-response"),
        (b"\xff", "invalid-response"),
        (b"[[[[]]]]", "invalid-response"),
        (b'{"list":[1,2,3,4,5,6,7,8,9,10]}', "invalid-response"),
    ],
)
async def test_bounded_json_rejects_duplicate_nonfinite_controls_depth_and_nodes(data, code):
    response = Response(data)
    auth, _, _ = fake_auth(response)
    gateway = RingAlarmRestGateway(auth, max_json_depth=3, max_json_nodes=8)
    with pytest.raises(AlarmRestError, match=code):
        await gateway.request_json(LOCATIONS_ENDPOINT)
    assert response.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Content-Length": "-1"}, "invalid-response"),
        ({"Content-Length": "bogus"}, "invalid-response"),
        ({"Content-Length": "２"}, "invalid-response"),
        ({"Content-Length": "129"}, "response-too-large"),
        ({"Content-Length": "3"}, "invalid-response"),
        ({"Content-Encoding": "gzip"}, "unsupported-encoding"),
    ],
)
async def test_header_policy_prevents_publication_and_always_closes(headers, code):
    response = Response(headers=headers)
    auth, _, _ = fake_auth(response)
    with pytest.raises(AlarmRestError, match=code):
        await RingAlarmRestGateway(auth, max_response_bytes=128).request_json(LOCATIONS_ENDPOINT)
    assert response.closed


@pytest.mark.asyncio
async def test_undeclared_overflow_reads_at_most_one_sentinel():
    response = Response(b"x" * 1000)
    auth, _, _ = fake_auth(response)
    with pytest.raises(AlarmRestError, match="response-too-large"):
        await RingAlarmRestGateway(auth, max_response_bytes=32).request_json(LOCATIONS_ENDPOINT)
    assert response.content.position == 33 and response.closed


@pytest.mark.asyncio
async def test_mapping_and_history_top_level_shapes_are_distinct():
    auth, _, _ = fake_auth(Response(b"[]"), Response(b"{}"))
    gateway = RingAlarmRestGateway(auth)
    with pytest.raises(AlarmRestError, match="invalid-response"):
        await gateway.request_json(LOCATIONS_ENDPOINT)
    with pytest.raises(AlarmRestError, match="invalid-response"):
        await gateway.request_history("one")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["_halo_refresh_serialized", "_halo_auth_request_scope"])
async def test_pinned_auth_coordinator_is_required(missing):
    auth, calls, _ = fake_auth()
    delattr(auth, missing)
    with pytest.raises(AlarmRestError, match="authentication-coordination-unavailable"):
        await RingAlarmRestGateway(auth).request_json(LOCATIONS_ENDPOINT)
    assert not calls


@pytest.mark.asyncio
async def test_version_and_token_preparation_cannot_widen_contract(monkeypatch):
    auth, calls, _ = fake_auth()
    gateway = RingAlarmRestGateway(auth)
    monkeypatch.setattr(rest, "version", lambda _name: "0.9.15")
    with pytest.raises(AlarmRestError, match="unsupported-auth-contract"):
        await gateway.request_json(LOCATIONS_ENDPOINT)
    monkeypatch.setattr(rest, "version", lambda _name: "0.9.14")
    auth._oauth_client.add_token = lambda _url, **kwargs: (
        "https://evil.invalid",
        kwargs["headers"],
        None,
    )
    with pytest.raises(AlarmRestError, match="token-preparation-changed-endpoint"):
        await gateway.request_json(LOCATIONS_ENDPOINT)
    assert not calls


def real_auth(session, persisted, *, expired=False, fail_persist=False):
    def update(token):
        if fail_persist:
            raise OSError("private keyring failure")
        persisted.append(dict(token))

    auth = Auth(
        "fixture-real-sdk",
        {
            "access_token": "access-a",
            "refresh_token": "refresh-a",
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": 1 if expired else 4_000_000_000,
        },
        update,
        http_client_session=session,
    )
    ring_client._install_serialized_auth_refresh(auth)
    return auth


class TokenResponse(Response):
    def __init__(self):
        super().__init__(
            json.dumps(
                {
                    "access_token": "access-b",
                    "refresh_token": "refresh-b",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            ).encode()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("refresher", ["alarm", "sdk"])
async def test_real_sdk_staggered_old_401_reuses_shared_refresh_generation(refresher):
    entered, release = asyncio.Event(), asyncio.Event()
    persisted = []

    class Session:
        refreshes = 0

        async def request(self, method, url, **kwargs):
            url = str(url)
            if url == OAuth.ENDPOINT:
                self.refreshes += 1
                return TokenResponse()
            header = kwargs["headers"]["Authorization"]
            if url == DEVICES_ENDPOINT and header == "Bearer access-a":
                entered.set()
                await release.wait()
            if header == "Bearer access-a":
                response = Response(status=401)
                if url.endswith("/sdk-fixture"):

                    async def sdk_read():
                        return b"{}"

                    response.read = sdk_read
                return response
            return Response()

    session = Session()
    auth = real_auth(session, persisted)
    gateway = RingAlarmRestGateway(auth)
    late = asyncio.create_task(gateway.request_json(DEVICES_ENDPOINT))
    await entered.wait()
    try:
        if refresher == "alarm":
            assert await gateway.request_json(LOCATIONS_ENDPOINT) == {}
        else:
            result = await auth.async_query(
                "https://api.ring.com/clients_api/sdk-fixture", raise_for_status=False
            )
            assert result.status_code == 401
        assert session.refreshes == len(persisted) == 1
        release.set()
        assert await late == {}
    finally:
        release.set()
        if not late.done():
            late.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await late
    assert session.refreshes == len(persisted) == 1
    assert auth._token["refresh_token"] == "refresh-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_persist", [False, True])
async def test_real_expired_token_persists_before_safe_read_or_stops_on_failure(fail_persist):
    persisted, calls = [], []

    class Session:
        async def request(self, method, url, **kwargs):
            calls.append(str(url))
            if str(url) == OAuth.ENDPOINT:
                return TokenResponse()
            assert kwargs["headers"]["Authorization"] == "Bearer access-b"
            return Response()

    gateway = RingAlarmRestGateway(
        real_auth(Session(), persisted, expired=True, fail_persist=fail_persist)
    )
    if fail_persist:
        with pytest.raises(AlarmRestError, match="token-refresh-failed"):
            await gateway.request_json(LOCATIONS_ENDPOINT)
        assert calls == [OAuth.ENDPOINT] and not persisted
    else:
        assert await gateway.request_json(LOCATIONS_ENDPOINT) == {}
        assert len(persisted) == 1 and calls == [OAuth.ENDPOINT, LOCATIONS_ENDPOINT]


@pytest.mark.asyncio
async def test_history_limits_coalescing_and_recovery_priority():
    entered = []
    gates = {}

    async def request(url):
        entered.append(url)
        gate = gates.setdefault(url, asyncio.Event())
        await gate.wait()
        return Response(b"[]" if url.startswith(HISTORY_ENDPOINT) else b"{}")

    auth, _, _ = fake_auth(request=request)
    gateway = RingAlarmRestGateway(auth)
    tasks = []

    async def tick_until(predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0)

    try:
        first = asyncio.create_task(gateway.request_history("one", offset=0))
        tasks.append(first)
        await tick_until(lambda: len(entered) == 1)
        same = asyncio.create_task(gateway.request_history("one", offset=0))
        second = asyncio.create_task(gateway.request_history("one", offset=50))
        third = asyncio.create_task(gateway.request_history("one", offset=100))
        tasks += [same, second, third]
        await asyncio.sleep(0)
        with pytest.raises(AlarmRestError, match="busy"):
            await gateway.request_history("one", offset=150)
        recovery1 = asyncio.create_task(gateway.request_json(LOCATIONS_ENDPOINT))
        tasks.append(recovery1)
        await tick_until(lambda: len(entered) == 2)
        recovery2 = asyncio.create_task(gateway.request_json(DEVICES_ENDPOINT))
        tasks.append(recovery2)
        await tick_until(lambda: gateway._recovery_waiters == 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(entered) == 2  # Coalesced second caller still owns the read.
        gates[entered[0]].set()
        await tick_until(lambda: len(entered) == 3)
        assert entered[-1] == DEVICES_ENDPOINT
        assert gateway._active == 2 and not second.done()
        gates[DEVICES_ENDPOINT].set()
        await tick_until(lambda: len(entered) == 4)
        assert parse_qs(urlsplit(entered[-1]).query)["offset"] == ["50"]
    finally:
        await gateway.close()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert gateway._active == 0 and not gateway._history_active


@pytest.mark.asyncio
async def test_history_deadline_includes_admission_and_cancelled_last_user_releases_slot():
    gate = asyncio.Event()
    response = Response(b"[]", gate=gate)
    auth, calls, _ = fake_auth(response)
    gateway = RingAlarmRestGateway(auth)
    active = asyncio.create_task(gateway.request_history("one"))
    async with asyncio.timeout(1):
        while not calls:
            await asyncio.sleep(0)
    with pytest.raises(AlarmRestError, match="timeout"):
        await gateway.request_history("two", timeout=0.01)
    assert len(calls) == 1
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    await asyncio.sleep(0)
    assert response.closed and gateway._active == 0 and not gateway._history


@pytest.mark.asyncio
async def test_recovery_queue_is_bounded_and_network_deadline_releases_slots():
    gate = asyncio.Event()

    async def request(_url):
        return Response(gate=gate)

    auth, _, _ = fake_auth(request=request)
    gateway = RingAlarmRestGateway(auth, max_pending_recovery=1)
    tasks = [asyncio.create_task(gateway.request_json(LOCATIONS_ENDPOINT)) for _ in range(3)]
    async with asyncio.timeout(1):
        while gateway._recovery_waiters != 1:
            await asyncio.sleep(0)
    with pytest.raises(AlarmRestError, match="busy"):
        await gateway.request_json(DEVICES_ENDPOINT)
    await gateway.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert gateway._active == gateway._recovery_waiters == 0
    with pytest.raises(AlarmRestError, match="closed"):
        await gateway.request_json(LOCATIONS_ENDPOINT)


@pytest.mark.asyncio
async def test_network_deadline_cancels_body_closes_response_and_releases_slot():
    response = Response(gate=asyncio.Event())
    auth, _, scopes = fake_auth(response)
    gateway = RingAlarmRestGateway(auth, request_timeout=0.01)
    with pytest.raises(AlarmRestError, match="timeout"):
        await gateway.request_json(LOCATIONS_ENDPOINT)
    assert response.closed and gateway._active == 0
    assert not gateway._requests and scopes == ["enter", "exit"]


@pytest.mark.asyncio
async def test_network_exception_is_sanitized_and_does_not_expose_url():
    async def request(_url):
        raise aiohttp.ClientConnectionError("private ticket URL/token")

    auth, _, _ = fake_auth(request=request)
    gateway = RingAlarmRestGateway(auth)
    with pytest.raises(AlarmRestError) as error:
        await gateway.request_json(LOCATIONS_ENDPOINT)
    assert str(error.value) == "network-unavailable"
    assert error.value.__cause__ is None
    assert gateway._active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location,limit,offset",
    [
        ("", 50, 0),
        ("bad\nlocation", 50, 0),
        ("one", 51, 0),
        ("one", True, 0),
        ("one", 50, -1),
        ("one", 50, True),
        ("one", 50, 2_147_483_648),
    ],
)
async def test_history_arguments_are_bounded_before_admission(location, limit, offset):
    auth, calls, _ = fake_auth()
    with pytest.raises(AlarmRestError, match="invalid-request"):
        await RingAlarmRestGateway(auth).request_history(location, limit, offset)
    assert not calls


@pytest.mark.asyncio
async def test_deadline_also_bounds_refresh_and_never_replays_after_it_expires():
    response = Response(status=401)
    auth, calls, scopes = fake_auth(response)

    async def stalled_refresh():
        assert response.closed
        await asyncio.Event().wait()

    auth.async_refresh_tokens.side_effect = stalled_refresh
    gateway = RingAlarmRestGateway(auth, request_timeout=0.01)
    with pytest.raises(AlarmRestError, match="timeout"):
        await gateway.request_json(LOCATIONS_ENDPOINT)
    assert len(calls) == 1 and gateway._active == 0
    assert scopes == ["enter", "exit"]


def test_retry_after_is_finite_bounded_and_supports_http_dates():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 28 <= rest._retry_after(future) <= 30
    assert rest._retry_after("900") == 300
    assert rest._retry_after("-10") == 0
    assert rest._retry_after("NaN") is None
    assert rest._retry_after("private invalid server message") is None
