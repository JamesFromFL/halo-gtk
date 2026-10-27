"""RingClient-owned Intercom lifecycle and GTK snapshot delivery.

Network work stays on the client's asyncio loop. Cached immutable snapshots and
cancelable intents are the only objects shared with the GTK thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading

from halo_gtk.intercom import IntercomAccountSnapshot, IntercomIntent

_log = logging.getLogger(__name__)


class IntercomClientState:
    def __init__(self, client, *, is_current):
        self._client = client
        self._is_current = is_current
        self._lock = threading.Lock()
        self._generation = None
        self._snapshot = IntercomAccountSnapshot(generation=0)
        self._retired = threading.Event()
        self._dispatch_pending = False
        self._callbacks = []
        self._service = None
        self._gateway = None
        self._close_task = None
        self._event_tasks = set()

    def initialize(self, generation):
        with self._lock:
            self._generation = generation
            self._snapshot = IntercomAccountSnapshot(generation=generation)
            self._dispatch_pending = False
            self._retired.clear()

    def invalidate(self, generation):
        # The local fence is shared with producer admission. Never take the
        # global session lock while holding this lock (publication does inverse).
        retired_generation = self._generation
        self._retired.set()
        from gi.repository import GLib

        from halo_gtk.intercom_notifications import withdraw_intercom_notifications

        GLib.idle_add(lambda: (withdraw_intercom_notifications(retired_generation), False)[1])
        with self._lock:
            self._generation = generation
            self._snapshot = IntercomAccountSnapshot(generation=generation)
            self._dispatch_pending = False
        self._callbacks.clear()

    def current(self, generation):
        client = self._client
        return (
            not self._retired.is_set()
            and not client._stopped
            and not client._stop_event.is_set()
            and client._authenticated
            and client._ring is not None
            and self._is_current(client, generation)
        )

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def add_callback(self, callback):
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_callback(self, callback):
        with contextlib.suppress(ValueError):
            self._callbacks.remove(callback)

    def _on_snapshot(self, snapshot):
        if not self.current(snapshot.generation):
            return
        with self._lock:
            if self._retired.is_set() or self._generation != snapshot.generation:
                return
            self._snapshot = snapshot
            schedule = not self._dispatch_pending
            self._dispatch_pending = True
        if schedule:
            from gi.repository import GLib

            GLib.idle_add(self._dispatch, snapshot.generation)

    def _dispatch(self, generation):
        if not self.current(generation):
            return False
        with self._lock:
            if self._generation != generation or self._retired.is_set():
                return False
            self._dispatch_pending = False
        for callback in list(self._callbacks):
            if not self.current(generation):
                return False
            snapshot = self.snapshot()
            if snapshot.generation != generation:
                return False
            try:
                callback(snapshot)
            except Exception as exc:
                _log.debug("Intercom snapshot callback failed (%s)", type(exc).__name__)
        return False

    def _get_service(self):
        generation = self._client._published_generation
        if not self.current(generation):
            raise RuntimeError("Ring session was superseded")
        if self._service is None:
            from halo_gtk.intercom import IntercomService
            from halo_gtk.intercom_rest import IntercomRestError, RingIntercomRestGateway

            def admit():
                if not self.current(generation):
                    raise IntercomRestError("session-changed")

            self._gateway = RingIntercomRestGateway(
                self._client._ring.auth,
                transport=self._client._get_ring_json_transport(),
                admission=admit,
            )
            self._service = IntercomService(
                self._gateway,
                self._on_snapshot,
                generation=generation,
                is_current=lambda: self.current(generation),
            )
        return self._service

    async def refresh(self, *, force=False):
        generation = self._client._published_generation
        result = await self._get_service().refresh(force=force)
        if not self.current(generation):
            raise RuntimeError("Ring session was superseded")
        return result

    async def command(self, api_id, *, expected_revision, intent):
        result = await self._get_service().request_unlock(
            api_id, expected_revision=expected_revision, intent=intent
        )
        if not self.current(intent.generation):
            raise RuntimeError("Ring session was superseded")
        return result

    def queue_event(self, event):
        generation = self._client._published_generation
        if not self.current(generation) or len(self._event_tasks) >= 4:
            return
        task = asyncio.create_task(self._handle_event(event, generation))
        self._event_tasks.add(task)

        def finished(done):
            self._event_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    async def _handle_event(self, event, generation):
        service = self._get_service()
        await service.refresh()
        if not self.current(generation):
            return
        device = service.observe_event(event)
        if device is not None:
            from gi.repository import GLib

            GLib.idle_add(self._client._dispatch_intercom_event, device, generation)

    async def acknowledge(self, api_id, operation_id):
        return await self._get_service().acknowledge(api_id, operation_id)

    @staticmethod
    def cancel_intent(intent):
        if isinstance(intent, IntercomIntent):
            intent.cancel()

    async def close(self):
        self._retired.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)

    async def _close_resources(self):
        tasks = tuple(self._event_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._event_tasks.clear()
        service, gateway = self._service, self._gateway
        try:
            if service is not None:
                # The service owns its feature gateway; neither owns the core.
                await service.close()
            elif gateway is not None:
                await gateway.close()
        finally:
            self._service = self._gateway = None
