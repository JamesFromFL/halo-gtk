import asyncio
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from halo_gtk import preview, ring_client
from halo_gtk.ring_client import RingClient
from tests.privacy_helpers import grant


@pytest.mark.asyncio
async def test_stop_cancels_authenticated_snapshot_wait_before_decode(monkeypatch):
    started = asyncio.Event()

    async def fetch(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ring_client, "_bounded_snapshot_bytes", fetch)
    client = RingClient()
    client._ring = SimpleNamespace(auth=object())
    grant(client, SimpleNamespace(id=123))
    task = asyncio.create_task(client._async_cached_snapshot_bytes(123))
    await started.wait()
    client.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client._preview_tasks


@pytest.mark.asyncio
async def test_retired_session_drops_finished_recording_result(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    current = [True]
    monkeypatch.setattr(ring_client, "_is_active_client_generation", lambda *args: current[0])
    client = RingClient()
    client._published_generation = 123

    async def recording(url):
        started.set()
        await release.wait()
        return b"normalized png"

    monkeypatch.setattr(client._preview, "recording", recording)
    device = SimpleNamespace(async_recording_url=AsyncMock(return_value="https://example.invalid/"))
    grant(client, device)
    task = asyncio.create_task(client._async_first_recording_frame(device, 1))
    await started.wait()
    current[0] = False
    release.set()
    assert await task is None
    assert not client._preview_tasks


@pytest.mark.asyncio
async def test_fresh_snapshot_uses_timestamp_sequence_and_bounded_fetch(monkeypatch):
    client = RingClient()
    query = AsyncMock(
        return_value=SimpleNamespace(json=lambda: {"timestamps": [{"timestamp": 10**20}]})
    )
    client._ring = SimpleNamespace(auth=object(), async_query=query)
    fetch = AsyncMock(return_value=b"image bytes")
    normalize = AsyncMock(return_value=b"bounded png")
    monkeypatch.setattr(ring_client, "_bounded_snapshot_bytes", fetch)
    monkeypatch.setattr(client._preview, "normalize_image", normalize)
    original_sleep = asyncio.sleep

    async def skip_snapshot_delay(delay):
        if delay != 1:
            await original_sleep(delay)

    monkeypatch.setattr(ring_client.asyncio, "sleep", skip_snapshot_delay)
    device = SimpleNamespace(
        id=123,
        name="fixture",
        async_get_snapshot=AsyncMock(side_effect=AssertionError("eager SDK image call")),
    )
    grant(client, device)
    assert await client._async_snapshot_bytes(device) == b"bounded png"
    assert query.await_count == 2
    fetch.assert_awaited_once_with(client._ring.auth, "/clients_api/snapshots/image/123")
    normalize.assert_awaited_once_with(b"image bytes")
    device.async_get_snapshot.assert_not_awaited()


def _wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "preview cleanup did not complete"
        time.sleep(0.005)


def test_view_cancellation_kills_child_and_releases_preview_slots(monkeypatch):
    monkeypatch.setattr(
        preview, "_worker_command", lambda: [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    client = RingClient()
    client._ring = SimpleNamespace(auth=SimpleNamespace(async_close=AsyncMock()))
    device = SimpleNamespace(
        id=123,
        name="fixture",
        async_history=AsyncMock(return_value=[{"id": 1}]),
        async_recording_url=AsyncMock(return_value="https://fixture.invalid/recording.mp4"),
    )
    fallback = AsyncMock(side_effect=AssertionError("cancelled preview must not fetch fallback"))
    grant(client, device)
    monkeypatch.setattr(client, "_async_cached_snapshot_bytes", fallback)
    cancelled = threading.Event()
    results = []
    caller = threading.Thread(
        target=lambda: results.append(
            client.last_event_frame_for_device(device, cancel_event=cancelled)
        )
    )
    try:
        caller.start()
        _wait_until(lambda: bool(client._preview._children))
        children = tuple(client._preview._children)
        cancelled.set()
        caller.join(2)
        assert not caller.is_alive() and results == [None]
        _wait_until(lambda: not client._preview._children and not client._preview_tasks)
        assert all(child.poll() is not None for child in children)
        fallback.assert_not_awaited()
        for slots in (preview._SLOTS, ring_client._PREVIEW_CALL_SLOTS):
            held = 0
            try:
                while slots.acquire(blocking=False):
                    held += 1
                assert held == 2
            finally:
                for _ in range(held):
                    slots.release()
    finally:
        cancelled.set()
        caller.join(2)
        client.stop()


def test_already_cancelled_preview_closes_unsubmitted_coroutine(monkeypatch):
    client = RingClient()
    monkeypatch.setattr(client, "submit", lambda coro: pytest.fail("cancelled view submitted work"))

    async def work():
        return b"late"

    coroutine = work()
    cancelled = threading.Event()
    cancelled.set()
    assert client._run_preview(coroutine, cancelled) is None
    assert coroutine.cr_frame is None


@pytest.mark.parametrize(
    "method", ["snapshot_for_device", "last_event_frame_for_device", "event_preview_for_device"]
)
def test_default_preview_path_preserves_run_injection(monkeypatch, method):
    client = RingClient()
    client._ring = object()

    def run(coroutine):
        coroutine.close()
        return b"injected preview"

    monkeypatch.setattr(client, "_run", run)
    arguments = (object(), object()) if method == "event_preview_for_device" else (object(),)
    assert getattr(client, method)(*arguments) == b"injected preview"


def test_explicit_preview_wrapper_deadline_cancels_underlying_work(monkeypatch):
    monkeypatch.setattr(ring_client, "_PREVIEW_CALL_TIMEOUT", 0.08)
    client = RingClient()
    finished = threading.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    try:
        started = time.monotonic()
        assert client._run_preview(work(), threading.Event()) is None
        assert time.monotonic() - started < 1
        assert finished.wait(1)
    finally:
        client.stop()


@pytest.mark.parametrize("admit_during", ["first_request", "delay", "second_request"])
async def test_live_admission_stops_all_further_fresh_capture_requests(monkeypatch, admit_during):
    from halo_gtk import live_sessions

    current = []
    device = SimpleNamespace(id=123, name="fixture")
    session = SimpleNamespace(device=device, get_current_frame_png=lambda: b"existing live frame")
    monkeypatch.setattr(
        live_sessions, "active_session_for_device", lambda _device: next(iter(current), None)
    )

    requests = []

    async def query(_endpoint, **_kwargs):
        assert not current, "fresh request submitted after live admission"
        requests.append(True)
        if (admit_during == "first_request" and len(requests) == 1) or (
            admit_during == "second_request" and len(requests) == 2
        ):
            current.append(session)
        return SimpleNamespace(json=lambda: {"timestamps": [{"timestamp": 10**20}]})

    async def delay(_delay):
        if admit_during == "delay":
            current.append(session)

    monkeypatch.setattr(ring_client.asyncio, "sleep", delay)
    monkeypatch.setattr(
        ring_client, "_bounded_snapshot_bytes", AsyncMock(side_effect=AssertionError)
    )
    client = RingClient()
    client._ring = SimpleNamespace(auth=object(), async_query=query)
    grant(client, device)
    assert await client._async_snapshot_bytes(device) == b"existing live frame"
    assert len(requests) == (2 if admit_during == "second_request" else 1)


async def test_reused_camera_id_during_close_cannot_return_the_retired_account_frame(monkeypatch):
    from halo_gtk import live_sessions

    client = RingClient()
    old = SimpleNamespace(id=123)
    current = SimpleNamespace(id=123, name="current")
    grant(client, current)
    client._ring = SimpleNamespace(async_query=AsyncMock())
    session = SimpleNamespace(
        device=old, get_current_frame_png=lambda: pytest.fail("old account frame accessed")
    )
    monkeypatch.setattr(live_sessions, "active_session_for_device", lambda _device: session)
    cached = AsyncMock(return_value=b"current account cached frame")
    monkeypatch.setattr(client, "_async_cached_snapshot_bytes", cached)
    assert await client._async_snapshot_bytes(current) == b"current account cached frame"
    cached.assert_awaited_once_with(current.id, device_name=current.name)
    client._ring.async_query.assert_not_awaited()
