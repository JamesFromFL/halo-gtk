"""Tests for Secret Service Ring token storage."""

import json

import pytest

from halo_gtk import secret_store


class _FakeSecret:
    COLLECTION_DEFAULT = "default"

    class SchemaFlags:
        NONE = 0

    class SchemaAttributeType:
        STRING = 0

    class Schema:
        @staticmethod
        def new(name, flags, attributes):
            return (name, flags, attributes)

    def __init__(self):
        self.passwords = {}
        self.cleared = False

    def password_lookup_sync(self, schema, attributes, cancellable):
        return self.passwords.get(tuple(sorted(attributes.items())))

    def password_store_sync(self, schema, attributes, collection, label, password, cancellable):
        self.passwords[tuple(sorted(attributes.items()))] = password
        return True

    def password_clear_sync(self, schema, attributes, cancellable):
        self.passwords.pop(tuple(sorted(attributes.items())), None)
        self.cleared = True
        return True


def test_save_and_load_ring_token(monkeypatch, tmp_path):
    fake_secret = _FakeSecret()
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)
    monkeypatch.setattr(secret_store, "LEGACY_TOKEN_CACHE_PATH", tmp_path / "token.cache")

    secret_store.save_ring_token({"refresh_token": "abc", "expires_in": 3600})

    assert secret_store.load_ring_token() == {"expires_in": 3600, "refresh_token": "abc"}


def test_load_ring_token_migrates_and_deletes_legacy_plaintext_cache(monkeypatch, tmp_path):
    fake_secret = _FakeSecret()
    legacy_cache = tmp_path / "token.cache"
    legacy_cache.write_text(json.dumps({"refresh_token": "legacy"}))
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)
    monkeypatch.setattr(secret_store, "LEGACY_TOKEN_CACHE_PATH", legacy_cache)

    assert secret_store.load_ring_token() == {"refresh_token": "legacy"}
    assert not legacy_cache.exists()


def test_clear_ring_token_removes_secret_and_legacy_plaintext_cache(monkeypatch, tmp_path):
    fake_secret = _FakeSecret()
    fake_secret.password_store_sync(
        None,
        {"service": "ring", "account": "oauth-token"},
        None,
        None,
        json.dumps({"refresh_token": "abc"}),
        None,
    )
    legacy_cache = tmp_path / "token.cache"
    legacy_cache.write_text(json.dumps({"refresh_token": "legacy"}))
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)
    monkeypatch.setattr(secret_store, "LEGACY_TOKEN_CACHE_PATH", legacy_cache)

    secret_store.clear_ring_token()

    assert fake_secret.cleared is True
    assert secret_store.load_ring_token() is None
    assert not legacy_cache.exists()


def test_save_load_and_clear_ring_account_email(monkeypatch, tmp_path):
    fake_secret = _FakeSecret()
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)
    monkeypatch.setattr(secret_store, "LEGACY_TOKEN_CACHE_PATH", tmp_path / "token.cache")

    secret_store.save_ring_token({"refresh_token": "abc"})
    secret_store.save_ring_account_email("person@example.com")

    assert secret_store.load_ring_token() == {"refresh_token": "abc"}
    assert secret_store.load_ring_account_email() == "person@example.com"

    secret_store.clear_ring_account_email()

    assert secret_store.load_ring_token() == {"refresh_token": "abc"}
    assert secret_store.load_ring_account_email() is None


def test_event_credentials_use_separate_secret_and_return_detached_data(
    monkeypatch,
    tmp_path,
):
    fake_secret = _FakeSecret()
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)
    monkeypatch.setattr(secret_store, "LEGACY_TOKEN_CACHE_PATH", tmp_path / "token.cache")
    credentials = {
        "fcm": {
            "installation": ["one", {"enabled": True}],
            "expires": 3600.5,
        }
    }

    secret_store.save_ring_token({"refresh_token": "oauth"})
    secret_store.save_ring_event_credentials(credentials)
    credentials["fcm"]["installation"][1]["enabled"] = False

    assert secret_store.load_ring_token() == {"refresh_token": "oauth"}
    assert secret_store.load_ring_event_credentials() == {
        "fcm": {
            "installation": ["one", {"enabled": True}],
            "expires": 3600.5,
        }
    }

    secret_store.clear_ring_event_credentials()

    assert secret_store.load_ring_event_credentials() is None
    assert secret_store.load_ring_token() == {"refresh_token": "oauth"}


@pytest.mark.parametrize(
    "raw_credentials",
    [
        "",
        "not-json",
        '["not", "an", "object"]',
        '{"nested":{"number":NaN}}',
        '{"nested":{"number":Infinity}}',
    ],
)
def test_invalid_stored_event_credentials_are_rejected_and_cleared(
    monkeypatch,
    tmp_path,
    raw_credentials,
):
    fake_secret = _FakeSecret()
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)
    monkeypatch.setattr(secret_store, "LEGACY_TOKEN_CACHE_PATH", tmp_path / "token.cache")
    secret_store.save_ring_token({"refresh_token": "oauth"})
    event_key = tuple(sorted(secret_store._EVENT_CREDENTIAL_ATTRIBUTES.items()))
    fake_secret.passwords[event_key] = raw_credentials

    with pytest.raises(secret_store.SecretStoreError, match="invalid"):
        secret_store.load_ring_event_credentials()

    assert event_key not in fake_secret.passwords
    assert secret_store.load_ring_token() == {"refresh_token": "oauth"}


@pytest.mark.parametrize(
    "credentials",
    [
        {"nested": {"number": float("nan")}},
        {"nested": {"number": float("inf")}},
        {"nested": {1: "non-string key"}},
        {"nested": b"not-json"},
        ["not", "an", "object"],
    ],
)
def test_save_event_credentials_rejects_invalid_nested_data(
    monkeypatch,
    credentials,
):
    fake_secret = _FakeSecret()
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)

    with pytest.raises(secret_store.SecretStoreError, match="invalid format"):
        secret_store.save_ring_event_credentials(credentials)

    event_key = tuple(sorted(secret_store._EVENT_CREDENTIAL_ATTRIBUTES.items()))
    assert event_key not in fake_secret.passwords


def test_save_event_credentials_rejects_cycles(monkeypatch):
    fake_secret = _FakeSecret()
    monkeypatch.setattr(secret_store, "_secret_module", lambda: fake_secret)
    credentials = {"nested": []}
    credentials["nested"].append(credentials)

    with pytest.raises(secret_store.SecretStoreError, match="invalid format"):
        secret_store.save_ring_event_credentials(credentials)
