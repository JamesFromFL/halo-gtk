"""Real bounded HTTP core with fake sessions; never release a real entrance."""

import asyncio
import json
import uuid

import pytest

from halo_gtk.intercom_rest import IntercomRestError, RingIntercomRestGateway
from halo_gtk.ring_json import RingJsonTransport
from tests.test_alarm_rest import Response, fake_auth

REQUEST_ID = "6cd14ac3-abd2-44e8-8f6b-67d2d6e958b1"


@pytest.mark.asyncio
async def test_exact_opener_route_payload_auth_tls_and_parsed_rpc_reply():
    auth, calls, _ = fake_auth(Response(b'{"result":{"code":0}}'))
    gateway = RingIntercomRestGateway(auth)
    admitted = []
    receipt = await gateway.unlock(42, REQUEST_ID, on_admitted=lambda: admitted.append(True))
    assert receipt.response_json == {"result": {"code": 0}} and admitted == [True]
    method, url, options = calls[0]
    assert method == "PUT" and url == "https://api.ring.com/commands/v1/devices/42/device_rpc"
    assert options["json"] == {
        "command_name": "device_rpc",
        "request": {
            "id": REQUEST_ID,
            "jsonrpc": "2.0",
            "method": "unlock_door",
            "params": {"door_id": 0, "user_id": -1},
        },
    }
    assert not options["allow_redirects"] and not options["auto_decompress"]
    assert options["ssl"].check_hostname
    assert options["headers"]["hardware_id"] == auth.get_hardware_id()
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_id", [True, 0, -1, "42", "42/elsewhere", 2**63, None])
async def test_invalid_id_never_reaches_session(api_id):
    auth, calls, _ = fake_auth()
    with pytest.raises(IntercomRestError, match="invalid-request"):
        await RingIntercomRestGateway(auth).unlock(api_id, REQUEST_ID)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [None, "garbage", "", REQUEST_ID.upper(), str(uuid.uuid1())])
async def test_request_identifier_is_a_canonical_fresh_v4_shape(request_id):
    auth, calls, _ = fake_auth()
    with pytest.raises(IntercomRestError):
        await RingIntercomRestGateway(auth).unlock(42, request_id)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b"", b"not json", b"[]", b'{"result":{"code":NaN}}', b"x" * 65537]
)
async def test_accepted_bad_or_oversized_reply_is_not_rpc_success(body):
    response = Response(body)
    auth, calls, _ = fake_auth(response)
    receipt = await RingIntercomRestGateway(auth).unlock(42, REQUEST_ID)
    assert receipt.accepted and not receipt.body_complete and receipt.response_json is None
    assert len(calls) == 1 and response.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500, 503])
async def test_http_error_never_replays_put(status):
    auth, calls, _ = fake_auth(Response(status=status))
    with pytest.raises(IntercomRestError) as caught:
        await RingIntercomRestGateway(auth).unlock(42, REQUEST_ID)
    assert caught.value.possibly_sent and len(calls) == 1
    auth.async_refresh_tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_is_get_only_and_feature_close_keeps_shared_core():
    auth, calls, _ = fake_auth(Response(b'{"other":[]}'))
    core = RingJsonTransport(auth)
    gateway = RingIntercomRestGateway(auth, transport=core)
    assert await gateway.read_inventory() == {"other": []}
    assert calls[0][:2] == ("GET", "https://api.ring.com/clients_api/ring_devices")
    await gateway.close()
    assert not core._closed
    await core.close()


@pytest.mark.asyncio
async def test_retirement_guard_after_slot_wait_prevents_opener_http():
    gate, current = asyncio.Event(), [True]
    auth, calls, _ = fake_auth(Response(gate=gate))
    core = RingJsonTransport(auth)
    busy = asyncio.create_task(core.request_json("https://api.ring.com/occupied"))
    while not calls:
        await asyncio.sleep(0)

    def admission():
        if not current[0]:
            raise IntercomRestError("session-changed")

    gateway = RingIntercomRestGateway(auth, transport=core, admission=admission)
    task = asyncio.create_task(gateway.unlock(42, REQUEST_ID))
    while not core._background:
        await asyncio.sleep(0)
    current[0] = False
    gate.set()
    await busy
    with pytest.raises(IntercomRestError, match="session-changed"):
        await task
    assert len(calls) == 1
    await gateway.close()
    await core.close()


def test_upstream_inventory_shape_parses_without_account_access():
    from halo_gtk.intercom import parse_intercom_inventory
    from tests.test_intercom import inventory

    (device,) = parse_intercom_inventory(json.loads(json.dumps(inventory())))
    assert device.kind == "intercom_handset_audio" and device.battery == 52
