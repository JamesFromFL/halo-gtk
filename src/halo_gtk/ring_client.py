"""Ring API integration layer.

Wraps python-ring-doorbell (0.9.x async API) and provides a synchronous
interface for use from GTK main-thread code.

Architecture
------------
A single asyncio event loop runs in a persistent daemon thread.  All
ring-doorbell coroutines are submitted to that loop via
``asyncio.run_coroutine_threadsafe`` and awaited synchronously from the
calling thread.  The FCM event listener also runs as a long-lived async
task on the same loop.

ring-doorbell manages its own aiohttp ClientSession internally (created
lazily in Auth._session on first use).  We do not inject a custom session —
doing so caused 406 Not Acceptable on /clients_api/session because the
default Accept header we were adding conflicted with Ring's CDN/load balancer
behaviour on that endpoint.

Usage
-----
    from halo_gtk.ring_client import get_client, init_client, init_client_from_cache

    # Fresh login (raises Requires2FAError if 2FA needed)
    client = init_client(email, password)

    # Second attempt after 2FA prompt
    client = init_client(email, password, otp_code="123456")

    # Restore from cache on startup (returns None if no cache)
    client = init_client_from_cache()

    client.start()   # start FCM event listener
    client.stop()    # call on app shutdown
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import io
import logging
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from halo_gtk.secret_store import (
    clear_ring_account_email,
    clear_ring_event_credentials,
    clear_ring_token,
    load_ring_account_email,
    load_ring_event_credentials,
    load_ring_token,
    save_ring_account_email,
    save_ring_event_credentials,
    save_ring_token,
    validate_ring_event_credentials,
)

_log = logging.getLogger(__name__)

_client: RingClient | None = None
_session_lock = threading.RLock()
_session_generation = 0
_active_client_generation = 0
_account_email_cache: str | None = None
_account_email_loaded = False
_account_email_lock = threading.Lock()

_APP_USER_AGENT = "android:com.ringapp"
_DEFAULT_CALL_TIMEOUT = 90
_LISTENER_STOP_TIMEOUT = 4
_ASYNC_CLOSE_TIMEOUT = 5
_LOOP_JOIN_TIMEOUT = 5
_LOOP_START_TIMEOUT = 5
_DEVICE_REFRESH_TTL = 8
_LISTENER_MONITOR_INTERVAL = 2
_LISTENER_RECONNECT_MIN = 5
_LISTENER_RECONNECT_MAX = 120

# Real-time event-listener connection state, surfaced to the UI for an
# "events offline / reconnecting" indicator. Module-level so subscribers survive
# the client being recreated on re-login.
LISTENER_CONNECTING = "connecting"
LISTENER_CONNECTED = "connected"
LISTENER_OFFLINE = "offline"

_listener_state = LISTENER_OFFLINE
_connection_callbacks: list = []


class SessionSupersededError(RuntimeError):
    """Raised when a newer login or logout invalidates session work."""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def get_client() -> RingClient | None:
    """Return the active RingClient, or None if not yet initialised."""
    with _session_lock:
        return _client


def _begin_session_operation() -> int:
    global _session_generation
    with _session_lock:
        _session_generation += 1
        return _session_generation


def _new_session_candidate() -> RingClient:
    client = RingClient()
    defer_persistence = getattr(client, "_defer_auth_token_persistence", None)
    if callable(defer_persistence):
        defer_persistence()
    return client


def _save_token_for_client(client: RingClient, generation: int, token: dict) -> None:
    with _session_lock:
        if generation != _session_generation and _client is not client:
            raise SessionSupersededError("Ring session was superseded")
        _save_token(token)


def _publish_session_candidate(
    client: RingClient,
    generation: int,
) -> tuple[bool, RingClient | None]:
    global _active_client_generation, _client
    with _session_lock:
        if generation != _session_generation:
            return False, None

        activate_persistence = getattr(client, "_activate_auth_token_persistence", None)
        if callable(activate_persistence):
            activate_persistence(lambda token: _save_token_for_client(client, generation, token))

        previous = _client
        _active_client_generation = generation
        client._published_generation = generation
        _client = client
        return True, previous


def _stop_client_quietly(client: RingClient | None) -> None:
    if client is None:
        return
    try:
        client.stop()
    except Exception as exc:
        _log.debug("Failed to stop discarded Ring client: %s", exc)


def _clear_rejected_cache_if_current(generation: int) -> None:
    global _account_email_cache, _account_email_loaded
    with _session_lock:
        active = _client
        if generation != _session_generation or (active is not None and active.is_authenticated):
            return

        for clear_secret in (
            _clear_token,
            clear_ring_account_email,
            clear_ring_event_credentials,
        ):
            try:
                clear_secret()
            except Exception as exc:
                _log.debug("Failed to clear rejected Ring credentials: %s", exc)
        with _account_email_lock:
            _account_email_cache = None
            _account_email_loaded = True


def get_account_email() -> str | None:
    """Return the signed-in Ring account email saved for display, if any."""
    global _account_email_cache, _account_email_loaded
    if _account_email_loaded:
        return _account_email_cache

    with _account_email_lock:
        if _account_email_loaded:
            return _account_email_cache
        _account_email_cache = load_ring_account_email()
        _account_email_loaded = True
        return _account_email_cache


def get_cached_account_email() -> str | None:
    """Return the in-memory account email without touching Secret Service."""
    return _account_email_cache


def warm_account_email_cache() -> str | None:
    """Populate the in-memory account email cache from Secret Service."""
    return get_account_email()


def listener_state() -> str:
    """Return the current real-time event-listener connection state."""
    return _listener_state


def add_connection_state_callback(callback) -> None:
    """Register a callback invoked (GTK main thread) with the new listener state."""
    if callback not in _connection_callbacks:
        _connection_callbacks.append(callback)


def remove_connection_state_callback(callback) -> None:
    with contextlib.suppress(ValueError):
        _connection_callbacks.remove(callback)


def _is_active_client_generation(client: RingClient, generation: int | None) -> bool:
    """Return whether *client* still owns *generation* of the global session."""
    if generation is None:
        return False
    with _session_lock:
        return (
            _client is client
            and _active_client_generation == generation
            and client._published_generation == generation
        )


def _set_listener_state(
    state: str,
    *,
    expected_client: RingClient | None = None,
    generation: int | None = None,
) -> None:
    global _listener_state
    with _session_lock:
        if generation is None:
            generation = _active_client_generation
        if generation != _active_client_generation:
            return
        if expected_client is not None and (
            _client is not expected_client or expected_client._published_generation != generation
        ):
            return
        if state == _listener_state:
            return
        _listener_state = state
        callbacks = list(_connection_callbacks)
    from gi.repository import GLib

    for callback in callbacks:
        GLib.idle_add(_dispatch_connection_state, callback, state, generation)


def _dispatch_connection_state(callback, state: str, generation: int) -> bool:
    with _session_lock:
        if generation != _active_client_generation:
            return False
    try:
        callback(state)
    except Exception as exc:
        _log.debug("Connection-state callback error: %s", exc)
    return False


def logout_client(*, expected_client: RingClient | None = None) -> bool:
    """Clear local Ring authentication state and stop the active client."""
    global _active_client_generation, _client, _session_generation
    global _account_email_cache, _account_email_loaded
    clear_error = None
    with _session_lock:
        if expected_client is not None and _client is not expected_client:
            return False
        _session_generation += 1
        _active_client_generation = _session_generation
        generation = _active_client_generation
        client = _client
        _client = None

        for clear_secret in (
            _clear_token,
            clear_ring_account_email,
            clear_ring_event_credentials,
        ):
            try:
                clear_secret()
            except Exception as exc:
                clear_error = clear_error or exc
        with _account_email_lock:
            _account_email_cache = None
            _account_email_loaded = True

    _set_listener_state(LISTENER_OFFLINE, generation=generation)
    _stop_client_quietly(client)
    if clear_error is not None:
        raise clear_error
    return True


def shutdown_client() -> None:
    """Invalidate session work and stop the active client without clearing secrets."""
    global _active_client_generation, _client, _session_generation
    with _session_lock:
        _session_generation += 1
        _active_client_generation = _session_generation
        generation = _active_client_generation
        client = _client
        _client = None

    _set_listener_state(LISTENER_OFFLINE, generation=generation)
    _stop_client_quietly(client)


def init_client(
    username: str,
    password: str,
    otp_code: str | None = None,
) -> RingClient:
    """Authenticate and return a RingClient singleton.

    Raises ``ring_doorbell.Requires2FAError`` if the account requires 2FA
    and *otp_code* was not supplied.  Call again with the code to complete.
    Raises ``ring_doorbell.AuthenticationError`` on bad credentials.
    """
    generation = _begin_session_operation()
    client = _new_session_candidate()
    try:
        client.authenticate(username, password, otp_code)
        published, previous = _publish_session_candidate(client, generation)
    except BaseException:
        _stop_client_quietly(client)
        raise

    if not published:
        _stop_client_quietly(client)
        raise SessionSupersededError("A newer Ring login or logout completed")

    if previous is not client:
        _stop_client_quietly(previous)
    return client


def init_client_from_cache() -> RingClient | None:
    """Restore a RingClient from a cached token without re-authenticating.

    Returns None if no token cache exists or if the cached token is invalid.
    """
    generation = _begin_session_operation()
    client = None
    try:
        token = _load_token()
        if token is None:
            return None
        client = _new_session_candidate()
        client.authenticate_from_token(token)
        published, previous = _publish_session_candidate(client, generation)
        if not published:
            _stop_client_quietly(client)
            return None
        if previous is not client:
            _stop_client_quietly(previous)
        _log.info("Restored Ring session from cache")
        return client
    except Exception as exc:
        _stop_client_quietly(client)
        if _should_clear_cached_token(exc):
            _log.warning("Cache restore failed (%s) — will require fresh login", exc)
            _clear_rejected_cache_if_current(generation)
        else:
            _log.warning("Cache restore failed (%s) — keeping saved token", exc)
        return None


def start_client(client: RingClient) -> bool:
    """Start events only if *client* still owns the active session."""
    with _session_lock:
        if _client is not client:
            return False
        client.start()
        return True


def _decode_first_recording_frame_png(url: str) -> bytes:
    """Decode one recording frame synchronously for use from a worker thread."""
    import av
    from PIL import Image

    container = av.open(url, options={"timeout": "5000000"})
    try:
        frame = next(container.decode(video=0))
        array = frame.to_ndarray(format="rgb24")
    finally:
        container.close()

    output = io.BytesIO()
    Image.fromarray(array).save(output, format="PNG")
    return output.getvalue()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RingClient:
    """Thin synchronous wrapper around the async ring-doorbell Ring object."""

    def __init__(
        self,
        token_updater: Callable[[dict], None] | None = None,
    ) -> None:
        self._ring = None
        self._authenticated = False
        self._token_updater = token_updater or _save_token
        self._pending_auth_token: dict | None = None
        self._defer_token_persistence = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_start_error: BaseException | None = None
        self._loop_thread: threading.Thread | None = None
        self._listener_future = None  # concurrent.futures.Future from run_coroutine_threadsafe
        self._stop_event = threading.Event()
        self._event_callbacks: list = []
        self._loop_lock = threading.Lock()
        self._last_device_update_at = 0.0
        self._published_generation: int | None = None
        self._event_credentials: dict[str, Any] | None = None
        self._event_credentials_loaded = False
        self._event_listener_revision = 0
        self._stopped = False

    # ------------------------------------------------------------------
    # Async event loop management
    # ------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Return the background loop, starting it if necessary."""
        with self._loop_lock:
            if self._stopped:
                raise RuntimeError("Ring client has been stopped")
            thread_alive = self._loop_thread is not None and self._loop_thread.is_alive()
            loop_ready = (
                self._loop is not None
                and not self._loop.is_closed()
                and self._loop.is_running()
                and thread_alive
            )
            if not loop_ready:
                ready = threading.Event()
                self._loop = None
                self._loop_start_error = None
                self._loop_thread = threading.Thread(
                    target=self._run_loop,
                    args=(ready,),
                    daemon=True,
                    name="ring-asyncio",
                )
                self._loop_thread.start()
                if not ready.wait(timeout=_LOOP_START_TIMEOUT):
                    raise RuntimeError("Ring asyncio loop did not start")
                if self._loop_start_error is not None:
                    raise RuntimeError(
                        "Ring asyncio loop failed to start"
                    ) from self._loop_start_error
                if self._loop is None:
                    raise RuntimeError("Ring asyncio loop started without an event loop")
            return self._loop

    def _run_loop(self, ready: threading.Event) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except BaseException as exc:
            self._loop_start_error = exc
            ready.set()
            return

        # Signal readiness from inside the first loop iteration. Publishing the
        # loop before run_forever() starts leaves a narrow window where a
        # thread-safe submission can be queued without waking the selector.
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _run(self, coro) -> Any:
        """Submit *coro* to the background loop and block until it completes."""
        future = self.submit(coro)
        try:
            return future.result(timeout=_DEFAULT_CALL_TIMEOUT)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("Ring API request timed out") from None

    def submit(self, coro) -> concurrent.futures.Future:
        """Submit *coro* to the Ring asyncio loop and return its Future."""
        try:
            loop = self._ensure_loop()
        except BaseException:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise
        return asyncio.run_coroutine_threadsafe(coro, loop)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str, otp_code: str | None = None) -> None:
        """Authenticate with Ring.

        Raises ``Requires2FAError`` if 2FA is required; call again with the
        OTP to complete.  Raises ``AuthenticationError`` on bad credentials.
        """
        self._run(self._async_authenticate(username, password, otp_code))

    def authenticate_from_token(self, token: dict) -> None:
        """Restore session from an already-loaded OAuth token."""
        self._run(self._async_authenticate_from_token(token))

    def refresh_session_token(self) -> None:
        """Refresh the saved OAuth token and reload Ring account data."""
        if self._ring is None:
            raise RuntimeError("Not signed in to Ring")
        self._run(self._async_refresh_session_token())

    async def _async_authenticate(self, username: str, password: str, otp_code: str | None) -> None:
        from ring_doorbell import Auth, Ring

        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None

        # Always do a fresh OAuth exchange — never shortcut via the cache on an
        # explicit sign-in.  Pass None for the token so ring-doorbell doesn't
        # try to reuse a stale cached credential.
        auth = Auth(_APP_USER_AGENT, None, self._capture_auth_token)

        try:
            # May raise Requires2FAError or AuthenticationError — let propagate.
            token = await auth.async_fetch_token(username, password, otp_code)
            if self._pending_auth_token is None:
                self._capture_auth_token(token)

            ring = Ring(auth)
            _log.debug(
                "OAuth token obtained — hardware_id=%s user_agent=%s",
                auth.get_hardware_id(),
                _APP_USER_AGENT,
            )
            await ring.async_update_data()
            self._finish_authentication(auth, ring)
            self._last_device_update_at = time.monotonic()
            _log.info(
                "Ring authenticated — %d device(s) found",
                len(ring.devices().all_devices),
            )
        except BaseException:
            self._pending_auth_token = None
            await self._close_auth(auth)
            raise

    async def _async_authenticate_from_token(self, token: dict) -> None:
        from ring_doorbell import Auth, Ring

        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None
        auth = Auth(_APP_USER_AGENT, token, self._capture_auth_token)

        try:
            ring = Ring(auth)

            # A cached token that expired (and whose refresh failed) raises
            # AuthenticationError here — let it propagate so init_client_from_cache()
            # can delete the stale secret.
            await ring.async_update_data()
            self._finish_authentication(auth, ring)
            self._last_device_update_at = time.monotonic()
        except BaseException:
            self._pending_auth_token = None
            await self._close_auth(auth)
            raise

    def _capture_auth_token(self, token: dict) -> None:
        self._pending_auth_token = dict(token)

    def _finish_authentication(self, auth, ring) -> None:
        if self._defer_token_persistence:
            auth.token_updater = self._capture_auth_token
        else:
            if self._pending_auth_token is not None:
                self._token_updater(self._pending_auth_token)
                self._pending_auth_token = None
            auth.token_updater = self._token_updater
        self._ring = ring
        self._authenticated = True

    def _defer_auth_token_persistence(self) -> None:
        self._defer_token_persistence = True

    def _activate_auth_token_persistence(
        self,
        token_updater: Callable[[dict], None],
    ) -> None:
        if not self._authenticated or self._ring is None:
            raise RuntimeError("Cannot publish an unauthenticated Ring client")
        if self._pending_auth_token is not None:
            token_updater(self._pending_auth_token)
            self._pending_auth_token = None
        self._token_updater = token_updater
        self._ring.auth.token_updater = token_updater

    @staticmethod
    async def _close_auth(auth) -> None:
        try:
            await auth.async_close()
        except Exception as exc:
            _log.debug("Ring auth close failed: %s", exc)

    async def _async_refresh_session_token(self) -> None:
        if self._ring is None:
            raise RuntimeError("Not signed in to Ring")

        await self._ring.auth.async_refresh_tokens()
        await self._ring.async_update_data()
        self._last_device_update_at = time.monotonic()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated and self._ring is not None

    @property
    def all_devices(self) -> list:
        if self._ring is None:
            return []
        return list(self._ring.devices().all_devices)

    def refresh_devices(
        self,
        families: set[str] | frozenset[str] | None = None,
        *,
        max_age_seconds: int = _DEVICE_REFRESH_TTL,
    ) -> list:
        """Refresh Ring account data and return devices, optionally filtered by family."""
        if self._ring is None:
            return []
        return self._run(self._async_refresh_devices(families, max_age_seconds))

    async def _async_refresh_devices(
        self,
        families: set[str] | frozenset[str] | None,
        max_age_seconds: int = _DEVICE_REFRESH_TTL,
    ) -> list:
        if time.monotonic() - self._last_device_update_at > max_age_seconds:
            await self._ring.async_update_data()
            self._last_device_update_at = time.monotonic()
        devices = list(self._ring.devices().all_devices)
        if families is None:
            return devices
        return [
            device for device in devices if (getattr(device, "family", None) or "other") in families
        ]

    def get_device_by_id(self, device_id: int) -> Any | None:
        """Return a Ring device by API id."""
        for device in self.all_devices:
            if getattr(device, "id", None) == device_id:
                return device
        return None

    def snapshot_for_device(self, device) -> bytes | None:
        """Return snapshot bytes for a device, including cached snapshot fallback."""
        if self._ring is None:
            return None
        return self._run(self._async_snapshot_bytes(device))

    def last_event_frame_for_device(self, device) -> bytes | None:
        """Return a first-frame preview from the latest event, with snapshot fallback."""
        if self._ring is None:
            return None
        return self._run(self._async_last_event_frame_for_device(device))

    def event_preview_for_device(self, device, event) -> bytes | None:
        """Return the best preview image for a Ring push event."""
        if self._ring is None:
            return None
        event_id = getattr(event, "id", None)
        return self._run(self._async_event_preview_for_device(device, event_id, event))

    async def _async_event_preview_for_device(
        self,
        device,
        event_id: int | None,
        event,
    ) -> bytes | None:
        history_event = None
        if event_id is not None:
            history_event = await self._async_find_history_event(device, event_id)
        return await self._async_event_preview_bytes(device, event_id, event, history_event)

    async def _async_last_event_frame_for_device(self, device) -> bytes | None:
        try:
            history = await device.async_history(limit=1)
            if not history:
                _log.debug("No history for %s; using cached snapshot", device.name)
                return await self._async_cached_snapshot_bytes(device.id)

            event_id = history[0].get("id")
            if event_id is None:
                return await self._async_cached_snapshot_bytes(device.id)

            history_snapshot = await self._async_history_snapshot_bytes(history[0])
            if history_snapshot:
                _log.debug("Fetched history snapshot preview for %s", device.name)
                return history_snapshot

            url = await device.async_recording_url(event_id)
            if not url:
                _log.debug("No recording URL for event %s; using cached snapshot", event_id)
                return await self._async_cached_snapshot_bytes(device.id)

            png_bytes = await asyncio.to_thread(_decode_first_recording_frame_png, url)
            _log.debug("Decoded first frame from recording for %s", device.name)
            return png_bytes
        except Exception as exc:
            _log.debug("Latest event preview failed for %s: %s", device.name, exc)
            return await self._async_cached_snapshot_bytes(device.id)

    def event_history(
        self,
        limit: int = 50,
        *,
        kind: str | None = None,
        older_than: int | dict[int, int] | None = None,
        device_ids: set[int] | None = None,
        enforce_limit: bool = False,
    ) -> tuple[list, list[dict[str, Any]]]:
        """Return all devices and merged Ring history events with device references."""
        if self._ring is None:
            return [], []
        return self._run(
            self._async_event_history(
                limit,
                kind=kind,
                older_than=older_than,
                device_ids=device_ids,
                enforce_limit=enforce_limit,
            )
        )

    async def _async_event_history(
        self,
        limit: int,
        *,
        kind: str | None = None,
        older_than: int | dict[int, int] | None = None,
        device_ids: set[int] | None = None,
        enforce_limit: bool = False,
    ) -> tuple[list, list[dict[str, Any]]]:
        devices = self.all_devices
        events: list[dict[str, Any]] = []
        for device in devices:
            if device_ids is not None and int(device.id) not in device_ids:
                continue
            try:
                older_event_id = (
                    older_than.get(int(device.id)) if isinstance(older_than, dict) else older_than
                )
                kwargs: dict[str, Any] = {
                    "limit": limit,
                    "enforce_limit": enforce_limit,
                }
                if kind:
                    kwargs["kind"] = kind
                if older_event_id:
                    kwargs["older_than"] = older_event_id
                history = await device.async_history(**kwargs)
                for event in history:
                    event["_device"] = device
                events.extend(history)
            except Exception as exc:
                _log.debug("History fetch failed for %s: %s", device.name, exc)
        return devices, events

    def recording_url(self, device, event_id: int) -> str | None:
        """Return a playable recording URL for a Ring history event."""
        return self._run(device.async_recording_url(event_id))

    def delete_recording(self, device, event_id: int) -> None:
        """Delete a Ring recording event."""
        self._run(device.async_delete_recording(event_id))

    def is_event_active(self, event) -> bool:
        """Return whether a push event should still have an active live event."""
        started_at = float(getattr(event, "now", 0) or 0)
        expires_in = float(getattr(event, "expires_in", 0) or 0)
        return time.time() < started_at + expires_in

    def get_event_notification_context(self, event) -> dict[str, Any]:
        """Return optional text/image context for a Ring push notification."""
        if self._ring is None:
            return {}

        device_id = getattr(event, "doorbot_id", None)
        event_id = getattr(event, "id", None)
        if device_id is None:
            return {}

        return self._run(self._async_event_notification_context(device_id, event_id, event))

    async def _async_event_notification_context(
        self,
        device_id: int,
        event_id: int | None,
        event,
    ) -> dict[str, Any]:
        device = self.get_device_by_id(device_id)
        if device is None:
            return {}

        history_event = None
        description = None
        if event_id is not None:
            history_event = await self._async_find_history_event(device, event_id)
            description = _extract_event_description(history_event)
            if history_event is not None and description is None:
                _log.debug(
                    "No Ring video description found for event %s; history keys=%s",
                    event_id,
                    sorted(history_event.keys()),
                )

        image_bytes = await self._async_event_preview_bytes(device, event_id, event, history_event)

        return {
            "description": description,
            "image_bytes": image_bytes,
        }

    async def _async_find_history_event(self, device, event_id: int) -> dict[str, Any] | None:
        try:
            history = await device.async_history(limit=20)
        except Exception as exc:
            _log.debug("History lookup failed for notification event %s: %s", event_id, exc)
            return None

        for item in history:
            if int(item.get("id", -1)) == int(event_id):
                return item
        return None

    async def _async_event_preview_bytes(
        self,
        device,
        event_id: int | None,
        event,
        history_event: dict[str, Any] | None,
    ) -> bytes | None:
        history_snapshot = await self._async_history_snapshot_bytes(history_event)
        if history_snapshot:
            return history_snapshot

        if event_id is not None and not self.is_event_active(event):
            recording_frame = await self._async_first_recording_frame(device, event_id)
            if recording_frame:
                return recording_frame

        snapshot = await self._async_snapshot_bytes(device)
        if snapshot:
            return snapshot

        if event_id is not None:
            return await self._async_first_recording_frame(device, event_id)

        return None

    async def _async_history_snapshot_bytes(
        self,
        history_event: dict[str, Any] | None,
    ) -> bytes | None:
        snapshot_url = history_event.get("snapshot_url") if history_event else None
        if not snapshot_url:
            return None

        try:
            resp = await self._ring.auth.async_query(snapshot_url)
            if resp.status_code == 200 and resp.content:
                return bytes(resp.content)
        except Exception as exc:
            _log.debug("Notification history snapshot fetch failed: %s", exc)
        return None

    async def _async_snapshot_bytes(self, device) -> bytes | None:
        try:
            snapshot = await device.async_get_snapshot(retries=2, delay=1)
            if snapshot:
                return bytes(snapshot)
        except Exception as exc:
            _log.debug("Notification snapshot fetch failed for %s: %s", device.name, exc)

        return await self._async_cached_snapshot_bytes(device.id, device_name=device.name)

    async def _async_cached_snapshot_bytes(
        self,
        device_id: int,
        *,
        device_name: str | None = None,
    ) -> bytes | None:
        try:
            from ring_doorbell.const import SNAPSHOT_ENDPOINT

            resp = await self._ring.async_query(SNAPSHOT_ENDPOINT.format(device_id))
            if resp.status_code == 200 and resp.content:
                return bytes(resp.content)
        except Exception as exc:
            label = device_name or str(device_id)
            _log.debug("Cached snapshot fetch failed for %s: %s", label, exc)

        return None

    async def _async_first_recording_frame(self, device, event_id: int) -> bytes | None:
        try:
            url = await device.async_recording_url(event_id)
            if not url:
                return None
            return await asyncio.to_thread(_decode_first_recording_frame_png, url)
        except Exception as exc:
            _log.debug("Notification recording preview failed for event %s: %s", event_id, exc)
            return None

    # ------------------------------------------------------------------
    # Background FCM listener
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the real-time FCM event listener (non-blocking)."""
        if self._ring is None:
            return
        if self._listener_future is not None and not self._listener_future.done():
            return
        self._stop_event.clear()
        self._listener_future = self.submit(self._async_listen())
        _log.debug("FCM listener task submitted")

    def stop(self) -> None:
        """Stop the listener, close the ring auth session, shut down the loop."""
        with self._loop_lock:
            self._stopped = True
        self._stop_event.set()
        self._retire_event_listener_revision()
        future = self._listener_future
        self._listener_future = None

        if future and not future.done():
            try:
                future.result(timeout=_LISTENER_STOP_TIMEOUT)
            except concurrent.futures.TimeoutError:
                future.cancel()
                with contextlib.suppress(Exception):
                    future.result(timeout=1)
            except Exception as exc:
                _log.debug("FCM listener stopped with error: %s", exc)

        # Hold the loop lock so a concurrent _ensure_loop() can't race the
        # teardown (read/replace self._loop/_loop_thread) and spawn an orphan loop.
        with self._loop_lock:
            loop = self._loop
            thread = self._loop_thread
            if loop and not loop.is_closed() and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(self._async_close(), loop).result(
                        timeout=_ASYNC_CLOSE_TIMEOUT
                    )
                except Exception as exc:
                    _log.debug("Error during async close: %s", exc)
                loop.call_soon_threadsafe(loop.stop)

            if thread is not None and thread.is_alive():
                thread.join(timeout=_LOOP_JOIN_TIMEOUT)
                if thread.is_alive():
                    _log.warning("Ring asyncio thread did not stop within timeout")
                    return

            self._loop = None
            self._loop_thread = None

    async def _async_close(self) -> None:
        """Close the ring auth session (which owns the aiohttp ClientSession)."""
        ring = self._ring
        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None
        if ring is not None:
            await self._close_auth(ring.auth)

    async def _async_listen(self) -> None:
        from ring_doorbell import RingEventListener, RingEventListenerConfig

        # Make the underlying FCM client retry effectively forever rather than
        # giving up after a handful of attempts (the library default), so a long
        # outage can't silently kill real-time events.
        config = RingEventListenerConfig.default_config()
        config.connection_retry_count = 1_000_000
        config.abort_on_sequential_error_count = None

        self._load_owned_event_credentials()
        backoff = _LISTENER_RECONNECT_MIN
        while not self._stop_event.is_set():
            generation, listener_revision, credentials = self._event_listener_context()
            listener = RingEventListener(
                self._ring,
                credentials=credentials,
                credentials_updated_callback=partial(
                    self._on_event_credentials_updated,
                    generation=generation,
                    listener_revision=listener_revision,
                ),
                config=config,
            )
            listener.add_notification_callback(self._on_ring_event)
            self._set_owned_listener_state(LISTENER_CONNECTING)
            try:
                started = await listener.start()
            except Exception as exc:
                _log.warning("FCM listener failed to start: %s", exc)
                started = False

            if not started:
                self._retire_event_listener_revision(listener_revision)
                self._set_owned_listener_state(LISTENER_OFFLINE)
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, _LISTENER_RECONNECT_MAX)
                continue

            backoff = _LISTENER_RECONNECT_MIN
            _log.info("FCM event listener started")
            try:
                while not self._stop_event.is_set():
                    await self._sleep_or_stop(_LISTENER_MONITOR_INTERVAL)
                    if self._stop_event.is_set():
                        break
                    state = self._derive_listener_state(listener)
                    if state == LISTENER_OFFLINE:
                        _log.warning("FCM listener stopped; reconnecting")
                        break
                    self._set_owned_listener_state(state)
            finally:
                self._retire_event_listener_revision(listener_revision)
                with contextlib.suppress(Exception):
                    await listener.stop()
                _log.info("FCM event listener stopped")

        self._set_owned_listener_state(LISTENER_OFFLINE)

    def _load_owned_event_credentials(self) -> None:
        with _session_lock:
            if self._event_credentials_loaded or not _is_active_client_generation(
                self,
                self._published_generation,
            ):
                return
            try:
                credentials = load_ring_event_credentials()
            except Exception:
                credentials = None
                _log.warning("Stored FCM credentials could not be loaded; registering again")
            self._event_credentials = credentials
            self._event_credentials_loaded = True

    def _event_listener_context(
        self,
    ) -> tuple[int | None, int, dict[str, Any] | None]:
        with _session_lock:
            self._event_listener_revision += 1
            credentials = (
                validate_ring_event_credentials(self._event_credentials)
                if self._event_credentials is not None
                else None
            )
            return self._published_generation, self._event_listener_revision, credentials

    def _retire_event_listener_revision(self, revision: int | None = None) -> None:
        with _session_lock:
            if revision is None or self._event_listener_revision == revision:
                self._event_listener_revision += 1

    def _discard_event_credentials(self) -> None:
        with _session_lock:
            self._event_credentials = None
            self._event_credentials_loaded = True
            self._event_listener_revision += 1

    def _on_event_credentials_updated(
        self,
        credentials: Any,
        *,
        generation: int | None,
        listener_revision: int,
    ) -> None:
        try:
            validated = validate_ring_event_credentials(credentials)
        except Exception:
            _log.warning("FCM listener supplied invalid credentials; update ignored")
            return

        with _session_lock:
            if (
                not _is_active_client_generation(self, generation)
                or self._event_listener_revision != listener_revision
            ):
                return
            # Keep the valid update for the next reconnect even when the desktop
            # secret service is temporarily unavailable.
            self._event_credentials = validated
            self._event_credentials_loaded = True
            try:
                save_ring_event_credentials(validated)
            except Exception:
                _log.warning(
                    "Updated FCM credentials could not be saved; retaining them for this session"
                )

    def _set_owned_listener_state(self, state: str) -> None:
        _set_listener_state(
            state,
            expected_client=self,
            generation=self._published_generation,
        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        elapsed = 0.0
        while elapsed < seconds and not self._stop_event.is_set():
            await asyncio.sleep(0.5)
            elapsed += 0.5

    @staticmethod
    def _derive_listener_state(listener) -> str:
        receiver = getattr(listener, "_receiver", None)
        run_state = getattr(receiver, "run_state", None)
        name = getattr(run_state, "name", None)
        if name is None:
            return LISTENER_CONNECTED
        if name == "STARTED":
            return LISTENER_CONNECTED
        if name == "STOPPED":
            return LISTENER_OFFLINE
        return LISTENER_CONNECTING

    def add_event_callback(self, callback) -> None:
        """Register a callable to be invoked (on the GTK main thread) for every FCM event."""
        if callback not in self._event_callbacks:
            self._event_callbacks.append(callback)

    def remove_event_callback(self, callback) -> None:
        """Remove a previously registered FCM event callback."""
        with contextlib.suppress(ValueError):
            self._event_callbacks.remove(callback)

    def _on_ring_event(self, event) -> None:
        """Called from the asyncio thread; marshal to GTK main loop."""
        from gi.repository import GLib

        generation = self._published_generation
        if not _is_active_client_generation(self, generation):
            return
        GLib.idle_add(self._dispatch_event, event, generation)

    def _dispatch_event(self, event, generation: int) -> bool:
        if not _is_active_client_generation(self, generation):
            return False
        from halo_gtk.notifications import send_ring_notification

        send_ring_notification(event)
        for cb in list(self._event_callbacks):
            try:
                cb(event)
            except Exception as exc:
                _log.debug("Event callback error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Token persistence (module-level so they can be passed as plain callbacks)
# ---------------------------------------------------------------------------


def _load_token() -> dict | None:
    return load_ring_token()


def _save_token(token: dict) -> None:
    save_ring_token(token)


def save_account_email(
    email: str,
    *,
    expected_client: RingClient | None = None,
) -> None:
    global _account_email_cache, _account_email_loaded
    with _session_lock:
        if expected_client is not None and _client is not expected_client:
            raise SessionSupersededError("Ring session was superseded before sign-in completed")
        with _account_email_lock:
            previous_email = (
                _account_email_cache if _account_email_loaded else load_ring_account_email()
            )

        account_changed = bool(previous_email) and (
            previous_email.strip().casefold() != email.strip().casefold()
        )
        if account_changed:
            owner = expected_client or _client
            discard_credentials = getattr(owner, "_discard_event_credentials", None)
            if callable(discard_credentials):
                discard_credentials()
            clear_ring_event_credentials()
        save_ring_account_email(email)
        with _account_email_lock:
            _account_email_cache = email
            _account_email_loaded = True


def _clear_token() -> None:
    clear_ring_token()


def is_ring_session_rejected(exc: Exception) -> bool:
    """Return whether Ring rejected API session creation after OAuth succeeded."""
    message = str(exc)
    return (
        "status code 406" in message
        and "/clients_api/session" in message
        and "Not Acceptable" in message
    )


def _should_clear_cached_token(exc: Exception) -> bool:
    from ring_doorbell import AuthenticationError

    return isinstance(exc, AuthenticationError)


def _extract_event_description(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None

    candidate_keys = (
        "full_description",
        "short_description",
        "detection_details",
        "video_description",
        "video_descriptions",
        "smart_description",
        "ai_description",
        "event_description",
        "description",
        "summary",
    )

    def _walk(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in candidate_keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
                if isinstance(candidate, dict | list):
                    found = _walk(candidate)
                    if found:
                        return found
            for nested in value.values():
                found = _walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = _walk(item)
                if found:
                    return found
        return None

    return _walk(event)
