"""Fixed Light Group routes on Halo's bounded, authenticated JSON transport."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from yarl import URL

from halo_gtk.light_group_models import ON_DURATIONS, LightGroupSchemaError, validate_identifier
from halo_gtk.ring_json import RingJsonError, RingJsonTransport, RingWriteReceipt, _callback

API_ORIGIN = "https://api.ring.com"
LOCATIONS_ENDPOINT = API_ORIGIN + "/devices/v1/locations"
_GROUP_LIST = object()


class LightGroupsRestError(RingJsonError):
    """Sanitized feature error with HTTP/backoff/possibly-sent metadata."""


class RingLightGroupsRestGateway:
    """Feature-owned cancellation; the injected client-wide core stays open."""

    def __init__(self, auth, *, transport=None, admission=None):
        if transport is not None and transport._auth is not auth:
            raise ValueError("Shared transport belongs to another Auth")
        if admission is not None and not callable(admission):
            raise ValueError("Client admission must be a synchronous callback")
        self._transport = transport if transport is not None else RingJsonTransport(auth)
        self._owns_transport = transport is None
        self._admission = admission
        self._closed = False
        self._requests = set()

    async def read_locations(self, *, timeout=None) -> dict:
        return await self._read(LOCATIONS_ENDPOINT, timeout=timeout)

    async def read_groups(self, location_id, *, timeout=None) -> dict:
        return await self._read(_group_url(location_id), timeout=timeout)

    async def read_report(self, location_id, group_id, *, timeout=None) -> dict:
        return await self._read(_group_url(location_id, group_id), timeout=timeout)

    async def set_lights(
        self,
        location_id,
        group_id,
        on,
        *,
        duration_seconds=None,
        timeout=None,
        admission=None,
        on_admitted=None,
    ) -> RingWriteReceipt:
        url = _group_url(location_id, group_id)
        if (
            not isinstance(on, bool)
            or (on and (type(duration_seconds) is not int or duration_seconds not in ON_DURATIONS))
            or (not on and duration_seconds is not None)
        ):
            raise LightGroupsRestError("invalid-request")
        if (admission is not None and not callable(admission)) or (
            on_admitted is not None and not callable(on_admitted)
        ):
            raise LightGroupsRestError("invalid-request")
        payload = {"lights_on": {"enabled": on}}
        if on:
            payload["lights_on"]["duration_seconds"] = duration_seconds
        self._check_open()
        current = asyncio.current_task()
        self._requests.add(current)

        def validate():
            self._check_open()
            if admission is not None:
                _callback(admission)

        try:
            return await self._transport.request_write(
                url,
                method="POST",
                json=payload,
                timeout=timeout,
                admission=validate,
                on_admitted=on_admitted,
            )
        except RingJsonError as exc:
            if isinstance(exc, LightGroupsRestError):
                raise
            raise _error(exc) from None
        finally:
            self._requests.discard(current)

    def _check_open(self):
        if self._closed:
            raise LightGroupsRestError("closed")
        if self._admission is not None:
            try:
                _callback(self._admission)
            except RingJsonError as exc:
                raise _error(exc) from None
            except Exception:
                raise LightGroupsRestError("admission-failed") from None

    async def _read(self, url, *, timeout):
        self._check_open()
        current = asyncio.current_task()
        self._requests.add(current)
        try:
            return await self._transport.request_json(
                url, timeout=timeout, admission=self._check_open
            )
        except RingJsonError as exc:
            raise _error(exc) from None
        finally:
            self._requests.discard(current)

    async def close(self):
        self._closed = True
        tasks = self._requests - {asyncio.current_task()}
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_transport:
            await self._transport.close()


def _segment(value):
    try:
        return quote(validate_identifier(value), safe="", encoding="utf-8", errors="strict")
    except LightGroupSchemaError:
        raise LightGroupsRestError("invalid-request") from None


def _group_url(location_id, group_id=_GROUP_LIST):
    path = f"/groups/v1/locations/{_segment(location_id)}/groups"
    if group_id is not _GROUP_LIST:
        path += f"/{_segment(group_id)}/devices"
    url = API_ORIGIN + path
    parsed = URL(url, encoded=True)
    if parsed.origin() != URL(API_ORIGIN) or parsed.raw_path != path or str(parsed) != url:
        raise LightGroupsRestError("invalid-request")
    return url


def _error(exc):
    return LightGroupsRestError(
        exc.code, status=exc.status, retry_after=exc.retry_after, possibly_sent=exc.possibly_sent
    )
