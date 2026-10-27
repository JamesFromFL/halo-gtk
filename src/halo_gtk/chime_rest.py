"""Fixed Chime routes and minimal JSON payloads; no arbitrary SDK writes."""

from __future__ import annotations

import asyncio

from halo_gtk.ring_json import RingJsonError, RingJsonTransport, RingWriteReceipt, _callback

API_ORIGIN = "https://api.ring.com"
INVENTORY_ENDPOINT = API_ORIGIN + "/clients_api/ring_devices"


class ChimeRestError(RingJsonError):
    """Chime request failure with sanitized HTTP/send metadata."""


class RingChimeRestGateway:
    """Feature-owned requests using a shared client-owned HTTP core."""

    def __init__(self, auth, *, transport=None, admission=None):
        if transport is not None and transport._auth is not auth:
            raise ValueError("Shared transport belongs to another Auth")
        self._transport = transport if transport is not None else RingJsonTransport(auth)
        self._owns_transport = transport is None
        self._closed = False
        self._requests = set()
        if admission is not None and not callable(admission):
            raise ValueError("Client admission must be a synchronous callback")
        self._admission = admission

    async def read_inventory(self, *, timeout=None) -> dict:
        return await self._read(INVENTORY_ENDPOINT, timeout=timeout)

    async def read_health(self, api_id, *, timeout=None) -> dict:
        return await self._read(_device_url(api_id) + "health", timeout=timeout)

    async def set_legacy_volume(
        self,
        api_id,
        value,
        *,
        timeout=None,
        admission=None,
        on_admitted=None,
    ) -> RingWriteReceipt:
        url = _device_url(api_id)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 11:
            raise ChimeRestError("invalid-request")
        return await self._write(
            url,
            "PUT",
            {"chime": {"settings": {"volume": value}}},
            timeout=timeout,
            admission=admission,
            on_admitted=on_admitted,
        )

    async def request_test_tone(
        self,
        api_id,
        kind,
        *,
        timeout=None,
        admission=None,
        on_admitted=None,
    ) -> RingWriteReceipt:
        url = _device_url(api_id) + "play_sound"
        if not isinstance(kind, str) or kind not in {"ding", "motion"}:
            raise ChimeRestError("invalid-request")
        return await self._write(
            url,
            "POST",
            {"kind": kind},
            timeout=timeout,
            admission=admission,
            on_admitted=on_admitted,
        )

    def _check_open(self):
        if self._closed:
            raise ChimeRestError("closed")
        if self._admission is not None:
            try:
                _callback(self._admission)
            except RingJsonError as exc:
                raise _error(exc) from None

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

    async def _write(self, url, method, payload, *, timeout, admission, on_admitted):
        self._check_open()
        current = asyncio.current_task()
        self._requests.add(current)

        def validate():
            self._check_open()
            if admission is not None:
                admission()

        try:
            return await self._transport.request_write(
                url,
                method=method,
                json=payload,
                timeout=timeout,
                admission=validate,
                on_admitted=on_admitted,
            )
        except RingJsonError as exc:
            if isinstance(exc, ChimeRestError):
                raise
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


def _device_url(api_id):
    if (
        isinstance(api_id, bool)
        or not isinstance(api_id, int)
        or not 1 <= api_id <= 9_223_372_036_854_775_807
    ):
        raise ChimeRestError("invalid-request")
    return API_ORIGIN + f"/clients_api/chimes/{api_id}/"


def _error(exc):
    return ChimeRestError(
        exc.code, status=exc.status, retry_after=exc.retry_after, possibly_sent=exc.possibly_sent
    )
