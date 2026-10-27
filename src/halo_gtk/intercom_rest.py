"""Fixed Intercom routes and one fixed door-release RPC; no arbitrary SDK writes."""

from __future__ import annotations

import asyncio

from halo_gtk.ring_json import RingJsonError, RingJsonTransport, _callback

API_ORIGIN = "https://api.ring.com"
INVENTORY_ENDPOINT = API_ORIGIN + "/clients_api/ring_devices"


class IntercomRestError(RingJsonError):
    """Intercom request failure with sanitized HTTP/send metadata."""


class RingIntercomRestGateway:
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

    async def unlock(self, api_id, request_id, *, timeout=None, admission=None, on_admitted=None):
        import uuid

        try:
            if (
                not isinstance(request_id, str)
                or str(uuid.UUID(request_id, version=4)) != request_id
            ):
                raise ValueError
        except (ValueError, AttributeError):
            raise IntercomRestError("invalid-request") from None
        return await self._write(
            _device_url(api_id),
            "PUT",
            {
                "command_name": "device_rpc",
                "request": {
                    "id": request_id,
                    "jsonrpc": "2.0",
                    "method": "unlock_door",
                    "params": {"door_id": 0, "user_id": -1},
                },
            },
            timeout=timeout,
            admission=admission,
            on_admitted=on_admitted,
        )

    def _check_open(self):
        if self._closed:
            raise IntercomRestError("closed")
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
                read_json=True,
            )
        except RingJsonError as exc:
            if isinstance(exc, IntercomRestError):
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
        raise IntercomRestError("invalid-request")
    return API_ORIGIN + f"/commands/v1/devices/{api_id}/device_rpc"


def _error(exc):
    return IntercomRestError(
        exc.code, status=exc.status, retry_after=exc.retry_after, possibly_sent=exc.possibly_sent
    )
