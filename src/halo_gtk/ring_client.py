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
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import replace
from functools import partial, wraps
from typing import Any

from halo_gtk.alarm.events import MAX_PENDING_EVENTS, AlarmEventBuffer, AlarmEventReducer
from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmMode,
    AlarmServiceStatus,
    alarm_monotonic_time,
    empty_alarm_snapshot,
)
from halo_gtk.alarm.provider import (
    AlarmCommandResult,
    AlarmCommandSupersededError,
    AlarmLocationSeed,
    AlarmModePreparation,
    AlarmRefreshResult,
    AlarmRefreshStatus,
    location_seeds_from_base_stations,
)
from halo_gtk.alarm.rest import DEVICES_ENDPOINT, LOCATIONS_ENDPOINT
from halo_gtk.alarm.rest import RingAlarmRestGateway as _RingAlarmRestGateway
from halo_gtk.chime_client import ChimeClientState
from halo_gtk.intercom_client import IntercomClientState
from halo_gtk.light_groups_client import LightGroupsClientState
from halo_gtk.lighting_actions import LightingActions
from halo_gtk.preview import PreviewManager
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
from halo_gtk.snapshot_http import authenticated_snapshot_url
from halo_gtk.snapshot_http import snapshot_bytes as _bounded_snapshot_bytes

_log = logging.getLogger(__name__)

_client: RingClient | None = None
_session_lock = threading.RLock()
_alarm_command_lock = threading.RLock()
_session_generation = 0
_active_client_generation = 0
_account_email_cache: str | None = None
_account_email_loaded = False
_account_email_lock = threading.Lock()

_APP_USER_AGENT = "android:com.ringapp"
_DEFAULT_CALL_TIMEOUT = 90
_PREVIEW_CALL_TIMEOUT = 15
_LISTENER_STOP_TIMEOUT = 4
_ALARM_STOP_TIMEOUT = 4
_ASYNC_CLOSE_TIMEOUT = 5
_LOOP_JOIN_TIMEOUT = 5
_LOOP_START_TIMEOUT = 5
_DEVICE_REFRESH_TTL = 8
_LISTENER_MONITOR_INTERVAL = 2
_LISTENER_RECONNECT_MIN = 5
_LISTENER_RECONNECT_MAX = 120
_RING_LOCATIONS_ENDPOINT = "https://api.ring.com/devices/v1/locations"
_ALARM_LOCATION_DISCOVERY_TIMEOUT = 10
_ALARM_DISCOVERY_RECORD_LIMIT = 1_024
_ALARM_DISCOVERY_LOCATION_LIMIT = 128
_ALARM_DISCOVERY_IDENTIFIER_LIMIT = 512
_PREVIEW_CALL_SLOTS = threading.BoundedSemaphore(2)


def _bounded_preview(method):
    """Bound a whole preview (including SDK waits) and track stop cancellation."""

    @wraps(method)
    async def call(self, *args, **kwargs):
        task = asyncio.current_task()
        entered = False
        try:
            async with asyncio.timeout(_PREVIEW_CALL_TIMEOUT):
                with self._preview_tasks_lock:
                    nested = task in self._preview_tasks
                    if nested:
                        self._preview_tasks[task] += 1
                        entered = True
                if not nested:
                    while not _PREVIEW_CALL_SLOTS.acquire(blocking=False):
                        if not self._preview_is_current():
                            return None
                        await asyncio.sleep(0.025)
                    with self._preview_tasks_lock:
                        self._preview_tasks[task] = 1
                    entered = True
                if not self._preview_is_current():
                    return None
                result = await method(self, *args, **kwargs)
                return result if self._preview_is_current() else None
        except TimeoutError:
            return None
        finally:
            if entered:
                with self._preview_tasks_lock:
                    self._preview_tasks[task] -= 1
                    if self._preview_tasks[task] == 0:
                        del self._preview_tasks[task]
                        _PREVIEW_CALL_SLOTS.release()

    return call


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


def _install_serialized_auth_refresh(auth) -> None:
    """Serialize token rotation and deduplicate stale in-flight request refreshes."""
    if getattr(auth, "_halo_refresh_serialized", False):
        return

    original_refresh = getattr(auth, "async_refresh_tokens", None)
    if not callable(original_refresh):
        return
    refresh_lock = asyncio.Lock()
    completed_refreshes = 0
    last_result = None
    request_refresh_generation: ContextVar[int | None] = ContextVar(
        "ring_request_refresh_generation",
        default=None,
    )

    @contextlib.contextmanager
    def request_scope():
        """Capture the token generation before either SDK or bounded image I/O."""
        token = request_refresh_generation.set(completed_refreshes)
        try:
            yield
        finally:
            request_refresh_generation.reset(token)

    async def serialized_refresh():
        nonlocal completed_refreshes, last_result
        observed_refreshes = completed_refreshes
        request_generation = request_refresh_generation.get()
        async with refresh_lock:
            if request_generation is not None and completed_refreshes != request_generation:
                return last_result
            if completed_refreshes != observed_refreshes:
                return last_result
            last_result = await original_refresh()
            completed_refreshes += 1
            return last_result

    auth.async_refresh_tokens = serialized_refresh
    auth._halo_auth_request_scope = request_scope
    original_query = getattr(auth, "async_query", None)
    if callable(original_query):

        async def generation_tracked_query(*args, **kwargs):
            with request_scope():
                return await original_query(*args, **kwargs)

        auth.async_query = generation_tracked_query
    auth._halo_refresh_serialized = True


def _install_device_inventory_refresh(ring) -> None:
    """Invalidate the pinned SDK's collection when devices are added or removed.

    ring-doorbell refreshes devices_data but retains its old RingDevices cache.
    Invalidate immediately after inventory refresh so its subsequent group
    discovery also sees the current devices. Normal state refreshes preserve
    identity, and active streams retain their own original device references.
    """
    if getattr(ring, "_halo_inventory_refresh_installed", False):
        return
    update_devices = getattr(ring, "async_update_devices", None)
    if not callable(update_devices) or not hasattr(ring, "_devices"):
        return
    families = {"doorbots", "authorized_doorbots", "stickup_cams", "chimes", "other"}

    def inventory_signature():
        return frozenset(
            (family, device_id, attrs.get("kind"))
            for family, devices in ring.devices_data.items()
            if family in families
            for device_id, attrs in devices.items()
        )

    async def refresh_devices():
        previous = inventory_signature()
        await update_devices()
        if inventory_signature() != previous:
            ring._devices = None

    ring.async_update_devices = refresh_devices
    ring._halo_inventory_refresh_installed = True


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
    with _alarm_command_lock, _session_lock:
        if generation != _session_generation:
            return False, None

        activate_persistence = getattr(client, "_activate_auth_token_persistence", None)
        if callable(activate_persistence):
            activate_persistence(lambda token: _save_token_for_client(client, generation, token))

        previous = _client
        _active_client_generation = generation
        client._published_generation = generation
        initialize_alarm = getattr(client, "_initialize_alarm_generation", None)
        if callable(initialize_alarm):
            initialize_alarm(generation)
        client_chimes = getattr(client, "_chimes", None)
        if client_chimes is not None:
            client_chimes.initialize(generation)
        for name in ("_light_groups", "_lighting_actions", "_intercoms"):
            feature = getattr(client, name, None)
            if feature is not None:
                feature.initialize(generation)
        _client = client
        if previous is not None and previous is not client:
            invalidate_alarm = getattr(previous, "_invalidate_alarm_state", None)
            if callable(invalidate_alarm):
                invalidate_alarm(generation)
            previous_chimes = getattr(previous, "_chimes", None)
            if previous_chimes is not None:
                previous_chimes.invalidate(generation)
            for name in ("_light_groups", "_lighting_actions", "_intercoms"):
                feature = getattr(previous, name, None)
                if feature is not None:
                    feature.invalidate(generation)
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
    with _alarm_command_lock, _session_lock:
        if expected_client is not None and _client is not expected_client:
            return False
        _session_generation += 1
        _active_client_generation = _session_generation
        generation = _active_client_generation
        client = _client
        _client = None
        if client is not None:
            invalidate_alarm = getattr(client, "_invalidate_alarm_state", None)
            if callable(invalidate_alarm):
                invalidate_alarm(generation)

            client_chimes = getattr(client, "_chimes", None)
            if client_chimes is not None:
                client_chimes.invalidate(generation)
            for name in ("_light_groups", "_lighting_actions", "_intercoms"):
                feature = getattr(client, name, None)
                if feature is not None:
                    feature.invalidate(generation)

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
    with _alarm_command_lock, _session_lock:
        _session_generation += 1
        _active_client_generation = _session_generation
        generation = _active_client_generation
        client = _client
        _client = None
        if client is not None:
            invalidate_alarm = getattr(client, "_invalidate_alarm_state", None)
            if callable(invalidate_alarm):
                invalidate_alarm(generation)

            client_chimes = getattr(client, "_chimes", None)
            if client_chimes is not None:
                client_chimes.invalidate(generation)
            for name in ("_light_groups", "_lighting_actions", "_intercoms"):
                feature = getattr(client, name, None)
                if feature is not None:
                    feature.invalidate(generation)

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
        self._alarm_future = None  # concurrent.futures.Future from run_coroutine_threadsafe
        self._alarm_stop_event: asyncio.Event | None = None
        self._alarm_service = None
        self._alarm_gateway = None
        self._ring_json_transport = None
        self._chimes = ChimeClientState(self, is_current=_is_active_client_generation)
        self._light_groups = LightGroupsClientState(self, is_current=_is_active_client_generation)
        self._intercoms = IntercomClientState(self, is_current=_is_active_client_generation)
        self._lighting_actions = LightingActions(self, is_current=_is_active_client_generation)
        self._alarm_history_service = None
        self._alarm_discovery_task = None
        self._alarm_discovery_at = None
        self._alarm_discovery_generation = None
        self._alarm_discovery_service = None
        self._alarm_event_reducer = AlarmEventReducer()
        self._alarm_event_buffer = AlarmEventBuffer()
        self._alarm_pending_revocations: set[str] = set()
        self._alarm_withdraw_all = False
        self._alarm_dispatch_pending = False
        self._alarm_notification_gap_count = 0
        self._alarm_closing_service = None
        self._alarm_close_task: asyncio.Task | None = None
        self._stop_event = threading.Event()
        self._preview = PreviewManager(self._stop_event)
        self._preview_tasks_lock = threading.Lock()
        self._preview_tasks = {}
        self._event_callbacks: list = []
        self._alarm_callbacks: list = []
        self._alarm_snapshot_lock = threading.Lock()
        self._alarm_snapshot = empty_alarm_snapshot()
        # Retirement shares this fence with snapshot admission. Never acquire
        # the global session lock while holding the snapshot lock: publishing
        # a replacement client takes those locks in the opposite order.
        self._alarm_generation: int | None = None
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

    def _run_preview(self, coro, cancel_event: threading.Event | None = None) -> bytes | None:
        """Cancel the actual async preview when its requesting view is retired."""
        if cancel_event is None:
            # Preserve the ordinary submission path for existing callers and
            # injected clients. The async preview scope still has its deadline.
            return self._run(coro)
        if cancel_event.is_set():
            coro.close()
            return None
        future = self.submit(coro)
        deadline = time.monotonic() + _PREVIEW_CALL_TIMEOUT
        while True:
            if cancel_event.is_set() or time.monotonic() >= deadline:
                future.cancel()
                return None
            try:
                result = future.result(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except concurrent.futures.TimeoutError:
                if future.done():
                    return None
                continue
            except concurrent.futures.CancelledError:
                return None
            # Completion and view retirement can arrive in the same interval.
            return None if cancel_event.is_set() else result

    def submit(self, coro) -> concurrent.futures.Future:
        """Submit *coro* to the Ring asyncio loop and return its Future."""
        try:
            loop = self._ensure_loop()
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except BaseException:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise

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
        _install_serialized_auth_refresh(auth)

        try:
            # May raise Requires2FAError or AuthenticationError — let propagate.
            token = await auth.async_fetch_token(username, password, otp_code)
            if self._pending_auth_token is None:
                self._capture_auth_token(token)

            ring = Ring(auth)
            _install_device_inventory_refresh(ring)
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
        _install_serialized_auth_refresh(auth)

        try:
            ring = Ring(auth)
            _install_device_inventory_refresh(ring)

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
        await self._async_reconcile_alarm_discovery()

    async def _async_reconcile_alarm_discovery(self) -> None:
        service = self._alarm_service
        if service is not None:
            try:
                await service.reconcile(await self._async_alarm_location_seeds())
            except Exception as exc:
                _log.warning(
                    "Ring Alarm discovery reconciliation failed after session refresh (%s)",
                    type(exc).__name__,
                )

    def _alarm_location_seeds(
        self,
        *,
        raw_locations: Mapping[object, object] | Iterable[object] | None = None,
    ) -> tuple[AlarmLocationSeed, ...]:
        ring = self._ring
        if ring is None:
            return ()
        base_stations = getattr(ring, "devices_data", {}).get("base_stations", {})
        return location_seeds_from_base_stations(
            base_stations,
            raw_locations=raw_locations,
        )

    async def _async_alarm_location_seeds(self) -> tuple[AlarmLocationSeed, ...]:
        seeds = self._alarm_location_seeds()
        if not seeds:
            return ()
        ring = self._ring
        auth = getattr(ring, "auth", None)
        if auth is None:
            return seeds
        try:
            payload = await self._get_alarm_gateway().request_json(
                _RING_LOCATIONS_ENDPOINT,
                timeout=_ALARM_LOCATION_DISCOVERY_TIMEOUT,
            )
            return self._alarm_location_seeds(raw_locations=payload)
        except Exception:
            return seeds

    def _get_ring_json_transport(self):
        if self._ring_json_transport is None:
            if self._ring is None:
                raise RuntimeError("Not signed in to Ring")
            from halo_gtk.ring_json import RingJsonTransport

            self._ring_json_transport = RingJsonTransport(self._ring.auth)
        return self._ring_json_transport

    def _get_alarm_gateway(self):
        if self._alarm_gateway is None:
            if self._ring is None:
                raise RuntimeError("Not signed in to Ring")
            self._alarm_gateway = _RingAlarmRestGateway(
                self._ring.auth, transport=self._get_ring_json_transport()
            )
        return self._alarm_gateway

    def _get_alarm_history_service(self):
        from halo_gtk.alarm.history import AlarmHistoryService

        if self._alarm_history_service is None:
            self._alarm_history_service = AlarmHistoryService(self._get_alarm_gateway())
        return self._alarm_history_service

    def alarm_history(self, location_id, *, offset=0, limit=50, cancel_event=None, force=False):
        """Bounded worker-thread history API; cancellation reaches the coroutine."""
        coro = self._async_alarm_history(location_id, offset=offset, limit=limit, force=force)
        if cancel_event is not None and cancel_event.is_set():
            coro.close()
            raise concurrent.futures.CancelledError
        future = self.submit(coro)
        deadline = time.monotonic() + 31
        while True:
            if (cancel_event is not None and cancel_event.is_set()) or time.monotonic() >= deadline:
                future.cancel()
                raise concurrent.futures.CancelledError
            try:
                return future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                if future.done():
                    return future.result()

    async def _async_alarm_history(self, location_id, *, offset=0, limit=50, force=False):
        generation = self._published_generation
        self._require_alarm_location(location_id, generation)
        result = await self._get_alarm_history_service().fetch_page(
            location_id, offset=offset, limit=limit, force=force
        )
        self._require_alarm_location(location_id, generation)
        return result

    def _require_alarm_location(self, location_id, generation):
        if not _is_active_client_generation(self, generation):
            raise SessionSupersededError("Ring session changed")
        if self.get_alarm_snapshot().find_location(location_id) is None:
            raise RuntimeError("Alarm location unavailable")

    async def _async_refresh_alarm_discovery(self):
        """Refresh Alarm-only ownership/discovery without mutating camera SDK data."""
        generation = self._published_generation
        service = self._alarm_service
        if service is None or not _is_active_client_generation(self, generation):
            return False
        now = alarm_monotonic_time()
        if (
            self._alarm_discovery_generation == generation
            and self._alarm_discovery_service is service
            and self._alarm_discovery_at is not None
            and 0 <= now - self._alarm_discovery_at < 5
        ):
            return True
        task = self._alarm_discovery_task
        if task is None or task.done():
            task = asyncio.create_task(self._async_read_alarm_discovery())
            self._alarm_discovery_task = task
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)

    async def _async_read_alarm_discovery(self):
        generation = self._published_generation
        service = self._alarm_service
        if service is None or not _is_active_client_generation(self, generation):
            return False
        try:
            async with asyncio.timeout(12):
                gateway = self._get_alarm_gateway()
                async with asyncio.TaskGroup() as group:
                    device_task = group.create_task(gateway.request_json(DEVICES_ENDPOINT))
                    location_task = group.create_task(gateway.request_json(LOCATIONS_ENDPOINT))
                devices, locations = device_task.result(), location_task.result()
                stations = devices.get("base_stations")
                if not isinstance(stations, list) or len(stations) > _ALARM_DISCOVERY_RECORD_LIMIT:
                    return False
                location_ids = set()
                for station in stations:
                    if not isinstance(station, Mapping):
                        return False
                    # Match the provider's ordered field fallback and text
                    # normalization, but reject any record it would drop.
                    identity = None
                    for key in ("location_id", "locationId"):
                        value = station.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            value = str(value)
                        if (
                            isinstance(value, str)
                            and value.strip()
                            and len(value.strip()) <= _ALARM_DISCOVERY_IDENTIFIER_LIMIT
                            and not any(ord(character) < 32 for character in value)
                        ):
                            identity = value.strip()
                            break
                    if identity is None:
                        return False
                    location_ids.add(identity)
                    if len(location_ids) > _ALARM_DISCOVERY_LOCATION_LIMIT:
                        return False
                seeds = location_seeds_from_base_stations(stations, raw_locations=locations)
                # Forgiving normalization must never turn partial discovery
                # into authoritative removal of other locations.
                if {seed.location_id for seed in seeds} != location_ids:
                    return False
                if (
                    not _is_active_client_generation(self, generation)
                    or service is not self._alarm_service
                ):
                    return False
                await service.reconcile(seeds)
                if (
                    not _is_active_client_generation(self, generation)
                    or service is not self._alarm_service
                ):
                    return False
                self._alarm_discovery_at = alarm_monotonic_time()
                self._alarm_discovery_generation = generation
                self._alarm_discovery_service = service
                return True
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed/unsupported read must never mean every location vanished.
            return False

    def refresh_alarm(self, location_id=None):
        return self.submit(self._async_refresh_alarm(location_id))

    async def _async_refresh_alarm(self, location_id=None):
        generation = self._published_generation
        if not _is_active_client_generation(self, generation):
            raise SessionSupersededError("Ring session changed")
        service = self._alarm_service
        if service is None:
            raise RuntimeError("Alarm service unavailable")
        discovery_ok = await self._async_refresh_alarm_discovery()
        result = await service.refresh(location_id)
        if not discovery_ok:
            return AlarmRefreshResult(AlarmRefreshStatus.INCOMPLETE, code="discovery-unavailable")
        if not _is_active_client_generation(self, generation):
            raise SessionSupersededError("Ring session changed")
        return result

    def prepare_alarm_mode(self, location_id):
        return self.submit(self._async_prepare_alarm_mode(location_id))

    async def _async_prepare_alarm_mode(self, location_id):
        generation = self._published_generation
        self._require_alarm_location(location_id, generation)
        service = self._alarm_service
        if service is None:
            raise RuntimeError("Alarm service unavailable")
        if not await self._async_refresh_alarm_discovery():
            return AlarmModePreparation(None, code="discovery-unavailable")
        self._require_alarm_location(location_id, generation)
        result = await service.prepare_mode(location_id)
        self._require_alarm_location(location_id, generation)
        return result

    def _create_alarm_service(
        self,
        generation: int,
        locations: Iterable[AlarmLocationSeed] | None = None,
    ):
        from halo_gtk.alarm.service import RingAlarmService

        ring = self._ring
        if ring is None:
            raise RuntimeError("Not signed in to Ring")
        return RingAlarmService(
            self._get_alarm_gateway(),
            tuple(locations) if locations is not None else self._alarm_location_seeds(),
            self._on_alarm_snapshot,
            generation=generation,
            command_admission=partial(self._alarm_command_admission, generation),
        )

    @contextlib.contextmanager
    def _alarm_command_admission(self, generation: int):
        """Exclude session invalidation across the single Alarm socket write."""
        with _alarm_command_lock:
            if not _is_active_client_generation(self, generation):
                raise AlarmCommandSupersededError
            yield

    async def _async_run_alarm(self, generation: int) -> None:
        if not _is_active_client_generation(self, generation) or self._stop_event.is_set():
            return
        stop_event = asyncio.Event()
        self._alarm_stop_event = stop_event

        service = None
        try:
            locations = await self._async_alarm_location_seeds()
            if not _is_active_client_generation(self, generation) or self._stop_event.is_set():
                return
            service = self._create_alarm_service(generation, locations)
            self._alarm_service = service
            await service.run(stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Alarm is independent of camera FCM and media. Do not let a
            # compatibility failure tear down the authenticated Ring client.
            _log.warning("Ring Alarm service stopped unexpectedly (%s)", type(exc).__name__)
            self._publish_alarm_error(generation)
        finally:
            try:
                if service is not None:
                    await self._async_close_alarm_service(service)
            except Exception as exc:
                _log.debug("Ring Alarm close failed (%s)", type(exc).__name__)
            finally:
                if self._alarm_service is service:
                    self._alarm_service = None
                if self._alarm_stop_event is stop_event:
                    self._alarm_stop_event = None

    async def _async_close_alarm_service(self, service) -> None:
        """Share one close operation across runner cancellation and client teardown."""
        task = self._alarm_close_task
        if task is None or self._alarm_closing_service is not service:
            task = asyncio.create_task(service.close(), name="ring-alarm-close")
            self._alarm_close_task = task
            self._alarm_closing_service = service
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._alarm_close_task is task:
                self._alarm_close_task = None
                self._alarm_closing_service = None

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

    def get_alarm_snapshot(self) -> AlarmAccountSnapshot:
        """Return the latest immutable Ring Alarm account snapshot."""
        with self._alarm_snapshot_lock:
            return self._alarm_snapshot

    def get_chime_snapshot(self):
        """Return cached immutable state without starting account traffic."""
        return self._chimes.snapshot()

    def get_intercom_snapshot(self):
        return self._intercoms.snapshot()

    def add_intercom_callback(self, callback):
        self._intercoms.add_callback(callback)

    def remove_intercom_callback(self, callback):
        self._intercoms.remove_callback(callback)

    def refresh_intercoms(self, *, force=False):
        return self.submit(self._intercoms.refresh(force=force))

    def request_intercom_unlock(self, api_id, *, expected_revision, intent_id):
        return self.submit(
            self._intercoms.command(api_id, expected_revision=expected_revision, intent=intent_id)
        )

    def acknowledge_intercom_request(self, api_id, operation_id):
        return self.submit(self._intercoms.acknowledge(api_id, operation_id))

    @staticmethod
    def cancel_intercom_intent(intent):
        IntercomClientState.cancel_intent(intent)

    def get_light_group_snapshot(self):
        """Return cached lighting state without starting network work."""
        return self._light_groups.snapshot()

    def add_light_group_callback(self, callback):
        self._light_groups.add_callback(callback)

    def remove_light_group_callback(self, callback):
        self._light_groups.remove_callback(callback)

    def refresh_light_locations(self, *, force=False):
        return self.submit(self._light_groups.read("refresh_locations", force=force))

    def refresh_light_groups(self, location_id, *, force=False):
        return self.submit(self._light_groups.read("refresh_groups", location_id, force=force))

    def refresh_light_group_details(self, location_id, group_id, *, force=False):
        return self.submit(
            self._light_groups.read("refresh_details", location_id, group_id, force=force)
        )

    def request_light_group_lights(
        self, location_id, group_id, on, *, duration, expected_revision, intent_id
    ):
        return self.submit(
            self._light_groups.command(
                location_id,
                group_id,
                on,
                duration=duration,
                expected_revision=expected_revision,
                intent=intent_id,
            )
        )

    @staticmethod
    def cancel_light_group_intent(intent_id):
        LightGroupsClientState.cancel_intent(intent_id)

    def camera_light_command(self, device, on):
        """Build an owned, bounded camera-light coroutine for this client's loop."""
        return self._lighting_actions.camera_light(device, on)

    def add_chime_callback(self, callback):
        self._chimes.add_callback(callback)

    def remove_chime_callback(self, callback):
        self._chimes.remove_callback(callback)

    def refresh_chimes(self, *, force=False):
        return self.submit(self._chimes.refresh(force=force))

    def refresh_chime_details(self, api_id, *, force=False):
        return self.submit(self._chimes.refresh_details(api_id, force=force))

    def request_chime_volume(self, api_id, value, *, expected_revision, intent_id):
        return self.submit(
            self._chimes.command(
                api_id, value, expected_revision=expected_revision, intent=intent_id
            )
        )

    def request_chime_test_sound(self, api_id, kind, *, expected_revision, intent_id):
        return self.submit(
            self._chimes.command(
                api_id, kind, expected_revision=expected_revision, intent=intent_id, sound=True
            )
        )

    def acknowledge_chime_sound_finished(self, api_id, operation_id):
        return self.submit(self._chimes.acknowledge(api_id, operation_id))

    @staticmethod
    def cancel_chime_intent(intent_id):
        ChimeClientState.cancel_intent(intent_id)

    def _initialize_alarm_generation(self, generation: int) -> None:
        with self._alarm_snapshot_lock:
            self._alarm_generation = generation
            self._alarm_snapshot = empty_alarm_snapshot(generation)
            self._alarm_dispatch_pending = False
            self._alarm_pending_revocations.clear()
            self._alarm_withdraw_all = False
            self._alarm_event_buffer.clear()
            self._alarm_event_reducer.clear()
            self._alarm_notification_gap_count = 0

    def _invalidate_alarm_state(self, generation: int) -> None:
        retired_generation = self.get_alarm_snapshot().generation
        self._initialize_alarm_generation(generation)
        self._alarm_callbacks.clear()
        discovery = self._alarm_discovery_task
        if discovery is not None and not discovery.done():
            loop = discovery.get_loop()
            if not loop.is_closed():
                loop.call_soon_threadsafe(discovery.cancel)
        self._alarm_discovery_at = None
        self._alarm_discovery_generation = None
        self._alarm_discovery_service = None
        with self._alarm_snapshot_lock:
            self._alarm_event_buffer.clear()
            self._alarm_event_reducer.clear()
            self._alarm_dispatch_pending = False
            self._alarm_notification_gap_count = 0
        history = self._alarm_history_service
        if history is not None:
            history.clear()
        from gi.repository import GLib

        from halo_gtk.notifications import withdraw_alarm_notifications

        GLib.idle_add(
            lambda: (withdraw_alarm_notifications(generation=retired_generation), False)[1]
        )

    def _publish_alarm_error(self, generation: int) -> None:
        with self._alarm_snapshot_lock:
            current = self._alarm_snapshot
        locations = (
            tuple(
                replace(
                    location,
                    can_set_mode=False,
                    command_unavailable_reason="service-failed",
                )
                for location in current.locations
            )
            if current.generation == generation
            else ()
        )
        revision = current.revision + 1 if current.generation == generation else 1
        self._on_alarm_snapshot(
            AlarmAccountSnapshot(
                generation=generation,
                revision=revision,
                status=AlarmServiceStatus.ERROR,
                locations=locations,
                updated_at=time.time(),
                error_code="service-failed",
            )
        )

    def get_alarm_notification_gap_count(self):
        with self._alarm_snapshot_lock:
            return self._alarm_notification_gap_count

    def add_alarm_callback(self, callback) -> None:
        """Register a GTK-thread callback for immutable Alarm snapshots."""
        if callback not in self._alarm_callbacks:
            self._alarm_callbacks.append(callback)

    def remove_alarm_callback(self, callback) -> None:
        """Remove a previously registered Alarm snapshot callback."""
        with contextlib.suppress(ValueError):
            self._alarm_callbacks.remove(callback)

    def request_alarm_mode(
        self,
        location_id: str,
        target: AlarmMode,
        *,
        expected_revision: int,
        bypass_ids: Iterable[str] = (),
        approval_id: str | None = None,
    ) -> concurrent.futures.Future[AlarmCommandResult]:
        """Submit one confirmed Alarm mode request without blocking GTK."""
        return self.submit(
            self._async_request_alarm_mode(
                location_id,
                target,
                expected_revision=expected_revision,
                bypass_ids=bypass_ids,
                approval_id=approval_id,
            )
        )

    async def _async_request_alarm_mode(
        self,
        location_id: str,
        target: AlarmMode,
        *,
        expected_revision: int,
        bypass_ids: Iterable[str],
        approval_id: str | None = None,
    ) -> AlarmCommandResult:
        generation = self._published_generation
        if not _is_active_client_generation(self, generation):
            raise SessionSupersededError("Ring session was superseded before Alarm command")
        service = self._alarm_service
        if service is None:
            raise RuntimeError("Ring Alarm service is not running")
        kwargs = {"bypass_zids": bypass_ids, "expected_revision": expected_revision}
        if approval_id is not None:
            kwargs["approval_id"] = approval_id
        result = await service.request_mode(location_id, target, **kwargs)
        if not _is_active_client_generation(self, generation):
            raise SessionSupersededError("Ring session was superseded during Alarm command")
        return result

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
            await self._async_reconcile_alarm_discovery()
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

    def snapshot_for_device(
        self, device, *, cancel_event: threading.Event | None = None
    ) -> bytes | None:
        """Return snapshot bytes for a device, including cached snapshot fallback."""
        if self._ring is None:
            return None
        return self._run_preview(self._async_snapshot_bytes(device), cancel_event)

    def last_event_frame_for_device(
        self, device, *, cancel_event: threading.Event | None = None
    ) -> bytes | None:
        """Return a first-frame preview from the latest event, with snapshot fallback."""
        if self._ring is None:
            return None
        return self._run_preview(self._async_last_event_frame_for_device(device), cancel_event)

    def event_preview_for_device(
        self, device, event, *, cancel_event: threading.Event | None = None
    ) -> bytes | None:
        """Return the best preview image for a Ring push event."""
        if self._ring is None:
            return None
        event_id = getattr(event, "id", None)
        return self._run_preview(
            self._async_event_preview_for_device(device, event_id, event), cancel_event
        )

    @_bounded_preview
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

    @_bounded_preview
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

            png_bytes = await self._preview.recording(url)
            _log.debug("Decoded first frame from recording for %s", device.name)
            return png_bytes if self._preview_is_current() else None
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

    @_bounded_preview
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

    @_bounded_preview
    async def _async_history_snapshot_bytes(
        self,
        history_event: dict[str, Any] | None,
    ) -> bytes | None:
        snapshot_url = history_event.get("snapshot_url") if history_event else None
        if not snapshot_url:
            return None

        try:
            try:
                endpoint = authenticated_snapshot_url(snapshot_url)
            except Exception:
                # Signed CDN URLs receive no Ring bearer token or SDK cookies.
                image = await self._preview.image_url(snapshot_url)
            else:
                data = await _bounded_snapshot_bytes(self._ring.auth, endpoint)
                image = await self._preview.normalize_image(data)
            return image if self._preview_is_current() else None
        except Exception as exc:
            _log.debug("Notification history snapshot fetch failed: %s", exc)
        return None

    @_bounded_preview
    async def _async_snapshot_bytes(self, device) -> bytes | None:
        try:
            from ring_doorbell.const import SNAPSHOT_ENDPOINT, SNAPSHOT_TIMESTAMP_ENDPOINT

            # Follow the pinned SDK's timestamp/retry sequence, but fetch the
            # image through our streaming cap instead of SDK resp.read().
            async with asyncio.timeout(8):
                payload = {"doorbot_ids": [device.id]}
                await self._ring.async_query(
                    SNAPSHOT_TIMESTAMP_ENDPOINT, method="POST", json=payload
                )
                requested = time.time()
                for _attempt in range(2):
                    await asyncio.sleep(1)
                    response = await self._ring.async_query(
                        SNAPSHOT_TIMESTAMP_ENDPOINT, method="POST", json=payload
                    )
                    if response.json()["timestamps"][0]["timestamp"] / 1000 > requested:
                        data = await _bounded_snapshot_bytes(
                            self._ring.auth, SNAPSHOT_ENDPOINT.format(device.id)
                        )
                        image = await self._preview.normalize_image(data)
                        return image if self._preview_is_current() else None
        except Exception as exc:
            _log.debug("Notification snapshot fetch failed for %s: %s", device.name, exc)

        return await self._async_cached_snapshot_bytes(device.id, device_name=device.name)

    @_bounded_preview
    async def _async_cached_snapshot_bytes(
        self,
        device_id: int,
        *,
        device_name: str | None = None,
    ) -> bytes | None:
        try:
            from ring_doorbell.const import SNAPSHOT_ENDPOINT

            data = await _bounded_snapshot_bytes(
                self._ring.auth, SNAPSHOT_ENDPOINT.format(device_id)
            )
            image = await self._preview.normalize_image(data)
            return image if self._preview_is_current() else None
        except Exception as exc:
            label = device_name or str(device_id)
            _log.debug("Cached snapshot fetch failed for %s: %s", label, exc)

        return None

    @_bounded_preview
    async def _async_first_recording_frame(self, device, event_id: int) -> bytes | None:
        try:
            if not self._preview_is_current():
                return None
            url = await device.async_recording_url(event_id)
            if not url:
                return None
            image = await self._preview.recording(url)
            return image if self._preview_is_current() else None
        except Exception as exc:
            _log.debug("Notification recording preview failed for event %s: %s", event_id, exc)
            return None

    def _preview_is_current(self) -> bool:
        generation = self._published_generation
        return (
            not self._stopped
            and not self._stop_event.is_set()
            and (generation is None or _is_active_client_generation(self, generation))
        )

    # ------------------------------------------------------------------
    # Background FCM listener
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start independent camera-event and Alarm services without blocking."""
        if self._ring is None:
            return
        self._stop_event.clear()
        if self._listener_future is None or self._listener_future.done():
            self._listener_future = self.submit(self._async_listen())
            _log.debug("FCM listener task submitted")
        generation = self._published_generation
        if _is_active_client_generation(self, generation) and (
            self._alarm_future is None or self._alarm_future.done()
        ):
            self._alarm_future = self.submit(self._async_run_alarm(generation))
            _log.debug("Ring Alarm task submitted")

    def stop(self) -> None:
        """Stop the listener, close the ring auth session, shut down the loop."""
        with self._loop_lock:
            self._stopped = True
        self._stop_event.set()
        self._preview.close()
        from halo_gtk.save_jobs import cancel_for_client

        cancel_for_client(self)
        with self._preview_tasks_lock:
            preview_tasks = tuple(self._preview_tasks)
        for task in preview_tasks:
            loop = task.get_loop()
            if not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
        self._retire_event_listener_revision()
        self._signal_alarm_stop()
        alarm_future = self._alarm_future
        self._alarm_future = None
        listener_future = self._listener_future
        self._listener_future = None

        self._wait_for_background_future(alarm_future, _ALARM_STOP_TIMEOUT, "Ring Alarm")
        self._wait_for_background_future(listener_future, _LISTENER_STOP_TIMEOUT, "FCM listener")

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

    def _signal_alarm_stop(self) -> None:
        stop_event = self._alarm_stop_event
        loop = self._loop
        if stop_event is None or loop is None or loop.is_closed() or not loop.is_running():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(stop_event.set)

    @staticmethod
    def _wait_for_background_future(
        future: concurrent.futures.Future | None,
        timeout: float,
        label: str,
    ) -> None:
        if future is None:
            return
        if future.done():
            try:
                future.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception as exc:
                _log.debug("%s stopped with error (%s)", label, type(exc).__name__)
            return
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            with contextlib.suppress(Exception):
                future.result(timeout=1)
        except Exception as exc:
            _log.debug("%s stopped with error (%s)", label, type(exc).__name__)

    async def _async_close(self) -> None:
        """Close feature requests, shared transport, then Auth-owned HTTP."""
        discovery = self._alarm_discovery_task
        self._alarm_discovery_task = None
        if discovery is not None and not discovery.done():
            discovery.cancel()
            await asyncio.gather(discovery, return_exceptions=True)
        service = self._alarm_service
        self._alarm_service = None
        self._alarm_stop_event = None
        if service is None:
            service = self._alarm_closing_service
        if service is not None:
            try:
                await self._async_close_alarm_service(service)
            except Exception as exc:
                _log.debug("Ring Alarm close failed (%s)", type(exc).__name__)
        history = self._alarm_history_service
        if history is not None:
            await history.close()
        gateway = self._alarm_gateway
        if gateway is not None:
            await gateway.close()
        self._alarm_history_service = None
        self._alarm_gateway = None
        await self._intercoms.close()
        await self._light_groups.close()
        await self._lighting_actions.close()
        await self._chimes.close()
        transport = self._ring_json_transport
        self._ring_json_transport = None
        if transport is not None:
            await transport.close()
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
            started = False
            try:
                try:
                    started = await listener.start()
                except Exception as exc:
                    _log.warning("FCM listener failed to start: %s", exc)
                if started:
                    backoff = _LISTENER_RECONNECT_MIN
                    _log.info("FCM event listener started")
                while started and not self._stop_event.is_set():
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
            if not started:
                self._set_owned_listener_state(LISTENER_OFFLINE)
                # The listener can allocate a receiver before start() fails.
                # Close it before waiting or creating its replacement.
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, _LISTENER_RECONNECT_MAX)

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

    def _on_alarm_snapshot(self, snapshot: AlarmAccountSnapshot) -> None:
        """Receive an Alarm snapshot on the asyncio thread and marshal it to GTK."""
        from gi.repository import GLib

        from halo_gtk import config
        from halo_gtk.notifications import alarm_event_enabled

        generation = self._published_generation
        if snapshot.generation != generation or not _is_active_client_generation(self, generation):
            return
        cfg = config.load()
        with self._alarm_snapshot_lock:
            if self._alarm_generation not in (None, generation):
                return
            self._alarm_generation = generation
            previous_ids = {location.location_id for location in self._alarm_snapshot.locations}
            self._alarm_snapshot = snapshot
            for event in self._alarm_event_reducer.update(snapshot):
                if alarm_event_enabled(event, cfg):
                    self._alarm_event_buffer.put(event)
            removed = previous_ids - {location.location_id for location in snapshot.locations}
            if snapshot.status in {
                AlarmServiceStatus.ONLINE,
                AlarmServiceStatus.DEGRADED,
                AlarmServiceStatus.NO_SYSTEM,
            }:
                for location_id in removed:
                    self._alarm_event_buffer.revoke(location_id)
                    if not self._alarm_withdraw_all:
                        self._alarm_pending_revocations.add(location_id)
                        if len(self._alarm_pending_revocations) > MAX_PENDING_EVENTS:
                            self._alarm_pending_revocations.clear()
                            self._alarm_withdraw_all = True
                    if self._alarm_history_service is not None:
                        self._alarm_history_service.revoke_location(location_id)
            schedule = not self._alarm_dispatch_pending
            self._alarm_dispatch_pending = True
        if schedule:
            GLib.idle_add(self._dispatch_alarm_snapshot, snapshot, generation)

    def _dispatch_alarm_snapshot(
        self,
        snapshot: AlarmAccountSnapshot,
        generation: int,
    ) -> bool:
        if snapshot.generation != generation or not _is_active_client_generation(self, generation):
            return False
        with self._alarm_snapshot_lock:
            if self._alarm_generation not in (None, generation):
                return False
            if self._alarm_dispatch_pending:
                snapshot = self._alarm_snapshot
                self._alarm_dispatch_pending = False
            events, suppressed = self._alarm_event_buffer.drain()
            self._alarm_notification_gap_count += suppressed
            revoked = tuple(self._alarm_pending_revocations)
            withdraw_all = self._alarm_withdraw_all
            self._alarm_pending_revocations.clear()
            self._alarm_withdraw_all = False
        from halo_gtk.notifications import send_alarm_notification, withdraw_alarm_notifications

        # Revoke old displays before showing current events, including a
        # location removed then regranted during the same GTK dispatch gap.
        if withdraw_all:
            withdraw_alarm_notifications(generation=generation)
        else:
            for location_id in revoked:
                withdraw_alarm_notifications(generation=generation, location_id=location_id)
        for event in events:
            if not _is_active_client_generation(self, generation):
                return False
            with self._alarm_snapshot_lock:
                allowed = (
                    self._alarm_generation == generation
                    and self._alarm_snapshot.generation == generation
                    and self._alarm_snapshot.find_location(event.location_id) is not None
                )
            if allowed:
                # Preserve a transient critical observation even if its
                # signal cleared, while discarding revoked location data.
                send_alarm_notification(event)
        if suppressed:
            _log.warning("Alarm notification updates coalesced or dropped (%d)", suppressed)
        for callback in list(self._alarm_callbacks):
            if not _is_active_client_generation(self, generation):
                return False
            with self._alarm_snapshot_lock:
                if self._alarm_generation not in (None, generation):
                    return False
                if self._alarm_generation is not None:
                    snapshot = self._alarm_snapshot
            try:
                callback(snapshot)
            except Exception as exc:
                _log.debug("Alarm snapshot callback error (%s)", type(exc).__name__)
        return False

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
        kind = (
            event.get("device_kind")
            if isinstance(event, dict)
            else getattr(event, "device_kind", None)
        )
        if isinstance(kind, str) and kind.startswith("intercom_"):
            # No worker/media enrichment and no automatic write for access events.
            self._intercoms.queue_event(event)
            return
        GLib.idle_add(self._dispatch_event, event, generation)

    def _dispatch_intercom_event(self, device, generation):
        if not _is_active_client_generation(self, generation):
            return False
        current = self.get_intercom_snapshot().find_device(device.api_id)
        if current is None or current.identity_epoch != device.identity_epoch:
            return False
        from halo_gtk.intercom_notifications import send_intercom_notification

        send_intercom_notification(current, device.observations[0], generation)
        return False

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
