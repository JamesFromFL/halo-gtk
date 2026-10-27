"""Shared GTK subscription, freshness watch and cancellable read ownership."""

from __future__ import annotations

import threading
import weakref

from gi.repository import GLib

from halo_gtk.ring_client import get_client


class DeviceViewSession:
    """One subscription, one GTK delivery and a bounded set of view consumers.

    Read futures are cancelled on retirement. Commands remain service-owned;
    cancelling their intent only prevents work that has not been admitted.
    """

    snapshot_method = "get_chime_snapshot"
    add_method = "add_chime_callback"
    remove_method = "remove_chime_callback"
    cancel_method = "cancel_chime_intent"
    snapshot_freshness = staticmethod(lambda snapshot: ())

    def __init__(self, changed, retired, *, provider=None):
        self._changed = changed
        self._retired = retired
        self._provider = provider or (lambda: get_client())
        self._lock = threading.Lock()
        self.client = None
        self.generation = None
        self.active = False
        self._epoch = 0
        self._futures = {}
        self._completed = {}
        self._intent = None
        self._idle_pending = False
        self._idle_id = None
        self._watch_id = None
        self._freshness = ()

    def current(self):
        client = self.client
        if (
            not self.active
            or client is None
            or self._provider() is not client
            or not client.is_authenticated
        ):
            return None
        snapshot = getattr(client, self.snapshot_method)()
        return snapshot if snapshot.generation == self.generation else None

    def open(self, client) -> bool:
        if self.active and client is self.client and self.current() is not None:
            return False
        self.close()
        snapshot = getattr(client, self.snapshot_method)()
        self.client, self.generation, self.active = client, snapshot.generation, True
        getattr(client, self.add_method)(self._on_snapshot)
        reference = weakref.ref(self)
        self._watch_id = GLib.timeout_add(250, self._watch, reference)
        self._queue()
        return True

    def close(self) -> None:
        with self._lock:
            self.active = False
            self._epoch += 1
            client, self.client = self.client, None
            reads = [
                future for key, future in self._futures.items() if key in {"inventory", "details"}
            ]
            intent, self._intent = self._intent, None
            self._futures.clear()
            self._completed.clear()
            idle, self._idle_id = self._idle_id, None
            self._idle_pending = False
            watch, self._watch_id = self._watch_id, None
        if client is not None:
            getattr(client, self.remove_method)(self._on_snapshot)
        if intent is not None:
            intent.cancel()
            if client is not None:
                getattr(client, self.cancel_method)(intent)
        for future in reads:
            future.cancel()
        for source in (idle, watch):
            if source is not None:
                GLib.source_remove(source)

    def pending(self, key) -> bool:
        with self._lock:
            return key in self._futures

    def request(self, key, invoke, *, intent=None) -> bool:
        if key not in {"inventory", "details", "command", "ack"}:
            return False
        if self.current() is None or self.pending(key):
            return False
        if intent is not None:
            self._intent = intent
        try:
            future = invoke()
        except Exception:
            if intent is not None:
                intent.cancel()
            with self._lock:
                self._completed[key] = None
            self._queue()
            return False
        with self._lock:
            self._futures[key] = future
            epoch = self._epoch
        reference = weakref.ref(self)

        def done(completed):
            owner = reference()
            if owner is None:
                return
            with owner._lock:
                if (
                    not owner.active
                    or owner._epoch != epoch
                    or owner._futures.get(key) is not completed
                ):
                    return
                owner._completed[key] = completed
            owner._queue()

        future.add_done_callback(done)
        self._queue()
        return True

    def _on_snapshot(self, _snapshot) -> None:
        # Do not retain a queued payload containing a retired account's names.
        self._queue()

    def _queue(self) -> None:
        with self._lock:
            if not self.active or self._idle_pending:
                return
            self._idle_pending = True
            epoch = self._epoch
        source = GLib.idle_add(self._deliver, weakref.ref(self), epoch)
        with self._lock:
            if self.active and self._epoch == epoch and self._idle_pending:
                self._idle_id = source

    @staticmethod
    def _deliver(reference, epoch):
        owner = reference()
        if owner is None:
            return False
        with owner._lock:
            if not owner.active or owner._epoch != epoch:
                return False
            owner._idle_pending = False
            owner._idle_id = None
            completed, owner._completed = owner._completed, {}
            for key, future in completed.items():
                if future is not None and owner._futures.get(key) is future:
                    owner._futures.pop(key)
        snapshot = owner.current()
        if snapshot is None:
            owner.close()
            owner._retired()
        else:
            owner._freshness = owner.snapshot_freshness(snapshot)
            owner._changed(snapshot, completed)
        return False

    @staticmethod
    def _watch(reference):
        owner = reference()
        if owner is None or not owner.active:
            return False
        snapshot = owner.current()
        if snapshot is None:
            owner._watch_id = None
            owner.close()
            owner._retired()
            return False
        if owner.snapshot_freshness(snapshot) != owner._freshness:
            owner._queue()
        return True
