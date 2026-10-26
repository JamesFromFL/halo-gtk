"""Bounded HTTPS downloads for Ring recording files."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import aiohttp
import requests
from yarl import URL

DOWNLOAD_TIMEOUT = (10, 30)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class DownloadError(RuntimeError):
    """Raised when a recording download violates Halo's safety policy."""


class DownloadControl(Protocol):
    deadline: float

    def checkpoint(self) -> None: ...
    def begin_commit(self) -> None: ...
    def progress(self, downloaded: int, total: int | None) -> None: ...


async def async_download_https(
    url: str,
    destination: Path,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    replace_existing: bool = True,
    control: DownloadControl | None = None,
    publish: bool = True,
) -> Path:
    """Cancellable native transport for app-owned saves; no Requests worker thread.

    Cancelling its asyncio task interrupts the active HTTP coroutine. DNS resolver
    executor calls are not force-killed. A supplied control shares the whole job's
    deadline and arbitrates cancellation before publishing the final filename.
    """
    current_url = _validated_https_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if control is not None:
        control.checkpoint()
    remaining = max(0.001, control.deadline - time.monotonic()) if control else None
    timeout = aiohttp.ClientTimeout(total=remaining, connect=10, sock_read=30)
    temporary: Path | None = None
    finished = False
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            trust_env=False,
        ) as session:
            for _ in range(MAX_REDIRECTS + 1):
                if control is not None:
                    control.checkpoint()
                async with session.get(
                    URL(current_url, encoded=True), allow_redirects=False
                ) as response:
                    if response.status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise DownloadError("Recording redirect did not include a destination")
                        current_url = _validated_https_url(urljoin(current_url, location))
                        continue
                    response.raise_for_status()
                    _validated_https_url(str(response.url))
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise DownloadError("Encoded recording downloads are not supported")
                    declared = response.headers.get("Content-Length")
                    if declared is not None and (not declared.isascii() or not declared.isdigit()):
                        raise DownloadError("Recording download has an invalid size")
                    try:
                        total = int(declared) if declared is not None else None
                    except ValueError:
                        raise DownloadError("Recording download has an invalid size") from None
                    _validate_declared_size(declared, max_bytes)
                    with tempfile.NamedTemporaryFile(
                        "wb",
                        dir=destination.parent,
                        prefix=f".{destination.stem}-",
                        suffix=".part",
                        delete=False,
                    ) as file:
                        temporary = Path(file.name)
                        downloaded = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if control is not None:
                                control.checkpoint()
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise DownloadError("Recording exceeds the download size limit")
                            file.write(chunk)
                            if control is not None:
                                control.progress(downloaded, total)
                        file.flush()
                        os.fsync(file.fileno())
                    if total is not None and downloaded != total:
                        raise DownloadError("Recording download did not match its declared size")
                    finished = True
                    break
        if not finished:
            raise DownloadError("Recording download exceeded the redirect limit")
        # All awaited HTTP cleanup completes before publication. Cancellation
        # after this fence cannot turn a committed save into a cancelled result.
        if control is not None:
            if publish:
                control.begin_commit()
            else:
                control.checkpoint()
        if replace_existing:
            os.replace(temporary, destination)
            installed = destination
        else:
            installed = _install_unique(temporary, destination)
        temporary = None
        return installed
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_https(
    url: str,
    destination: Path,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    replace_existing: bool = True,
) -> Path:
    """Stream an HTTPS URL to *destination* with bounded, atomic file behavior."""
    current_url = _validated_https_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    for _redirect_count in range(MAX_REDIRECTS + 1):
        with requests.get(
            current_url,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=False,
        ) as response:
            status_code = int(getattr(response, "status_code", 200))
            if status_code in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise DownloadError("Recording redirect did not include a destination")
                current_url = _validated_https_url(urljoin(current_url, location))
                continue

            response.raise_for_status()
            _validated_https_url(getattr(response, "url", current_url))
            _validate_declared_size(response.headers.get("Content-Length"), max_bytes)
            return _write_response_atomically(
                response,
                destination,
                max_bytes,
                replace_existing=replace_existing,
            )

    raise DownloadError("Recording download exceeded the redirect limit")


def _validated_https_url(url: str) -> str:
    value = str(url)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise DownloadError("Recording downloads require a valid HTTPS URL") from None
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise DownloadError("Recording downloads require an HTTPS URL with a host")
    return value


def _validate_declared_size(value: str | None, max_bytes: int) -> None:
    if value is None:
        return
    try:
        declared_size = int(value)
    except (TypeError, ValueError):
        return
    if declared_size > max_bytes:
        raise DownloadError("Recording exceeds the download size limit")


def _write_response_atomically(
    response,
    destination: Path,
    max_bytes: int,
    *,
    replace_existing: bool,
) -> Path:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.stem}-",
            suffix=".part",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            downloaded = 0
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise DownloadError("Recording exceeds the download size limit")
                tmp.write(chunk)
            tmp.flush()
            os.fsync(tmp.fileno())
        if replace_existing:
            os.replace(tmp_path, destination)
            installed_path = destination
        else:
            installed_path = _install_unique(tmp_path, destination)
        tmp_path = None
        return installed_path
    except BaseException:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def _install_unique(source: Path, destination: Path) -> Path:
    for index in range(1, 1_000_000):
        candidate = _numbered_path(destination, index)
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            os.close(descriptor)
            os.replace(source, candidate)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                candidate.unlink()
            raise
        return candidate
    raise OSError("Could not allocate a unique download filename")


def _numbered_path(path: Path, index: int) -> Path:
    if index == 1:
        return path
    return path.with_name(f"{path.stem}_{index}{path.suffix}")
