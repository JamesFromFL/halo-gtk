"""Shared bounded Ring JSON transport; feature wrappers own route policy.

This core reuses the existing pinned Auth/session and refresh coordinator. An
admitted write is possibly sent, never a physical-wire or actuator guarantee.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json as json_module
import math
import ssl
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from urllib.parse import urlsplit

import aiohttp
from oauthlib.oauth2 import TokenExpiredError
from yarl import URL

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_WRITE_REPLY_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000
_ORIGINS = frozenset({"api.ring.com", "prd-api-us.prd.rings.solutions"})


class RingJsonError(RuntimeError):
    """Sanitized transport failure; upstream text/URLs are never retained."""

    def __init__(self, code, *, status=None, retry_after=None, possibly_sent=False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.possibly_sent = possibly_sent


@dataclass(frozen=True, slots=True)
class RingWriteReceipt:
    """HTTP-level acceptance only; body completion is separate from actuation."""

    status: int
    body_complete: bool = True
    body_error: str | None = None
    response_json: dict | None = None

    @property
    def accepted(self):
        return 200 <= self.status < 300


class RingJsonTransport:
    """Two aggregate slots, one shared FIFO non-recovery slot, recovery priority."""

    def __init__(self, auth, *, max_pending_recovery=8, max_pending_background=8):
        for value in (max_pending_recovery, max_pending_background):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16:
                raise ValueError("Pending request bounds must be integers from 1 to 16")
        self._auth = auth
        self._max_pending_recovery = max_pending_recovery
        self._max_pending_background = max_pending_background
        self._condition = asyncio.Condition()
        self._recovery = deque()
        self._background = deque()
        self._active = 0
        self._background_active = 0
        self._requests = set()
        self._closed = False
        self._tls = ssl.create_default_context()

    @property
    def recovery_waiters(self):
        return len(self._recovery)

    async def request_json(
        self,
        url,
        *,
        timeout=10,
        recovery=False,
        expected_type=dict,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_json_depth=MAX_JSON_DEPTH,
        max_json_nodes=MAX_JSON_NODES,
        admission: Callable[[], None] | None = None,
        timeout_limit=10,
    ):
        if (
            expected_type not in (dict, list)
            or timeout_limit not in (10, 30)
            or not isinstance(recovery, bool)
        ):
            raise RingJsonError("invalid-request")
        bounds = (
            (max_response_bytes, MAX_RESPONSE_BYTES),
            (max_json_depth, MAX_JSON_DEPTH),
            (max_json_nodes, MAX_JSON_NODES),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= cap
            for value, cap in bounds
        ):
            raise RingJsonError("invalid-request")
        return await self._request(
            url,
            method="GET",
            payload=None,
            timeout=timeout,
            recovery=recovery,
            expected_type=expected_type,
            bounds=bounds,
            admission=admission,
            on_admitted=None,
            timeout_limit=timeout_limit,
        )

    async def request_write(
        self,
        url,
        *,
        method,
        json: Mapping,
        timeout=10,
        admission: Callable[[], None] | None = None,
        on_admitted: Callable[[], None] | None = None,
        read_json: bool = False,
    ) -> RingWriteReceipt:
        if (
            method not in {"PUT", "POST"}
            or not isinstance(json, Mapping)
            or not isinstance(read_json, bool)
        ):
            raise RingJsonError("operation-not-allowed")
        try:
            encoded = json_module.dumps(dict(json), allow_nan=False, ensure_ascii=True).encode()
            if len(encoded) > 4096:
                raise ValueError
            payload = _decode_json(encoded, MAX_JSON_DEPTH, MAX_JSON_NODES)
        except (TypeError, ValueError, UnicodeError):
            raise RingJsonError("invalid-request") from None
        return await self._request(
            url,
            method=method,
            payload=payload,
            timeout=timeout,
            recovery=False,
            expected_type=None,
            bounds=((MAX_WRITE_REPLY_BYTES, MAX_WRITE_REPLY_BYTES),),
            admission=admission,
            on_admitted=on_admitted,
            timeout_limit=10,
            decode_write_json=read_json,
        )

    def _check_auth(self):
        if self._closed:
            raise RingJsonError("closed")
        try:
            compatible = version("ring-doorbell") == "0.9.14"
        except Exception:
            compatible = False
        if not compatible:
            raise RingJsonError("unsupported-auth-contract")
        if getattr(self._auth, "_halo_refresh_serialized", False) is not True or not callable(
            getattr(self._auth, "_halo_auth_request_scope", None)
        ):
            raise RingJsonError("authentication-coordination-unavailable")

    @contextlib.asynccontextmanager
    async def _admit(self, recovery):
        queue = self._recovery if recovery else self._background
        bound = self._max_pending_recovery if recovery else self._max_pending_background
        ticket = object()
        admitted = False
        async with self._condition:
            if len(queue) >= bound:
                raise RingJsonError("busy")
            queue.append(ticket)
            try:
                while (
                    self._active >= 2
                    or queue[0] is not ticket
                    or (not recovery and (self._background_active or self._recovery))
                ):
                    self._check_auth()
                    await self._condition.wait()
                self._check_auth()
                queue.popleft()
                self._active += 1
                self._background_active += int(not recovery)
                admitted = True
            finally:
                if ticket in queue:
                    queue.remove(ticket)
                self._condition.notify_all()
        try:
            yield
        finally:
            if admitted:
                async with self._condition:
                    self._active -= 1
                    self._background_active -= int(not recovery)
                    self._condition.notify_all()

    async def _request(
        self,
        url,
        *,
        method,
        payload,
        timeout,
        recovery,
        expected_type,
        bounds,
        admission,
        on_admitted,
        timeout_limit,
        decode_write_json=False,
    ):
        _validate_url(url)
        duration = _duration(timeout, timeout_limit)
        self._check_auth()
        current = asyncio.current_task()
        self._requests.add(current)
        deadline = asyncio.get_running_loop().time() + duration
        write = method != "GET"
        sent = False
        accepted_status = None
        try:
            async with asyncio.timeout_at(deadline), self._admit(recovery):
                with self._auth._halo_auth_request_scope():
                    refreshed = False
                    while True:
                        self._check_auth()
                        headers = {
                            "User-Agent": self._auth.user_agent,
                            "hardware_id": self._auth.get_hardware_id(),
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                        }
                        if write:
                            headers["Content-Type"] = "application/json"
                        try:
                            prepared, headers, body = self._auth._oauth_client.add_token(
                                url,
                                http_method=method,
                                body=None,
                                headers=headers,
                            )
                        except TokenExpiredError:
                            if refreshed:
                                raise RingJsonError("authentication-required") from None
                            await self._refresh()
                            refreshed = True
                            continue
                        if prepared != url or body is not None:
                            raise RingJsonError("token-preparation-changed-endpoint")
                        self._check_auth()
                        if asyncio.get_running_loop().time() >= deadline:
                            raise RingJsonError("timeout")
                        if admission is not None:
                            _callback(admission)
                        if write:
                            if on_admitted is not None:
                                _callback(on_admitted)
                            sent = True
                        # No await separates validation/marking from handing the
                        # request to aiohttp. Connector work can still await.
                        remaining = max(0.001, deadline - asyncio.get_running_loop().time())
                        options = {"json": payload} if write else {}
                        response = await self._auth._session.request(
                            method,
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
                            **options,
                        )
                        refresh = not write and response.status == 401 and not refreshed
                        async with response:
                            if not refresh:
                                _check_status(response, write=write)
                                if write:
                                    accepted_status = response.status
                                data = await _read(response, bounds[0][0])
                        if refresh:
                            await self._refresh()
                            refreshed = True
                            continue
                        if write:
                            if asyncio.get_running_loop().time() >= deadline:
                                return RingWriteReceipt(accepted_status, False, "timeout")
                            reply = None
                            if decode_write_json:
                                reply = _decode_json(data, MAX_JSON_DEPTH, MAX_JSON_NODES)
                                if not isinstance(reply, dict):
                                    raise RingJsonError("invalid-response")
                            return RingWriteReceipt(accepted_status, response_json=reply)
                        result = _decode_json(data, bounds[1][0], bounds[2][0])
                        if asyncio.get_running_loop().time() >= deadline:
                            raise RingJsonError("timeout")
                        if not isinstance(result, expected_type):
                            raise RingJsonError("invalid-response")
                        return result
        except asyncio.CancelledError:
            raise
        except RingJsonError as exc:
            if accepted_status is not None:
                return RingWriteReceipt(accepted_status, False, exc.code)
            exc.possibly_sent = sent
            raise
        except TimeoutError:
            if accepted_status is not None:
                return RingWriteReceipt(accepted_status, False, "timeout")
            raise RingJsonError("timeout", possibly_sent=sent) from None
        except aiohttp.ClientError:
            if accepted_status is not None:
                return RingWriteReceipt(accepted_status, False, "network-unavailable")
            raise RingJsonError("network-unavailable", possibly_sent=sent) from None
        except Exception:
            if accepted_status is not None:
                return RingWriteReceipt(accepted_status, False, "request-failed")
            raise RingJsonError("request-failed", possibly_sent=sent) from None
        finally:
            self._requests.discard(current)

    async def _refresh(self):
        from ring_doorbell import AuthenticationError

        try:
            self._auth._token = await self._auth.async_refresh_tokens()
        except asyncio.CancelledError:
            raise
        except AuthenticationError:
            raise RingJsonError("authentication-required") from None
        except (aiohttp.ClientError, TimeoutError):
            raise RingJsonError("network-unavailable") from None
        except Exception:
            raise RingJsonError("token-refresh-failed") from None

    async def close(self):
        self._closed = True
        current = asyncio.current_task()
        tasks = self._requests - {current}
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _duration(value, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise RingJsonError("invalid-request")
    if value <= 0:
        raise RingJsonError("timeout")
    return min(default, value)


def _callback(callback):
    result = callback()
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise RingJsonError("invalid-admission")


def _validate_url(url):
    try:
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ORIGINS
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
        ):
            raise ValueError
    except ValueError:
        raise RingJsonError("endpoint-not-allowed") from None


async def _read(response, max_bytes):
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise RingJsonError("unsupported-encoding")
    declared = response.headers.get("Content-Length")
    if declared is not None:
        if not declared.isascii() or not declared.isdigit():
            raise RingJsonError("invalid-response")
        try:
            declared = int(declared)
        except ValueError:
            raise RingJsonError("invalid-response") from None
        if declared > max_bytes:
            raise RingJsonError("response-too-large")
    result = bytearray()
    while True:
        chunk = await response.content.read(min(16 * 1024, max_bytes - len(result) + 1))
        if not chunk:
            break
        result.extend(chunk)
        if len(result) > max_bytes:
            raise RingJsonError("response-too-large")
    if declared is not None and len(result) != declared:
        raise RingJsonError("invalid-response")
    return bytes(result)


def _check_status(response, *, write=False):
    status = response.status
    if (200 <= status < 300) if write else status == 200:
        return
    code = {401: "authentication-required", 403: "permission-denied", 429: "rate-limited"}.get(
        status, "redirect-not-allowed" if 300 <= status < 400 else "request-failed"
    )
    raise RingJsonError(
        code, status=status, retry_after=_retry_after(response.headers.get("Retry-After"))
    )


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
        raise RingJsonError("invalid-response") from None
