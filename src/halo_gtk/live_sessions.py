"""Shared live stream session ownership for Halo camera views.

The Ring/WebRTC stream is the scarce resource.  UI widgets should attach to a
session instead of each creating their own stream, so Live Monitoring and the
focused camera page can present the same camera stream without reconnecting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # noqa: E402

_log = logging.getLogger(__name__)

LIVE_MONITORING_OWNER = "live-monitoring"
FOCUSED_OWNER = "focused-live"


class StreamLimitExceeded(RuntimeError):
    """Raised when starting another stream would exceed Halo's stream cap."""


class StreamState(StrEnum):
    """Lifecycle state for one GUI live stream."""

    STOPPED = "stopped"
    WAITING = "waiting"
    CONNECTING = "connecting"
    RECOVERING = "recovering"
    ACTIVE = "active"
    FAILED = "failed"
    STOPPING = "stopping"


class LiveStreamViewProtocol(Protocol):
    """Runtime contract required by the live-session manager."""

    def start_for_device(self, device: object) -> None: ...

    def stop(self) -> Future | None: ...

    def get_parent(self) -> Any: ...

    def set_volume(self, value: float) -> None: ...

    def start_talking(self, *, on_state_changed=None) -> None: ...

    def stop_talking(self) -> None: ...

    def has_current_frame(self) -> bool: ...

    def get_current_frame_png(self, viewport=None) -> bytes | None: ...

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
    limit: int = 4

    @property
    def device_id(self) -> int:
        return int(self.device.id)

    @property
    def active(self) -> bool:
        """Return whether owners have requested or are using this stream."""
        return self.state in {
            StreamState.WAITING,
            StreamState.CONNECTING,
            StreamState.ACTIVE,
            StreamState.RECOVERING,
        }

    def start(self, *, volume: float = 0.0) -> None:
        self.set_volume(volume)
        self.state = StreamState.CONNECTING
        try:
            self.view.start_for_device(self.device)
        except Exception:
            self.state = StreamState.FAILED
            raise

    def stop(self) -> Future | None:
        if self.state in {StreamState.STOPPED, StreamState.WAITING}:
            self.state = StreamState.STOPPED
            self.owners.clear()
            self.detach()
            return
        self.state = StreamState.STOPPING
        try:
            return self.view.stop()
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

    def start_talking(self, *, on_state_changed=None) -> None:
        if on_state_changed is None:
            self.view.start_talking()
        else:
            self.view.start_talking(on_state_changed=on_state_changed)

    def stop_talking(self) -> None:
        self.view.stop_talking()

    def has_current_frame(self) -> bool:
        return self.view.has_current_frame()

    def get_current_frame_png(self, viewport=None) -> bytes | None:
        return self.view.get_current_frame_png(viewport)

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
        self._closing: dict[int, LiveStreamSession] = {}
        self._view_factory = view_factory
        self._max_streams_factory = max_streams_factory

    def active_count(self) -> int:
        """Count transports during startup, playback and cleanup."""
        return len(self._closing) + sum(
            session.active and session.state != StreamState.WAITING
            for session in self._sessions.values()
        )

    def reservation_count(self) -> int:
        return sum(session.active for session in self._sessions.values())

    def session_for(self, device_id: int) -> LiveStreamSession | None:
        return self._sessions.get(int(device_id))

    def preview_session_for(self, device) -> LiveStreamSession | None:
        session = self.session_for(device.id)
        if session is not None and session.active:
            return session
        return self._closing.get(int(device.id))

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
        if session is not None and session.device is not device:
            # An inventory replacement or another account may reuse an API id.
            # Its old view and owners cannot be transferred to the new wrapper.
            self._stop_session(session)
            session = None
        created = session is None
        if session is not None and session.active:
            session.owners.add(owner)
            session.set_volume(volume)
            return session

        limit = self._max_streams() if max_streams is None else max_streams
        if self.reservation_count() >= limit:
            raise StreamLimitExceeded(f"maximum live streams reached ({limit})")
        if session is None:
            session = LiveStreamSession(device=device, view=self._make_view())
            self._sessions[device_id] = session
            self._wire_stream_ended(session)
        session.limit = limit
        session.owners.add(owner)
        if self._must_wait(session):
            session.state = StreamState.WAITING
            session.set_volume(volume)
            return session
        try:
            session.start(volume=volume)
        except Exception:
            session.owners.discard(owner)
            if created or not session.owners:
                try:
                    self._stop_session(session)
                except Exception:
                    _log.exception("Failed to clean up live session %s after startup", device_id)
            raise
        return session

    def _must_wait(self, session: LiveStreamSession) -> bool:
        return self.active_count() >= session.limit or session.device_id in self._closing

    def _stop_session(self, session: LiveStreamSession) -> None:
        device_id = session.device_id
        # Publish closing ownership before detaching the GTK view. A preview
        # can inspect this registry from the Ring thread during GStreamer stop.
        occupied = session.state not in {StreamState.STOPPED, StreamState.WAITING}
        if occupied:
            self._closing[device_id] = session
        self._sessions.pop(device_id, None)
        try:
            closing = session.stop()
        except Exception:
            if occupied:
                self._closing.pop(device_id, None)
            raise
        if occupied:
            if closing is None or closing.done():
                self._closing.pop(device_id, None)
            else:
                closing.add_done_callback(lambda _done: GLib.idle_add(self._finish_close, session))

    def _finish_close(self, retired: LiveStreamSession) -> bool:
        if self._closing.get(retired.device_id) is retired:
            self._closing.pop(retired.device_id)
            self._admit_waiters()
        return GLib.SOURCE_REMOVE

    def _admit_waiters(self) -> None:
        for session in tuple(self._sessions.values()):
            if session.state == StreamState.WAITING and not self._must_wait(session):
                try:
                    session.start(volume=session.volume)
                except Exception:
                    _log.exception("Could not start waiting camera %s", session.device_id)
                    try:
                        self._stop_session(session)
                    except Exception:
                        _log.exception("Could not clean up waiting camera %s", session.device_id)

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
            if not session.active:
                self._admit_waiters()
        except ValueError:
            _log.warning("Ignoring unknown live stream state for %s: %s", device_id, state)

    def release(self, device_id: int, *, owner: str) -> None:
        session = self._sessions.get(int(device_id))
        if session is None:
            return
        session.owners.discard(owner)
        if not session.owners:
            self._stop_session(session)
            self._admit_waiters()

    def stop_all(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            try:
                self._stop_session(session)
            except Exception:
                _log.exception("Failed to stop live session %s", session.device_id)

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


def active_session_for_device(device) -> LiveStreamSession | None:
    return _MANAGER.preview_session_for(device)
