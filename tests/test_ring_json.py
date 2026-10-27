"""Shared transport admission, cancellation and authenticated write boundaries."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from ring_doorbell.const import OAuth

from halo_gtk import ring_json
from halo_gtk.alarm.rest import HISTORY_ENDPOINT, LOCATIONS_ENDPOINT, RingAlarmRestGateway
from halo_gtk.chime_rest import ChimeRestError, RingChimeRestGateway
from halo_gtk.ring_json import RingJsonError, RingJsonTransport
from tests.test_alarm_rest import Response, TokenResponse, fake_auth, real_auth


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_shared_recovery_priority_and_fifo_between_history_and_chime():
    entered, gates = [], {}

    async def request(url):
        entered.append(url)
        await gates.setdefault(url, asyncio.Event()).wait()
        return Response(b"[]" if url.startswith(HISTORY_ENDPOINT) else b"{}")

    auth, _, _ = fake_auth(request=request)
    core = RingJsonTransport(auth)
    alarm, chime = (
        RingAlarmRestGateway(auth, transport=core),
        RingChimeRestGateway(auth, transport=core),
    )
    tasks = []
    try:
        first = asyncio.create_task(alarm.request_history("one", offset=0))
        tasks.append(first)
        await until(lambda: len(entered) == 1)
        recovery = asyncio.create_task(alarm.request_json(LOCATIONS_ENDPOINT))
        tasks.append(recovery)
        await until(lambda: len(entered) == 2)
        history = asyncio.create_task(alarm.request_history("one", offset=50))
        tasks.append(history)
        await until(lambda: len(core._background) == 1)
        tone = asyncio.create_task(chime.request_test_tone(123, "ding"))
        tasks.append(tone)
        await until(lambda: len(core._background) == 2)
        next_recovery_url = "https://api.ring.com/clients_api/ring_devices"
        next_recovery = asyncio.create_task(alarm.request_json(next_recovery_url))
        tasks.append(next_recovery)
        await until(lambda: core.recovery_waiters == 1)
        gates[entered[0]].set()
        await until(lambda: len(entered) == 3)
        assert entered[-1] == next_recovery_url and core._active == 2
        gates[LOCATIONS_ENDPOINT].set()
        await until(lambda: len(entered) == 4)
        assert "offset=50" in entered[-1]
        assert core._active == 2 and core._background_active == 1
        gates[entered[-1]].set()
        await until(lambda: len(entered) == 5)
        assert entered[-1].endswith("/chimes/123/play_sound")
        assert core._active == 2 and core._background_active == 1
    finally:
        await alarm.close()
        await chime.close()
        await core.close()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert core._active == core._background_active == core.recovery_waiters == 0


async def test_background_pending_bound_cancellation_preserves_fifo_and_releases_slots():
    entered, release = [], asyncio.Event()

    async def request(url):
        entered.append(url)
        await release.wait()
        return Response()

    auth, _, _ = fake_auth(request=request)
    core = RingJsonTransport(auth, max_pending_background=2)
    tasks = [asyncio.create_task(core.request_json("https://api.ring.com/active"))]
    await until(lambda: len(entered) == 1)
    tasks += [asyncio.create_task(core.request_json(f"https://api.ring.com/{i}")) for i in range(2)]
    await until(lambda: len(core._background) == 2)
    with pytest.raises(RingJsonError, match="busy"):
        await core.request_json("https://api.ring.com/excess")
    tasks[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[1]
    release.set()
    await asyncio.gather(tasks[0], tasks[2])
    assert entered == ["https://api.ring.com/active", "https://api.ring.com/1"]
    assert not core._background and core._active == 0


async def test_feature_close_cancels_only_its_jobs_and_keeps_core_and_auth_usable():
    gate = asyncio.Event()
    first = Response(gate=gate)
    auth, calls, _ = fake_auth(first, Response(), Response())
    core = RingJsonTransport(auth)
    alarm, chime = (
        RingAlarmRestGateway(auth, transport=core),
        RingChimeRestGateway(auth, transport=core),
    )
    active = asyncio.create_task(chime.read_health(123))
    await until(lambda: len(calls) == 1)
    await chime.close()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert first.closed and not core._closed
    assert await alarm.request_json(LOCATIONS_ENDPOINT) == {}
    await alarm.close()
    assert not core._closed
    assert await core.request_json(LOCATIONS_ENDPOINT) == {}
    await core.close()
    with pytest.raises(RingJsonError, match="closed"):
        await core.request_json(LOCATIONS_ENDPOINT)


async def test_write_preparation_and_admission_follow_token_refresh_and_persistence():
    refresh_started, release = asyncio.Event(), asyncio.Event()
    persisted, calls, order = [], [], []

    class Session:
        async def request(self, method, url, **options):
            calls.append((method, str(url), options))
            if str(url) == OAuth.ENDPOINT:
                refresh_started.set()
                await release.wait()
                return TokenResponse()
            assert persisted
            order.append("request")
            return Response()

    auth = real_auth(Session(), persisted, expired=True)
    core = RingJsonTransport(auth)
    task = asyncio.create_task(
        core.request_write(
            LOCATIONS_ENDPOINT,
            method="PUT",
            json={},
            admission=lambda: order.append("admission"),
            on_admitted=lambda: order.append("possibly-sent"),
        )
    )
    await refresh_started.wait()
    assert not order and not persisted
    release.set()
    receipt = await task
    assert receipt.accepted and order == ["admission", "possibly-sent", "request"]
    assert len(persisted) == 1 and [item[0] for item in calls] == ["POST", "PUT"]


async def test_stale_guard_after_refresh_prevents_device_send_and_possibly_sent_callback():
    refresh_started, release = asyncio.Event(), asyncio.Event()
    calls, marked = [], []

    class Session:
        async def request(self, method, url, **options):
            calls.append(str(url))
            assert str(url) == OAuth.ENDPOINT
            refresh_started.set()
            await release.wait()
            return TokenResponse()

    auth = real_auth(Session(), [], expired=True)
    gateway = RingChimeRestGateway(auth)
    current = [True]

    def admission():
        if not current[0]:
            raise ChimeRestError("superseded")

    task = asyncio.create_task(
        gateway.set_legacy_volume(
            123, 2, admission=admission, on_admitted=lambda: marked.append(True)
        )
    )
    await refresh_started.wait()
    current[0] = False
    release.set()
    with pytest.raises(ChimeRestError, match="superseded") as error:
        await task
    assert not error.value.possibly_sent and not marked and calls == [OAuth.ENDPOINT]


async def test_persistence_failure_prevents_write_admission_and_send():
    calls = []

    class Session:
        async def request(self, method, url, **options):
            calls.append(str(url))
            return TokenResponse()

    auth = real_auth(Session(), [], expired=True, fail_persist=True)
    gateway = RingChimeRestGateway(auth)
    with pytest.raises(ChimeRestError, match="token-refresh-failed") as error:
        await gateway.request_test_tone(123, "ding", on_admitted=lambda: pytest.fail("admitted"))
    assert not error.value.possibly_sent and calls == [OAuth.ENDPOINT]


@pytest.mark.parametrize("action", ["timeout", "cancel"])
async def test_stalled_token_refresh_can_timeout_or_cancel_without_admitting_write(action):
    started = asyncio.Event()
    calls, marked = [], []

    class Session:
        async def request(self, method, url, **options):
            calls.append(str(url))
            assert str(url) == OAuth.ENDPOINT
            started.set()
            await asyncio.Event().wait()

    auth = real_auth(Session(), [], expired=True)
    gateway = RingChimeRestGateway(auth)
    task = asyncio.create_task(
        gateway.request_test_tone(
            123,
            "ding",
            timeout=0.01 if action == "timeout" else 10,
            on_admitted=lambda: marked.append(True),
        )
    )
    await started.wait()
    if action == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ChimeRestError, match="timeout") as error:
            await task
        assert not error.value.possibly_sent
    assert not marked and calls == [OAuth.ENDPOINT]
    assert gateway._transport._active == 0 and not gateway._requests


async def test_write_payload_is_frozen_before_waiting_and_timeout_before_admission_is_not_sent():
    release, calls = asyncio.Event(), []

    async def request(url):
        await release.wait()
        return Response()

    auth, fake_calls, _ = fake_auth(request=request)
    core = RingJsonTransport(auth)
    hold = asyncio.create_task(core.request_json(LOCATIONS_ENDPOINT))
    await until(lambda: fake_calls)
    payload = {"chime": {"settings": {"volume": 2}}}
    pending = asyncio.create_task(
        core.request_write(
            LOCATIONS_ENDPOINT,
            method="PUT",
            json=payload,
            on_admitted=lambda: calls.append("admitted"),
        )
    )
    await until(lambda: len(core._background) == 1)
    payload["chime"]["settings"]["volume"] = 11
    with pytest.raises(RingJsonError, match="timeout") as error:
        await core.request_write(
            LOCATIONS_ENDPOINT,
            method="PUT",
            json={},
            timeout=0.01,
            on_admitted=lambda: pytest.fail("expired write admitted"),
        )
    assert not error.value.possibly_sent
    release.set()
    await asyncio.gather(hold, pending)
    assert calls == ["admitted"]
    assert fake_calls[-1][2]["json"] == {"chime": {"settings": {"volume": 2}}}


async def test_legacy_history_keeps_explicit_thirty_second_whole_page_deadline(monkeypatch):
    deadlines = []
    original = asyncio.timeout_at

    def capture(deadline):
        deadlines.append(deadline - asyncio.get_running_loop().time())
        return original(deadline)

    monkeypatch.setattr(ring_json.asyncio, "timeout_at", capture)
    auth, _, _ = fake_auth(Response(b"[]"), Response())
    gateway = RingAlarmRestGateway(auth)
    await gateway.request_history("one")
    await gateway.request_json(LOCATIONS_ENDPOINT)
    assert 29 < deadlines[0] <= 30 and 9 < deadlines[1] <= 10


async def test_async_admission_callback_is_rejected_before_send():
    auth, calls, _ = fake_auth()

    async def invalid():
        pass

    with pytest.raises(RingJsonError, match="invalid-admission") as error:
        await RingJsonTransport(auth).request_write(
            LOCATIONS_ENDPOINT, method="PUT", json={}, admission=invalid
        )
    assert not error.value.possibly_sent and not calls


@pytest.mark.parametrize(
    "url",
    [
        "http://api.ring.com/x",
        "https://user@api.ring.com/x",
        "https://api.ring.com:444/x",
        "https://evil.invalid/x",
    ],
)
async def test_core_adds_origin_defense_before_token_preparation(url):
    auth, calls, _ = fake_auth()
    with pytest.raises(RingJsonError, match="endpoint-not-allowed"):
        await RingJsonTransport(auth).request_json(url)
    assert not calls


async def test_read_network_error_remains_sanitized():
    async def request(url):
        raise aiohttp.ClientConnectionError("secret upstream body/token")

    auth, _, _ = fake_auth(request=request)
    with pytest.raises(RingJsonError) as error:
        await RingJsonTransport(auth).request_json(LOCATIONS_ENDPOINT)
    assert str(error.value) == "network-unavailable" and error.value.__cause__ is None
