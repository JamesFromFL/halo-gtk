"""Owned, experimental media for the exact Ring Intercom Video model.

This authority is a local opt-in plus current Intercom account/model evidence.
It does not manufacture Ring camera privacy settings or enable audio-only models.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from importlib.metadata import version
from types import SimpleNamespace

from halo_gtk.camera_privacy import PrivacyLease, privacy_time
from halo_gtk.intercom import FRESH_SECONDS

VIDEO_KIND = "intercom_handset_video"
MAX_MEDIA_SECONDS = 300.0
START_TIMEOUT = 20.0
_log = logging.getLogger(__name__)
_MANAGER_LOCK = threading.Lock()
try:
    _SDK_VERSION = version("ring-doorbell")
except Exception:
    _SDK_VERSION = None


class IntercomMediaError(RuntimeError):
    pass


def qualified_intercom(client, api_id, *, epoch=None, now=None):
    """Return one current SDK wrapper joined to fresh, immutable inventory."""
    try:
        if _SDK_VERSION != "0.9.14" or not client._preview_is_current():
            return None
        snapshot = client.get_intercom_snapshot()
        if not snapshot.complete or not client._intercoms.current(snapshot.generation):
            return None
        item = snapshot.find_device(api_id)
        if (
            item is None
            or item.kind != VIDEO_KIND
            or item.stale
            or item.denied
            or item.inventory_monotonic is None
            or not 0
            <= (privacy_time() if now is None else now) - item.inventory_monotonic
            <= FRESH_SECONDS
            or (epoch is not None and item.identity_epoch != epoch)
        ):
            return None
        matches = [device for device in client.all_devices if getattr(device, "id", None) == api_id]
        if len(matches) != 1:
            return None
        source = matches[0]
        # RingDevices 0.9.14 marks every RingOther shared=True. That synthetic
        # flag is not account-role evidence. Current inventory establishes
        # account visibility; Ring still decides permission for this request.
        if (
            type(api_id) is not int
            or not 0 < api_id <= 9_223_372_036_854_775_807
            or source.device_api_id != api_id
            or source.family != "other"
            or source.kind != VIDEO_KIND
            or source._ring is not client._ring
            or source.location_id != item.location_id
            or (item._hardware_id is not None and source.device_id != item._hardware_id)
        ):
            return None
        return source, item
    except Exception:
        return None


class IntercomMediaOwner:
    """One capture consumer per Intercom until its resources actually close."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = {}

    def acquire(self, api_id, authority):
        with self._lock:
            if api_id in self._active or len(self._active) >= 128:
                return False
            self._active[api_id] = authority
            return True

    def owns(self, api_id, authority):
        with self._lock:
            return self._active.get(api_id) is authority

    def release(self, api_id, authority):
        with self._lock:
            if self._active.get(api_id) is authority:
                self._active.pop(api_id)


def _owner(client):
    with _MANAGER_LOCK:
        owner = getattr(client, "_intercom_media_owner", None)
        if owner is None:
            owner = client._intercom_media_owner = IntercomMediaOwner()
        return owner


class IntercomMediaAuthority:
    """Explicit per-view opt-in; never a substitute Ring camera GET receipt."""

    def __init__(self, client, api_id, identity_epoch):
        self.client, self.api_id, self.epoch = client, api_id, identity_epoch
        self._owner = _owner(client)
        self._lock = threading.RLock()
        self._listeners = set()
        self._enabled = False
        self._lease = None
        self._revision = 0
        self._started_at = None
        self._message = "Enable experimental Intercom Video to start."
        qualified = qualified_intercom(client, api_id, epoch=identity_epoch)
        if qualified is None:
            raise IntercomMediaError("Current Intercom Video account evidence is unavailable")
        self.source = qualified[0]
        self._last_receipt_at = qualified[1].inventory_monotonic
        self.adapter = IntercomVideoAdapter(self)

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = enabled is True
        if not self._enabled:
            self.invalidate("Intercom Video disabled for this view.")

    def _qualified(self):
        current = qualified_intercom(self.client, self.api_id, epoch=self.epoch)
        if current is None or current[0] is not self.source:
            return False
        stamp = current[1].inventory_monotonic
        gap = self._lease is not None and stamp - self._last_receipt_at > FRESH_SECONDS
        self._last_receipt_at = stamp
        return not gap

    def acquire(self, device, purpose):
        with self._lock:
            if (
                device is not self.adapter
                or purpose != "live"
                or not self._enabled
                or not self._qualified()
            ):
                self._message = "Enable Intercom Video and reload current device evidence."
                return None
            if self._lease is not None or not self._owner.acquire(self.api_id, self):
                self._message = "This Intercom already has a media view open. End it first."
                return None
            self._revision += 1
            self._started_at = privacy_time()
            self._lease = PrivacyLease(self, device, purpose, self._revision)
            self._message = "Experimental Intercom Video · hardware not yet verified."
            return self._lease

    def valid(self, lease):
        with self._lock:
            valid = (
                lease is not None
                and lease is self._lease
                and lease.revision == self._revision
                and self._enabled
                and self._owner.owns(self.api_id, self)
                and self._qualified()
                and self._started_at is not None
                and 0 <= privacy_time() - self._started_at <= MAX_MEDIA_SECONDS
            )
            if (
                not valid
                and lease is not None
                and lease is self._lease
                and lease.revision == self._revision
            ):
                self._revision += 1
            return valid

    def allowed(self, device, purpose):
        return (
            device is self.adapter
            and purpose in {"live", "audio", "talkback"}
            and self.valid(self._lease)
        )

    def apply(self, lease, callback):
        with self._lock:
            if not self.valid(lease):
                return False
            callback()
            return True

    def view(self, _device):
        if self._lease is not None and not self.valid(self._lease):
            return SimpleNamespace(
                message="Intercom media ended or current access could not be verified."
            )
        return SimpleNamespace(message=self._message)

    def subscribe(self, callback):
        with self._lock:
            self._listeners.add(callback)

    def unsubscribe(self, callback):
        with self._lock:
            self._listeners.discard(callback)

    def invalidate(self, message="Intercom media ended."):
        with self._lock:
            self._revision += 1
            self._message = message
            callbacks = tuple(self._listeners)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                _log.debug("Intercom media observer failed", exc_info=True)

    def release(self, lease, *, resources_closed=True):
        """Release only after successful Ring, microphone and peer cleanup."""
        with self._lock:
            if self._lease is lease and not resources_closed:
                self.adapter._cleanup_incomplete = True
                self._message = (
                    "Local media cleanup could not be verified. Close Halo before trying again."
                )
            if self._lease is lease and not self.adapter._cleanup_incomplete:
                self._lease = None
                self._started_at = None
                self._owner.release(self.api_id, self)


class IntercomVideoAdapter:
    """Instance-owned RingWebRtcStream adapter; RingOther remains untouched."""

    def __init__(self, authority):
        self.authority = authority
        self.device_api_id = self.id = authority.api_id
        self.kind = VIDEO_KIND
        self.family = "other"
        self.name = "Intercom Video"
        self._webrtc_streams = {}
        self._natural_readers = set()
        self._cleanup_incomplete = False

    def get_ice_servers(self):
        from ring_doorbell.const import ICE_SERVERS

        return list(ICE_SERVERS)

    def _require_current(self):
        if not self.authority.allowed(self, "live"):
            raise IntercomMediaError("Intercom media permission or ownership changed")

    async def generate_async_webrtc_stream(
        self, sdp_offer, session_id, on_message_callback, *, keep_alive_timeout=300
    ):
        from ring_doorbell.webrtcstream import RingWebRtcStream

        self._require_current()
        if self._webrtc_streams:
            raise IntercomMediaError("This Intercom already has an active stream")

        async def closed():
            # The SDK invokes this inside its reader. Do not recursively close
            # or await that same task. Retain it until actual reader teardown.
            reader = stream.read_task
            if reader is asyncio.current_task():
                self._natural_readers.add(reader)
                reader.add_done_callback(self._natural_readers.discard)
                stream.read_task = None
            self.authority.invalidate("Ring ended the Intercom media session.")

        stream = RingWebRtcStream(
            self.authority.client._ring,
            self.device_api_id,
            on_message_callback=on_message_callback,
            keep_alive_timeout=300,
            on_close_callback=closed,
        )
        self._webrtc_streams[session_id] = stream
        try:
            async with asyncio.timeout(START_TIMEOUT):
                self._require_current()
                await stream.generate(sdp_offer)
                self._require_current()
        except BaseException:
            await self.close_webrtc_stream(session_id)
            raise

    async def on_webrtc_candidate(self, session_id, candidate, multi_line_index):
        self._require_current()
        stream = self._webrtc_streams.get(session_id)
        if stream is not None:
            await stream.on_ice_candidate(candidate, multi_line_index)

    async def close_webrtc_stream(self, session_id):
        stream = self._webrtc_streams.pop(session_id, None)
        if stream is None:
            return
        readers = tuple(
            task for task in self._natural_readers if task is not asyncio.current_task()
        )
        # SDK 0.9.14 clears its handles before awaiting their shutdown. Keep our
        # own references so a partial SDK close cannot hide unfinished cleanup.
        original_tasks = (stream.read_task, stream.ping_task)
        original_websocket = stream.websocket
        try:
            async with asyncio.timeout(5):
                await stream.close()
                if readers:
                    await asyncio.gather(*readers, return_exceptions=True)
        except BaseException:
            tasks = set(readers) | set(original_tasks) | {stream.read_task, stream.ping_task}
            tasks = {
                task
                for task in tasks
                if task is not None and task is not asyncio.current_task() and not task.done()
            }
            for task in tasks:
                task.cancel()
            websocket = stream.websocket or original_websocket
            if websocket is not None:
                self._cleanup_incomplete = True
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    async with asyncio.timeout(2):
                        await websocket.close()
                    self._cleanup_incomplete = False
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=2)
                # Do not admit a second capture if local shutdown cannot be
                # established. A replacement account has an independent owner.
                self._cleanup_incomplete = self._cleanup_incomplete or bool(pending)
            raise
