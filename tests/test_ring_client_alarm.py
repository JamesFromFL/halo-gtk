"""Lifecycle tests for Ring Alarm integration owned by RingClient."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from types import SimpleNamespace

import pytest
from ring_doorbell import Auth, AuthenticationError
from ring_doorbell.const import OAuth

from halo_gtk import ring_client
from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmLocationSnapshot,
    AlarmMode,
    AlarmServiceStatus,
    empty_alarm_snapshot,
)
from halo_gtk.alarm.provider import AlarmRequestError
from halo_gtk.ring_client import (
    RingClient,
    _install_serialized_auth_refresh,
    _RingAlarmRestGateway,
)


@pytest.mark.asyncio
async def test_serialized_auth_refresh_collapses_waiters_on_rotated_token():
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    class FakeAuth:
        def __init__(self):
            self._token = {"refresh_token": "old"}
            self.refresh_calls = 0

        async def async_refresh_tokens(self):
            self.refresh_calls += 1
            refresh_started.set()
            await release_refresh.wait()
            self._token = {"refresh_token": "new"}
            return self._token

    auth = FakeAuth()
    _install_serialized_auth_refresh(auth)

    first = asyncio.create_task(auth.async_refresh_tokens())
    await refresh_started.wait()
    second = asyncio.create_task(auth.async_refresh_tokens())
    await asyncio.sleep(0)
    release_refresh.set()

    first_result, second_result = await asyncio.gather(first, second)

    assert auth.refresh_calls == 1
    assert first_result == {"refresh_token": "new"}
    assert second_result == {"refresh_token": "new"}


@pytest.mark.asyncio
async def test_serialized_auth_refresh_does_not_double_wrap_auth():
    class FakeAuth:
        def __init__(self):
            self._token = {"refresh_token": "one"}
            self.refresh_calls = 0

        async def async_refresh_tokens(self):
            self.refresh_calls += 1
            self._token = {"refresh_token": str(self.refresh_calls + 1)}
            return self._token

    auth = FakeAuth()
    _install_serialized_auth_refresh(auth)
    wrapped = auth.async_refresh_tokens
    _install_serialized_auth_refresh(auth)

    assert auth.async_refresh_tokens is wrapped
    assert await auth.async_refresh_tokens() == {"refresh_token": "2"}
    assert auth.refresh_calls == 1


@pytest.mark.asyncio
async def test_serialized_auth_refresh_allows_waiter_to_retry_failure():
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    class FakeAuth:
        def __init__(self):
            self.refresh_calls = 0

        async def async_refresh_tokens(self):
            self.refresh_calls += 1
            if self.refresh_calls == 1:
                refresh_started.set()
                await release_refresh.wait()
                raise AuthenticationError("rejected")
            return {"refresh_token": "recovered"}

    auth = FakeAuth()
    _install_serialized_auth_refresh(auth)
    first = asyncio.create_task(auth.async_refresh_tokens())
    await refresh_started.wait()
    second = asyncio.create_task(auth.async_refresh_tokens())
    release_refresh.set()

    results = await asyncio.gather(first, second, return_exceptions=True)

    assert isinstance(results[0], AuthenticationError)
    assert results[1] == {"refresh_token": "recovered"}
    assert auth.refresh_calls == 2


@pytest.mark.asyncio
async def test_installed_auth_deduplicates_staggered_401_refresh_and_persists_latest_token():
    late_request_started = asyncio.Event()
    refresh_response_ready = asyncio.Event()
    persisted = []

    class Response:
        def __init__(self, status, body=b"{}"):
            self.status = status
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self):
            return self._body.decode()

        async def read(self):
            return self._body

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self):
            self.refresh_calls = 0
            self.protected_headers = []

        async def request(self, method, url, **kwargs):
            if url == OAuth.ENDPOINT:
                self.refresh_calls += 1
                refresh_response_ready.set()
                suffix = "b" if self.refresh_calls == 1 else "c"
                body = (
                    f'{{"access_token":"access-{suffix}",'
                    f'"refresh_token":"refresh-{suffix}",'
                    '"token_type":"Bearer","expires_in":3600}'
                )
                return Response(200, body.encode())
            self.protected_headers.append(dict(kwargs.get("headers", {})))
            if url.endswith("/late"):
                late_request_started.set()
                await refresh_response_ready.wait()
            return Response(401)

    session = FakeSession()
    auth = Auth(
        "android:com.ringapp",
        {
            "access_token": "access-a",
            "refresh_token": "refresh-a",
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": 4_000_000_000,
        },
        lambda token: persisted.append(dict(token)),
        http_client_session=session,
    )
    _install_serialized_auth_refresh(auth)

    late = asyncio.create_task(
        auth.async_query("https://example.invalid/late", raise_for_status=False)
    )
    await late_request_started.wait()
    first = await auth.async_query(
        "https://example.invalid/first",
        raise_for_status=False,
    )
    late_result = await late

    assert first.status_code == 401
    assert late_result.status_code == 401
    assert session.refresh_calls == 1
    assert len(persisted) == 1
    assert persisted[0]["access_token"] == "access-b"
    assert persisted[0]["refresh_token"] == "refresh-b"
    assert auth._token["refresh_token"] == "refresh-b"

    await auth.async_query("https://example.invalid/new-token", raise_for_status=False)

    assert session.refresh_calls == 2
    assert len(persisted) == 2
    assert persisted[-1]["access_token"] == "access-c"
    assert persisted[-1]["refresh_token"] == "refresh-c"
    assert auth._token["refresh_token"] == "refresh-c"


@pytest.mark.asyncio
async def test_alarm_gateway_uses_active_auth_and_returns_detached_mapping():
    calls = []
    payload = {"ticket": "redacted"}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return payload

    class FakeAuth:
        async def async_query(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    auth = FakeAuth()
    gateway = _RingAlarmRestGateway(auth)

    result = await gateway.request_json(
        "https://example.invalid/alarm",
        method="POST",
        json={"request": "inventory"},
        timeout=3,
    )

    assert result == payload
    assert result is not payload
    assert calls == [
        (
            "https://example.invalid/alarm",
            {
                "method": "POST",
                "json": {"request": "inventory"},
                "timeout": 3,
                "raise_for_status": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_alarm_gateway_replays_one_get_after_ring_auth_refreshes_401():
    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    responses = [Response(401, {}), Response(200, {"ticket": "fresh"})]

    class FakeAuth:
        @staticmethod
        async def async_query(url, **kwargs):
            calls.append((url, kwargs))
            return responses.pop(0)

    result = await _RingAlarmRestGateway(FakeAuth()).request_json("https://example.invalid/alarm")

    assert result == {"ticket": "fresh"}
    assert len(calls) == 2
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_alarm_gateway_never_replays_non_get_after_401():
    calls = []

    class Response:
        status_code = 401

        @staticmethod
        def json():
            return {}

    class FakeAuth:
        @staticmethod
        async def async_query(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    with pytest.raises(AlarmRequestError) as exc_info:
        await _RingAlarmRestGateway(FakeAuth()).request_json(
            "https://example.invalid/alarm",
            method="POST",
            json={"command": "write"},
        )

    assert exc_info.value.code == "authentication-required"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_alarm_location_discovery_keeps_only_bounded_names():
    calls = []
    payload = {
        "user_locations": [
            {
                "location_id": "home",
                "name": "Primary Home",
                "address": "must not be retained",
                "latitude": 12.34,
                "longitude": 56.78,
            },
            {
                "location_id": "work",
                "name": "x" * 257,
                "address": "also private",
            },
        ]
    }

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return payload

    class FakeAuth:
        @staticmethod
        async def async_query(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    client = RingClient()
    client._ring = SimpleNamespace(
        auth=FakeAuth(),
        devices_data={
            "base_stations": {
                "one": {
                    "location_id": "home",
                    "location_name": "Fallback Home",
                    "asset_id": "asset-home",
                    "description": "not authoritative",
                },
                "two": {
                    "location_id": "work",
                    "location_name": "Fallback Work",
                    "asset_id": "asset-work",
                },
            }
        },
    )

    seeds = await client._async_alarm_location_seeds()

    assert [(seed.location_id, seed.name) for seed in seeds] == [
        ("home", "Primary Home"),
        ("work", "Fallback Work"),
    ]
    assert "must not be retained" not in repr(seeds)
    assert "also private" not in repr(seeds)
    assert calls[0][0] == ring_client._RING_LOCATIONS_ENDPOINT


@pytest.mark.asyncio
async def test_alarm_location_discovery_falls_back_without_logging_response_details(caplog):
    class FakeAuth:
        @staticmethod
        async def async_query(*_args, **_kwargs):
            raise RuntimeError("private response detail")

    client = RingClient()
    client._ring = SimpleNamespace(
        auth=FakeAuth(),
        devices_data={
            "base_stations": [
                {
                    "location_id": "home",
                    "location_name": "Fallback Home",
                    "asset_id": "asset-home",
                }
            ]
        },
    )

    seeds = await client._async_alarm_location_seeds()

    assert [(seed.location_id, seed.name) for seed in seeds] == [("home", "Fallback Home")]
    assert "private response detail" not in caplog.text


@pytest.mark.asyncio
async def test_alarm_gateway_rejects_non_object_json():
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return []

    class FakeAuth:
        @staticmethod
        async def async_query(*_args, **_kwargs):
            return Response()

    with pytest.raises(AlarmRequestError) as exc_info:
        await _RingAlarmRestGateway(FakeAuth()).request_json("https://example.invalid/alarm")
    assert exc_info.value.code == "invalid-response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_retry_after"),
    [
        (401, "authentication-required", None),
        (429, "rate-limited", 60.0),
        (503, "request-failed", None),
    ],
)
async def test_alarm_gateway_classifies_http_failures(
    status_code,
    expected_code,
    expected_retry_after,
):
    class Response:
        @staticmethod
        def json():
            return {}

    Response.status_code = status_code

    class FakeAuth:
        @staticmethod
        async def async_query(*_args, **_kwargs):
            return Response()

    with pytest.raises(AlarmRequestError) as exc_info:
        await _RingAlarmRestGateway(FakeAuth()).request_json("https://example.invalid/alarm")
    assert exc_info.value.code == expected_code
    assert exc_info.value.retry_after == expected_retry_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (AuthenticationError("secret detail"), "authentication-required"),
        (RuntimeError("secret detail"), "request-failed"),
    ],
)
async def test_alarm_gateway_sanitizes_query_failures(error, expected_code):
    class FakeAuth:
        @staticmethod
        async def async_query(*_args, **_kwargs):
            raise error

    with pytest.raises(AlarmRequestError) as exc_info:
        await _RingAlarmRestGateway(FakeAuth()).request_json("https://example.invalid/alarm")

    assert exc_info.value.code == expected_code
    assert str(exc_info.value) == expected_code
    assert "secret detail" not in str(exc_info.value)


class _PendingFuture(concurrent.futures.Future):
    pass


def test_signal_alarm_stop_tolerates_loop_closing_race():
    class ClosingLoop:
        @staticmethod
        def is_closed():
            return False

        @staticmethod
        def is_running():
            return True

        @staticmethod
        def call_soon_threadsafe(*_args):
            raise RuntimeError("loop closed")

    client = RingClient()
    client._loop = ClosingLoop()
    client._alarm_stop_event = SimpleNamespace(set=lambda: None)

    client._signal_alarm_stop()


def test_done_alarm_future_error_is_consumed_without_logging_exception_text(caplog):
    class RecordingFuture(concurrent.futures.Future):
        result_calls = 0

        def result(self, timeout=None):
            self.result_calls += 1
            return super().result(timeout)

    future = RecordingFuture()
    future.set_exception(RuntimeError("private transport detail"))
    caplog.set_level(logging.DEBUG, logger="halo_gtk.ring_client")

    RingClient._wait_for_background_future(future, 1, "Ring Alarm")

    assert future.result_calls == 1
    assert "RuntimeError" in caplog.text
    assert "private transport detail" not in caplog.text


def test_alarm_start_is_independent_when_fcm_is_already_running(monkeypatch):
    client = RingClient()
    client._ring = object()
    client._published_generation = 11
    client._listener_future = _PendingFuture()
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 11)
    submitted = []

    def submit(coroutine):
        submitted.append(coroutine.cr_code.co_name)
        coroutine.close()
        return _PendingFuture()

    monkeypatch.setattr(client, "submit", submit)

    client.start()

    assert submitted == ["_async_run_alarm"]
    assert client._alarm_future is not None


def test_repeated_start_does_not_duplicate_background_services(monkeypatch):
    client = RingClient()
    client._ring = object()
    client._published_generation = 12
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 12)
    submitted = []

    def submit(coroutine):
        submitted.append(coroutine.cr_code.co_name)
        coroutine.close()
        return _PendingFuture()

    monkeypatch.setattr(client, "submit", submit)

    client.start()
    client.start()

    assert submitted == ["_async_listen", "_async_run_alarm"]


def test_unpublished_client_does_not_start_alarm(monkeypatch):
    client = RingClient()
    client._ring = object()
    submitted = []

    def submit(coroutine):
        submitted.append(coroutine.cr_code.co_name)
        coroutine.close()
        return _PendingFuture()

    monkeypatch.setattr(client, "submit", submit)
    monkeypatch.setattr(ring_client, "_client", None)

    client.start()

    assert submitted == ["_async_listen"]
    assert client._alarm_future is None


@pytest.mark.asyncio
async def test_alarm_runner_failure_does_not_escape_into_camera_client(monkeypatch):
    from gi.repository import GLib

    client = RingClient()
    client._ring = object()
    client._published_generation = 13
    client._alarm_snapshot = AlarmAccountSnapshot(
        generation=13,
        locations=(AlarmLocationSnapshot(location_id="home", can_set_mode=True),),
    )
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 13)
    monkeypatch.setattr(GLib, "idle_add", lambda *_args: 1)
    monkeypatch.setattr(
        client,
        "_create_alarm_service",
        lambda _generation, _locations: (_ for _ in ()).throw(
            RuntimeError("transport unavailable")
        ),
    )

    await client._async_run_alarm(13)

    assert client._ring is not None
    assert client._alarm_service is None
    assert client._alarm_stop_event is None
    assert client.get_alarm_snapshot().generation == 13
    assert client.get_alarm_snapshot().status is AlarmServiceStatus.ERROR
    assert client.get_alarm_snapshot().error_code == "service-failed"
    failed_location = client.get_alarm_snapshot().find_location("home")
    assert failed_location is not None
    assert failed_location.can_set_mode is False
    assert failed_location.command_unavailable_reason == "service-failed"


@pytest.mark.asyncio
async def test_alarm_runner_creates_service_with_enriched_locations(monkeypatch):
    enriched = (ring_client.AlarmLocationSeed("home", "Primary Home"),)
    created = []

    class FakeService:
        @staticmethod
        async def run(_stop_event):
            return None

        @staticmethod
        async def close():
            return None

    client = RingClient()
    client._ring = object()
    client._published_generation = 16
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 16)

    async def location_seeds():
        return enriched

    def create_service(generation, locations):
        created.append((generation, locations))
        return FakeService()

    monkeypatch.setattr(client, "_async_alarm_location_seeds", location_seeds)
    monkeypatch.setattr(client, "_create_alarm_service", create_service)

    await client._async_run_alarm(16)

    assert created == [(16, enriched)]


@pytest.mark.asyncio
async def test_alarm_runner_drops_queued_work_after_session_is_superseded(monkeypatch):
    client = RingClient()
    client._ring = object()
    client._published_generation = 14
    monkeypatch.setattr(ring_client, "_client", RingClient())
    monkeypatch.setattr(ring_client, "_active_client_generation", 15)
    created = []
    monkeypatch.setattr(client, "_create_alarm_service", created.append)

    await client._async_run_alarm(14)

    assert created == []
    assert client._alarm_service is None


def test_alarm_snapshot_is_cached_then_generation_guarded_for_glib(monkeypatch):
    from gi.repository import GLib

    client = RingClient()
    client._published_generation = 51
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 51)
    scheduled = []
    monkeypatch.setattr(
        GLib,
        "idle_add",
        lambda callback, *args: scheduled.append((callback, args)) or 1,
    )
    received = []
    client.add_alarm_callback(received.append)
    snapshot = empty_alarm_snapshot(generation=51)

    client._on_alarm_snapshot(snapshot)

    assert client.get_alarm_snapshot() is snapshot
    assert len(scheduled) == 1

    replacement = RingClient()
    replacement._published_generation = 52
    ring_client._client = replacement
    ring_client._active_client_generation = 52
    callback, args = scheduled.pop()

    assert callback(*args) is False
    assert received == []


def test_alarm_snapshot_dispatches_callbacks_only_for_active_client(monkeypatch):
    client = RingClient()
    client._published_generation = 61
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 61)
    received = []
    client.add_alarm_callback(received.append)
    client.add_alarm_callback(received.append)
    snapshot = empty_alarm_snapshot(generation=61)

    assert client._dispatch_alarm_snapshot(snapshot, 61) is False
    client.remove_alarm_callback(received.append)

    assert received == [snapshot]
    assert client._alarm_callbacks == []


def test_alarm_snapshot_callback_error_log_does_not_include_exception_text(
    monkeypatch,
    caplog,
):
    client = RingClient()
    client._published_generation = 62
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 62)

    def fail(_snapshot):
        raise RuntimeError("private household detail")

    client.add_alarm_callback(fail)
    caplog.set_level(logging.DEBUG, logger="halo_gtk.ring_client")

    client._dispatch_alarm_snapshot(empty_alarm_snapshot(62), 62)

    assert "RuntimeError" in caplog.text
    assert "private household detail" not in caplog.text


def test_alarm_snapshot_from_wrong_generation_is_not_cached_or_scheduled(monkeypatch):
    from gi.repository import GLib

    client = RingClient()
    client._published_generation = 62
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 62)
    scheduled = []
    monkeypatch.setattr(GLib, "idle_add", lambda *_args: scheduled.append(True))
    original = client.get_alarm_snapshot()

    client._on_alarm_snapshot(empty_alarm_snapshot(generation=61))

    assert client.get_alarm_snapshot() is original
    assert scheduled == []


def test_publishing_client_initializes_stopped_alarm_snapshot_generation(monkeypatch):
    client = RingClient()
    client._authenticated = True
    client._ring = SimpleNamespace(auth=SimpleNamespace(token_updater=None))
    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_session_generation", 63)

    published, _previous = ring_client._publish_session_candidate(client, 63)

    assert published is True
    snapshot = client.get_alarm_snapshot()
    assert snapshot.generation == 63
    assert snapshot.status is AlarmServiceStatus.STOPPED


def test_publishing_replacement_clears_previous_clients_alarm_snapshot(monkeypatch):
    previous = RingClient()
    previous._published_generation = 63
    previous._alarm_snapshot = AlarmAccountSnapshot(
        generation=63,
        status=AlarmServiceStatus.ONLINE,
        locations=(AlarmLocationSnapshot("private-home"),),
    )
    previous.add_alarm_callback(lambda _snapshot: None)
    replacement = RingClient()
    replacement._authenticated = True
    replacement._ring = SimpleNamespace(auth=SimpleNamespace(token_updater=None))
    monkeypatch.setattr(ring_client, "_client", previous)
    monkeypatch.setattr(ring_client, "_session_generation", 64)
    monkeypatch.setattr(ring_client, "_active_client_generation", 63)

    published, returned_previous = ring_client._publish_session_candidate(replacement, 64)

    assert published is True
    assert returned_previous is previous
    assert previous.get_alarm_snapshot() == empty_alarm_snapshot(64)
    assert previous._alarm_callbacks == []
    assert replacement.get_alarm_snapshot() == empty_alarm_snapshot(64)


def test_logout_clears_retired_clients_alarm_snapshot(monkeypatch):
    client = RingClient()
    client._published_generation = 64
    client._alarm_snapshot = AlarmAccountSnapshot(
        generation=64,
        status=AlarmServiceStatus.ONLINE,
        locations=(AlarmLocationSnapshot("private-home"),),
    )
    client.add_alarm_callback(lambda _snapshot: None)
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_session_generation", 64)
    monkeypatch.setattr(ring_client, "_active_client_generation", 64)
    monkeypatch.setattr(ring_client, "_clear_token", lambda: None)
    monkeypatch.setattr(ring_client, "clear_ring_account_email", lambda: None)
    monkeypatch.setattr(ring_client, "clear_ring_event_credentials", lambda: None)
    monkeypatch.setattr(ring_client, "_stop_client_quietly", lambda _client: None)

    assert ring_client.logout_client(expected_client=client) is True

    snapshot = client.get_alarm_snapshot()
    assert snapshot.generation == 65
    assert snapshot.status is AlarmServiceStatus.STOPPED
    assert snapshot.locations == ()
    assert client._alarm_callbacks == []


@pytest.mark.asyncio
async def test_manual_session_refresh_reconciles_alarm_discovery(monkeypatch):
    calls = []
    seeds = (object(),)

    class FakeAuth:
        @staticmethod
        async def async_refresh_tokens():
            calls.append("refresh-token")

    class FakeRing:
        auth = FakeAuth()

        @staticmethod
        async def async_update_data():
            calls.append("refresh-ring")

    class FakeService:
        @staticmethod
        async def reconcile(received_seeds):
            calls.append(("reconcile", received_seeds))

    client = RingClient()
    client._ring = FakeRing()
    client._alarm_service = FakeService()
    monkeypatch.setattr(client, "_alarm_location_seeds", lambda: seeds)

    await client._async_refresh_session_token()

    assert calls == ["refresh-token", "refresh-ring", ("reconcile", seeds)]


@pytest.mark.asyncio
async def test_device_data_refresh_reconciles_alarm_discovery(monkeypatch):
    calls = []
    seeds = (object(),)

    class FakeDevices:
        all_devices = []

    class FakeRing:
        @staticmethod
        async def async_update_data():
            calls.append("refresh-ring")

        @staticmethod
        def devices():
            return FakeDevices()

    class FakeService:
        @staticmethod
        async def reconcile(received_seeds):
            calls.append(("reconcile", received_seeds))

    client = RingClient()
    client._ring = FakeRing()
    client._alarm_service = FakeService()
    client._last_device_update_at = 0
    monkeypatch.setattr(client, "_alarm_location_seeds", lambda: seeds)

    assert await client._async_refresh_devices(None, max_age_seconds=0) == []
    assert calls == ["refresh-ring", ("reconcile", seeds)]


def test_request_alarm_mode_returns_submitted_future(monkeypatch):
    client = RingClient()
    submitted = []

    def submit(coroutine):
        submitted.append(coroutine.cr_code.co_name)
        coroutine.close()
        future = concurrent.futures.Future()
        future.set_result("scheduled")
        return future

    monkeypatch.setattr(client, "submit", submit)

    future = client.request_alarm_mode(
        "home",
        AlarmMode.AWAY,
        expected_revision=9,
        bypass_ids=("front-door",),
    )

    assert isinstance(future, concurrent.futures.Future)
    assert future.result() == "scheduled"
    assert submitted == ["_async_request_alarm_mode"]


def test_request_alarm_mode_does_not_consume_bypass_iterable_on_caller(monkeypatch):
    client = RingClient()
    bypass_ids = iter(("front-door",))
    received = []

    def submit(coroutine):
        received.append(coroutine.cr_frame.f_locals["bypass_ids"])
        coroutine.close()
        future = concurrent.futures.Future()
        future.set_result("scheduled")
        return future

    monkeypatch.setattr(client, "submit", submit)

    future = client.request_alarm_mode(
        "home",
        AlarmMode.AWAY,
        expected_revision=9,
        bypass_ids=bypass_ids,
    )

    assert future.result() == "scheduled"
    assert received == [bypass_ids]


@pytest.mark.asyncio
async def test_active_alarm_mode_request_is_forwarded_to_service(monkeypatch):
    calls = []

    class FakeService:
        @staticmethod
        async def request_mode(location_id, target, **kwargs):
            calls.append((location_id, target, kwargs))
            return "confirmed"

    client = RingClient()
    client._alarm_service = FakeService()
    client._published_generation = 71
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 71)

    result = await client._async_request_alarm_mode(
        "home",
        AlarmMode.AWAY,
        expected_revision=9,
        bypass_ids=("front-door",),
    )

    assert result == "confirmed"
    assert calls == [
        (
            "home",
            AlarmMode.AWAY,
            {"bypass_zids": ("front-door",), "expected_revision": 9},
        )
    ]


@pytest.mark.asyncio
async def test_alarm_mode_result_is_rejected_after_session_replacement(monkeypatch):
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    class FakeService:
        @staticmethod
        async def request_mode(*_args, **_kwargs):
            request_started.set()
            await release_request.wait()
            return "confirmed"

    client = RingClient()
    client._alarm_service = FakeService()
    client._published_generation = 72
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 72)
    request = asyncio.create_task(
        client._async_request_alarm_mode(
            "home",
            AlarmMode.AWAY,
            expected_revision=9,
            bypass_ids=(),
        )
    )
    await request_started.wait()
    replacement = RingClient()
    replacement._published_generation = 73
    ring_client._client = replacement
    ring_client._active_client_generation = 73
    release_request.set()

    with pytest.raises(ring_client.SessionSupersededError):
        await request


def test_alarm_command_admission_rejects_stale_client_before_send(monkeypatch):
    old_client = RingClient()
    old_client._published_generation = 74
    replacement = RingClient()
    replacement._published_generation = 75
    monkeypatch.setattr(ring_client, "_client", replacement)
    monkeypatch.setattr(ring_client, "_active_client_generation", 75)
    sends = []

    with (
        pytest.raises(ring_client.AlarmCommandSupersededError),
        old_client._alarm_command_admission(74),
    ):
        sends.append("sent")

    assert sends == []


@pytest.mark.asyncio
async def test_alarm_command_admission_does_not_block_concurrent_location_tasks(monkeypatch):
    client = RingClient()
    client._published_generation = 76
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 76)
    both_entered = asyncio.Event()
    release = asyncio.Event()
    entered = []

    async def send(location_id):
        with client._alarm_command_admission(76):
            entered.append(location_id)
            if len(entered) == 2:
                both_entered.set()
            await release.wait()

    first = asyncio.create_task(send("first"))
    second = asyncio.create_task(send("second"))
    await asyncio.wait_for(both_entered.wait(), timeout=1)
    release.set()
    await asyncio.gather(first, second)

    assert entered == ["first", "second"]


@pytest.mark.asyncio
async def test_stale_client_cannot_request_alarm_mode(monkeypatch):
    old_client = RingClient()
    replacement = RingClient()
    old_client._published_generation = 80
    replacement._published_generation = 81
    monkeypatch.setattr(ring_client, "_client", replacement)
    monkeypatch.setattr(ring_client, "_active_client_generation", 81)

    with pytest.raises(ring_client.SessionSupersededError):
        await old_client._async_request_alarm_mode(
            "home",
            AlarmMode.HOME,
            expected_revision=1,
            bypass_ids=(),
        )


@pytest.mark.asyncio
async def test_alarm_service_closes_before_ring_auth():
    order = []

    class FakeService:
        @staticmethod
        async def close():
            order.append("alarm-close")

    class FakeAuth:
        @staticmethod
        async def async_close():
            order.append("auth-close")

    client = RingClient()
    client._ring = SimpleNamespace(auth=FakeAuth())
    client._authenticated = True
    client._alarm_service = FakeService()

    await client._async_close()

    assert order == ["alarm-close", "auth-close"]


@pytest.mark.asyncio
async def test_concurrent_alarm_close_is_joined_before_ring_auth():
    order = []
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class FakeService:
        close_calls = 0

        @classmethod
        async def close(cls):
            cls.close_calls += 1
            close_started.set()
            await release_close.wait()
            order.append("alarm-close")

    class FakeAuth:
        @staticmethod
        async def async_close():
            order.append("auth-close")

    client = RingClient()
    client._ring = SimpleNamespace(auth=FakeAuth())
    client._authenticated = True
    client._alarm_service = FakeService()

    runner_close = asyncio.create_task(client._async_close_alarm_service(client._alarm_service))
    await close_started.wait()
    client_close = asyncio.create_task(client._async_close())
    await asyncio.sleep(0)

    assert FakeService.close_calls == 1
    assert order == []

    release_close.set()
    await asyncio.gather(runner_close, client_close)

    assert order == ["alarm-close", "auth-close"]
