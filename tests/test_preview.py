"""Process containment and image policy contracts, using no Ring account."""

import asyncio
import io
import subprocess
import sys
import threading
import time

import pytest
from PIL import Image

from halo_gtk import preview, preview_worker


def png_bytes(size=(64, 32)):
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_normalize_image_runs_in_worker_and_bounds_dimensions():
    manager = preview.PreviewManager()
    try:
        result = await manager.normalize_image(png_bytes((1600, 900)))
        with Image.open(io.BytesIO(result)) as image:
            assert image.size == (1280, 720)
        assert not manager._children
    finally:
        manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", [b"not an image", png_bytes((4097, 1)), b"x" * (4 * 1024 * 1024 + 1)]
)
async def test_reject_invalid_oversized_or_wide_image(data):
    manager = preview.PreviewManager()
    try:
        with pytest.raises(preview.PreviewError):
            await manager.normalize_image(data)
        assert not manager._children
    finally:
        manager.close()


def test_encoded_output_is_capped_before_buffer_growth():
    output = preview_worker.CappedOutput()
    with pytest.raises(preview_worker.PreviewError, match="output_limit"):
        output.write(b"x" * (4 * 1024 * 1024 + 1))
    assert output.getvalue() == b""


@pytest.mark.parametrize("tag", ["bare", '"bad space"', '"unescaped"quote"'])
def test_reject_invalid_strong_etag(tag):
    with pytest.raises(preview_worker.PreviewError, match="invalid_etag"):
        preview_worker.strong_etag(tag)


def test_strong_and_weak_etag_syntax():
    assert preview_worker.strong_etag('""') == '""'
    assert preview_worker.strong_etag('"tag+123"') == '"tag+123"'
    assert preview_worker.strong_etag('W/"tag"') is None


@pytest.mark.asyncio
async def test_combined_watchdog_kills_reaps_and_keeps_url_out_of_argv(monkeypatch):
    children = []
    original = subprocess.Popen

    def spawn(args, **kwargs):
        assert "private-signed-token" not in repr(args)
        child = original(args, **kwargs)
        children.append(child)
        return child

    # Simulates a child stuck in native decode or resolver/executor cleanup.
    monkeypatch.setattr(
        preview, "_worker_command", lambda: [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    monkeypatch.setattr(preview.subprocess, "Popen", spawn)
    manager = preview.PreviewManager(timeout=0.2)
    started = time.monotonic()
    with pytest.raises(preview.PreviewError, match="timed out"):
        await manager.recording("https://example.invalid/?token=private-signed-token")
    assert time.monotonic() - started < 2
    assert children and all(child.poll() is not None for child in children)
    assert not manager._children


@pytest.mark.asyncio
async def test_task_cancellation_reaps_worker_and_returns_slot(monkeypatch):
    monkeypatch.setattr(preview, "_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        preview, "_worker_command", lambda: [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    manager = preview.PreviewManager()
    task = asyncio.create_task(manager.recording("https://example.invalid/recording"))
    while not manager._children:
        await asyncio.sleep(0.01)
    children = tuple(manager._children)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(child.poll() is not None for child in children)
    assert preview._SLOTS.acquire(blocking=False)
    preview._SLOTS.release()


@pytest.mark.asyncio
async def test_close_cancels_active_worker_and_rejects_later_work(monkeypatch):
    monkeypatch.setattr(
        preview, "_worker_command", lambda: [sys.executable, "-c", "import time;time.sleep(30)"]
    )
    manager = preview.PreviewManager()
    task = asyncio.create_task(manager.recording("https://example.invalid/recording"))
    while not manager._children:
        await asyncio.sleep(0.01)
    children = tuple(manager._children)
    manager.close()
    with pytest.raises(preview.PreviewError, match="cancelled"):
        await task
    assert all(child.poll() is not None for child in children)
    with pytest.raises(preview.PreviewError, match="cancelled"):
        await manager.normalize_image(png_bytes())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "import sys;sys.stdout.buffer.write(b'invalid')",
        "import sys;sys.stdout.buffer.write(b'x'*(5*1024*1024))",
    ],
)
async def test_parent_rejects_invalid_or_oversized_worker_output(monkeypatch, code):
    monkeypatch.setattr(preview, "_worker_command", lambda: [sys.executable, "-c", code])
    with pytest.raises(preview.PreviewError, match="output"):
        await preview.PreviewManager().normalize_image(png_bytes())


@pytest.mark.asyncio
async def test_stalled_dns_is_inside_parent_watchdog(monkeypatch, tmp_path):
    marker = tmp_path / "resolver-entered"
    code = f"""import socket,time,runpy
from pathlib import Path
def stalled(*args,**kwargs):
    Path({str(marker)!r}).write_text('entered')
    time.sleep(30)
socket.getaddrinfo=stalled
runpy.run_module('halo_gtk.preview_worker',run_name='__main__')
"""
    monkeypatch.setattr(preview, "_worker_command", lambda: [sys.executable, "-c", code])
    manager = preview.PreviewManager(timeout=2)
    started = time.monotonic()
    with pytest.raises(preview.PreviewError, match="timed out"):
        await manager.recording("https://fake-resolver.invalid/recording.mp4")
    assert marker.read_text() == "entered"
    assert time.monotonic() - started < 4 and not manager._children


def test_small_full_fallback_keeps_same_aggregate_budget(monkeypatch):
    budget = preview_worker.Budget(cap=100)

    class Reader:
        def __init__(self, url, budget):
            self.budget = budget

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def decode(source):
        if isinstance(source, Reader):
            source.budget.transferred += 64
            raise preview_worker.PreviewError("strong_validator_required")
        return source.getvalue()

    def download(url, received):
        assert received is budget and received.transferred == 64
        received.transferred += 35
        return b"full"

    monkeypatch.setattr(preview_worker, "RangeReader", Reader)
    monkeypatch.setattr(preview_worker, "decode_first", decode)
    monkeypatch.setattr(preview_worker, "capped_download", download)
    assert preview_worker.recording_preview("https://example.invalid/", budget) == b"full"
    assert budget.transferred == 99


def test_security_failures_do_not_fall_back(monkeypatch):
    class Reader:
        def __init__(self, *args, **kwargs):
            raise preview_worker.PreviewError("entity_changed")

    monkeypatch.setattr(preview_worker, "RangeReader", Reader)
    monkeypatch.setattr(
        preview_worker, "capped_download", lambda *args: pytest.fail("unsafe fallback")
    )
    with pytest.raises(preview_worker.PreviewError, match="entity_changed"):
        preview_worker.recording_preview("https://example.invalid/", preview_worker.Budget())
