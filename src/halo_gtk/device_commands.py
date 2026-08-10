"""Shared capability, state, and submission helpers for Ring device commands."""

from __future__ import annotations

import concurrent.futures
import threading
import time
import weakref
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

_COMMAND_STATE_RECONCILE_SECONDS = 30.0


def has_capability(device, capability: str) -> bool:
    """Return a Ring capability without allowing SDK errors into the UI."""
    checker = getattr(device, "has_capability", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(capability))
    except Exception:
        return False


def reported_light_enabled(device) -> bool:
    """Parse the light state formats exposed by supported Ring devices."""
    return _first_reported_state(device, ("light", "lights"))


def reported_siren_active(device) -> bool:
    """Parse the siren state formats exposed by supported Ring devices."""
    return _first_reported_state(device, ("siren",), include_active=True)


def _first_reported_state(
    device,
    attributes: tuple[str, ...],
    *,
    include_active: bool = False,
) -> bool:
    active_strings = {"1", "on", "true"}
    if include_active:
        active_strings.add("active")
    for attribute in attributes:
        try:
            value = getattr(device, attribute)
        except Exception:
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return value > 0
        if isinstance(value, str):
            return value.strip().lower() in active_strings
    return False


@dataclass
class _OptimisticState:
    light_enabled: bool | None = None
    light_expires_at: float | None = None
    siren_active_until: float | None = None
    siren_expires_at: float | None = None
    # Retain identity-only devices so a recycled object id cannot inherit state.
    identity_device: Any | None = None


@dataclass
class _ClientState:
    reference: Callable[[], Any | None]
    devices: dict[tuple[Any, ...], _OptimisticState]


class DeviceCommandState:
    """Track successful commands that ring-doorbell does not reflect locally.

    State is scoped to the exact Ring client and a stable device identifier. This
    prevents a completed command from an old authenticated session being applied
    to a replacement client that happens to expose the same device id.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._clients: dict[int, _ClientState] = {}

    def light_enabled(self, client, device) -> bool:
        with self._lock:
            state = self._state_for(client, device, create=False)
            if state is not None and state.light_enabled is not None:
                reported = reported_light_enabled(device)
                if reported == state.light_enabled:
                    state.light_enabled = None
                    state.light_expires_at = None
                    return reported
                if state.light_expires_at is not None and state.light_expires_at > self._clock():
                    return state.light_enabled
                state.light_enabled = None
                state.light_expires_at = None
            return reported_light_enabled(device)

    def siren_active(self, client, device) -> bool:
        with self._lock:
            state = self._state_for(client, device, create=False)
            if state is not None and state.siren_active_until is not None:
                now = self._clock()
                if state.siren_expires_at is not None and state.siren_expires_at > now:
                    return state.siren_active_until > now
                state.siren_active_until = None
                state.siren_expires_at = None
            return reported_siren_active(device)

    def record_light_success(self, client, device, enabled: bool) -> None:
        with self._lock:
            state = self._state_for(client, device, create=True)
            if state is not None:
                state.light_enabled = bool(enabled)
                state.light_expires_at = self._clock() + _COMMAND_STATE_RECONCILE_SECONDS

    def record_siren_success(self, client, device, duration: int) -> None:
        with self._lock:
            state = self._state_for(client, device, create=True)
            if state is not None:
                now = self._clock()
                state.siren_active_until = now + max(0, duration)
                state.siren_expires_at = state.siren_active_until + _COMMAND_STATE_RECONCILE_SECONDS

    def _state_for(self, client, device, *, create: bool) -> _OptimisticState | None:
        if client is None:
            return None
        with self._lock:
            client_state = self._client_state(client, create=create)
            if client_state is None:
                return None
            device_key, identity_device = _device_key(device)
            state = client_state.devices.get(device_key)
            if state is not None:
                if identity_device is None or state.identity_device is device:
                    return state
                # Defensive only: the retained identity normally prevents reuse.
                client_state.devices.pop(device_key, None)
            if not create:
                return None
            state = _OptimisticState(identity_device=identity_device)
            client_state.devices[device_key] = state
            return state

    def _client_state(self, client, *, create: bool) -> _ClientState | None:
        client_identity = id(client)
        state = self._clients.get(client_identity)
        if state is not None and state.reference() is client:
            return state
        if state is not None:
            self._clients.pop(client_identity, None)
        if not create:
            return None

        try:
            reference = weakref.ref(
                client,
                lambda ref, key=client_identity: self._discard_client(key, ref),
            )
        except TypeError:
            # RingClient is weak-referenceable. This fallback keeps the helper
            # correct for small proxy/fake clients that are not.
            def strong_reference(client=client):
                return client

            reference = strong_reference
        state = _ClientState(reference=reference, devices={})
        self._clients[client_identity] = state
        return state

    def _discard_client(self, client_identity: int, reference: weakref.ReferenceType) -> None:
        with self._lock:
            state = self._clients.get(client_identity)
            if state is not None and state.reference is reference:
                self._clients.pop(client_identity, None)


def _device_key(device) -> tuple[tuple[Any, ...], Any | None]:
    for attribute in ("id", "device_api_id"):
        try:
            value = getattr(device, attribute)
            hash(value)
        except Exception:
            continue
        if value is not None:
            return (attribute, type(value), value), None
    return ("identity", id(device)), device


command_state = DeviceCommandState()


def submit_observed(
    client,
    coroutine: Coroutine[Any, Any, Any],
    on_complete: Callable[[Exception | None], Any],
) -> concurrent.futures.Future | None:
    """Submit a device command and report its result on the GTK main thread."""
    try:
        future = client.submit(coroutine)
    except Exception as exc:
        coroutine.close()
        GLib.idle_add(on_complete, exc)
        return None

    def observe(done: concurrent.futures.Future) -> None:
        try:
            done.result()
        except Exception as exc:
            error = exc
        else:
            error = None
        GLib.idle_add(on_complete, error)

    future.add_done_callback(observe)
    return future
