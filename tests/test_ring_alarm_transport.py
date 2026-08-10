"""Fixture-only tests for the native Ring Alarm WebSocket transport."""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from halo_gtk.alarm.models import AlarmMode
from halo_gtk.alarm.protocol import DeviceInfoDocList
from halo_gtk.alarm.provider import AlarmLocationSeed, AlarmRequestError
from halo_gtk.alarm.transport import AlarmTransportError, RingAlarmTransport


def _ticket(*, token="short-lived-ticket", host="alarm.example.test", assets=None):
    return {
        "ticket": token,
        "host": host,
        "assets": assets
        or [
            {
                "uuid": "asset-1",
                "kind": "base_station_v1",
                "status": "online",
                "onBattery": False,
            }
        ],
    }


def _device_list_frame(asset_id="asset-1"):
    return json.dumps(
        {
            "channel": "message",
            "msg": {
                "msg": "DeviceInfoDocGetList",
                "src": asset_id,
                "body": [
                    {
                        "general": {"v2": {"zid": "panel-1", "name": "Panel"}},
                        "device": {"v1": {"zid": "panel-1", "deviceType": "security-panel"}},
                    }
                ],
            },
        }
    )


class _Gateway:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def request_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _WebSocket:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    async def receive(self):
        if self.frames:
            return self.frames.pop(0)
        return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)

    async def send_str(self, frame):
        self.sent.append(frame)

    async def close(self):
        self.closed = True


class _StallingModeWebSocket(_WebSocket):
    def __init__(self):
        super().__init__()
        self.inventory_sent = asyncio.Event()
        self.mode_send_started = asyncio.Event()
        self.receive_released = asyncio.Event()

    async def receive(self):
        await self.receive_released.wait()
        return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)

    async def send_str(self, frame):
        if not self.sent:
            self.sent.append(frame)
            self.inventory_sent.set()
            return
        self.mode_send_started.set()
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True
        self.receive_released.set()


class _FailingModeWebSocket(_StallingModeWebSocket):
    async def send_str(self, frame):
        if not self.sent:
            await super().send_str(frame)
            return
        self.mode_send_started.set()
        raise RuntimeError("raw-send-failure")


class _Session:
    def __init__(self, *websockets):
        self.websockets = list(websockets)
        self.urls = []
        self.closed = False

    async def ws_connect(self, url, **kwargs):
        self.urls.append((url, kwargs))
        return self.websockets.pop(0)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_transport_uses_authenticated_ticket_and_owned_wss_session():
    gateway = _Gateway(_ticket())
    websocket = _WebSocket(
        [SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=_device_list_frame())]
    )
    session = _Session(websocket)
    transport = RingAlarmTransport(
        gateway,
        AlarmLocationSeed("location/one"),
        session_factory=lambda: session,
        receive_poll_interval=0.01,
    )
    stop = asyncio.Event()
    received = []

    async def on_message(message):
        received.append(message)
        stop.set()

    await transport.connect_once(
        epoch=1,
        stop_event=stop,
        on_open=lambda _assets: None,
        on_message=on_message,
    )
    await transport.close()

    ticket_url, ticket_kwargs = gateway.calls[0]
    assert parse_qs(urlsplit(ticket_url).query) == {
        "locationID": ["location/one"],
        "enableExtendedEmergencyCellUsage": ["true"],
        "requestedTransport": ["ws"],
    }
    assert ticket_kwargs == {"timeout": 20.0}
    wss_url, connect_kwargs = session.urls[0]
    parsed = urlsplit(wss_url)
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "wss",
        "alarm.example.test",
        "/ws",
    )
    assert parse_qs(parsed.query) == {"authcode": ["short-lived-ticket"], "ack": ["false"]}
    assert connect_kwargs["max_msg_size"] == 1_048_576
    assert isinstance(received[0], DeviceInfoDocList)
    assert json.loads(websocket.sent[0])["msg"] == {
        "msg": "DeviceInfoDocGetList",
        "dst": "asset-1",
        "seq": 1,
    }
    assert websocket.closed is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_transport_gets_fresh_ticket_and_keeps_sequence_monotonic():
    gateway = _Gateway(_ticket(token="ticket-one"), _ticket(token="ticket-two"))
    first = _WebSocket()
    second = _WebSocket()
    session = _Session(first, second)
    transport = RingAlarmTransport(
        gateway,
        AlarmLocationSeed("location-1"),
        session_factory=lambda: session,
        receive_poll_interval=0.01,
    )

    async def connect(epoch, websocket):
        stop = asyncio.Event()

        async def opened(_assets):
            await transport.send_mode(
                "asset-1",
                "panel-1",
                AlarmMode.HOME,
                epoch=epoch,
            )
            stop.set()

        await transport.connect_once(
            epoch=epoch,
            stop_event=stop,
            on_open=opened,
            on_message=lambda _message: None,
        )
        return [json.loads(frame)["msg"]["seq"] for frame in websocket.sent]

    assert await connect(1, first) == [1, 2]
    assert await connect(2, second) == [3, 4]
    assert "ticket-one" in session.urls[0][0]
    assert "ticket-two" in session.urls[1][0]
    await transport.close()


@pytest.mark.asyncio
async def test_stalled_send_times_out_retires_socket_and_releases_send_lock():
    websocket = _StallingModeWebSocket()
    session = _Session(websocket)
    transport = RingAlarmTransport(
        _Gateway(_ticket()),
        AlarmLocationSeed("location-1"),
        session_factory=lambda: session,
        send_timeout=0.01,
        receive_poll_interval=0.01,
    )
    stop = asyncio.Event()
    connection = asyncio.create_task(
        transport.connect_once(
            epoch=1,
            stop_event=stop,
            on_open=lambda _assets: None,
            on_message=lambda _message: None,
        )
    )
    await websocket.inventory_sent.wait()

    with pytest.raises(AlarmTransportError) as raised:
        await asyncio.wait_for(
            transport.send_mode(
                "asset-1",
                "panel-1",
                AlarmMode.AWAY,
                epoch=1,
            ),
            timeout=0.2,
        )

    assert raised.value.code == "websocket-send-timeout"
    assert websocket.mode_send_started.is_set()
    assert websocket.closed is True
    assert transport.connected is False
    assert transport._send_lock.locked() is False
    with pytest.raises(AlarmTransportError, match="websocket-closed"):
        await connection
    await transport.close()


@pytest.mark.asyncio
async def test_send_exception_retires_socket_without_exposing_error_details():
    websocket = _FailingModeWebSocket()
    session = _Session(websocket)
    transport = RingAlarmTransport(
        _Gateway(_ticket()),
        AlarmLocationSeed("location-1"),
        session_factory=lambda: session,
        receive_poll_interval=0.01,
    )
    stop = asyncio.Event()
    connection = asyncio.create_task(
        transport.connect_once(
            epoch=1,
            stop_event=stop,
            on_open=lambda _assets: None,
            on_message=lambda _message: None,
        )
    )
    await websocket.inventory_sent.wait()

    with pytest.raises(AlarmTransportError) as raised:
        await transport.send_mode(
            "asset-1",
            "panel-1",
            AlarmMode.AWAY,
            epoch=1,
        )

    assert raised.value.code == "websocket-send-failed"
    assert "raw-send-failure" not in str(raised.value)
    assert websocket.closed is True
    assert transport.connected is False
    assert transport._send_lock.locked() is False
    with pytest.raises(AlarmTransportError, match="websocket-closed"):
        await connection
    await transport.close()


@pytest.mark.asyncio
async def test_cancelled_partial_send_retires_socket_before_propagating():
    websocket = _StallingModeWebSocket()
    session = _Session(websocket)
    transport = RingAlarmTransport(
        _Gateway(_ticket()),
        AlarmLocationSeed("location-1"),
        session_factory=lambda: session,
        send_timeout=1,
        receive_poll_interval=0.01,
    )
    stop = asyncio.Event()
    connection = asyncio.create_task(
        transport.connect_once(
            epoch=1,
            stop_event=stop,
            on_open=lambda _assets: None,
            on_message=lambda _message: None,
        )
    )
    await websocket.inventory_sent.wait()
    command = asyncio.create_task(
        transport.send_mode(
            "asset-1",
            "panel-1",
            AlarmMode.AWAY,
            epoch=1,
        )
    )
    await websocket.mode_send_started.wait()

    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command

    assert websocket.closed is True
    assert transport.connected is False
    assert transport._send_lock.locked() is False
    with pytest.raises(AlarmTransportError, match="websocket-closed"):
        await connection
    await transport.close()


@pytest.mark.asyncio
async def test_transport_rejects_untrusted_ticket_host_without_leaking_values():
    secret = "do-not-report-this-ticket"
    gateway = _Gateway(_ticket(token=secret, host="good.example/ws@evil.example"))
    transport = RingAlarmTransport(
        gateway,
        AlarmLocationSeed("location-1"),
        session_factory=lambda: pytest.fail("session must not be created"),
    )

    with pytest.raises(AlarmTransportError) as raised:
        await transport.connect_once(
            epoch=1,
            stop_event=asyncio.Event(),
            on_open=lambda _assets: None,
            on_message=lambda _message: None,
        )

    assert raised.value.code == "invalid-ticket-host"
    assert secret not in str(raised.value)
    assert "evil.example" not in str(raised.value)


@pytest.mark.asyncio
async def test_transport_propagates_bounded_retry_after_without_error_details():
    gateway = _Gateway(AlarmRequestError("rate-limited", retry_after=900))
    transport = RingAlarmTransport(gateway, AlarmLocationSeed("location-1"))

    with pytest.raises(AlarmTransportError) as raised:
        await transport.connect_once(
            epoch=1,
            stop_event=asyncio.Event(),
            on_open=lambda _assets: None,
            on_message=lambda _message: None,
        )

    assert raised.value.code == "rate-limited"
    assert raised.value.retry_after == 300


@pytest.mark.asyncio
async def test_transport_bounds_invalid_frames_and_never_reports_raw_data():
    raw_secret = "raw-household-payload"
    frames = [
        SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=raw_secret),
        SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=raw_secret),
    ]
    websocket = _WebSocket(frames)
    session = _Session(websocket)
    transport = RingAlarmTransport(
        _Gateway(_ticket()),
        AlarmLocationSeed("location-1"),
        session_factory=lambda: session,
        max_invalid_frames=2,
    )

    with pytest.raises(AlarmTransportError) as raised:
        await transport.connect_once(
            epoch=1,
            stop_event=asyncio.Event(),
            on_open=lambda _assets: None,
            on_message=lambda _message: None,
        )

    assert raised.value.code == "invalid-frame-limit"
    assert raw_secret not in str(raised.value)
    await transport.close()
