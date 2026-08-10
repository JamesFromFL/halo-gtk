"""Tests for Ring client helpers."""

import asyncio
import threading

import pytest
import ring_doorbell
from ring_doorbell import AuthenticationError, Requires2FAError

from halo_gtk import ring_client
from halo_gtk.ring_client import RingClient, _extract_event_description, is_ring_session_rejected


class _FakeDevices:
    def __init__(self, devices):
        self.all_devices = devices


class _FakeRing:
    def __init__(self, devices):
        self._devices = devices
        self.updated = False
        self.auth = None

    async def async_update_data(self):
        self.updated = True

    def devices(self):
        return _FakeDevices(self._devices)


class _FakeDevice:
    _next_id = 1

    def __init__(self, name="Front Door", family="doorbots", history=None):
        self.id = _FakeDevice._next_id
        _FakeDevice._next_id += 1
        self.name = name
        self.family = family
        self._history = history or []
        self.recording_url_calls = 0
        self.history_calls = []

    async def async_history(self, limit=50, **kwargs):
        self.history_calls.append({"limit": limit, **kwargs})
        return self._history[:limit]

    async def async_recording_url(self, _event_id):
        self.recording_url_calls += 1
        return None


class _FakeResponse:
    status_code = 200
    content = b"history-snapshot"


class _FakeAuth:
    async def async_query(self, _url):
        return _FakeResponse()


async def _polling_to_thread(function, *args):
    """Run a test worker without relying on sandbox-blocked selector wakeups."""
    result = []
    error = []

    def worker():
        try:
            result.append(function(*args))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.001)
    thread.join()
    if error:
        raise error[0]
    return result[0]


def test_extract_event_description_from_known_top_level_keys():
    event = {"id": 123, "video_description": "A person walked up the driveway."}

    assert _extract_event_description(event) == "A person walked up the driveway."


def test_extract_event_description_from_nested_keys():
    event = {
        "id": 123,
        "metadata": {
            "details": {
                "summary": "A package was delivered.",
            }
        },
    }

    assert _extract_event_description(event) == "A package was delivered."


def test_extract_event_description_from_ring_cv_properties():
    event = {
        "id": 123,
        "cv_properties": {
            "full_description": "A person walked across the yard.",
            "short_description": "Person in yard.",
        },
    }

    assert _extract_event_description(event) == "A person walked across the yard."


def test_extract_event_description_returns_none_when_missing():
    assert _extract_event_description({"id": 123, "kind": "motion"}) is None


def test_is_ring_session_rejected_detects_session_406():
    exc = RuntimeError(
        "HTTP error with status code 406 during query of url "
        "https://api.ring.com/clients_api/session: 406, message='Not Acceptable'"
    )

    assert is_ring_session_rejected(exc) is True


def test_is_ring_session_rejected_ignores_other_errors():
    assert is_ring_session_rejected(RuntimeError("HTTP error with status code 500")) is False


def test_init_client_from_cache_preserves_token_on_session_rejection(monkeypatch):
    cleared = False
    stopped = False

    class FakeRingClient:
        def authenticate_from_token(self, _token):
            raise RuntimeError(
                "HTTP error with status code 406 during query of url "
                "https://api.ring.com/clients_api/session: 406, message='Not Acceptable'"
            )

        def stop(self):
            nonlocal stopped
            stopped = True

    def clear_token():
        nonlocal cleared
        cleared = True

    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_load_token", lambda: {"refresh_token": "saved"})
    monkeypatch.setattr(ring_client, "_clear_token", clear_token)
    monkeypatch.setattr(ring_client, "clear_ring_account_email", clear_token)
    monkeypatch.setattr(ring_client, "clear_ring_event_credentials", clear_token)
    monkeypatch.setattr(ring_client, "RingClient", FakeRingClient)

    assert ring_client.init_client_from_cache() is None
    assert cleared is False
    assert stopped is True


def test_init_client_from_cache_clears_token_on_authentication_error(monkeypatch):
    cleared = False
    stopped = False

    class FakeRingClient:
        def authenticate_from_token(self, _token):
            raise AuthenticationError("expired")

        def stop(self):
            nonlocal stopped
            stopped = True

    def clear_token():
        nonlocal cleared
        cleared = True

    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_load_token", lambda: {"refresh_token": "saved"})
    monkeypatch.setattr(ring_client, "_clear_token", clear_token)
    monkeypatch.setattr(ring_client, "clear_ring_account_email", clear_token)
    monkeypatch.setattr(ring_client, "clear_ring_event_credentials", clear_token)
    monkeypatch.setattr(ring_client, "RingClient", FakeRingClient)

    assert ring_client.init_client_from_cache() is None
    assert cleared is True
    assert stopped is True


def test_account_email_is_cached_after_first_lookup(monkeypatch):
    calls = 0

    def load_email():
        nonlocal calls
        calls += 1
        return "user@example.com"

    monkeypatch.setattr(ring_client, "_account_email_cache", None)
    monkeypatch.setattr(ring_client, "_account_email_loaded", False)
    monkeypatch.setattr(ring_client, "load_ring_account_email", load_email)

    assert ring_client.get_account_email() == "user@example.com"
    assert ring_client.get_account_email() == "user@example.com"
    assert ring_client.get_cached_account_email() == "user@example.com"
    assert calls == 1


def test_derive_listener_state_maps_fcm_run_state():
    from types import SimpleNamespace

    def listener_with(run_state_name):
        run_state = SimpleNamespace(name=run_state_name) if run_state_name is not None else None
        return SimpleNamespace(_receiver=SimpleNamespace(run_state=run_state))

    derive = RingClient._derive_listener_state
    assert derive(listener_with("STARTED")) == ring_client.LISTENER_CONNECTED
    assert derive(listener_with("STOPPED")) == ring_client.LISTENER_OFFLINE
    assert derive(listener_with("RESETTING")) == ring_client.LISTENER_CONNECTING
    # Can't introspect the receiver — assume alive after a successful start.
    assert derive(listener_with(None)) == ring_client.LISTENER_CONNECTED
    assert derive(SimpleNamespace(_receiver=None)) == ring_client.LISTENER_CONNECTED


def test_connection_state_callbacks_register_and_unregister():
    states = []
    cb = states.append
    ring_client.add_connection_state_callback(cb)
    try:
        assert cb in ring_client._connection_callbacks
        ring_client.add_connection_state_callback(cb)  # idempotent
        assert ring_client._connection_callbacks.count(cb) == 1
    finally:
        ring_client.remove_connection_state_callback(cb)
    assert cb not in ring_client._connection_callbacks


def test_init_client_from_cache_loads_token_once(monkeypatch):
    calls = 0
    authenticated_token = None

    class FakeRingClient:
        def authenticate_from_token(self, token):
            nonlocal authenticated_token
            authenticated_token = token

        def stop(self):
            pass

    def load_token():
        nonlocal calls
        calls += 1
        return {"refresh_token": "saved"}

    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_load_token", load_token)
    monkeypatch.setattr(ring_client, "RingClient", FakeRingClient)

    client = ring_client.init_client_from_cache()

    assert client is not None
    assert calls == 1
    assert authenticated_token == {"refresh_token": "saved"}


@pytest.mark.asyncio
async def test_explicit_auth_closes_auth_when_two_factor_is_required(monkeypatch):
    auth_instances = []

    class FakeAuth:
        def __init__(self, _user_agent, _token, token_updater):
            self.token_updater = token_updater
            self.closed = False
            auth_instances.append(self)

        async def async_fetch_token(self, _username, _password, _otp_code):
            raise Requires2FAError

        async def async_close(self):
            self.closed = True

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)

    client = RingClient()
    with pytest.raises(Requires2FAError):
        await client._async_authenticate("user@example.com", "password", None)

    assert auth_instances[0].closed is True
    assert client.is_authenticated is False


@pytest.mark.asyncio
async def test_explicit_auth_persists_token_after_account_validation(monkeypatch):
    saved_tokens = []
    saved_during_update = None

    class FakeAuth:
        def __init__(self, _user_agent, _token, token_updater):
            self.token_updater = token_updater
            self._token = {}
            self.closed = False

        async def async_fetch_token(self, _username, _password, _otp_code):
            self._token = {"refresh_token": "new-token"}
            self.token_updater(self._token)
            return self._token

        def get_hardware_id(self):
            return "fake-hardware"

        async def async_close(self):
            self.closed = True

    class FakeRing:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_data(self):
            nonlocal saved_during_update
            saved_during_update = list(saved_tokens)

        def devices(self):
            return _FakeDevices([])

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)
    monkeypatch.setattr(ring_doorbell, "Ring", FakeRing)
    monkeypatch.setattr(ring_client, "_save_token", saved_tokens.append)

    client = RingClient()
    await client._async_authenticate("user@example.com", "password", "123456")

    assert saved_during_update == []
    assert saved_tokens == [{"refresh_token": "new-token"}]
    assert client.is_authenticated is True
    await client._async_close()


@pytest.mark.asyncio
async def test_explicit_auth_closes_auth_when_account_validation_fails(monkeypatch):
    auth_instances = []

    class FakeAuth:
        def __init__(self, _user_agent, _token, token_updater):
            self.token_updater = token_updater
            self.closed = False
            auth_instances.append(self)

        async def async_fetch_token(self, _username, _password, _otp_code):
            token = {"refresh_token": "new-token"}
            self.token_updater(token)
            return token

        def get_hardware_id(self):
            return "fake-hardware"

        async def async_close(self):
            self.closed = True

    class FakeRing:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_data(self):
            raise RuntimeError("account data unavailable")

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)
    monkeypatch.setattr(ring_doorbell, "Ring", FakeRing)
    monkeypatch.setattr(ring_client, "_save_token", lambda _token: None)

    client = RingClient()
    with pytest.raises(RuntimeError, match="account data unavailable"):
        await client._async_authenticate("user@example.com", "password", None)

    assert auth_instances[0].closed is True
    assert client.is_authenticated is False


@pytest.mark.asyncio
async def test_explicit_auth_closes_auth_when_token_persistence_fails(monkeypatch):
    auth_instances = []

    class FakeAuth:
        def __init__(self, _user_agent, _token, token_updater):
            self.token_updater = token_updater
            self.closed = False
            auth_instances.append(self)

        async def async_fetch_token(self, _username, _password, _otp_code):
            token = {"refresh_token": "new-token"}
            self.token_updater(token)
            return token

        def get_hardware_id(self):
            return "fake-hardware"

        async def async_close(self):
            self.closed = True

    class FakeRing:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_data(self):
            pass

        def devices(self):
            return _FakeDevices([])

    def reject_token(_token):
        raise RuntimeError("secret storage unavailable")

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)
    monkeypatch.setattr(ring_doorbell, "Ring", FakeRing)
    monkeypatch.setattr(ring_client, "_save_token", reject_token)

    client = RingClient()
    with pytest.raises(RuntimeError, match="secret storage unavailable"):
        await client._async_authenticate("user@example.com", "password", None)

    assert auth_instances[0].closed is True
    assert client.is_authenticated is False


@pytest.mark.asyncio
async def test_cached_auth_closes_auth_when_account_validation_fails(monkeypatch):
    auth_instances = []

    class FakeAuth:
        def __init__(self, _user_agent, _token, token_updater):
            self.token_updater = token_updater
            self.closed = False
            auth_instances.append(self)

        async def async_close(self):
            self.closed = True

    class FakeRing:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_data(self):
            raise AuthenticationError("expired")

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)
    monkeypatch.setattr(ring_doorbell, "Ring", FakeRing)

    client = RingClient()
    with pytest.raises(AuthenticationError):
        await client._async_authenticate_from_token({"refresh_token": "saved"})

    assert auth_instances[0].closed is True
    assert client.is_authenticated is False


def test_failed_explicit_login_keeps_existing_session(monkeypatch):
    class ExistingClient:
        def __init__(self):
            self._ring = object()
            self.stopped = False

        @property
        def is_authenticated(self):
            return self._ring is not None

        def authenticate(self, _username, _password, _otp_code):
            raise AssertionError("the active client must not be reused for a new login")

        def stop(self):
            self.stopped = True

    class FailedCandidate:
        def __init__(self):
            self.stopped = False

        def authenticate(self, _username, _password, _otp_code):
            raise AuthenticationError("bad credentials")

        def stop(self):
            self.stopped = True

    existing = ExistingClient()
    candidates = []

    def make_candidate():
        candidate = FailedCandidate()
        candidates.append(candidate)
        return candidate

    monkeypatch.setattr(ring_client, "_client", existing)
    monkeypatch.setattr(ring_client, "RingClient", make_candidate)

    with pytest.raises(AuthenticationError):
        ring_client.init_client("user@example.com", "bad-password")

    assert ring_client.get_client() is existing
    assert existing.is_authenticated is True
    assert existing.stopped is False
    assert candidates[0].stopped is True


def test_slow_cache_restore_cannot_replace_newer_login(monkeypatch):
    restore_started = threading.Event()
    release_restore = threading.Event()
    clients = []
    restore_result = []

    class FakeRingClient:
        def __init__(self):
            self.kind = "restore" if not clients else "login"
            self.stopped = False
            clients.append(self)

        def authenticate_from_token(self, _token):
            restore_started.set()
            assert release_restore.wait(timeout=2)

        def authenticate(self, _username, _password, _otp_code):
            pass

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_load_token", lambda: {"refresh_token": "saved"})
    monkeypatch.setattr(ring_client, "RingClient", FakeRingClient)

    restore_thread = threading.Thread(
        target=lambda: restore_result.append(ring_client.init_client_from_cache())
    )
    restore_thread.start()
    assert restore_started.wait(timeout=2)

    login_client = ring_client.init_client("user@example.com", "password")
    release_restore.set()
    restore_thread.join(timeout=2)

    assert not restore_thread.is_alive()
    assert ring_client.get_client() is login_client
    assert login_client.kind == "login"
    assert clients[0].stopped is True
    assert restore_result == [None]


def test_logout_invalidates_in_progress_cache_restore(monkeypatch):
    restore_started = threading.Event()
    release_restore = threading.Event()
    clients = []

    class FakeRingClient:
        def __init__(self):
            self.stopped = False
            clients.append(self)

        def authenticate_from_token(self, _token):
            restore_started.set()
            assert release_restore.wait(timeout=2)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_load_token", lambda: {"refresh_token": "saved"})
    monkeypatch.setattr(ring_client, "_clear_token", lambda: None)
    monkeypatch.setattr(ring_client, "clear_ring_account_email", lambda: None)
    monkeypatch.setattr(ring_client, "clear_ring_event_credentials", lambda: None)
    monkeypatch.setattr(ring_client, "RingClient", FakeRingClient)

    restore_thread = threading.Thread(target=ring_client.init_client_from_cache)
    restore_thread.start()
    assert restore_started.wait(timeout=2)

    ring_client.logout_client()
    release_restore.set()
    restore_thread.join(timeout=2)

    assert not restore_thread.is_alive()
    assert ring_client.get_client() is None
    assert clients[0].stopped is True


def test_shutdown_invalidates_restore_without_clearing_credentials(monkeypatch):
    restore_started = threading.Event()
    release_restore = threading.Event()
    clients = []
    clear_calls = []

    class FakeRingClient:
        def __init__(self):
            self.stopped = False
            clients.append(self)

        def authenticate_from_token(self, _token):
            restore_started.set()
            assert release_restore.wait(timeout=2)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_load_token", lambda: {"refresh_token": "saved"})
    monkeypatch.setattr(ring_client, "_clear_token", lambda: clear_calls.append("token"))
    monkeypatch.setattr(
        ring_client,
        "clear_ring_account_email",
        lambda: clear_calls.append("email"),
    )
    monkeypatch.setattr(
        ring_client,
        "clear_ring_event_credentials",
        lambda: clear_calls.append("event"),
    )
    monkeypatch.setattr(ring_client, "RingClient", FakeRingClient)

    restore_thread = threading.Thread(target=ring_client.init_client_from_cache)
    restore_thread.start()
    assert restore_started.wait(timeout=2)

    ring_client.shutdown_client()
    release_restore.set()
    restore_thread.join(timeout=2)

    assert not restore_thread.is_alive()
    assert ring_client.get_client() is None
    assert clients[0].stopped is True
    assert clear_calls == []


def test_stale_cache_rejection_cannot_clear_newer_login(monkeypatch):
    restore_started = threading.Event()
    release_restore = threading.Event()
    clients = []
    clear_calls = []

    class FakeRingClient:
        def __init__(self):
            self.kind = "restore" if not clients else "login"
            self.stopped = False
            clients.append(self)

        def authenticate_from_token(self, _token):
            restore_started.set()
            assert release_restore.wait(timeout=2)
            raise AuthenticationError("expired")

        def authenticate(self, _username, _password, _otp_code):
            pass

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(ring_client, "_client", None)
    monkeypatch.setattr(ring_client, "_load_token", lambda: {"refresh_token": "saved"})
    monkeypatch.setattr(ring_client, "_clear_token", lambda: clear_calls.append("token"))
    monkeypatch.setattr(
        ring_client,
        "clear_ring_account_email",
        lambda: clear_calls.append("email"),
    )
    monkeypatch.setattr(
        ring_client,
        "clear_ring_event_credentials",
        lambda: clear_calls.append("event"),
    )
    monkeypatch.setattr(ring_client, "RingClient", FakeRingClient)

    restore_thread = threading.Thread(target=ring_client.init_client_from_cache)
    restore_thread.start()
    assert restore_started.wait(timeout=2)

    login_client = ring_client.init_client("user@example.com", "password")
    release_restore.set()
    restore_thread.join(timeout=2)

    assert not restore_thread.is_alive()
    assert ring_client.get_client() is login_client
    assert clear_calls == []
    assert clients[0].stopped is True


def test_start_client_requires_active_session_ownership(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.starts = 0

        def start(self):
            self.starts += 1

    active = FakeClient()
    stale = FakeClient()
    monkeypatch.setattr(ring_client, "_client", active)

    assert ring_client.start_client(stale) is False
    assert stale.starts == 0
    assert ring_client.start_client(active) is True
    assert active.starts == 1


def test_save_account_email_requires_active_session_ownership(monkeypatch):
    active = object()
    stale = object()
    saved = []
    monkeypatch.setattr(ring_client, "_client", active)
    monkeypatch.setattr(ring_client, "_account_email_cache", None)
    monkeypatch.setattr(ring_client, "_account_email_loaded", True)
    monkeypatch.setattr(ring_client, "save_ring_account_email", saved.append)

    with pytest.raises(ring_client.SessionSupersededError):
        ring_client.save_account_email("stale@example.com", expected_client=stale)

    ring_client.save_account_email("active@example.com", expected_client=active)
    assert saved == ["active@example.com"]


def test_account_switch_clears_event_credentials_and_retires_callbacks(monkeypatch):
    client = RingClient()
    client._published_generation = 21
    client._event_credentials = {"registration": {"token": "old-account"}}
    client._event_credentials_loaded = True
    _generation, listener_revision, _credentials = client._event_listener_context()
    saved_emails = []
    cleared = []
    persisted_events = []
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_session_generation", 21)
    monkeypatch.setattr(ring_client, "_active_client_generation", 21)
    monkeypatch.setattr(ring_client, "_account_email_cache", "old@example.com")
    monkeypatch.setattr(ring_client, "_account_email_loaded", True)
    monkeypatch.setattr(ring_client, "save_ring_account_email", saved_emails.append)
    monkeypatch.setattr(
        ring_client,
        "clear_ring_event_credentials",
        lambda: cleared.append("event"),
    )
    monkeypatch.setattr(
        ring_client,
        "save_ring_event_credentials",
        persisted_events.append,
    )

    ring_client.save_account_email("new@example.com", expected_client=client)
    client._on_event_credentials_updated(
        {"registration": {"token": "late-old-account"}},
        generation=21,
        listener_revision=listener_revision,
    )

    assert saved_emails == ["new@example.com"]
    assert cleared == ["event"]
    assert client._event_credentials is None
    assert client._event_credentials_loaded is True
    assert persisted_events == []


def test_same_account_email_preserves_event_credentials(monkeypatch):
    client = RingClient()
    client._event_credentials = {"registration": {"token": "keep"}}
    client._event_credentials_loaded = True
    saved_emails = []
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_account_email_cache", "Person@Example.com")
    monkeypatch.setattr(ring_client, "_account_email_loaded", True)
    monkeypatch.setattr(ring_client, "save_ring_account_email", saved_emails.append)
    monkeypatch.setattr(
        ring_client,
        "clear_ring_event_credentials",
        lambda: pytest.fail("same-account sign-in must preserve event credentials"),
    )

    ring_client.save_account_email(" person@example.COM ", expected_client=client)

    assert saved_emails == [" person@example.COM "]
    assert client._event_credentials == {"registration": {"token": "keep"}}


def test_logout_and_rejected_cache_clear_event_credentials(monkeypatch):
    client = RingClient()
    client._published_generation = 30
    clear_calls = []
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_session_generation", 30)
    monkeypatch.setattr(ring_client, "_active_client_generation", 30)
    monkeypatch.setattr(ring_client, "_listener_state", ring_client.LISTENER_OFFLINE)
    monkeypatch.setattr(ring_client, "_clear_token", lambda: clear_calls.append("token"))
    monkeypatch.setattr(
        ring_client,
        "clear_ring_account_email",
        lambda: clear_calls.append("email"),
    )
    monkeypatch.setattr(
        ring_client,
        "clear_ring_event_credentials",
        lambda: clear_calls.append("event"),
    )

    assert ring_client.logout_client(expected_client=client) is True
    assert clear_calls == ["token", "email", "event"]

    clear_calls.clear()
    generation = ring_client._begin_session_operation()
    ring_client._clear_rejected_cache_if_current(generation)
    assert clear_calls == ["token", "email", "event"]


@pytest.mark.asyncio
async def test_async_refresh_devices_filters_by_family():
    client = RingClient()
    doorbell = _FakeDevice(name="Front Door", family="doorbots")
    sensor = _FakeDevice(name="Mailbox", family="other")
    fake_ring = _FakeRing([doorbell, sensor])
    client._ring = fake_ring

    devices = await client._async_refresh_devices(frozenset({"doorbots"}))

    assert fake_ring.updated is True
    assert devices == [doorbell]


@pytest.mark.asyncio
async def test_async_refresh_devices_reuses_fresh_account_data():
    import time

    client = RingClient()
    device = _FakeDevice(name="Front Door", family="doorbots")
    fake_ring = _FakeRing([device])
    client._ring = fake_ring
    client._last_device_update_at = time.monotonic()

    devices = await client._async_refresh_devices(None)

    assert fake_ring.updated is False
    assert devices == [device]


@pytest.mark.asyncio
async def test_async_event_history_attaches_device_to_events():
    client = RingClient()
    device = _FakeDevice(history=[{"id": 1}, {"id": 2}])
    client._ring = _FakeRing([device])

    devices, events = await client._async_event_history(limit=50)

    assert devices == [device]
    assert [event["id"] for event in events] == [1, 2]
    assert all(event["_device"] is device for event in events)


@pytest.mark.asyncio
async def test_async_event_history_passes_backend_history_parameters():
    client = RingClient()
    included = _FakeDevice(history=[{"id": 4}])
    skipped = _FakeDevice(history=[{"id": 9}])
    client._ring = _FakeRing([included, skipped])

    _devices, events = await client._async_event_history(
        limit=25,
        kind="motion",
        older_than={included.id: 99},
        device_ids={included.id},
        enforce_limit=True,
    )

    assert [event["id"] for event in events] == [4]
    assert included.history_calls == [
        {
            "limit": 25,
            "kind": "motion",
            "older_than": 99,
            "enforce_limit": True,
        }
    ]
    assert skipped.history_calls == []


@pytest.mark.asyncio
async def test_last_event_frame_prefers_history_snapshot_url():
    client = RingClient()
    device = _FakeDevice(history=[{"id": 1, "snapshot_url": "https://ring/snapshot.jpg"}])
    fake_ring = _FakeRing([device])
    fake_ring.auth = _FakeAuth()
    client._ring = fake_ring

    preview = await client._async_last_event_frame_for_device(device)

    assert preview == b"history-snapshot"
    assert device.recording_url_calls == 0


@pytest.mark.asyncio
async def test_event_preview_for_device_uses_matching_history_event_snapshot():
    client = RingClient()
    device = _FakeDevice(
        history=[
            {"id": 1, "snapshot_url": "https://ring/old.jpg"},
            {"id": 2, "snapshot_url": "https://ring/new.jpg"},
        ]
    )
    fake_ring = _FakeRing([device])
    fake_ring.auth = _FakeAuth()
    client._ring = fake_ring
    event = type("FakeEvent", (), {"id": 2})()

    preview = await client._async_event_preview_for_device(device, 2, event)

    assert preview == b"history-snapshot"
    assert device.recording_url_calls == 0


def test_stopped_client_rejects_submissions_without_restarting_loop():
    client = RingClient()
    client.stop()

    async def work():
        return None

    coroutine = work()
    with pytest.raises(RuntimeError, match="has been stopped"):
        client.submit(coroutine)

    assert coroutine.cr_frame is None
    assert client._loop_thread is None


def test_stale_client_cannot_publish_listener_state(monkeypatch):
    old_client = RingClient()
    new_client = RingClient()
    old_client._published_generation = 7
    new_client._published_generation = 8
    monkeypatch.setattr(ring_client, "_client", new_client)
    monkeypatch.setattr(ring_client, "_session_generation", 8)
    monkeypatch.setattr(ring_client, "_active_client_generation", 8)
    monkeypatch.setattr(ring_client, "_listener_state", ring_client.LISTENER_CONNECTED)

    old_client._set_owned_listener_state(ring_client.LISTENER_OFFLINE)

    assert ring_client.listener_state() == ring_client.LISTENER_CONNECTED


def test_pending_login_does_not_stale_the_still_active_client(monkeypatch):
    from gi.repository import GLib

    active_client = RingClient()
    active_client._published_generation = 7
    scheduled = []
    monkeypatch.setattr(ring_client, "_client", active_client)
    monkeypatch.setattr(ring_client, "_active_client_generation", 7)
    monkeypatch.setattr(ring_client, "_session_generation", 8)
    monkeypatch.setattr(ring_client, "_listener_state", ring_client.LISTENER_OFFLINE)
    monkeypatch.setattr(ring_client, "_connection_callbacks", [])
    monkeypatch.setattr(
        GLib,
        "idle_add",
        lambda callback, *args: scheduled.append((callback, args)) or 1,
    )

    active_client._set_owned_listener_state(ring_client.LISTENER_CONNECTING)
    active_client._on_ring_event(object())

    assert ring_client.listener_state() == ring_client.LISTENER_CONNECTING
    assert len(scheduled) == 1


def test_queued_event_from_superseded_client_is_discarded(monkeypatch):
    from gi.repository import GLib

    from halo_gtk import notifications

    old_client = RingClient()
    old_client._published_generation = 10
    monkeypatch.setattr(ring_client, "_client", old_client)
    monkeypatch.setattr(ring_client, "_session_generation", 10)
    monkeypatch.setattr(ring_client, "_active_client_generation", 10)

    scheduled = []
    callbacks = []
    notifications_sent = []
    monkeypatch.setattr(
        GLib,
        "idle_add",
        lambda callback, *args: scheduled.append((callback, args)) or 1,
    )
    monkeypatch.setattr(notifications, "send_ring_notification", notifications_sent.append)
    old_client.add_event_callback(callbacks.append)

    event = object()
    old_client._on_ring_event(event)
    assert len(scheduled) == 1

    replacement = RingClient()
    replacement._published_generation = 11
    ring_client._client = replacement
    ring_client._session_generation = 11
    ring_client._active_client_generation = 11
    callback, args = scheduled.pop()

    assert callback(*args) is False
    assert notifications_sent == []
    assert callbacks == []


@pytest.mark.asyncio
async def test_listener_reuses_persisted_and_updated_credentials_on_reconnect(
    monkeypatch,
):
    persisted = {"registration": {"token": "persisted", "keys": ["a", "b"]}}
    updated = {"registration": {"token": "updated", "keys": ["c", "d"]}}
    loaded = []
    saved = []
    listeners = []
    client = RingClient()
    client._ring = object()
    client._published_generation = 41
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_session_generation", 41)
    monkeypatch.setattr(ring_client, "_active_client_generation", 41)
    monkeypatch.setattr(client, "_set_owned_listener_state", lambda _state: None)

    def load_credentials():
        loaded.append(True)
        return persisted

    monkeypatch.setattr(ring_client, "load_ring_event_credentials", load_credentials)
    monkeypatch.setattr(ring_client, "save_ring_event_credentials", saved.append)

    async def skip_backoff(_seconds):
        return None

    monkeypatch.setattr(client, "_sleep_or_stop", skip_backoff)

    class FakeConfig:
        @staticmethod
        def default_config():
            return FakeConfig()

    class FakeListener:
        def __init__(
            self,
            _ring,
            credentials=None,
            credentials_updated_callback=None,
            *,
            config=None,
        ):
            self.credentials = credentials
            self.credentials_updated_callback = credentials_updated_callback
            self.config = config
            listeners.append(self)

        def add_notification_callback(self, _callback):
            return None

        async def start(self):
            if len(listeners) == 1:
                self.credentials_updated_callback(updated)
            else:
                client._stop_event.set()
            return False

        async def stop(self):
            return None

    monkeypatch.setattr(ring_doorbell, "RingEventListenerConfig", FakeConfig)
    monkeypatch.setattr(ring_doorbell, "RingEventListener", FakeListener)

    await client._async_listen()

    assert loaded == [True]
    assert len(listeners) == 2
    assert listeners[0].credentials == persisted
    assert listeners[0].credentials is not persisted
    assert listeners[1].credentials == updated
    assert listeners[1].credentials is not updated
    assert saved == [updated]


def test_event_credential_save_failure_is_contained_and_reused(
    monkeypatch,
    caplog,
):
    private_value = "must-not-appear-in-logs"
    updated = {"registration": {"token": private_value}}
    client = RingClient()
    client._published_generation = 42
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_session_generation", 42)
    monkeypatch.setattr(ring_client, "_active_client_generation", 42)
    generation, listener_revision, _credentials = client._event_listener_context()

    def fail_save(_credentials):
        raise RuntimeError(f"failed while storing {private_value}")

    monkeypatch.setattr(ring_client, "save_ring_event_credentials", fail_save)

    client._on_event_credentials_updated(
        updated,
        generation=generation,
        listener_revision=listener_revision,
    )

    assert client._event_credentials == updated
    assert client._event_credentials is not updated
    assert private_value not in caplog.text
    _generation, _revision, reconnect_credentials = client._event_listener_context()
    assert reconnect_credentials == updated


def test_invalid_and_stale_event_credential_callbacks_are_ignored(
    monkeypatch,
    caplog,
):
    client = RingClient()
    client._published_generation = 43
    monkeypatch.setattr(ring_client, "_client", client)
    monkeypatch.setattr(ring_client, "_session_generation", 43)
    monkeypatch.setattr(ring_client, "_active_client_generation", 43)
    saved = []
    monkeypatch.setattr(ring_client, "save_ring_event_credentials", saved.append)
    generation, retired_revision, _credentials = client._event_listener_context()
    _generation, current_revision, _credentials = client._event_listener_context()

    client._on_event_credentials_updated(
        {"private-invalid-value": float("nan")},
        generation=generation,
        listener_revision=current_revision,
    )
    client._on_event_credentials_updated(
        {"registration": {"token": "retired-listener"}},
        generation=generation,
        listener_revision=retired_revision,
    )

    replacement = RingClient()
    replacement._published_generation = 44
    ring_client._client = replacement
    ring_client._session_generation = 44
    ring_client._active_client_generation = 44
    client._on_event_credentials_updated(
        {"registration": {"token": "retired-client"}},
        generation=generation,
        listener_revision=current_revision,
    )

    assert client._event_credentials is None
    assert saved == []
    assert "private-invalid-value" not in caplog.text


@pytest.mark.asyncio
async def test_recording_frame_decode_runs_off_the_ring_event_loop(monkeypatch):
    decode_threads = []
    decoded_urls = []
    event_loop_thread = threading.get_ident()

    def decode(url):
        decode_threads.append(threading.get_ident())
        decoded_urls.append(url)
        return b"decoded-frame"

    class RecordingDevice(_FakeDevice):
        async def async_recording_url(self, _event_id):
            self.recording_url_calls += 1
            return "https://example.test/recording.mp4"

    monkeypatch.setattr(ring_client, "_decode_first_recording_frame_png", decode)
    monkeypatch.setattr(ring_client.asyncio, "to_thread", _polling_to_thread)
    device = RecordingDevice(history=[{"id": 22}])
    client = RingClient()
    client._ring = _FakeRing([device])

    first_task = asyncio.create_task(client._async_first_recording_frame(device, 22))
    while not first_task.done():
        await asyncio.sleep(0.001)
    first = await first_task

    latest_task = asyncio.create_task(client._async_last_event_frame_for_device(device))
    while not latest_task.done():
        await asyncio.sleep(0.001)
    latest = await latest_task

    assert first == b"decoded-frame"
    assert latest == b"decoded-frame"
    assert decoded_urls == [
        "https://example.test/recording.mp4",
        "https://example.test/recording.mp4",
    ]
    assert decode_threads and all(thread_id != event_loop_thread for thread_id in decode_threads)


@pytest.mark.asyncio
async def test_recording_decode_does_not_block_asyncio_progress(monkeypatch):
    decode_started = threading.Event()
    release_decode = threading.Event()

    def decode(_url):
        decode_started.set()
        assert release_decode.wait(timeout=2)
        return b"decoded-frame"

    class RecordingDevice(_FakeDevice):
        async def async_recording_url(self, _event_id):
            return "https://example.test/recording.mp4"

    monkeypatch.setattr(ring_client, "_decode_first_recording_frame_png", decode)
    monkeypatch.setattr(ring_client.asyncio, "to_thread", _polling_to_thread)
    client = RingClient()
    device = RecordingDevice()

    decode_task = asyncio.create_task(client._async_first_recording_frame(device, 1))
    for _ in range(1_000):
        if decode_started.is_set():
            break
        await asyncio.sleep(0.001)
    assert decode_started.is_set()
    await asyncio.sleep(0)
    assert not decode_task.done()
    release_decode.set()

    while not decode_task.done():
        await asyncio.sleep(0.001)
    assert await decode_task == b"decoded-frame"
