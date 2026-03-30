"""Ring API integration layer.

Wraps python-ring-doorbell and provides:
  - Token-based auth with on-disk cache
  - Async-safe device polling via a background thread
  - Real-time event listener (doorbell presses, motion) via websocket
  - Signals (GLib) emitted on the main loop for UI updates

Usage
-----
    from ring_gtk.ring_client import RingClient, get_client, init_client

    client = init_client(username, password, otp_callback)
    client.start()          # starts background listener
    client.stop()           # call on app shutdown
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

from gi.repository import GLib

_log = logging.getLogger(__name__)

# Singleton instance
_client: "RingClient | None" = None

TOKEN_CACHE_PATH = Path.home() / ".cache" / "ring-gtk" / "token.json"


def get_client() -> "RingClient | None":
    """Return the active RingClient, or None if not yet initialised."""
    return _client


def init_client(
    username: str,
    password: str,
    otp_callback: Callable[[], str] | None = None,
) -> "RingClient":
    """Create and authenticate a RingClient singleton."""
    global _client
    _client = RingClient(username, password, otp_callback)
    _client.authenticate()
    return _client


def init_client_from_cache() -> "RingClient | None":
    """Restore a RingClient from a cached token without re-authenticating."""
    global _client
    if not TOKEN_CACHE_PATH.exists():
        return None
    _client = RingClient._from_cache()
    return _client


class RingClient:
    """Thin wrapper around :class:`ring_doorbell.Ring`."""

    def __init__(
        self,
        username: str = "",
        password: str = "",
        otp_callback: Callable[[], str] | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._otp_callback = otp_callback
        self._ring = None
        self._listener_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        try:
            from ring_doorbell import Auth, Ring
            from ring_doorbell.const import USER_AGENT
        except ImportError as exc:
            raise RuntimeError("ring-doorbell not installed") from exc

        token_update_cb = self._save_token

        if TOKEN_CACHE_PATH.exists():
            token = json.loads(TOKEN_CACHE_PATH.read_text())
        else:
            token = None

        auth = Auth(USER_AGENT, token, token_update_cb)
        if not token:
            auth.fetch_token(self._username, self._password, self._otp_callback)

        self._ring = Ring(auth)
        self._ring.update_data()
        _log.info("Ring authenticated, %d devices found", len(self.all_devices))

    @classmethod
    def _from_cache(cls) -> "RingClient":
        instance = cls()
        instance.authenticate()
        return instance

    def _save_token(self, token) -> None:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(json.dumps(token))
        _log.debug("Ring token cached to %s", TOKEN_CACHE_PATH)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return self._ring is not None

    @property
    def all_devices(self) -> list:
        if self._ring is None:
            return []
        return (
            self._ring.devices().get("doorbots", [])
            + self._ring.devices().get("authorized_doorbots", [])
            + self._ring.devices().get("stickup_cams", [])
            + self._ring.devices().get("base_stations", [])
            + self._ring.devices().get("chimes", [])
        )

    # ------------------------------------------------------------------
    # Background listener
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the real-time event listener in a background thread."""
        if self._ring is None:
            return
        self._stop_event.clear()
        self._listener_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="ring-listener"
        )
        self._listener_thread.start()

    def stop(self) -> None:
        """Signal the listener thread to exit."""
        self._stop_event.set()

    def _listen_loop(self) -> None:
        try:
            from ring_doorbell import RingEventListener
        except ImportError:
            _log.warning("ring-doorbell listen extra not installed; no real-time events")
            return

        listener = RingEventListener(self._ring)
        listener.add_notification_callback(self._on_ring_event)

        try:
            listener.start()
            _log.info("Ring event listener started")
            self._stop_event.wait()
        finally:
            listener.stop()
            _log.info("Ring event listener stopped")

    def _on_ring_event(self, event) -> None:
        """Called from the listener thread; dispatches to the main loop."""
        GLib.idle_add(self._dispatch_event, event)

    def _dispatch_event(self, event) -> bool:
        from ring_gtk.notifications import send_ring_notification

        send_ring_notification(event)
        return GLib.SOURCE_REMOVE
