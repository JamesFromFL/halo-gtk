"""Shared live stream session ownership for Halo camera views.

The Ring/WebRTC stream is the scarce resource.  UI widgets should attach to a
session instead of each creating their own stream, so Live Monitoring and the
focused camera page can present the same camera stream without reconnecting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

_log = logging.getLogger(__name__)

LIVE_MONITORING_OWNER = "live-monitoring"
FOCUSED_OWNER = "focused-live"


class StreamLimitExceeded(RuntimeError):
    """Raised when starting another stream would exceed Halo's stream cap."""


class StreamState(StrEnum):
    """Lifecycle state for one GUI live stream."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    ACTIVE = "active"
    FAILED = "failed"
    STOPPING = "stopping"


class LiveStreamViewProtocol(Protocol):
    """Runtime contract required by the live-session manager."""

    def start_for_device(self, device: object) -> None: ...

    def stop(self) -> None: ...

    def get_parent(self) -> Any: ...

    def set_volume(self, value: float) -> None: ...

    def start_talking(self) -> None: ...

    def stop_talking(self) -> None: ...

    def get_current_frame_png(self) -> bytes | None: ...

    def get_paintable(self) -> Any: ...

    def set_on_stream_state_changed(self, callback: Callable[[str], None]) -> None: ...


@dataclass
class LiveStreamSession:
    """One active-or-reusable Ring live stream for a camera device."""

    device: object
    view: LiveStreamViewProtocol
    owners: set[str] = field(default_factory=set)
    state: StreamState = StreamState.STOPPED
    volume: float = 0.0

    @property
    def device_id(self) -> int:
        return int(self.device.id)

    @property
    def active(self) -> bool:
        """Return whether this session currently occupies a stream-cap slot."""
        return self.state in {StreamState.CONNECTING, StreamState.ACTIVE}

    def start(self, *, volume: float = 0.0) -> None:
        self.set_volume(volume)
        if self.active:
            return
        self.state = StreamState.CONNECTING
        try:
            self.view.start_for_device(self.device)
        except Exception:
            self.state = StreamState.FAILED
            raise

    def stop(self) -> None:
        if self.state is StreamState.STOPPED:
            self.owners.clear()
            self.detach()
            return
        self.state = StreamState.STOPPING
        try:
            self.view.stop()
        finally:
            self.state = StreamState.STOPPED
            self.owners.clear()
            self.detach()

    def attach_to(self, container: Gtk.Box) -> None:
        """Move the session's display widget into *container*."""
        parent = self.view.get_parent()
        if parent is container:
            return
        if parent is not None and hasattr(parent, "remove"):
            parent.remove(self.view)
        container.append(self.view)
        container.set_visible(True)

    def detach(self) -> None:
        parent = self.view.get_parent()
        if parent is not None and hasattr(parent, "remove"):
            parent.remove(self.view)

    def set_volume(self, value: float) -> None:
        self.volume = max(0.0, min(1.0, value))
        self.view.set_volume(self.volume)

    def start_talking(self) -> None:
        self.view.start_talking()

    def stop_talking(self) -> None:
        self.view.stop_talking()

    def get_current_frame_png(self) -> bytes | None:
        return self.view.get_current_frame_png()

    def get_paintable(self):
        return self.view.get_paintable()


class LiveSessionManager:
    """Central registry for Ring live stream sessions."""

    def __init__(
        self,
        *,
        view_factory: Callable[[], LiveStreamViewProtocol] | None = None,
        max_streams_factory: Callable[[], int] | None = None,
    ) -> None:
        self._sessions: dict[int, LiveStreamSession] = {}
        self._view_factory = view_factory
        self._max_streams_factory = max_streams_factory

    def active_count(self) -> int:
        return sum(1 for session in self._sessions.values() if session.active)

    def session_for(self, device_id: int) -> LiveStreamSession | None:
        return self._sessions.get(int(device_id))

    def acquire(
        self,
        device,
        *,
        owner: str,
        volume: float = 0.0,
        max_streams: int | None = None,
    ) -> LiveStreamSession:
        device_id = int(device.id)
        session = self._sessions.get(device_id)
        created = session is None
        # Connecting and active sessions occupy a slot. Failed/stopped sessions
        # can restart only when another slot is available.
        if session is None or not session.active:
            limit = self._max_streams() if max_streams is None else max_streams
            if self.active_count() >= limit:
                raise StreamLimitExceeded(f"maximum live streams reached ({limit})")
            if session is None:
                session = LiveStreamSession(device=device, view=self._make_view())
                self._sessions[device_id] = session
                self._wire_stream_ended(session)

        session.owners.add(owner)
        try:
            session.start(volume=volume)
        except Exception:
            session.owners.discard(owner)
            if created or not session.owners:
                session.stop()
                self._sessions.pop(device_id, None)
            raise
        return session

    def _wire_stream_ended(self, session: LiveStreamSession) -> None:
        """Let the view report lifecycle transitions so cap accounting stays exact."""
        session.view.set_on_stream_state_changed(
            lambda state: self._on_stream_state_changed(session, state)
        )

    def _on_stream_state_changed(self, session: LiveStreamSession, state: str) -> None:
        device_id = session.device_id
        if self._sessions.get(device_id) is not session:
            return
        try:
            session.state = StreamState(state)
        except ValueError:
            _log.warning("Ignoring unknown live stream state for %s: %s", device_id, state)

    def release(self, device_id: int, *, owner: str) -> None:
        session = self._sessions.get(int(device_id))
        if session is None:
            return
        session.owners.discard(owner)
        if not session.owners:
            session.stop()
            self._sessions.pop(int(device_id), None)

    def stop_all(self) -> None:
        for session in list(self._sessions.values()):
            session.stop()
        self._sessions.clear()

    def enforce_limit(self, max_streams: int | None = None) -> bool:
        """Return True if within cap; stop everything if a bug exceeded it."""
        limit = self._max_streams() if max_streams is None else max_streams
        if self.active_count() <= limit:
            return True
        _log.error("Live stream count exceeded %s; stopping all sessions", limit)
        self.stop_all()
        return False

    def _make_view(self) -> LiveStreamViewProtocol:
        if self._view_factory is not None:
            return self._view_factory()
        from halo_gtk.live_stream import LiveStreamView

        return LiveStreamView()

    def _max_streams(self) -> int:
        if self._max_streams_factory is not None:
            return int(self._max_streams_factory())
        return 4


_MANAGER = LiveSessionManager()


def get_live_session_manager() -> LiveSessionManager:
    return _MANAGER
