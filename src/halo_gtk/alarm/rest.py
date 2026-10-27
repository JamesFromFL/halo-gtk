"""Bounded, read-only Alarm REST operations using the pinned Ring Auth session.

No OAuth client or HTTP session is created here. The existing Auth owns token
rotation/persistence; its shared request scope fences staggered old-token 401s.
The SDK's eager response reader is deliberately not used for Alarm JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as json_module
import math
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp
from oauthlib.oauth2 import TokenExpiredError
from yarl import URL

from halo_gtk.alarm.provider import AlarmRequestError

APP_API_ORIGIN = "https://prd-api-us.prd.rings.solutions"
TICKET_ENDPOINT = APP_API_ORIGIN + "/api/v1/clap/tickets"
HISTORY_ENDPOINT = APP_API_ORIGIN + "/api/v1/rs/history"
LOCATIONS_ENDPOINT = "https://api.ring.com/devices/v1/locations"
DEVICES_ENDPOINT = "https://api.ring.com/clients_api/ring_devices"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000


class AlarmRestError(AlarmRequestError):
    """A sanitized failure; status is metadata, never an upstream message/body."""

    def __init__(self, code: str, *, status: int | None = None, retry_after=None):
        super().__init__(code, retry_after=retry_after)
        self.status = status


@dataclass
class _HistoryRequest:
    task: asyncio.Task
    users: int = 0


class RingAlarmRestGateway:
    """Allowlisted GETs; two active reads, with one reserved from history use.

    History page calls coalesce identical keys, with one active and two pending
    page requests. Recovery requests take priority when a slot becomes free.
    A history controller still owns the deadline across multiple pages.
    """

    def __init__(
        self,
        auth,
        *,
        request_timeout=10.0,
        history_timeout=30.0,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_json_depth=MAX_JSON_DEPTH,
        max_json_nodes=MAX_JSON_NODES,
        max_pending_recovery=8,
    ):
        for value in (request_timeout, history_timeout):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("REST deadlines must be positive and finite")
        for value in (max_response_bytes, max_json_depth, max_json_nodes, max_pending_recovery):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("REST bounds must be positive integers")
        self._auth = auth
        self._request_timeout = min(10.0, request_timeout)
        self._history_timeout = min(30.0, history_timeout)
        self._max_bytes = min(MAX_RESPONSE_BYTES, max_response_bytes)
        self._max_depth = min(MAX_JSON_DEPTH, max_json_depth)
        self._max_nodes = min(MAX_JSON_NODES, max_json_nodes)
        self._max_pending_recovery = min(8, max_pending_recovery)
        self._condition = asyncio.Condition()
        self._active = 0
        self._history_active = False
        self._recovery_waiters = 0
        self._history: dict[tuple[str, int, int], _HistoryRequest] = {}
        self._requests: set[asyncio.Task] = set()
        self._closed = False
        self._tls = ssl.create_default_context()

    async def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        json: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Keep the original gateway interface mapping-only and read-only."""
        if method != "GET" or json is not None:
            raise AlarmRestError("operation-not-allowed")
        url = _mapping_url(url)
        duration = _duration(timeout, self._request_timeout)
        return await self._request(url, history=False, duration=duration)

    async def request_history(self, location_id, limit=50, offset=0, *, timeout=None) -> list:
        """Read one scoped Alarm history page; no camera/video event coercion."""
        location_id = _identifier(location_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 2_147_483_647
        ):
            raise AlarmRestError("invalid-request")
        self._check_auth()
        duration = _duration(timeout, self._history_timeout)
        key = (location_id, limit, offset)
        entry = self._history.get(key)
        if entry is None:
            if len(self._history) >= 3:
                raise AlarmRestError("busy")
            url = (
                HISTORY_ENDPOINT
                + "?"
                + urlencode(
                    {
                        "accountId": location_id,
                        "category": "alarm",
                        "limit": limit,
                        "offset": offset,
                        "maxLevel": 50,
                    }
                )
            )
            task = asyncio.create_task(self._request(url, history=True, duration=duration))
            entry = _HistoryRequest(task)
            self._history[key] = entry

            def completed(task):
                if self._history.get(key) is entry:
                    self._history.pop(key)
                if not task.cancelled():
                    task.exception()

            task.add_done_callback(completed)
        entry.users += 1
        try:
            async with asyncio.timeout(duration):
                return await asyncio.shield(entry.task)
        except TimeoutError:
            raise AlarmRestError("timeout") from None
        finally:
            entry.users -= 1
            if not entry.users and not entry.task.done():
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, AlarmRequestError):
                    await entry.task

    async def close(self):
        """Cancel owned work without closing the shared Auth session."""
        self._closed = True
        current = asyncio.current_task()
        tasks = self._requests | {entry.task for entry in self._history.values()}
        tasks.discard(current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _check_auth(self):
        if self._closed:
            raise AlarmRestError("closed")
        try:
            compatible = version("ring-doorbell") == "0.9.14"
        except Exception:
            compatible = False
        if not compatible:
            raise AlarmRestError("unsupported-auth-contract")
        if getattr(self._auth, "_halo_refresh_serialized", False) is not True or not callable(
            getattr(self._auth, "_halo_auth_request_scope", None)
        ):
            raise AlarmRestError("authentication-coordination-unavailable")

    @contextlib.asynccontextmanager
    async def _admit(self, history):
        admitted = False
        async with self._condition:
            if not history:
                if self._recovery_waiters >= self._max_pending_recovery:
                    raise AlarmRestError("busy")
                self._recovery_waiters += 1
            try:
                while self._active >= 2 or (
                    history and (self._history_active or self._recovery_waiters)
                ):
                    await self._condition.wait()
                self._check_auth()
                self._active += 1
                if history:
                    self._history_active = True
                admitted = True
            finally:
                if not history:
                    self._recovery_waiters -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            if admitted:
                async with self._condition:
                    self._active -= 1
                    if history:
                        self._history_active = False
                    self._condition.notify_all()

    async def _request(self, url, *, history, duration):
        self._check_auth()
        current = asyncio.current_task()
        self._requests.add(current)
        deadline = asyncio.get_running_loop().time() + duration
        try:
            async with asyncio.timeout_at(deadline), self._admit(history):
                with self._auth._halo_auth_request_scope():
                    result = await self._authenticated_get(url, deadline)
                result = _decode_json(result, self._max_depth, self._max_nodes)
                if asyncio.get_running_loop().time() >= deadline:
                    raise AlarmRestError("timeout")
                expected = list if history else dict
                if not isinstance(result, expected):
                    raise AlarmRestError("invalid-response")
                return result
        except (AlarmRequestError, asyncio.CancelledError):
            raise
        except TimeoutError:
            raise AlarmRestError("timeout") from None
        except aiohttp.ClientError:
            raise AlarmRestError("network-unavailable") from None
        except Exception:
            raise AlarmRestError("request-failed") from None
        finally:
            self._requests.discard(current)

    async def _refresh(self):
        from ring_doorbell import AuthenticationError

        try:
            self._auth._token = await self._auth.async_refresh_tokens()
        except asyncio.CancelledError:
            raise
        except AuthenticationError:
            raise AlarmRestError("authentication-required") from None
        except (aiohttp.ClientError, TimeoutError):
            raise AlarmRestError("network-unavailable") from None
        except Exception:
            # Includes token persistence failures: do not replay with a token
            # whose refresh did not finish the shared persistence contract.
            raise AlarmRestError("token-refresh-failed") from None

    async def _authenticated_get(self, url, deadline):
        for attempt in range(2):
            headers = {
                "User-Agent": self._auth.user_agent,
                "hardware_id": self._auth.get_hardware_id(),
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            try:
                prepared, headers, body = self._auth._oauth_client.add_token(
                    url,
                    http_method="GET",
                    body=None,
                    headers=headers,
                )
            except TokenExpiredError:
                if attempt:
                    raise AlarmRestError("authentication-required") from None
                await self._refresh()
                continue
            if prepared != url or body is not None:
                raise AlarmRestError("token-preparation-changed-endpoint")
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            response = await self._auth._session.request(
                "GET",
                URL(prepared, encoded=True),
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=min(10, remaining),
                    connect=min(3, remaining),
                    sock_connect=min(3, remaining),
                    sock_read=min(3, remaining),
                ),
                allow_redirects=False,
                auto_decompress=False,
                ssl=self._tls,
            )
            refresh = response.status == 401 and attempt == 0
            async with response:
                if not refresh:
                    _check_status(response)
                    return await self._read(response)
            # The connector slot is released before token rotation/replay.
            if refresh:
                await self._refresh()
        raise AlarmRestError("authentication-required")

    async def _read(self, response):
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise AlarmRestError("unsupported-encoding")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            if not declared.isascii() or not declared.isdigit():
                raise AlarmRestError("invalid-response")
            try:
                declared = int(declared)
            except ValueError:
                raise AlarmRestError("invalid-response") from None
            if declared > self._max_bytes:
                raise AlarmRestError("response-too-large")
        result = bytearray()
        while True:
            chunk = await response.content.read(min(16 * 1024, self._max_bytes - len(result) + 1))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > self._max_bytes:
                raise AlarmRestError("response-too-large")
        if declared is not None and len(result) != declared:
            raise AlarmRestError("invalid-response")
        return bytes(result)


def _duration(value, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise AlarmRestError("invalid-request")
    if value <= 0:
        raise AlarmRestError("timeout")
    return min(default, value)


def _identifier(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise AlarmRestError("invalid-request")
    return value


def _mapping_url(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise AlarmRestError("endpoint-not-allowed")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(ord(character) <= 32 or ord(character) == 127 for character in url)
        ):
            raise ValueError
        if url in {LOCATIONS_ENDPOINT, DEVICES_ENDPOINT}:
            return url
        if (
            parsed.hostname != "prd-api-us.prd.rings.solutions"
            or parsed.path != "/api/v1/clap/tickets"
        ):
            raise ValueError
        values = parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
        )
        query = dict(values)
        if len(values) != len(query) or set(query) != {
            "locationID",
            "enableExtendedEmergencyCellUsage",
            "requestedTransport",
        }:
            raise ValueError
        if (
            query["enableExtendedEmergencyCellUsage"] != "true"
            or query["requestedTransport"] != "ws"
        ):
            raise ValueError
        location = _identifier(query["locationID"])
        canonical = (
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
        # Normalize only allowlisted, fully parsed query values; caller syntax
        # can never widen the credential destination or select another route.
        return canonical
    except (ValueError, AlarmRequestError):
        raise AlarmRestError("endpoint-not-allowed") from None


def _check_status(response):
    status = response.status
    if status == 200:
        return
    if status == 401:
        code = "authentication-required"
    elif status == 403:
        code = "permission-denied"
    elif status == 429:
        code = "rate-limited"
    elif 300 <= status < 400:
        code = "redirect-not-allowed"
    else:
        code = "request-failed"
    retry_after = _retry_after(response.headers.get("Retry-After"))
    raise AlarmRestError(code, status=status, retry_after=retry_after)


def _retry_after(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        try:
            result = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(300.0, max(0.0, result)) if math.isfinite(result) else None


def _decode_json(data, max_depth, max_nodes):
    try:
        text = data.decode("utf-8")
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > max_depth:
                    raise ValueError
            elif char in "]}":
                depth -= 1

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        def number(value):
            result = float(value)
            if not math.isfinite(result):
                raise ValueError
            return result

        def invalid_constant(_value):
            raise ValueError

        result = json_module.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=number,
            parse_constant=invalid_constant,
        )
        pending = [result]
        nodes = 0
        while pending:
            value = pending.pop()
            nodes += 1
            if nodes > max_nodes:
                raise ValueError
            if isinstance(value, dict):
                pending.extend(value)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str) and any(
                ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                for char in value
            ):
                raise ValueError
        return result
    except (UnicodeError, ValueError, RecursionError):
        raise AlarmRestError("invalid-response") from None
