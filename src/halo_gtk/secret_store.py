"""Secret Service storage for Ring authentication material."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from halo_gtk import APP_ID

_log = logging.getLogger(__name__)

LEGACY_TOKEN_CACHE_PATH = Path.home() / ".local" / "share" / "halo-gtk" / "token.cache"

_SCHEMA_NAME = f"{APP_ID}.Ring"
_ATTRIBUTES = {
    "service": "ring",
    "account": "oauth-token",
}
_EMAIL_ATTRIBUTES = {
    "service": "ring",
    "account": "email",
}
_EVENT_CREDENTIAL_ATTRIBUTES = {
    "service": "ring",
    "account": "event-listener-credentials",
}
_LABEL = "Halo Ring OAuth token"
_EMAIL_LABEL = "Halo Ring account email"
_EVENT_CREDENTIAL_LABEL = "Halo Ring event listener credentials"
_MAX_JSON_NESTING = 64


class SecretStoreError(RuntimeError):
    """Raised when Secret Service storage cannot be used."""


def _secret_module():
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except Exception as exc:
        raise SecretStoreError(
            "Secret Service support is unavailable. Install libsecret and make sure a"
            " Secret Service provider such as GNOME Keyring or KeePassXC is running."
        ) from exc
    return Secret


def _schema():
    Secret = _secret_module()
    return Secret.Schema.new(
        _SCHEMA_NAME,
        Secret.SchemaFlags.NONE,
        {
            "service": Secret.SchemaAttributeType.STRING,
            "account": Secret.SchemaAttributeType.STRING,
        },
    )


def load_ring_token() -> dict[str, Any] | None:
    """Load the Ring OAuth token from Secret Service."""
    Secret = _secret_module()
    raw_token = Secret.password_lookup_sync(_schema(), dict(_ATTRIBUTES), None)
    if raw_token:
        try:
            token = json.loads(raw_token)
        except json.JSONDecodeError as exc:
            _log.warning("Stored Ring token is invalid JSON; clearing it")
            clear_ring_token()
            raise SecretStoreError("Stored Ring token is invalid") from exc
        if not isinstance(token, dict):
            clear_ring_token()
            raise SecretStoreError("Stored Ring token has an invalid format")
        return token

    return _migrate_legacy_token_cache()


def save_ring_token(token: dict[str, Any]) -> None:
    """Store the Ring OAuth token in Secret Service."""
    Secret = _secret_module()
    raw_token = json.dumps(token, separators=(",", ":"), sort_keys=True)
    stored = Secret.password_store_sync(
        _schema(),
        dict(_ATTRIBUTES),
        Secret.COLLECTION_DEFAULT,
        _LABEL,
        raw_token,
        None,
    )
    if not stored:
        raise SecretStoreError("Secret Service refused to store the Ring token")
    LEGACY_TOKEN_CACHE_PATH.unlink(missing_ok=True)
    _log.debug("Ring token stored in Secret Service")


def clear_ring_token() -> None:
    """Remove the Ring OAuth token from Secret Service and delete legacy plaintext cache."""
    Secret = _secret_module()
    Secret.password_clear_sync(_schema(), dict(_ATTRIBUTES), None)
    LEGACY_TOKEN_CACHE_PATH.unlink(missing_ok=True)


def load_ring_account_email() -> str | None:
    """Load the signed-in Ring account email from Secret Service."""
    Secret = _secret_module()
    email = Secret.password_lookup_sync(_schema(), dict(_EMAIL_ATTRIBUTES), None)
    return email or None


def save_ring_account_email(email: str) -> None:
    """Store the signed-in Ring account email in Secret Service."""
    Secret = _secret_module()
    stored = Secret.password_store_sync(
        _schema(),
        dict(_EMAIL_ATTRIBUTES),
        Secret.COLLECTION_DEFAULT,
        _EMAIL_LABEL,
        email,
        None,
    )
    if not stored:
        raise SecretStoreError("Secret Service refused to store the Ring account email")


def clear_ring_account_email() -> None:
    """Remove the signed-in Ring account email from Secret Service."""
    Secret = _secret_module()
    Secret.password_clear_sync(_schema(), dict(_EMAIL_ATTRIBUTES), None)


def validate_ring_event_credentials(credentials: Any) -> dict[str, Any]:
    """Return a detached, JSON-safe event-listener credential object."""
    try:
        validated = _copy_json_value(credentials, depth=0, ancestors=set())
        if not isinstance(validated, dict):
            raise ValueError("root value is not an object")
        # Keep this final encoding check close to storage so future accepted
        # value types cannot accidentally exceed JSON's data model.
        json.dumps(validated, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (RecursionError, TypeError, ValueError) as exc:
        raise SecretStoreError("Ring event credentials have an invalid format") from exc
    return validated


def load_ring_event_credentials() -> dict[str, Any] | None:
    """Load the FCM registration credentials from their own Secret Service item."""
    Secret = _secret_module()
    raw_credentials = Secret.password_lookup_sync(
        _schema(),
        dict(_EVENT_CREDENTIAL_ATTRIBUTES),
        None,
    )
    if raw_credentials is None:
        return None

    try:
        credentials = json.loads(
            raw_credentials,
            parse_constant=_reject_json_constant,
        )
        return validate_ring_event_credentials(credentials)
    except (json.JSONDecodeError, RecursionError, SecretStoreError, TypeError, ValueError) as exc:
        _log.warning("Stored Ring event credentials are invalid; clearing them")
        try:
            clear_ring_event_credentials()
        except Exception as clear_exc:
            raise SecretStoreError(
                "Stored Ring event credentials are invalid and could not be cleared"
            ) from clear_exc
        raise SecretStoreError("Stored Ring event credentials are invalid") from exc


def save_ring_event_credentials(credentials: Any) -> None:
    """Store deeply validated FCM registration credentials in Secret Service."""
    validated = validate_ring_event_credentials(credentials)
    raw_credentials = json.dumps(
        validated,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    Secret = _secret_module()
    stored = Secret.password_store_sync(
        _schema(),
        dict(_EVENT_CREDENTIAL_ATTRIBUTES),
        Secret.COLLECTION_DEFAULT,
        _EVENT_CREDENTIAL_LABEL,
        raw_credentials,
        None,
    )
    if not stored:
        raise SecretStoreError("Secret Service refused to store Ring event credentials")


def clear_ring_event_credentials() -> None:
    """Remove the FCM registration credentials from Secret Service."""
    Secret = _secret_module()
    Secret.password_clear_sync(_schema(), dict(_EVENT_CREDENTIAL_ATTRIBUTES), None)


def _copy_json_value(value: Any, *, depth: int, ancestors: set[int]) -> Any:
    if depth > _MAX_JSON_NESTING:
        raise ValueError("JSON value is nested too deeply")
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return value
    if not isinstance(value, (dict, list)):
        raise TypeError("value is outside JSON's data model")

    identity = id(value)
    if identity in ancestors:
        raise ValueError("JSON value contains a cycle")
    ancestors.add(identity)
    try:
        if isinstance(value, list):
            return [_copy_json_value(item, depth=depth + 1, ancestors=ancestors) for item in value]

        copied = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            copied[key] = _copy_json_value(
                item,
                depth=depth + 1,
                ancestors=ancestors,
            )
        return copied
    finally:
        ancestors.remove(identity)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("JSON number must be finite")


def _migrate_legacy_token_cache() -> dict[str, Any] | None:
    if not LEGACY_TOKEN_CACHE_PATH.exists():
        return None

    try:
        token = json.loads(LEGACY_TOKEN_CACHE_PATH.read_text())
    except Exception as exc:
        LEGACY_TOKEN_CACHE_PATH.unlink(missing_ok=True)
        _log.warning("Deleted unreadable legacy plaintext Ring token cache: %s", exc)
        return None

    if not isinstance(token, dict):
        LEGACY_TOKEN_CACHE_PATH.unlink(missing_ok=True)
        _log.warning("Discarded legacy plaintext Ring token with an invalid format")
        return None

    try:
        # save_ring_token() deletes the legacy cache on success and raises
        # (leaving it in place) when Secret Service is unavailable.
        save_ring_token(token)
        _log.info("Migrated legacy plaintext Ring token cache to Secret Service")
    except SecretStoreError as exc:
        _log.warning(
            "Could not move the legacy Ring token into Secret Service; keeping the"
            " cache to retry next launch: %s",
            exc,
        )
    return token
