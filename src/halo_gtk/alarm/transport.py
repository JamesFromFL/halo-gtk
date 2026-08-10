"""Native aiohttp transport for Ring's undocumented Alarm CLAP socket."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp

from halo_gtk.alarm.models import AlarmMode
from halo_gtk.alarm.protocol import (
    MAX_FRAME_BYTES,
    AlarmProtocolError,
    ClapMessage,
    HubDisconnection,
    encode_clap_frame,
    encode_device_list_request,
    encode_mode_command,
    parse_clap_frame,
)
from halo_gtk.alarm.provider import AlarmLocationSeed, AlarmRequestError, AlarmRestGateway

_TICKET_ENDPOINT = "https://app.ring.com/api/v1/clap/tickets"
_MAX_TICKET_LENGTH = 16_384
_MAX_HOST_LENGTH = 512
_MAX_ASSETS = 128
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")

MessageCallback: TypeAlias = Callable[[ClapMessage], Awaitable[None] | None]
OpenCallback: TypeAlias = Callable[[tuple["AlarmTicketAsset", ...]], Awaitable[None] | None]


class AlarmTransportError(RuntimeError):
    """A sanitized, reconnectable transport failure."""

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class AlarmTransportClosedError(AlarmTransportError):
    """Raised when a command targets a closed or replaced connection."""


@dataclass(frozen=True, slots=True)
class AlarmTicketAsset:
    """Validated, non-secret asset metadata returned with a CLAP ticket."""

    asset_id: str
    kind: str
    status: str
    on_battery: bool | None = None


@dataclass(frozen=True, slots=True)
class _Ticket:
    url: str
    assets: tuple[AlarmTicketAsset, ...]


class RingAlarmTransport:
    """One persistent per-location transport with fresh tickets per connect."""

    def __init__(
        self,
        request: AlarmRestGateway,
        location: AlarmLocationSeed,
        *,
        session_factory: Callable[[], Any] | None = None,
        ticket_timeout: float = 20.0,
        connect_timeout: float = 20.0,
        send_timeout: float = 10.0,
        receive_poll_interval: float = 0.5,
        close_timeout: float = 2.0,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        max_invalid_frames: int = 3,
    ) -> None:
        if (
            ticket_timeout <= 0
            or connect_timeout <= 0
            or send_timeout <= 0
            or receive_poll_interval <= 0
            or close_timeout <= 0
        ):
            raise ValueError("transport timeouts must be positive")
        if max_frame_bytes <= 0 or max_invalid_frames <= 0:
            raise ValueError("transport bounds must be positive")
        self._request = request
        self.location = location
        self._session_factory = session_factory or self._new_session
        self._ticket_timeout = ticket_timeout
        self._connect_timeout = connect_timeout
        self._send_timeout = send_timeout
        self._receive_poll_interval = receive_poll_interval
        self._close_timeout = close_timeout
        self._max_frame_bytes = max_frame_bytes
        self._max_invalid_frames = max_invalid_frames
        self._session: Any | None = None
        self._websocket: Any | None = None
        self._connection_epoch: int | None = None
        self._assets: tuple[AlarmTicketAsset, ...] = ()
        self._sequence = 1
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @staticmethod
    def _new_session() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20),
            raise_for_status=True,
        )

    @property
    def assets(self) -> tuple[AlarmTicketAsset, ...]:
        return self._assets

    @property
    def connected_epoch(self) -> int | None:
        return self._connection_epoch

    @property
    def connected(self) -> bool:
        websocket = self._websocket
        return (
            not self._closed
            and websocket is not None
            and not bool(getattr(websocket, "closed", False))
        )

    async def connect_once(
        self,
        *,
        epoch: int,
        stop_event: Any,
        on_open: OpenCallback,
        on_message: MessageCallback,
    ) -> None:
        """Run one socket connection until it closes or requests reconnect."""

        if self._closed:
            raise AlarmTransportClosedError("transport-closed")
        if epoch < 1:
            raise ValueError("epoch must be positive")

        ticket = await self._request_ticket()
        if self._closed or _event_is_set(stop_event):
            return
        session = self._ensure_session()
        try:
            websocket = await asyncio.wait_for(
                session.ws_connect(
                    ticket.url,
                    autoping=True,
                    heartbeat=30,
                    max_msg_size=self._max_frame_bytes,
                ),
                timeout=self._connect_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AlarmTransportError("websocket-connect-failed") from None

        self._websocket = websocket
        self._connection_epoch = epoch
        self._assets = ticket.assets
        invalid_frames = 0
        try:
            await _invoke(on_open, ticket.assets)
            for asset in ticket.assets:
                await self.request_inventory(asset.asset_id, epoch=epoch)

            while not self._closed and not _event_is_set(stop_event):
                try:
                    incoming = await asyncio.wait_for(
                        websocket.receive(), timeout=self._receive_poll_interval
                    )
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise AlarmTransportError("websocket-receive-failed") from None

                frame = _frame_payload(incoming)
                if frame is None:
                    if _is_terminal_message(incoming):
                        raise AlarmTransportError("websocket-closed")
                    continue
                try:
                    message = parse_clap_frame(
                        frame,
                        max_frame_bytes=self._max_frame_bytes,
                    )
                except AlarmProtocolError:
                    invalid_frames += 1
                    if invalid_frames >= self._max_invalid_frames:
                        raise AlarmTransportError("invalid-frame-limit") from None
                    continue
                invalid_frames = 0
                if isinstance(message, HubDisconnection):
                    raise AlarmTransportError("hub-requested-reconnect")
                await _invoke(on_message, message)
        finally:
            if self._websocket is websocket:
                self._websocket = None
                self._connection_epoch = None
            await _close_websocket(websocket, timeout=self._close_timeout)

    async def request_inventory(self, asset_id: str, *, epoch: int) -> None:
        """Request a fresh complete device list on the current connection."""

        await self._send_encoded(
            lambda sequence: encode_device_list_request(asset_id, sequence),
            epoch=epoch,
        )

    async def send_mode(
        self,
        asset_id: str,
        panel_zid: str,
        mode: AlarmMode,
        *,
        epoch: int,
        bypass_ids: tuple[str, ...] = (),
    ) -> None:
        """Send one mode command once; callers own confirmation and retry policy."""

        await self._send_encoded(
            lambda sequence: encode_mode_command(
                asset_id,
                panel_zid,
                mode,
                sequence,
                bypass_ids,
            ),
            epoch=epoch,
        )

    async def close(self) -> None:
        """Close the socket and its independently owned aiohttp session."""

        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(self._close_owned_resources())
            self._close_task = task
        await asyncio.shield(task)

    async def _close_owned_resources(self) -> None:
        websocket, self._websocket = self._websocket, None
        self._connection_epoch = None
        await _close_websocket(websocket, timeout=self._close_timeout)
        session, self._session = self._session, None
        if session is not None and not bool(getattr(session, "closed", False)):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.close(), timeout=self._close_timeout)

    async def _request_ticket(self) -> _Ticket:
        query = urlencode(
            {
                "locationID": self.location.location_id,
                "enableExtendedEmergencyCellUsage": "true",
                "requestedTransport": "ws",
            }
        )
        url = f"{_TICKET_ENDPOINT}?{query}"
        try:
            payload = await self._request.request_json(
                url,
                timeout=self._ticket_timeout,
            )
        except asyncio.CancelledError:
            raise
        except AlarmRequestError as exc:
            code = (
                exc.code
                if exc.code in {"authentication-required", "rate-limited"}
                else "ticket-request-failed"
            )
            raise AlarmTransportError(
                code,
                retry_after=_bounded_retry_after(exc.retry_after),
            ) from None
        except Exception as exc:
            raise AlarmTransportError(
                "ticket-request-failed",
                retry_after=_bounded_retry_after(getattr(exc, "retry_after", None)),
            ) from None
        return _parse_ticket(payload)

    def _ensure_session(self) -> Any:
        session = self._session
        if session is None or bool(getattr(session, "closed", False)):
            session = self._session_factory()
            self._session = session
        return session

    async def _send_encoded(
        self,
        encode: Callable[[int], Mapping[str, Any]],
        *,
        epoch: int,
    ) -> None:
        async with self._send_lock:
            websocket = self._websocket
            if (
                self._closed
                or websocket is None
                or bool(getattr(websocket, "closed", False))
                or self._connection_epoch != epoch
            ):
                raise AlarmTransportClosedError("connection-unavailable")
            sequence = self._sequence
            self._sequence += 1
            frame = encode_clap_frame(encode(sequence))
            try:
                await asyncio.wait_for(
                    websocket.send_str(frame),
                    timeout=self._send_timeout,
                )
            except TimeoutError:
                await asyncio.shield(self._retire_websocket(websocket))
                raise AlarmTransportError("websocket-send-timeout") from None
            except asyncio.CancelledError:
                await asyncio.shield(self._retire_websocket(websocket))
                raise
            except Exception:
                await asyncio.shield(self._retire_websocket(websocket))
                raise AlarmTransportError("websocket-send-failed") from None

    async def _retire_websocket(self, websocket: Any) -> None:
        if self._websocket is websocket:
            self._websocket = None
            self._connection_epoch = None
        await _close_websocket(websocket, timeout=self._close_timeout)


def _parse_ticket(payload: object) -> _Ticket:
    if not isinstance(payload, Mapping):
        raise AlarmTransportError("invalid-ticket-response")
    host = _bounded_text(payload.get("host"), "invalid-ticket-host", _MAX_HOST_LENGTH)
    ticket = _bounded_text(payload.get("ticket"), "invalid-ticket-token", _MAX_TICKET_LENGTH)
    assets_value = payload.get("assets")
    if not isinstance(assets_value, list | tuple) or len(assets_value) > _MAX_ASSETS:
        raise AlarmTransportError("invalid-ticket-assets")

    assets: list[AlarmTicketAsset] = []
    seen: set[str] = set()
    for value in assets_value:
        if not isinstance(value, Mapping):
            raise AlarmTransportError("invalid-ticket-assets")
        asset_id = _bounded_text(value.get("uuid"), "invalid-ticket-assets", 512)
        kind = _bounded_text(value.get("kind"), "invalid-ticket-assets", 128)
        if not (kind.startswith("base_station") or kind.startswith("beams_bridge")):
            continue
        if asset_id in seen:
            raise AlarmTransportError("invalid-ticket-assets")
        seen.add(asset_id)
        raw_status = value.get("status")
        status = raw_status if raw_status in {"online", "offline"} else "unknown"
        on_battery = value.get("onBattery")
        assets.append(
            AlarmTicketAsset(
                asset_id=asset_id,
                kind=kind,
                status=status,
                on_battery=on_battery if isinstance(on_battery, bool) else None,
            )
        )
    if not assets:
        raise AlarmTransportError("no-supported-assets")
    return _Ticket(url=_build_wss_url(host, ticket), assets=tuple(assets))


def _build_wss_url(host: str, ticket: str) -> str:
    if not _HOST_RE.fullmatch(host) or len(host) > _MAX_HOST_LENGTH:
        raise AlarmTransportError("invalid-ticket-host")
    parsed = urlsplit(f"wss://{host}")
    try:
        port = parsed.port
    except ValueError:
        raise AlarmTransportError("invalid-ticket-host") from None
    if (
        parsed.scheme != "wss"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise AlarmTransportError("invalid-ticket-host")
    labels = parsed.hostname.split(".")
    if any(not label or label.startswith("-") or label.endswith("-") for label in labels):
        raise AlarmTransportError("invalid-ticket-host")
    query = urlencode({"authcode": ticket, "ack": "false"})
    return urlunsplit(("wss", parsed.netloc, "/ws", query, ""))


def _bounded_text(value: object, code: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise AlarmTransportError(code)
    return value


def _bounded_retry_after(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0:
        return None
    return min(float(value), 300.0)


def _event_is_set(event: Any) -> bool:
    checker = getattr(event, "is_set", None)
    return bool(checker()) if callable(checker) else False


async def _invoke(callback: Callable[..., Any], *args: Any) -> None:
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


def _frame_payload(message: Any) -> str | bytes | None:
    message_type = getattr(message, "type", None)
    type_name = getattr(message_type, "name", str(message_type)).upper()
    if type_name.endswith("TEXT") or message_type == aiohttp.WSMsgType.TEXT:
        data = getattr(message, "data", None)
        return data if isinstance(data, str) else None
    if type_name.endswith("BINARY") or message_type == aiohttp.WSMsgType.BINARY:
        data = getattr(message, "data", None)
        return bytes(data) if isinstance(data, bytes | bytearray | memoryview) else None
    return None


def _is_terminal_message(message: Any) -> bool:
    message_type = getattr(message, "type", None)
    return message_type in {
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSING,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.ERROR,
    } or getattr(message_type, "name", "").upper() in {
        "CLOSE",
        "CLOSING",
        "CLOSED",
        "ERROR",
    }


async def _close_websocket(websocket: Any | None, *, timeout: float) -> None:
    if websocket is None or bool(getattr(websocket, "closed", False)):
        return
    try:
        result = websocket.close()
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=timeout)
    except Exception:
        pass
