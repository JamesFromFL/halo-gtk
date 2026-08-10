"""Bounded HTTPS downloads for Ring recording files."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

DOWNLOAD_TIMEOUT = (10, 30)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class DownloadError(RuntimeError):
    """Raised when a recording download violates Halo's safety policy."""


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
    parsed = urlsplit(str(url))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise DownloadError("Recording downloads require an HTTPS URL with a host")
    return str(url)


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
