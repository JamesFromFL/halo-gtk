"""Bounded application save ownership and cancellation/publication races."""

import asyncio
import threading
import time

import pytest

from halo_gtk.save_jobs import SaveJobManager, SaveQueueFull


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("save worker did not reach expected state")
        time.sleep(0.005)


@pytest.fixture
def manager():
    value = SaveJobManager(max_active=1, max_queued=1, dispatch=lambda callback: callback())
    yield value
    value.shutdown()
    if value._loop is not None:
        wait_until(value._loop.is_closed)


def test_registry_deduplicates_bounds_queue_and_cancels_active_coroutine(manager):
    owner = object()
    entered, cleaned = threading.Event(), threading.Event()

    async def blocked(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def second(job):
        assert cleaned.is_set()
        return "second"

    first = manager.submit(owner, "one", "Saving", blocked)
    assert entered.wait(2)
    assert manager.submit(owner, "one", "Saving", blocked) is first
    queued = manager.submit(owner, "two", "Saving", second)
    with pytest.raises(SaveQueueFull):
        manager.submit(owner, "three", "Saving", second)
    assert queued.state == "queued"
    assert first.cancel()
    wait_until(lambda: queued.state == "succeeded")
    assert first.state == "cancelled"
    assert queued.result == "second"


def test_commit_wins_cancellation_and_reports_success(manager, tmp_path):
    committing, finish = threading.Event(), threading.Event()
    destination = tmp_path / "saved"

    async def operation(job):
        job.begin_commit()
        committing.set()
        assert finish.wait(2)
        destination.write_text("complete")
        return destination

    job = manager.submit(object(), "one", "Saving", operation)
    try:
        assert committing.wait(2)
        assert job.cancel() is False
    finally:
        finish.set()
    wait_until(lambda: job.state == "succeeded")
    assert destination.read_text() == "complete"


def test_owner_cancellation_and_deadline_leave_no_commit(manager, tmp_path):
    owner = object()
    started = threading.Event()
    destination = tmp_path / "saved"

    async def operation(job):
        started.set()
        await asyncio.Event().wait()
        job.begin_commit()
        destination.write_text("wrong")

    job = manager.submit(owner, "one", "Saving", operation)
    assert started.wait(2)
    manager.cancel_for_client(owner)
    wait_until(lambda: job.state == "cancelled")
    timed = manager.submit(owner, "two", "Saving", operation, timeout=0.03)
    wait_until(lambda: timed.state == "failed")
    assert isinstance(timed.error, TimeoutError)
    assert not destination.exists()


def test_unpublished_owner_cannot_cross_commit_fence_without_stop_hook(manager, tmp_path):
    current = [True]
    started = threading.Event()
    release = None
    destination = tmp_path / "saved"

    async def operation(job):
        nonlocal release
        release = asyncio.Event()
        started.set()
        await release.wait()
        job.begin_commit()
        destination.write_text("wrong")

    job = manager.submit(object(), "one", "Saving", operation, is_current=lambda: current[0])
    assert started.wait(2)
    current[0] = False
    manager._loop.call_soon_threadsafe(release.set)
    wait_until(lambda: job.state == "cancelled")
    assert not destination.exists()


def test_shutdown_cancels_queued_jobs_and_is_idempotent(manager):
    async def operation(job):
        await asyncio.Event().wait()

    jobs = [manager.submit(object(), i, "Saving", operation) for i in range(2)]
    manager.shutdown()
    manager.shutdown()
    wait_until(lambda: all(job.state == "cancelled" for job in jobs))
    with pytest.raises(SaveQueueFull):
        manager.submit(object(), "late", "Saving", operation)


def test_admission_does_not_wait_for_loop_start_and_queued_cancel_wins(monkeypatch):
    from halo_gtk import save_jobs

    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    original = asyncio.new_event_loop

    def slow_start():
        entered.set()
        assert release.wait(2)
        loop = original()
        ended.set()
        return loop

    monkeypatch.setattr(save_jobs.asyncio, "new_event_loop", slow_start)
    manager = SaveJobManager(dispatch=lambda callback: callback())

    async def operation(job):
        pytest.fail("cancelled queued operation must not run")

    started = time.monotonic()
    job = manager.submit(object(), "queued", "Saving", operation)
    assert time.monotonic() - started < 0.2
    assert entered.wait(2)
    assert job.state == "queued"
    assert job.cancel()
    assert job.state == "cancelled"
    manager.shutdown()
    release.set()
    assert ended.wait(2)
    wait_until(lambda: manager._loop is not None and manager._loop.is_closed())


def test_worker_start_failure_finishes_jobs_visibly(monkeypatch):
    from halo_gtk import save_jobs

    def fail_start():
        raise OSError("no selector available")

    monkeypatch.setattr(save_jobs.asyncio, "new_event_loop", fail_start)
    outcomes = []
    manager = SaveJobManager(dispatch=lambda callback: callback())

    async def operation(job):
        pytest.fail("failed startup must not run operation")

    job = manager.submit(
        object(),
        "queued",
        "Saving",
        operation,
        on_update=lambda value: outcomes.append(value.state),
    )
    wait_until(lambda: job.state == "failed")
    assert isinstance(job.error, OSError)
    assert outcomes[-1] == "failed"
    manager.shutdown()


def test_stalled_worker_start_has_bounded_failure_and_late_loop_is_closed(monkeypatch):
    from halo_gtk import save_jobs

    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    loops = []
    original = asyncio.new_event_loop

    def slow_start():
        entered.set()
        assert release.wait(2)
        loop = original()
        loops.append(loop)
        ended.set()
        return loop

    monkeypatch.setattr(save_jobs, "STARTUP_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(save_jobs.asyncio, "new_event_loop", slow_start)
    manager = SaveJobManager(dispatch=lambda callback: callback())

    async def operation(job):
        pytest.fail("expired startup must not run operation")

    try:
        job = manager.submit(object(), "queued", "Saving", operation)
        assert entered.wait(2)
        wait_until(lambda: job.state == "failed")
        assert isinstance(job.error, TimeoutError)
    finally:
        manager.shutdown()
        release.set()
    assert ended.wait(2)
    wait_until(lambda: loops[0].is_closed())


def test_retry_after_startup_watchdog_does_not_spawn_another_stalled_worker(monkeypatch):
    from halo_gtk import save_jobs

    release, entered = threading.Event(), threading.Event()
    original = asyncio.new_event_loop
    loops = []
    calls = []

    def slow_start():
        calls.append(threading.current_thread())
        entered.set()
        assert release.wait(2)
        loop = original()
        loops.append(loop)
        return loop

    monkeypatch.setattr(save_jobs, "STARTUP_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(save_jobs.asyncio, "new_event_loop", slow_start)
    manager = SaveJobManager(dispatch=lambda callback: callback())

    async def operation(job):
        pytest.fail("timed out startup must not execute a save")

    try:
        first = manager.submit(object(), "first", "Saving", operation)
        assert entered.wait(2)
        wait_until(lambda: first.state == "failed")
        worker = manager._worker_thread
        for number in range(2):
            retried = manager.submit(object(), number, "Saving", operation)
            assert retried.state == "failed"
            assert isinstance(retried.error, RuntimeError)
            assert manager._worker_thread is worker
        assert len(calls) == 1
    finally:
        manager.shutdown()
        release.set()
        manager._worker_thread.join(timeout=2)
    assert loops and all(loop.is_closed() for loop in loops)
    assert not worker.is_alive()


def test_repeated_shutdown_drains_awaited_cleanup_without_cancelling_it_again():
    entered, cleanup_entered, cleaned = threading.Event(), threading.Event(), threading.Event()
    manager = SaveJobManager(dispatch=lambda callback: callback())

    async def operation(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await asyncio.sleep(0.10)
            cleaned.set()

    job = manager.submit(object(), "active", "Saving", operation)
    try:
        assert entered.wait(2)
        manager.shutdown()
        assert cleanup_entered.wait(2)
        assert manager.shutdown(wait=True, timeout=1)
        assert cleaned.is_set()
        assert job.state == "cancelled"
        assert manager._loop.is_closed()
    finally:
        manager.shutdown(wait=True)
