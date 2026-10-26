"""Private preview child: verified bounded HTTPS and untrusted media decoding.

Run only through preview.PreviewManager, whose parent watchdog also contains
stalled DNS/executor shutdown and native codec work. No credentials in argv.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import resource
import ssl
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image
from yarl import URL

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_CONTROL_BYTES = 32 * 1024


class CappedOutput(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_IMAGE_BYTES:
            raise PreviewError("output_limit")
        return super().write(data)


class PreviewError(RuntimeError):
    """Errors intentionally contain a category, never source URLs."""


@dataclass
class Budget:
    cap: int = 8 * 1024 * 1024
    total_seconds: float = 12.0
    connect_seconds: float = 3.0
    read_seconds: float = 2.0
    max_requests: int = 24
    max_redirects: int = 3
    max_source: int = 512 * 1024 * 1024
    transferred: int = 0
    requests: int = 0
    redirects: int = 0
    responses_closed: int = 0
    started: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)

    def check(self):
        if self.cancel.is_set():
            raise PreviewError("cancelled")
        if self.remaining <= 0:
            raise PreviewError("total_deadline")

    @property
    def remaining(self):
        return self.total_seconds - (time.monotonic() - self.started)


def validate_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise PreviewError("https_required")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise PreviewError("url_credentials_or_fragment")
    try:
        _port = parsed.port
    except ValueError:
        raise PreviewError("invalid_port") from None
    return url


def strong_etag(value):
    """RFC 9110 entity-tag syntax; weak tags cannot validate multiple ranges."""
    if value is None:
        return None
    if re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value):
        return value
    if re.fullmatch(r'W/"[\x21\x23-\x7e\x80-\xff]*"', value):
        return None
    raise PreviewError("invalid_etag")


async def _fetch(url, budget, tls, start=None, length=None, expected_size=None, etag=None):
    """Fetch one bounded response, checking redirects before requesting target.

    App bytes are capped; TCP/TLS and aiohttp receive buffers can read ahead.
    asyncio.timeout limits the network coroutine without aiohttp's >=5 second
    timeout rounding. The outer asyncio.run wrapper can wait for a stalled DNS
    executor during cleanup; a combined network/decode process watchdog is still
    required. Cancellation is polled every 50 ms while the event loop responds.
    """
    budget.check()
    validate_url(url)
    if tls is None:
        tls = ssl.create_default_context()
    if (
        not isinstance(tls, ssl.SSLContext)
        or not tls.check_hostname
        or tls.verify_mode != ssl.CERT_REQUIRED
    ):
        raise PreviewError("tls_verification_required")
    headers = {"Accept-Encoding": "identity"}
    if start is not None:
        headers["Range"] = f"bytes={start}-{start + length - 1}"
    if etag:
        headers["If-Range"] = etag
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=budget.connect_seconds,
        sock_connect=budget.connect_seconds,
        sock_read=budget.read_seconds,
    )

    async def cancel_watch(task):
        while not task.done():
            await asyncio.sleep(0.05)
            if budget.cancel.is_set():
                task.cancel()
                return

    owner = asyncio.current_task()
    watcher = asyncio.create_task(cancel_watch(owner))
    try:
        async with asyncio.timeout(max(0.001, budget.remaining)):
            async with aiohttp.ClientSession(
                timeout=timeout,
                auto_decompress=False,
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                read_bufsize=16 * 1024,
            ) as session:
                current = url
                for hop in range(budget.max_redirects + 1):
                    budget.check()
                    validate_url(current)
                    budget.requests += 1
                    if budget.requests > budget.max_requests:
                        raise PreviewError("request_limit")
                    async with session.get(
                        URL(current, encoded=True), headers=headers, ssl=tls, allow_redirects=False
                    ) as response:
                        try:
                            if response.status in (301, 302, 303, 307, 308):
                                location = response.headers.get("Location")
                                if not location:
                                    raise PreviewError("redirect_missing_location")
                                target = validate_url(urljoin(current, location))
                                if hop == budget.max_redirects:
                                    raise PreviewError("redirect_limit")
                                budget.redirects += 1
                                current = target
                                continue
                            if (
                                response.headers.get("Content-Encoding", "identity").lower()
                                != "identity"
                            ):
                                raise PreviewError("content_encoding")
                            if response.status not in (200, 206):
                                raise PreviewError("http_status")
                            size = None
                            received_etag = strong_etag(response.headers.get("ETag"))
                            if etag and received_etag != etag:
                                raise PreviewError("entity_changed")
                            if start is not None:
                                if response.status != 206:
                                    raise PreviewError("range_unsupported")
                                match = re.fullmatch(
                                    r"bytes (\d+)-(\d+)/(\d+)",
                                    response.headers.get("Content-Range", ""),
                                )
                                if not match:
                                    raise PreviewError("content_range")
                                first, last, size = map(int, match.groups())
                                if (
                                    first != start
                                    or last != min(start + length - 1, size - 1)
                                    or last < first
                                ):
                                    raise PreviewError("content_range")
                                if size > budget.max_source or (
                                    expected_size is not None and size != expected_size
                                ):
                                    raise PreviewError("source_size")
                                expected = last - first + 1
                            else:
                                if response.status != 200:
                                    raise PreviewError("unexpected_partial")
                                expected = None
                            content_length = response.headers.get("Content-Length")
                            if content_length is not None:
                                try:
                                    declared = int(content_length)
                                except ValueError:
                                    raise PreviewError("content_length") from None
                                if declared < 0 or declared > budget.cap - budget.transferred:
                                    raise PreviewError("declared_byte_limit")
                                if expected is not None and declared != expected:
                                    raise PreviewError("content_length")
                            body = bytearray()
                            while True:
                                budget.check()
                                # Read one sentinel beyond remaining cap, never a full extra block.
                                chunk = await response.content.read(
                                    min(16 * 1024, budget.cap - budget.transferred + 1)
                                )
                                if not chunk:
                                    break
                                budget.transferred += len(chunk)
                                if budget.transferred > budget.cap:
                                    raise PreviewError("byte_limit")
                                body.extend(chunk)
                                if expected is not None and len(body) > expected:
                                    raise PreviewError("range_body_size")
                            if expected is not None and len(body) != expected:
                                raise PreviewError("truncated_range")
                            return bytes(body), size, received_etag
                        finally:
                            budget.responses_closed += 1
    except asyncio.CancelledError:
        if budget.cancel.is_set():
            raise PreviewError("cancelled") from None
        raise
    except TimeoutError:
        raise PreviewError("network_or_total_timeout") from None
    except aiohttp.ClientError:
        raise PreviewError("network_or_tls_error") from None
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def capped_download(url, budget, tls=None):
    return asyncio.run(_fetch(url, budget, tls))[0]


class RangeReader(io.RawIOBase):
    def __init__(self, url, budget=None, tls=None, block=64 * 1024):
        super().__init__()
        self.url = validate_url(url)
        self.budget = budget or Budget()
        self.tls = tls
        self.block = block
        self.position = 0
        self.size = None
        self.etag = None
        self.cache = {}
        self.seeks = 0
        self.reads = 0
        self.served = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def _block(self, index):
        if self.closed:
            raise PreviewError("reader_closed")
        if index not in self.cache:
            if self.cache and not self.etag:
                raise PreviewError("strong_validator_required")
            body, size, tag = asyncio.run(
                _fetch(
                    self.url,
                    self.budget,
                    self.tls,
                    index * self.block,
                    self.block,
                    self.size,
                    self.etag,
                )
            )
            self.size = size
            if tag:
                self.etag = tag
            self.cache[index] = body
        return self.cache[index]

    def seek(self, offset, whence=io.SEEK_SET):
        if self.closed:
            raise PreviewError("reader_closed")
        self.budget.check()
        self.seeks += 1
        if self.seeks > 64:
            raise PreviewError("seek_limit")
        if whence == io.SEEK_END:
            if self.size is None:
                self._block(0)
            target = self.size + offset
        elif whence == io.SEEK_CUR:
            target = self.position + offset
        elif whence == io.SEEK_SET:
            target = offset
        else:
            raise PreviewError("seek_whence")
        if target < 0 or target > self.budget.max_source:
            raise PreviewError("seek_offset")
        self.position = target
        return target

    def read(self, n=-1):
        if self.closed:
            raise PreviewError("reader_closed")
        self.budget.check()
        self.reads += 1
        if self.reads > 1024:
            raise PreviewError("read_limit")
        if n < 0 or n > self.budget.cap:
            raise PreviewError("read_size")
        if self.size is None:
            self._block(0)
        end = min(self.size, self.position + n)
        body = bytearray()
        while self.position < end:
            index, offset = divmod(self.position, self.block)
            block = self._block(index)
            part = block[offset : offset + min(end - self.position, self.block - offset)]
            if not part:
                raise PreviewError("empty_range")
            body.extend(part)
            self.position += len(part)
        self.served += len(body)
        if self.served > 32 * 1024 * 1024:
            raise PreviewError("served_byte_limit")
        return bytes(body)

    def close(self):
        self.cache.clear()
        super().close()


def decode_first(source, max_pixels=4096 * 2160, max_packets=64):
    import av

    def deny_secondary(*args):
        raise PreviewError("secondary_io_denied")

    # Explicit mov avoids network manifests and secondary demuxers. io_open
    # rejects secondary I/O even if an MP4 references an external resource.
    with av.open(
        source,
        format="mov",
        mode="r",
        buffer_size=32 * 1024,
        options={"protocol_whitelist": "", "probesize": "1048576", "analyzeduration": "1000000"},
        io_open=deny_secondary,
    ) as container:
        if len(container.streams) > 8 or len(container.streams.video) != 1:
            raise PreviewError("stream_limit")
        video = container.streams.video[0]
        width, height = video.codec_context.width, video.codec_context.height
        if (
            width <= 0
            or height <= 0
            or width > 4096
            or height > 4096
            or width * height > max_pixels
        ):
            raise PreviewError("pixel_limit")
        video.codec_context.thread_count = 1
        for index, packet in enumerate(container.demux(video)):
            if index >= max_packets:
                raise PreviewError("packet_limit")
            for frame in packet.decode():
                if (
                    frame.width <= 0
                    or frame.height <= 0
                    or frame.width > 4096
                    or frame.height > 4096
                    or frame.width * frame.height > max_pixels
                ):
                    raise PreviewError("pixel_limit")
                image = frame.to_image(threads=1)
                image.thumbnail((1280, 720))
                out = CappedOutput()
                image.save(out, format="PNG")
                if out.tell() > 4 * 1024 * 1024:
                    raise PreviewError("output_limit")
                return out.getvalue()
        raise PreviewError("no_frame")


def _snapshot_image(data, max_bytes=MAX_IMAGE_BYTES, max_pixels=4096 * 2160):
    if len(data) > max_bytes:
        raise PreviewError("snapshot_byte_limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG")) as image:
            if image.width > 4096 or image.height > 4096 or image.width * image.height > max_pixels:
                raise PreviewError("snapshot_pixel_limit")
            if getattr(image, "n_frames", 1) != 1:
                raise PreviewError("snapshot_frame_limit")
            image.load()
            image.thumbnail((1280, 720))
            return image.convert("RGB")


def validate_snapshot(data, max_bytes=MAX_IMAGE_BYTES, max_pixels=4096 * 2160):
    with _snapshot_image(data, max_bytes, max_pixels) as image:
        return image.size


def normalize_snapshot(data):
    with _snapshot_image(data) as image:
        output = CappedOutput()
        image.save(output, format="PNG")
        return output.getvalue()


def recording_preview(url, budget):
    try:
        with RangeReader(url, budget=budget) as source:
            return decode_first(source)
    except PreviewError as error:
        if str(error) not in {"range_unsupported", "strong_validator_required"}:
            raise
    # Keep all bytes/time/requests already spent by range probing in this budget.
    return decode_first(io.BytesIO(capped_download(url, budget)))


def main():
    # Resource limits supplement the parent watchdog; they are not a syscall,
    # filesystem or credential sandbox. RSS is measured nowhere as an allowance.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_IMAGE_BYTES, MAX_IMAGE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
    import av  # noqa: F401, PLC0415
    import numpy  # noqa: F401, PLC0415

    status = Path("/proc/self/status").read_text(encoding="ascii")
    virtual = int(re.search(r"^VmSize:\s+(\d+)", status, re.M).group(1)) * 1024
    resource.setrlimit(resource.RLIMIT_AS, (virtual + 384 * 1024 * 1024,) * 2)
    # One line of bounded JSON control followed by optional raw image bytes.
    line = sys.stdin.buffer.readline(MAX_CONTROL_BYTES + 1)
    if len(line) > MAX_CONTROL_BYTES or not line.endswith(b"\n"):
        raise PreviewError("control_limit")
    request = json.loads(line)
    budget = Budget(total_seconds=min(12.0, float(request.get("network_seconds", 12))))
    operation = request.get("operation")
    if operation == "recording":
        result = recording_preview(request["url"], budget)
    elif operation == "image_url":
        budget.cap = MAX_IMAGE_BYTES
        result = normalize_snapshot(capped_download(request["url"], budget))
    elif operation == "image":
        data = sys.stdin.buffer.read(MAX_IMAGE_BYTES + 1)
        result = normalize_snapshot(data)
    else:
        raise PreviewError("operation")
    if len(result) > MAX_IMAGE_BYTES or not result.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PreviewError("output_limit")
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # Do not serialize exceptions or signed URLs out of this worker.
        os._exit(2)
