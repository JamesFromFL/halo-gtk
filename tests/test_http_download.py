from __future__ import annotations

import pytest

from halo_gtk import http_download


class FakeResponse:
    def __init__(
        self,
        chunks,
        *,
        url="https://cdn.example.test/video",
        headers=None,
        status_code=200,
    ):
        self._chunks = chunks
        self.url = url
        self.headers = headers or {}
        self.status_code = status_code
        self.raise_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        self.raise_calls += 1

    def iter_content(self, *, chunk_size):
        assert chunk_size == http_download.DOWNLOAD_CHUNK_BYTES
        yield from self._chunks


def test_download_streams_https_to_atomic_destination(monkeypatch, tmp_path):
    response = FakeResponse(
        [b"abc", b"", b"def"],
        headers={"Content-Length": "6"},
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(http_download.requests, "get", fake_get)
    destination = tmp_path / "clip.mp4"
    destination.write_bytes(b"old")

    http_download.download_https("https://example.test/clip", destination)

    assert destination.read_bytes() == b"abcdef"
    assert response.raise_calls == 1
    assert response.closed is True
    assert calls == [
        (
            "https://example.test/clip",
            {
                "stream": True,
                "timeout": http_download.DOWNLOAD_TIMEOUT,
                "allow_redirects": False,
            },
        )
    ]
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/clip",
        "file:///tmp/clip",
        "https:///missing-host",
    ],
)
def test_download_rejects_non_https_or_missing_host(monkeypatch, tmp_path, url):
    monkeypatch.setattr(
        http_download.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("network request should not be made"),
    )

    with pytest.raises(http_download.DownloadError, match="HTTPS"):
        http_download.download_https(url, tmp_path / "clip.mp4")


def test_download_rejects_insecure_redirect(monkeypatch, tmp_path):
    response = FakeResponse(
        [],
        headers={"Location": "http://cdn.example.test/video"},
        status_code=302,
    )
    monkeypatch.setattr(http_download.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(http_download.DownloadError, match="HTTPS"):
        http_download.download_https("https://example.test/clip", tmp_path / "clip.mp4")


def test_download_rejects_declared_oversize_without_replacing_destination(
    monkeypatch,
    tmp_path,
):
    response = FakeResponse([], headers={"Content-Length": "11"})
    monkeypatch.setattr(http_download.requests, "get", lambda *_args, **_kwargs: response)
    destination = tmp_path / "clip.mp4"
    destination.write_bytes(b"old")

    with pytest.raises(http_download.DownloadError, match="size limit"):
        http_download.download_https(
            "https://example.test/clip",
            destination,
            max_bytes=10,
        )

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.part")) == []


def test_download_rejects_streamed_oversize_and_removes_partial(monkeypatch, tmp_path):
    response = FakeResponse([b"123456", b"789012"])
    monkeypatch.setattr(http_download.requests, "get", lambda *_args, **_kwargs: response)
    destination = tmp_path / "clip.mp4"

    with pytest.raises(http_download.DownloadError, match="size limit"):
        http_download.download_https(
            "https://example.test/clip",
            destination,
            max_bytes=10,
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


def test_download_interruption_keeps_existing_destination(monkeypatch, tmp_path):
    def interrupted_chunks():
        yield b"partial"
        raise OSError("connection dropped")

    response = FakeResponse(interrupted_chunks())
    monkeypatch.setattr(http_download.requests, "get", lambda *_args, **_kwargs: response)
    destination = tmp_path / "clip.mp4"
    destination.write_bytes(b"old")

    with pytest.raises(OSError, match="connection dropped"):
        http_download.download_https("https://example.test/clip", destination)

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.part")) == []


def test_download_can_install_with_a_unique_name_without_replacing(monkeypatch, tmp_path):
    response = FakeResponse([b"new"])
    monkeypatch.setattr(http_download.requests, "get", lambda *_args, **_kwargs: response)
    destination = tmp_path / "clip.mp4"
    destination.write_bytes(b"old")

    installed = http_download.download_https(
        "https://example.test/clip",
        destination,
        replace_existing=False,
    )

    assert destination.read_bytes() == b"old"
    assert installed == tmp_path / "clip_2.mp4"
    assert installed.read_bytes() == b"new"
