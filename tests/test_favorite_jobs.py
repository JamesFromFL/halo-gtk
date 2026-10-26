"""Background archive snapshots and cancellable index publication."""

import asyncio
import fcntl
import gc
import threading
import time
import weakref
from pathlib import Path

import pytest

from halo_gtk import favorites


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


@pytest.fixture
def archive(monkeypatch, tmp_path):
    root = tmp_path / "favorites"
    for name, value in {
        "FAVORITES_DIR": root,
        "FAVORITE_VIDEOS_DIR": root / "videos",
        "FAVORITE_THUMBNAILS_DIR": root / "thumbnails",
        "FAVORITES_INDEX": root / "index.json",
        "_snapshot_key": None,
        "_snapshot_events": (),
        "_snapshot_ids": frozenset(),
        "_snapshot_updated": 0.0,
        "_snapshot_loading": False,
        "_snapshot_callbacks": [],
        "_snapshot_generation": 0,
        "_snapshot_retry_after": 0.0,
    }.items():
        monkeypatch.setattr(favorites, name, value)
    yield root
    wait_until(lambda: not favorites._snapshot_loading)


@pytest.fixture
def idle(monkeypatch):
    from gi.repository import GLib

    pending = []
    monkeypatch.setattr(GLib, "idle_add", lambda callback: pending.append(callback))
    return pending


def test_mutation_during_scan_discards_old_snapshot_and_reruns(monkeypatch, archive, idle):
    entered, release = threading.Event(), threading.Event()
    rows = [{"_favorite_id": "old"}]
    calls = []
    ready = []

    def scan():
        result = [dict(row) for row in rows]
        calls.append(result)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(favorites, "favorite_events", scan)
    favorites.request_reconcile(
        lambda: ready.append(favorites.cached_favorite_events()), force=True
    )
    assert entered.wait(2)
    rows[:] = [{"_favorite_id": "new"}]
    favorites.request_reconcile(force=True)
    release.set()
    wait_until(lambda: bool(idle))
    for callback in idle[:]:
        callback()
    assert len(calls) == 2
    assert ready == [[{"_favorite_id": "new"}]]
    assert favorites._snapshot_ids == frozenset({"new"})


def test_cached_rows_are_copied_no_filesystem_scan_and_ready_callback_fires(
    monkeypatch, archive, idle
):
    monkeypatch.setattr(favorites, "favorite_events", lambda: [{"_favorite_id": "101-202"}])
    favorites.request_reconcile(force=True)
    wait_until(lambda: not favorites._snapshot_loading)
    monkeypatch.setattr(
        favorites, "favorite_events", lambda: pytest.fail("fresh cache must not scan")
    )
    rows = favorites.cached_favorite_events()
    rows[0]["_favorite_id"] = "changed"
    assert favorites.cached_is_favorited({"id": 202, "device_id": 101})
    assert favorites.cached_favorite_events()[0]["_favorite_id"] == "101-202"
    called = []
    favorites.request_reconcile(lambda: called.append(True))
    assert len(idle) == 1
    idle[0]()
    assert called == [True]


def test_cache_callback_does_not_retain_page(monkeypatch, archive, idle):
    entered, release = threading.Event(), threading.Event()

    def scan():
        entered.set()
        assert release.wait(2)
        return []

    class Page:
        def ready(self):
            pytest.fail("destroyed page callback must not run")

    monkeypatch.setattr(favorites, "favorite_events", scan)
    page = Page()
    reference = weakref.ref(page)
    favorites.request_reconcile(page.ready, force=True)
    assert entered.wait(2)
    del page
    gc.collect()
    assert reference() is None
    release.set()
    wait_until(lambda: bool(idle))
    idle[0]()


def test_scan_error_preserves_last_good_snapshot_and_thread_failure_can_retry(
    monkeypatch, archive, idle
):
    monkeypatch.setattr(favorites, "favorite_events", lambda: [{"_favorite_id": "good"}])
    favorites.request_reconcile(force=True)
    wait_until(lambda: not favorites._snapshot_loading)

    def fail_scan():
        raise OSError("temporary permission failure")

    monkeypatch.setattr(favorites, "favorite_events", fail_scan)
    favorites.request_reconcile(force=True)
    wait_until(lambda: not favorites._snapshot_loading)
    assert favorites.cached_favorite_events() == [{"_favorite_id": "good"}]
    monkeypatch.setattr(favorites, "_snapshot_retry_after", 0)
    original_thread = threading.Thread

    class CannotStart:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("no thread resources")

    monkeypatch.setattr(favorites.threading, "Thread", CannotStart)
    favorites.request_reconcile(force=True)
    assert not favorites._snapshot_loading
    assert favorites._snapshot_retry_after > time.monotonic()
    monkeypatch.setattr(favorites.threading, "Thread", original_thread)
    monkeypatch.setattr(favorites, "_snapshot_retry_after", 0)
    monkeypatch.setattr(favorites, "favorite_events", lambda: [{"_favorite_id": "recovered"}])
    favorites.request_reconcile(force=True)
    wait_until(lambda: not favorites._snapshot_loading)
    assert favorites.cached_favorite_events() == [{"_favorite_id": "recovered"}]


@pytest.mark.asyncio
async def test_favorite_wait_for_process_lock_yields_and_cancel_removes_staging(
    monkeypatch, archive
):
    archive.mkdir(parents=True)
    downloaded = asyncio.Event()

    async def download(_url, destination, **kwargs):
        assert kwargs["publish"] is False
        Path(destination).write_bytes(b"video")
        downloaded.set()

    monkeypatch.setattr(favorites, "async_download_https", download)
    with (archive / ".index.lock").open("a+b") as foreign_lock:
        fcntl.flock(foreign_lock, fcntl.LOCK_EX)
        task = asyncio.create_task(
            favorites.async_add_favorite(
                {"id": 202, "device_id": 101}, "https://example.test/video"
            )
        )
        try:
            await asyncio.wait_for(downloaded.wait(), 2)
            # Another coroutine runs while the first waits for an external lock.
            progressed = asyncio.Event()
            asyncio.get_running_loop().call_later(0.03, progressed.set)
            await asyncio.wait_for(progressed.wait(), 1)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            fcntl.flock(foreign_lock, fcntl.LOCK_UN)
    assert not favorites.FAVORITES_INDEX.exists()
    assert list(favorites.FAVORITE_VIDEOS_DIR.iterdir()) == []


@pytest.mark.asyncio
async def test_async_index_transactions_serialize_coroutines_on_same_thread(archive):
    first = asyncio.Event()
    release = asyncio.Event()
    entered = []

    async def transaction(number):
        async with favorites._async_index_transaction():
            entered.append(number)
            if number == 1:
                first.set()
                await release.wait()

    task1 = asyncio.create_task(transaction(1))
    await first.wait()
    task2 = asyncio.create_task(transaction(2))
    await asyncio.sleep(0.02)
    assert entered == [1]
    release.set()
    await asyncio.gather(task1, task2)
    assert entered == [1, 2]
