"""Redaction of credentials and signed URL data from diagnostic text."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit

_URL_RE = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}"
_SENSITIVE_KEY = (
    r"(?:authorization|access[_-]?token|refresh[_-]?token|oauth[_-]?token|"
    r"id[_-]?token|session[_-]?token|token|password|passwd|client[_-]?secret|"
    r"api[_-]?key|authcode|ticket)"
)
_AUTHORIZATION_RE = re.compile(
    rf"(?i)(\b{_SENSITIVE_KEY}\b\s*[:=]\s*)"
    r"(?:bearer|basic|digest)?\s*[^\s,;}\]]+"
)
_QUOTED_VALUE_RE = re.compile(
    rf"""(?ix)
    (\b{_SENSITIVE_KEY}\b["']?\s*[:=]\s*)
    (["'])
    .*?
    \2
    """
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")


def redact_text(value: object) -> str:
    """Return diagnostic text with common secret representations removed."""
    text = str(value)
    text = _URL_RE.sub(_redact_url_match, text)
    text = _QUOTED_VALUE_RE.sub(r"\1\2<redacted>\2", text)
    text = _AUTHORIZATION_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    return _JWT_RE.sub("<redacted>", text)


class RedactingFormatter(logging.Formatter):
    """Logging formatter that removes secrets from the rendered record."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def _redact_url_match(match: re.Match[str]) -> str:
    raw_url = match.group(0)
    url = raw_url.rstrip(_URL_TRAILING_PUNCTUATION)
    trailing = raw_url[len(url) :]
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if hostname is None:
            return raw_url
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        redacted = urlunsplit(
            (
                parsed.scheme,
                host,
                parsed.path,
                "<redacted>" if parsed.query else "",
                "<redacted>" if parsed.fragment else "",
            )
        )
    except ValueError:
        return "<redacted-url>"
    return redacted + trailing
