"""Pinned SDK authentication with capped streaming; no real token or network."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from oauthlib.oauth2 import TokenExpiredError
from ring_doorbell import Auth
from ring_doorbell.const import OAuth

from halo_gtk import ring_client, snapshot_http
from halo_gtk.preview import PreviewError


class Stream:
    def __init__(self, data):
        self.data = data
        self.position = 0
        self.calls = []

    async def read(self, size):
        self.calls.append(size)
        chunk = self.data[self.position : self.position + min(size, 13)]
        self.position += len(chunk)
        return chunk


class Response:
    def __init__(self, data=b"image", *, status=200, headers=None):
        self.content = Stream(data)
        self.status = status
        self.headers = headers or {}
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def read(self):
        raise AssertionError("SDK eager resp.read must never be called")


def auth_for(*responses, expired=False):
    calls = []

    class OAuth:
        def add_token(self, url, **kwargs):
            nonlocal expired
            if expired:
                expired = False
                raise TokenExpiredError()
            headers = kwargs["headers"]
            headers["Authorization"] = "Bearer fixture-only"
            return url, headers, None

    async def request(method, url, **kwargs):
        calls.append((method, str(url), kwargs))
        return responses[len(calls) - 1]

    auth = SimpleNamespace(
        user_agent="fixture",
        get_hardware_id=lambda: "fixture",
        _oauth_client=OAuth(),
        _session=SimpleNamespace(request=request),
        async_refresh_tokens=AsyncMock(return_value={}),
        _halo_auth_request_scope=contextlib.nullcontext,
    )
    return auth, calls


@pytest.mark.asyncio
async def test_streams_without_resp_read_with_verified_tls_no_redirects():
    response = Response(b"bounded image")
    auth, calls = auth_for(response)
    result = await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/123")
    assert result == b"bounded image" and response.closed
    method, url, options = calls[0]
    assert method == "GET" and url == "https://api.ring.com/clients_api/snapshots/image/123"
    assert options["allow_redirects"] is False and options["auto_decompress"] is False
    assert options["ssl"].check_hostname is True
    assert options["ssl"].verify_mode.name == "CERT_REQUIRED"


@pytest.mark.asyncio
async def test_declared_size_rejected_before_body_read(monkeypatch):
    monkeypatch.setattr(snapshot_http, "MAX_IMAGE_BYTES", 32)
    response = Response(headers={"Content-Length": "33"})
    auth, calls = auth_for(response)
    with pytest.raises(PreviewError, match="size limit"):
        await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1")
    assert not response.content.calls and response.closed


@pytest.mark.asyncio
async def test_chunked_undeclared_body_capped_at_one_sentinel(monkeypatch):
    monkeypatch.setattr(snapshot_http, "MAX_IMAGE_BYTES", 32)
    response = Response(b"x" * 100)
    auth, calls = auth_for(response)
    with pytest.raises(PreviewError, match="size limit"):
        await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1")
    assert response.content.position == 33 and response.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308, 404])
async def test_redirect_or_error_never_forwards_credentials(status):
    response = Response(status=status, headers={"Location": "https://evil.invalid/image"})
    auth, calls = auth_for(response)
    with pytest.raises(PreviewError, match="rejected"):
        await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1")
    assert len(calls) == 1 and response.closed and not response.content.calls


@pytest.mark.asyncio
async def test_401_refreshes_once_then_streams():
    unauthorized = Response(status=401)
    valid = Response(b"image")
    auth, calls = auth_for(unauthorized, valid)
    assert await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1") == b"image"
    assert auth.async_refresh_tokens.await_count == 1 and len(calls) == 2
    assert unauthorized.closed and not unauthorized.content.calls and valid.closed


@pytest.mark.asyncio
async def test_401_releases_connector_slot_before_refresh():
    unauthorized = Response(status=401)
    auth, calls = auth_for(unauthorized, Response())

    async def refresh():
        assert unauthorized.closed, "401 response holds the token refresh connector slot"
        return {}

    auth.async_refresh_tokens = AsyncMock(side_effect=refresh)
    assert await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1") == b"image"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_token_uses_sdk_refresh_coordination():
    auth, calls = auth_for(Response(), expired=True)
    assert await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1") == b"image"
    assert auth.async_refresh_tokens.await_count == 1 and len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.ring.com/clients_api/snapshots/image/1",
        "https://@api.ring.com/clients_api/snapshots/image/1",
        "https://api.ring.com:bad/clients_api/snapshots/image/1",
        "https://api.ring.com.evil/clients_api/snapshots/image/1",
        "https://api.ring.com/clients_api/session",
        "https://api.ring.com:444/clients_api/snapshots/image/1",
    ],
)
def test_authenticated_endpoint_allowlist(url):
    with pytest.raises(PreviewError):
        snapshot_http.authenticated_snapshot_url(url)


@pytest.mark.asyncio
async def test_sdk_version_drift_fails_before_authentication(monkeypatch):
    monkeypatch.setattr(snapshot_http, "version", lambda name: "0.9.15")
    auth, calls = auth_for(Response())
    with pytest.raises(PreviewError, match="contract"):
        await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1")
    assert not calls


@pytest.mark.asyncio
async def test_token_preparation_cannot_change_endpoint():
    auth, calls = auth_for(Response())
    auth._oauth_client.add_token = lambda *args, **kwargs: (
        "https://api.ring.com/clients_api/snapshots/image/999",
        {},
        None,
    )
    with pytest.raises(PreviewError, match="changed endpoint"):
        await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1")
    assert not calls


class TokenResponse(Response):
    def __init__(self):
        super().__init__()
        self.body = json.dumps(
            {
                "access_token": "access-b",
                "refresh_token": "refresh-b",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )

    async def text(self):
        return self.body


class SDKResponse(Response):
    async def read(self):
        return b"{}"


def real_auth(session, persisted, *, expired=False):
    auth = Auth(
        "android:com.ringapp",
        {
            "access_token": "access-a",
            "refresh_token": "refresh-a",
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": 1 if expired else 4_000_000_000,
        },
        lambda token: persisted.append(dict(token)),
        http_client_session=session,
    )
    ring_client._install_serialized_auth_refresh(auth)
    return auth


@pytest.mark.asyncio
@pytest.mark.parametrize("refresher", ["snapshot", "sdk"])
async def test_real_auth_staggered_snapshot_401_reuses_already_rotated_token(refresher):
    late_started = asyncio.Event()
    release_late = asyncio.Event()
    persisted = []

    class Session:
        refresh_calls = 0

        def __init__(self):
            self.protected = []

        async def request(self, method, url, **kwargs):
            url = str(url)
            if url == OAuth.ENDPOINT:
                self.refresh_calls += 1
                return TokenResponse()
            authorization = kwargs["headers"]["Authorization"]
            self.protected.append((url, authorization))
            image = "/snapshots/image/" in url
            if url.endswith("/2") and authorization == "Bearer access-a":
                late_started.set()
                await release_late.wait()
            if authorization == "Bearer access-a":
                return Response(status=401) if image else SDKResponse(status=401)
            return Response(b"image") if image else SDKResponse()

    session = Session()
    auth = real_auth(session, persisted)
    late = asyncio.create_task(snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/2"))
    await late_started.wait()
    try:
        if refresher == "snapshot":
            assert (
                await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1")
                == b"image"
            )
        else:
            response = await auth.async_query(
                "https://api.ring.com/clients_api/devices", raise_for_status=False
            )
            assert response.status_code == 401
        # The first refresh and persistence finish before B's old 401 reaches
        # the coordinator, so lock-only overlap deduplication cannot pass this.
        assert session.refresh_calls == len(persisted) == 1
        assert persisted[0]["refresh_token"] == "refresh-b"
        release_late.set()
        assert await late == b"image"
    finally:
        if not late.done():
            late.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await late
    assert session.refresh_calls == len(persisted) == 1
    assert auth._token["refresh_token"] == "refresh-b"
    assert session.protected[-1][1] == "Bearer access-b"


@pytest.mark.asyncio
async def test_real_auth_expired_snapshot_token_refreshes_and_persists_once():
    persisted = []

    class Session:
        refresh_calls = 0
        image_calls = 0

        async def request(self, method, url, **kwargs):
            if str(url) == OAuth.ENDPOINT:
                self.refresh_calls += 1
                return TokenResponse()
            self.image_calls += 1
            assert kwargs["headers"]["Authorization"] == "Bearer access-b"
            return Response(b"image")

    session = Session()
    auth = real_auth(session, persisted, expired=True)
    assert await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1") == b"image"
    assert session.refresh_calls == session.image_calls == len(persisted) == 1
    assert auth._token["refresh_token"] == "refresh-b"


@pytest.mark.asyncio
async def test_snapshot_rejects_missing_shared_auth_coordination():
    auth, calls = auth_for(Response())
    del auth._halo_auth_request_scope
    with pytest.raises(PreviewError, match="coordination"):
        await snapshot_http.snapshot_bytes(auth, "/clients_api/snapshots/image/1")
    assert not calls
