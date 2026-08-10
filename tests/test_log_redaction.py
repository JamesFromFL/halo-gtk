"""Tests for logging-time secret redaction."""

import logging

from halo_gtk.log_redaction import RedactingFormatter, redact_text


def test_redact_text_removes_url_credentials_query_and_fragment():
    redacted = redact_text(
        "open https://user:pass@[2001:db8::1]:443/video?sig=secret#access-token now"
    )

    assert redacted == "open https://[2001:db8::1]:443/video?<redacted>#<redacted> now"


def test_redact_text_removes_alarm_websocket_credentials():
    redacted = redact_text(
        "connect wss://alarm.example.test/ws?authcode=socket-secret "
        '{"ticket": "ticket-secret", "authcode": "auth-secret"}'
    )

    assert "socket-secret" not in redacted
    assert "ticket-secret" not in redacted
    assert "auth-secret" not in redacted
    assert redacted.count("<redacted>") == 3


def test_redact_text_removes_common_token_fields():
    redacted = redact_text(
        """{"access_token": "access-secret", "client_secret": "client-secret"} """
        "api_key=key-secret Authorization: Basic auth-secret"
    )

    assert "access-secret" not in redacted
    assert "client-secret" not in redacted
    assert "key-secret" not in redacted
    assert "auth-secret" not in redacted
    assert redacted.count("<redacted>") == 4


def test_redacting_formatter_sanitizes_rendered_log_arguments():
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord(
        "halo_gtk.test",
        logging.WARNING,
        __file__,
        1,
        "request failed: %s",
        ("https://example.test/path?oauth_token=secret",),
        None,
    )

    rendered = formatter.format(record)

    assert rendered == "WARNING request failed: https://example.test/path?<redacted>"
