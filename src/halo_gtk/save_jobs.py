"""Application-owned, bounded asynchronous media saves with an atomic commit fence."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Awaitable, Callable, Hashable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

SAVE_TIMEOUT_SECONDS = 3600.0
STARTUP_TIMEOUT_SECONDS = 5.0
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class SaveCancelled(Exception):
    """A save was cancelled before publishing its final files."""


class SaveQueueFull(RuntimeError):
    """The bounded save queue has no free slots."""


@dataclass(eq=False)
class SaveJob:
    owner: Any
    key: Hashable
    label: str
    deadline: float
    _manager: SaveJobManager = field(repr=False)
    _on_update: Callable[[SaveJob], None] | None = field(default=None, repr=False)
    _is_current: Callable[[], bool] | None = field(default=None, repr=False)
    state: str = "queued"
    downloaded: int = 0
    total: int | None = None
    result: Any = None
    error: Exception | None = None
    _future: concurrent.futures.Future | None = field(default=None, repr=False)
    _operation: Callable[[SaveJob], Awaitable[Any]] | None = field(default=None, repr=False)
    _cancel_requested: bool = False
    _last_progress_at: float = 0.0
    _revision: int = 0

    def cancel(self) -> bool:
        """Cancel before the commit fence; False means publication already won."""
        with self._manager._lock:
            if self.state in _TERMINAL or self.state == "committing":
                return False
            self._cancel_requested = True
            future = self._future
        if future is not None:
            future.cancel()
        else:
            self._manager._finish(self, "cancelled")
        return True

    def checkpoint(self) -> None:
        if self._cancel_requested or (self._is_current is not None and not self._is_current()):
            raise SaveCancelled
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Saving timed out")

    def begin_commit(self) -> None:
        """Linearize cancellation versus the final local file/index publication."""
        with self._manager._lock:
            self.checkpoint()
            self.state = "committing"
        self._manager._notify(self)

    def progress(self, downloaded: int, total: int | None) -> None:
        self.downloaded, self.total = downloaded, total
        now = time.monotonic()
        if now - self._last_progress_at >= 1 or (total is not None and downloaded >= total):
            self._last_progress_at = now
            self._manager._notify(self)


class SaveJobManager:
    def __init__(self, *, max_active: int = 2, max_queued: int = 4, dispatch=None) -> None:
        if max_active < 1 or max_queued < 0:
            raise ValueError("Invalid save concurrency limits")
        self._max_active = max_active
        self._capacity = max_active + max_queued
        self._dispatch = dispatch or _gtk_dispatch
        self._lock = threading.RLock()
        self._jobs: dict[tuple[int, Hashable], SaveJob] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._closing = False
        self._startup_token = None
        self._startup_timer: threading.Timer | None = None
        self._worker_thread: threading.Thread | None = None

    def submit(
        self,
        owner,
        key: Hashable,
        label: str,
        operation: Callable[[SaveJob], Awaitable[Any]],
        *,
        on_update: Callable[[SaveJob], None] | None = None,
        timeout: float = SAVE_TIMEOUT_SECONDS,
        is_current: Callable[[], bool] | None = None,
    ) -> SaveJob:
        with self._lock:
            existing = self._jobs.get((id(owner), key))
            if existing is not None:
                return existing
            if self._closing or len(self._jobs) >= self._capacity:
                raise SaveQueueFull("Too many saves are in progress")
            job = SaveJob(
                owner, key, label, time.monotonic() + timeout, self, on_update, is_current
            )
            self._jobs[(id(owner), key)] = job
            job._operation = operation
            if self._loop is not None and not self._loop.is_closed():
                self._schedule(job)
            elif self._startup_token is None:
                if self._worker_thread is not None and self._worker_thread.is_alive():
                    self._finish(job, "failed", error=RuntimeError("Save worker is still stopping"))
                else:
                    self._start_loop()
        self._notify(job)
        return job

    def _schedule(self, job):
        operation = job._operation
        job._operation = None
        job._future = asyncio.run_coroutine_threadsafe(self._run(job, operation), self._loop)

        def observe(future):
            # Cancellation can arrive before the coroutine's first step.
            if future.cancelled() and job.state == "queued":
                self._finish(job, "cancelled")

        job._future.add_done_callback(observe)

    def _startup_failed(self, token, error):
        with self._lock:
            if self._startup_token is not token:
                return
            self._startup_token = None
            if self._startup_timer is not None:
                self._startup_timer.cancel()
            jobs = [job for job in self._jobs.values() if job._future is None]
            for job in jobs:
                self._finish(job, "failed", error=error)

    def _start_loop(self) -> None:
        """Start off GTK; queued jobs remain cancellable while setup is pending."""
        token = self._startup_token = object()

        def worker():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            except Exception as exc:
                self._startup_failed(token, exc)
                return
            with self._lock:
                if self._startup_token is not token:
                    loop.close()
                    return
                self._startup_token = None
                self._startup_timer.cancel()
                self._loop = loop
                self._semaphore = asyncio.Semaphore(self._max_active)
                for job in list(self._jobs.values()):
                    self._schedule(job)
                run = not self._closing or bool(self._jobs)
            try:
                if run:
                    loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self._startup_timer = threading.Timer(
            STARTUP_TIMEOUT_SECONDS,
            self._startup_failed,
            args=(token, TimeoutError("Save worker could not start")),
        )
        self._startup_timer.daemon = True
        try:
            self._startup_timer.start()
            self._worker_thread = threading.Thread(target=worker, name="halo-saves", daemon=True)
            self._worker_thread.start()
        except RuntimeError as exc:
            self._startup_failed(token, exc)

    async def _run(self, job: SaveJob, operation) -> None:
        try:
            async with asyncio.timeout_at(job.deadline), self._semaphore:
                job.checkpoint()
                job.state = "running"
                self._notify(job)
                result = await operation(job)
        except (asyncio.CancelledError, SaveCancelled):
            self._finish(job, "cancelled")
        except Exception as exc:
            self._finish(job, "failed", error=exc)
        else:
            self._finish(job, "succeeded", result=result)

    def _finish(self, job: SaveJob, state: str, *, result=None, error=None) -> None:
        with self._lock:
            if job.state in _TERMINAL:
                return
            job.state, job.result, job.error = state, result, error
            job._operation = None
            key = (id(job.owner), job.key)
            if self._jobs.get(key) is job:
                self._jobs.pop(key)
            stop = self._closing and not self._jobs
        self._notify(job)
        if stop and self._loop is not None:
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._loop.stop)

    def _notify(self, job: SaveJob) -> None:
        with self._lock:
            job._revision += 1
            revision = job._revision

        def deliver():
            if not self._closing and job._revision == revision and job._on_update is not None:
                job._on_update(job)
            return False

        self._dispatch(deliver)

    def cancel_for_client(self, owner) -> None:
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.owner is owner]
        for job in jobs:
            job.cancel()

    def shutdown(self, *, wait: bool = False, timeout: float = 2.0) -> bool:
        """Cancel all work; final app shutdown can allow bounded cleanup time.

        A blocked local filesystem call cannot be force-killed. False reports
        that the daemon worker outlived the caller's cleanup allowance.
        """
        with self._lock:
            already_closing = self._closing
            self._closing = True
            jobs = [] if already_closing else list(self._jobs.values())
            empty = not self._jobs
            loop = self._loop
            worker = self._worker_thread
            if self._startup_timer is not None:
                self._startup_timer.cancel()
        for job in jobs:
            job.cancel()
        if empty and loop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if (
            wait
            and worker is not None
            and worker is not threading.current_thread()
            and worker.is_alive()
        ):
            worker.join(timeout=timeout)
        return worker is None or not worker.is_alive()


def _gtk_dispatch(callback) -> None:
    from gi.repository import GLib

    GLib.idle_add(callback)


_manager: SaveJobManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> SaveJobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = SaveJobManager()
        return _manager


def cancel_for_client(client) -> None:
    if _manager is not None:
        _manager.cancel_for_client(client)


def shutdown() -> bool:
    if _manager is not None:
        return _manager.shutdown(wait=True)
    return True
