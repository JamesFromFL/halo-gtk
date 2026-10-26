"""Bounded authenticated image fetch for the pinned Ring Auth contract only."""

from __future__ import annotations

import asyncio
import re
import ssl
from importlib.metadata import version
from urllib.parse import urlsplit

import aiohttp
from oauthlib.oauth2 import TokenExpiredError
from yarl import URL

from halo_gtk.preview import MAX_IMAGE_BYTES, PreviewError


def authenticated_snapshot_url(url):
    if str(url).startswith("/clients_api/snapshots/image/"):
        url = "https://api.ring.com" + str(url)
    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except ValueError:
        raise PreviewError("Invalid snapshot port") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.ring.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not re.fullmatch(r"/clients_api/snapshots/image/\d+", parsed.path)
    ):
        raise PreviewError("Untrusted authenticated snapshot endpoint")
    return str(url)


async def snapshot_bytes(auth, url):
    """Use SDK token preparation/serialized refresh, never its eager resp.read().

    Auth headers can go only to the exact Ring snapshot endpoint, and redirects
    are rejected before another request. Signed CDN URLs use the no-auth worker.
    """
    url = authenticated_snapshot_url(url)
    if version("ring-doorbell") != "0.9.14":
        raise PreviewError("Unsupported snapshot authentication contract")
    request_scope = getattr(auth, "_halo_auth_request_scope", None)
    if not callable(request_scope):
        raise PreviewError("Snapshot authentication coordination unavailable")
    with request_scope():
        return await _snapshot_request(auth, url)


async def _snapshot_request(auth, url):
    """Remain in the shared request generation through token refresh and retry."""
    try:
        async with asyncio.timeout(8):
            for attempt in range(2):
                headers = {
                    "User-Agent": auth.user_agent,
                    "hardware_id": auth.get_hardware_id(),
                    "Accept-Encoding": "identity",
                }
                try:
                    prepared, headers, body = auth._oauth_client.add_token(
                        url, http_method="GET", body=None, headers=headers
                    )
                except TokenExpiredError:
                    if attempt:
                        raise PreviewError("Snapshot authentication expired") from None
                    auth._token = await auth.async_refresh_tokens()
                    continue
                # Token preparation must not widen the endpoint's authority.
                if authenticated_snapshot_url(prepared) != url:
                    raise PreviewError("Snapshot token preparation changed endpoint")
                timeout = aiohttp.ClientTimeout(total=7, connect=3, sock_connect=3, sock_read=2)
                response = await auth._session.request(
                    "GET",
                    URL(prepared, encoded=True),
                    headers=headers,
                    data=body,
                    timeout=timeout,
                    allow_redirects=False,
                    auto_decompress=False,
                    ssl=ssl.create_default_context(),
                )
                refresh = response.status == 401 and attempt == 0
                async with response:
                    if not refresh:
                        if response.status != 200:
                            raise PreviewError("Snapshot response rejected")
                        if (
                            response.headers.get("Content-Encoding", "identity").lower()
                            != "identity"
                        ):
                            raise PreviewError("Snapshot encoding rejected")
                        declared = response.headers.get("Content-Length")
                        if declared is not None:
                            try:
                                length = int(declared)
                            except ValueError:
                                raise PreviewError("Invalid snapshot length") from None
                            if not 0 <= length <= MAX_IMAGE_BYTES:
                                raise PreviewError("Snapshot exceeds size limit")
                        result = bytearray()
                        while True:
                            chunk = await response.content.read(
                                min(16 * 1024, MAX_IMAGE_BYTES - len(result) + 1)
                            )
                            if not chunk:
                                return bytes(result)
                            result.extend(chunk)
                            if len(result) > MAX_IMAGE_BYTES:
                                raise PreviewError("Snapshot exceeds size limit")
                # Release/close the 401 response before token refresh, which
                # may need the same connector slot (including limit=1).
                if refresh:
                    auth._token = await auth.async_refresh_tokens()
    except (aiohttp.ClientError, TimeoutError):
        raise PreviewError("Snapshot network unavailable") from None
    raise PreviewError("Snapshot authentication unavailable")
