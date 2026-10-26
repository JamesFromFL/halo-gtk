"""Native save transport policy and cancellation before atomic publication."""

import asyncio
from contextlib import suppress

import pytest

from halo_gtk import http_download
from halo_gtk.save_jobs import SaveJobManager


class AsyncResponse:
    def __init__(self, *, headers=None, chunks=(b"video",), close=None, status=200):
        self.headers = headers or {}
        self.status = status
        self.url = "https://example.test/video"
        self.content = self
        self.chunks = chunks
        self.close = close

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self.close is not None:
            await self.close()

    def raise_for_status(self):
        pass

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class AsyncSession:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


def fake_session(monkeypatch, response):
    session = AsyncSession(response)
    options = {}

    def create(**kwargs):
        options.update(kwargs)
        return session

    monkeypatch.setattr(http_download.aiohttp, "ClientSession", create)
    return session, options


@pytest.mark.asyncio
async def test_signed_url_is_literal_and_transport_is_cookieless(monkeypatch, tmp_path):
    response = AsyncResponse(headers={"Content-Length": "5"})
    session, options = fake_session(monkeypatch, response)
    signed = "https://example.test/a%2Fb?sig=a%2Bb%2f&key=a+b&n=%31"
    result = await http_download.async_download_https(signed, tmp_path / "video.mp4")
    assert str(session.requests[0][0]) == signed
    assert session.requests[0][1] == {"allow_redirects": False}
    assert isinstance(options["cookie_jar"], http_download.aiohttp.DummyCookieJar)
    assert options["auto_decompress"] is False
    assert options["trust_env"] is False
    assert result.read_bytes() == b"video"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Encoding": "gzip"},
        {"Content-Length": "-1"},
        {"Content-Length": "+5"},
        {"Content-Length": "５"},
        {"Content-Length": "no"},
        {"Content-Length": "6"},
    ],
)
async def test_invalid_encoding_or_length_never_publishes(monkeypatch, tmp_path, headers):
    fake_session(monkeypatch, AsyncResponse(headers=headers))
    destination = tmp_path / "video.mp4"
    destination.write_bytes(b"old")
    with pytest.raises(http_download.DownloadError):
        await http_download.async_download_https("https://example.test/video", destination)
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.test/video",
        "https://example.test:0/video",
        "https://example.test:65536/video",
        "https://example.test:invalid/video",
        "https://[invalid/video",
        "https://example.test/a\nb",
        "https://example.test/a b",
    ],
)
def test_shared_url_validation_rejects_userinfo_ports_and_controls(url):
    with pytest.raises(http_download.DownloadError):
        http_download._validated_https_url(url)


@pytest.mark.asyncio
async def test_cancellation_while_closing_response_does_not_publish(monkeypatch, tmp_path):
    closing = asyncio.Event()

    async def close():
        closing.set()
        await asyncio.Event().wait()

    fake_session(monkeypatch, AsyncResponse(close=close))
    destination = tmp_path / "video.mp4"
    destination.write_bytes(b"old")
    task = asyncio.create_task(
        http_download.async_download_https("https://example.test/video", destination)
    )
    await asyncio.wait_for(closing.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_active_job_cancellation_closes_native_transport_and_removes_partial(
    monkeypatch, tmp_path, shutdown
):
    # Use plain loopback only to isolate active native transport cancellation.
    # HTTPS admission and TLS defaults remain separately covered above.
    monkeypatch.setattr(http_download, "_validated_https_url", lambda url: url)
    received = asyncio.Event()
    disconnected = asyncio.Event()

    async def handler(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nabc")
            await writer.drain()
            received.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    manager = SaveJobManager(dispatch=lambda callback: callback())
    destination = tmp_path / "video.mp4"
    destination.write_bytes(b"old")
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def operation(job):
        return await http_download.async_download_https(
            f"http://127.0.0.1:{port}/video", destination, control=job
        )

    try:
        job = manager.submit(object(), "video", "Saving", operation)
        await asyncio.wait_for(received.wait(), 2)
        if shutdown:
            assert manager.shutdown(wait=True)
            assert manager._loop.is_closed()
            assert not manager._worker_thread.is_alive()
        else:
            assert job.cancel()
        async with asyncio.timeout(2):
            while job.state != "cancelled":
                await asyncio.sleep(0.005)
        await asyncio.wait_for(disconnected.wait(), 2)
        assert destination.read_bytes() == b"old"
        assert list(tmp_path.glob(".*.part")) == []
    finally:
        manager.shutdown()
        server.close()
        await server.wait_closed()
        async with asyncio.timeout(2):
            while manager._loop is not None and not manager._loop.is_closed():
                await asyncio.sleep(0.005)
