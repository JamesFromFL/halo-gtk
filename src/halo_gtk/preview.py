"""Finite, cancelable preview workers with a parent-owned wall-clock deadline."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_CONTROL_BYTES = 32 * 1024
_SLOTS = threading.BoundedSemaphore(2)


class PreviewError(RuntimeError):
    """A bounded preview was unavailable; messages never contain source URLs."""


def _worker_command():
    return [sys.executable, "-m", "halo_gtk.preview_worker"]


class PreviewManager:
    def __init__(self, cancel_event=None, *, timeout=15.0):
        self._cancel_event = cancel_event or threading.Event()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._children = set()
        self.timeout = min(15.0, max(0.01, timeout))

    def _check(self, deadline):
        if self._closed.is_set() or self._cancel_event.is_set():
            raise PreviewError("Preview cancelled")
        if time.monotonic() >= deadline:
            raise PreviewError("Preview timed out")

    @staticmethod
    def _reap(child):
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                child.kill()
        child.wait(timeout=1)

    def close(self):
        self._closed.set()
        with self._lock:
            children = tuple(self._children)
        for child in children:
            self._reap(child)

    async def recording(self, url):
        return await self._run("recording", url=url)

    async def image_url(self, url):
        """Fetch an unauthenticated signed image URL without forwarding Ring auth."""
        return await self._run("image_url", url=url)

    async def normalize_image(self, data):
        if len(data) > MAX_IMAGE_BYTES:
            raise PreviewError("Preview image exceeds size limit")
        return await self._run("image", data=data)

    async def _run(self, operation, *, url=None, data=b""):
        deadline = time.monotonic() + self.timeout
        self._check(deadline)
        request = {"operation": operation, "network_seconds": min(12, self.timeout)}
        if url is not None:
            if not isinstance(url, str) or len(url) > 16 * 1024:
                raise PreviewError("Invalid preview URL")
            request["url"] = url
        control = json.dumps(request, ensure_ascii=True).encode() + b"\n"
        if len(control) > MAX_CONTROL_BYTES or len(data) > MAX_IMAGE_BYTES:
            raise PreviewError("Preview input exceeds size limit")
        while not _SLOTS.acquire(blocking=False):
            self._check(deadline)
            await asyncio.sleep(0.025)
        child = None
        try:
            self._check(deadline)
            # Regular anonymous files avoid pipe deadlocks and unbounded
            # communicate() buffers. Child RLIMIT_FSIZE also bounds stdout.
            with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
                source.write(control)
                source.write(data)
                source.seek(0)
                environment = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
                source_root = str(Path(__file__).resolve().parent.parent)
                environment["PYTHONPATH"] = (
                    source_root + os.pathsep + environment.get("PYTHONPATH", "")
                )
                child = subprocess.Popen(
                    _worker_command(),
                    stdin=source,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    close_fds=True,
                )
                with self._lock:
                    self._children.add(child)
                while child.poll() is None:
                    self._check(deadline)
                    await asyncio.sleep(0.025)
                self._check(deadline)
                if child.returncode != 0:
                    raise PreviewError("Preview unavailable")
                output.seek(0)
                png = output.read(MAX_IMAGE_BYTES + 1)
                if (
                    len(png) > MAX_IMAGE_BYTES
                    or len(png) < 24
                    or png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                ):
                    raise PreviewError("Invalid preview output")
                width, height = struct.unpack(">II", png[16:24])
                if not (0 < width <= 1280 and 0 < height <= 720):
                    raise PreviewError("Invalid preview dimensions")
                return png
        finally:
            if child is not None:
                self._reap(child)
                with self._lock:
                    self._children.discard(child)
            _SLOTS.release()
