"""Allowlisted Alarm GETs over the shared bounded authenticated transport."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from halo_gtk.alarm.provider import AlarmRequestError
from halo_gtk.ring_json import (
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_RESPONSE_BYTES,
    RingJsonError,
    RingJsonTransport,
)
from halo_gtk.ring_json import (
    _retry_after as _retry_after,
)

APP_API_ORIGIN = "https://prd-api-us.prd.rings.solutions"
TICKET_ENDPOINT = APP_API_ORIGIN + "/api/v1/clap/tickets"
HISTORY_ENDPOINT = APP_API_ORIGIN + "/api/v1/rs/history"
LOCATIONS_ENDPOINT = "https://api.ring.com/devices/v1/locations"
DEVICES_ENDPOINT = "https://api.ring.com/clients_api/ring_devices"


class AlarmRestError(AlarmRequestError):
    """Preserve the Alarm feature's existing sanitized error contract."""

    def __init__(self, code, *, status=None, retry_after=None):
        super().__init__(code, retry_after=retry_after)
        self.status = status


@dataclass
class _HistoryRequest:
    task: asyncio.Task
    users: int = 0


class RingAlarmRestGateway:
    """GET-only route policy and feature-owned history coalescing/lifetime."""

    def __init__(
        self,
        auth,
        *,
        transport=None,
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
        if transport is not None and transport._auth is not auth:
            raise ValueError("Shared transport belongs to another Auth")
        self._auth = auth
        self._transport = (
            transport
            if transport is not None
            else RingJsonTransport(auth, max_pending_recovery=min(8, max_pending_recovery))
        )
        self._owns_transport = transport is None
        self._request_timeout = min(10.0, request_timeout)
        self._history_timeout = min(30.0, history_timeout)
        self._max_bytes = min(MAX_RESPONSE_BYTES, max_response_bytes)
        self._max_depth = min(MAX_JSON_DEPTH, max_json_depth)
        self._max_nodes = min(MAX_JSON_NODES, max_json_nodes)
        self._history = {}
        self._requests = set()
        self._closed = False

    @property
    def _active(self):
        return self._transport._active

    @property
    def _history_active(self):
        return bool(self._transport._background_active)

    @property
    def _recovery_waiters(self):
        return self._transport.recovery_waiters

    async def request_json(
        self, url: str, *, method="GET", json: Mapping[str, Any] | None = None, timeout=None
    ) -> Mapping[str, Any]:
        if method != "GET" or json is not None:
            raise AlarmRestError("operation-not-allowed")
        url = _mapping_url(url)
        return await self._request(
            url, history=False, duration=_duration(timeout, self._request_timeout)
        )

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
        """Retire this feature without closing another feature's core/Auth."""
        self._closed = True
        tasks = (self._requests | {entry.task for entry in self._history.values()}) - {
            asyncio.current_task()
        }
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_transport:
            await self._transport.close()

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

    async def _request(self, url, *, history, duration):
        self._check_auth()
        current = asyncio.current_task()
        self._requests.add(current)
        try:
            return await self._transport.request_json(
                url,
                timeout=duration,
                recovery=not history,
                expected_type=list if history else dict,
                max_response_bytes=self._max_bytes,
                max_json_depth=self._max_depth,
                max_json_nodes=self._max_nodes,
                admission=self._check_auth,
                timeout_limit=30 if history else 10,
            )
        except RingJsonError as exc:
            raise AlarmRestError(exc.code, status=exc.status, retry_after=exc.retry_after) from None
        finally:
            self._requests.discard(current)


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
