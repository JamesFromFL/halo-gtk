"""Credential/profile binding uses a fake Secret Service, never real secrets."""

import json

import pytest

from halo_gtk import secret_store as store


class FakeSecret:
    COLLECTION_DEFAULT = "default"

    def __init__(self):
        self.values = {}
        self.refuse = False

    def password_lookup_sync(self, _schema, attributes, _cancel):
        return self.values.get(tuple(sorted(attributes.items())))

    def password_store_sync(self, _schema, attributes, _collection, _label, value, _cancel):
        if self.refuse:
            return False
        self.values[tuple(sorted(attributes.items()))] = value
        return True

    def password_clear_sync(self, _schema, attributes, _cancel):
        self.values.pop(tuple(sorted(attributes.items())), None)
        return True


@pytest.fixture
def secret(monkeypatch, tmp_path):
    value = FakeSecret()
    monkeypatch.setattr(store, "_secret_module", lambda: value)
    monkeypatch.setattr(store, "_schema", lambda: "synthetic")
    monkeypatch.setattr(store, "LEGACY_TOKEN_CACHE_PATH", tmp_path / "legacy")
    return value


def test_bound_token_roundtrip_has_only_oauth_keys(secret):
    profile = store.history_profile_for_account("first@example.invalid")
    token = {"refresh_token": "synthetic", "expires_in": 3600}
    store.save_ring_token(token, history_profile=profile)
    loaded = store.load_ring_token()
    assert loaded == token
    assert loaded.history_profile == profile
    assert dict(loaded) == token
    raw = json.loads(secret.values[tuple(sorted(store._ATTRIBUTES.items()))])
    assert all(raw[key] == value for key, value in token.items())
    assert raw[store._HISTORY_METADATA_KEY] == {"version": 1, "profile": profile}


def test_profile_reused_only_for_same_normalized_authenticated_account(secret):
    first = store.history_profile_for_account(" First@example.invalid ")
    assert store.history_profile_for_account("first@EXAMPLE.invalid") == first
    assert store.history_profile_for_account("other@example.invalid") != first
    store.save_ring_token({"refresh_token": "synthetic"}, history_profile=first)
    store.clear_ring_token()
    assert store.load_ring_token() is None
    assert store.history_profile_for_account("first@example.invalid") == first


def test_refresh_preserves_binding_and_legacy_restore_remains_unbound(secret):
    store.save_ring_token({"refresh_token": "old"})
    assert getattr(store.load_ring_token(), "history_profile", None) is None
    profile = store.history_profile_for_account("first@example.invalid")
    store.save_ring_token({"refresh_token": "one"}, history_profile=profile)
    store.save_ring_token({"refresh_token": "two"}, history_profile=profile)
    assert store.load_ring_token().history_profile == profile
    assert store.load_ring_token()["refresh_token"] == "two"


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "../escape"},
        {"version": 2},
        {"version": True},
        {"profile": None},
        {"unexpected": True},
    ],
)
def test_invalid_optional_binding_does_not_grant_history_or_force_logout(secret, change):
    metadata = {"version": 1, "profile": "a" * 32}
    metadata.update(change)
    envelope = {"refresh_token": "synthetic", store._HISTORY_METADATA_KEY: metadata}
    secret.values[tuple(sorted(store._ATTRIBUTES.items()))] = json.dumps(envelope)
    loaded = store.load_ring_token()
    assert loaded == {"refresh_token": "synthetic"}
    assert loaded.history_profile is None


def test_previous_loader_and_installed_oauth_client_can_read_top_level_credentials(secret):
    from oauthlib.oauth2 import LegacyApplicationClient

    token = {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "token_type": "Bearer",
        "expires_at": 4102444700,
    }
    store.save_ring_token(token, history_profile="a" * 32)
    # Previous Halo releases deserialize this exact item directly as a dict.
    legacy_token = json.loads(secret.values[tuple(sorted(store._ATTRIBUTES.items()))])
    client = LegacyApplicationClient("synthetic-client", token=legacy_token)
    assert client.access_token == token["access_token"]
    assert client.refresh_token == token["refresh_token"]
    assert (
        client.add_token("https://example.invalid")[1]["Authorization"] == "Bearer synthetic-access"
    )
    # A previous release refreshing and saving ordinary OAuth keys loses only
    # the optional binding; a later release can still restore the Ring session.
    store.save_ring_token(token)
    assert store.load_ring_token() == token
    assert getattr(store.load_ring_token(), "history_profile", None) is None


def test_unbound_save_cannot_import_binding_from_api_token_metadata(secret):
    store.save_ring_token(
        {
            "refresh_token": "synthetic",
            store._HISTORY_METADATA_KEY: {"version": 1, "profile": "a" * 32},
        }
    )
    assert getattr(store.load_ring_token(), "history_profile", None) is None


@pytest.mark.parametrize(
    "raw",
    ["not-json", "[]", '{"name":"../bad"}', '{"one":"' + "a" * 32 + '","two":"' + "a" * 32 + '"}'],
)
def test_corrupt_profile_map_fails_without_overwriting_other_account(secret, raw):
    key = tuple(sorted(store._PROFILE_ATTRIBUTES.items()))
    secret.values[key] = raw
    with pytest.raises(store.SecretStoreError):
        store.history_profile_for_account("first@example.invalid")
    assert secret.values[key] == raw


def test_bounded_profile_mapping_and_save_failure(secret):
    values = [
        store.history_profile_for_account(f"account{i}@example.invalid")
        for i in range(store._MAX_HISTORY_PROFILES)
    ]
    with pytest.raises(store.SecretStoreError, match="limit"):
        store.history_profile_for_account("overflow@example.invalid")
    assert store.history_profile_for_account("account0@example.invalid") == values[0]
    secret.refuse = True
    with pytest.raises(store.SecretStoreError, match="refused"):
        store.save_ring_token({"refresh_token": "synthetic"}, history_profile=values[0])


@pytest.mark.parametrize("account", [None, "", " ", "bad\naccount", "x" * 257])
def test_invalid_account_identity_is_rejected(secret, account):
    with pytest.raises(store.SecretStoreError):
        store.history_profile_for_account(account)
