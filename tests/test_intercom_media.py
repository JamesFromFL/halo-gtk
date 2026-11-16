"""Offline Intercom Video authority, single capture and SDK-adapter lifecycle."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_gtk import intercom_media as media
from halo_gtk.camera_privacy import CameraPrivacy, privacy_time
from halo_gtk.intercom import IntercomAccountSnapshot, IntercomSnapshot
from halo_gtk.live_stream import LiveStreamView, _StreamResources


def fixture():
    ring = object()
    device = SimpleNamespace(
        id=42,
        device_api_id=42,
        kind=media.VIDEO_KIND,
        family="other",
        location_id="home",
        device_id="fixture-hardware",
        shared=False,
        _ring=ring,
    )
    item = IntercomSnapshot(
        42,
        "Fixture Intercom",
        media.VIDEO_KIND,
        "home",
        identity_epoch=3,
        stale=False,
        inventory_monotonic=privacy_time(),
        _hardware_id="fixture-hardware",
    )
    snapshot = IntercomAccountSnapshot(7, devices=(item,), complete=True, has_inventory=True)
    client = SimpleNamespace(
        _ring=ring, all_devices=[device], active=True, camera_privacy=CameraPrivacy()
    )
    client._preview_is_current = lambda: client.active
    client._intercoms = SimpleNamespace(
        current=lambda generation: client.active and generation == 7
    )
    client.snapshot = snapshot
    client.get_intercom_snapshot = lambda: client.snapshot
    return client, device, item


def authority():
    client, device, item = fixture()
    policy = media.IntercomMediaAuthority(client, 42, 3)
    return client, device, item, policy


class Stream:
    instances = []
    generate_hook = None
    close_hook = None

    def __init__(self, ring, api_id, **kwargs):
        self.ring, self.api_id, self.options = ring, api_id, kwargs
        self.read_task = self.ping_task = self.websocket = None
        self.closed = False
        self.candidates = []
        self.__class__.instances.append(self)

    async def generate(self, offer):
        self.offer = offer
        if self.generate_hook:
            await self.generate_hook()

    async def close(self):
        if self.close_hook:
            await self.close_hook()
        self.closed = True

    async def on_ice_candidate(self, *candidate):
        self.candidates.append(candidate)


@pytest.fixture
def streams(monkeypatch):
    from ring_doorbell import webrtcstream

    class FakeStream(Stream):
        instances = []

    monkeypatch.setattr(webrtcstream, "RingWebRtcStream", FakeStream)
    return FakeStream


def test_explicit_opt_in_does_not_fabricate_ring_camera_permissions():
    client, source, _, policy = authority()
    assert policy.acquire(policy.adapter, "live") is None
    assert client.camera_privacy.view(source).live_allowed is None
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    assert lease and policy.valid(lease)
    assert policy.allowed(policy.adapter, "audio") and policy.allowed(policy.adapter, "talkback")
    assert not policy.allowed(policy.adapter, "preview")
    assert policy.acquire(policy.adapter, "preview") is None
    assert client.camera_privacy.view(source).live_allowed is None
    policy.release(lease)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "intercom_handset_audio"),
        ("family", "stickup_cams"),
        ("location_id", "other"),
        ("device_id", "replacement"),
        ("device_api_id", 99),
    ],
)
def test_only_exact_video_model_and_identity_is_qualified(field, value):
    client, device, _ = fixture()
    setattr(device, field, value)
    assert media.qualified_intercom(client, 42) is None
    with pytest.raises(media.IntercomMediaError):
        media.IntercomMediaAuthority(client, 42, 3)


@pytest.mark.parametrize(
    "change",
    [
        dict(stale=True),
        dict(denied=True),
        dict(kind="intercom_handset_audio"),
        dict(identity_epoch=4),
        dict(inventory_monotonic=0),
    ],
)
def test_inventory_unknown_stale_denied_or_replaced_never_admits(change):
    client, _, item = fixture()
    client.snapshot = replace(client.snapshot, devices=(replace(item, **change),))
    assert media.qualified_intercom(client, 42, epoch=3) is None


def test_sdk_contract_is_pinned(monkeypatch):
    client, _, _ = fixture()
    monkeypatch.setattr(media, "_SDK_VERSION", "unqualified")
    assert media.qualified_intercom(client, 42) is None


def test_revocation_expiry_and_fresh_unchanged_read_do_not_resurrect_old_lease(monkeypatch):
    client, source, item, policy = authority()
    now = [privacy_time()]
    monkeypatch.setattr(media, "privacy_time", lambda: now[0])
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    assert policy.valid(lease)
    client.snapshot = replace(client.snapshot, devices=(replace(item, inventory_monotonic=now[0]),))
    assert policy.valid(lease)
    now[0] += 31
    assert not policy.valid(lease)
    policy.invalidate()
    client.snapshot = replace(client.snapshot, devices=(replace(item, inventory_monotonic=now[0]),))
    assert not policy.valid(lease)
    policy.release(lease)
    replacement = policy.acquire(policy.adapter, "live")
    assert policy.valid(replacement)
    client.all_devices = [SimpleNamespace(**vars(source))]
    assert not policy.valid(replacement)
    policy.release(replacement)


def test_session_limit_counts_suspend_even_with_fresh_inventory(monkeypatch):
    client, _, item, policy = authority()
    now = [privacy_time()]
    monkeypatch.setattr(media, "privacy_time", lambda: now[0])
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    now[0] += 301
    client.snapshot = replace(client.snapshot, devices=(replace(item, inventory_monotonic=now[0]),))
    assert not policy.valid(lease)
    policy.release(lease)


def test_only_one_capture_and_old_release_cannot_release_new_owner():
    client, _, _, first = authority()
    second = media.IntercomMediaAuthority(client, 42, 3)
    first.set_enabled(True)
    second.set_enabled(True)
    lease = first.acquire(first.adapter, "live")
    assert second.acquire(second.adapter, "live") is None
    first.invalidate()
    assert second.acquire(second.adapter, "live") is None  # Still closing.
    first.release(lease)
    next_lease = second.acquire(second.adapter, "live")
    first.release(lease)
    assert second.valid(next_lease)
    second.release(next_lease)


@pytest.mark.asyncio
async def test_owned_adapter_passes_signaling_without_patching_ring_other(streams):
    from ring_doorbell.other import RingOther

    original = getattr(RingOther, "generate_async_webrtc_stream", None)
    client, _, _, policy = authority()
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    messages = []
    await policy.adapter.generate_async_webrtc_stream("synthetic-offer", "one", messages.append)
    stream = streams.instances[0]
    assert stream.ring is client._ring and stream.api_id == 42 and stream.offer == "synthetic-offer"
    stream.options["on_message_callback"]("synthetic-answer")
    assert messages == ["synthetic-answer"]
    await policy.adapter.on_webrtc_candidate("one", "synthetic-candidate", 0)
    assert stream.candidates == [("synthetic-candidate", 0)]
    with pytest.raises(media.IntercomMediaError):
        await policy.adapter.generate_async_webrtc_stream("offer", "two", messages.append)
    await policy.adapter.close_webrtc_stream("one")
    assert stream.closed and not policy.adapter._webrtc_streams
    assert getattr(RingOther, "generate_async_webrtc_stream", None) is original
    policy.release(lease)


@pytest.mark.asyncio
async def test_natural_close_does_not_self_await_or_release_capture_before_reader_exits(streams):
    client, _, _, policy = authority()
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    await policy.adapter.generate_async_webrtc_stream("offer", "one", lambda _: None)
    stream = streams.instances[0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def reader():
        stream.read_task = asyncio.current_task()
        await stream.options["on_close_callback"]()
        assert stream.read_task is None
        entered.set()
        await release.wait()

    task = asyncio.create_task(reader())
    await entered.wait()
    resources = _StreamResources(
        1, policy.adapter, client, "one", privacy_lease=lease, ring_session_started=True
    )
    cleanup = asyncio.create_task(resources.cleanup())
    await asyncio.sleep(0)
    competing = media.IntercomMediaAuthority(client, 42, 3)
    competing.set_enabled(True)
    assert competing.acquire(competing.adapter, "live") is None
    assert not cleanup.done()
    release.set()
    await task
    await cleanup
    assert competing.acquire(competing.adapter, "live") is not None
    competing.release(competing._lease)


@pytest.mark.asyncio
async def test_startup_cancellation_closes_stream_and_releases_only_after_resource_cleanup(streams):
    client, _, _, policy = authority()
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    started = asyncio.Event()

    async def block():
        started.set()
        await asyncio.Event().wait()

    streams.generate_hook = staticmethod(block)
    task = asyncio.create_task(
        policy.adapter.generate_async_webrtc_stream("offer", "one", lambda _: None)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert streams.instances[0].closed and not policy.adapter._webrtc_streams
    resources = _StreamResources(
        1, policy.adapter, client, "one", privacy_lease=lease, ring_session_started=True
    )
    await resources.cleanup()
    assert policy._lease is None


@pytest.mark.asyncio
async def test_resource_cleanup_stops_microphone_then_stream_peer_and_owner(streams):
    client, _, _, policy = authority()
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    await policy.adapter.generate_async_webrtc_stream("offer", "one", lambda _: None)
    order = []

    async def mic():
        order.append("mic")

    async def peer():
        assert policy._lease is lease
        order.append("peer")

    async def close():
        order.append("stream")

    streams.instances[0].close_hook = close
    resources = _StreamResources(
        1,
        policy.adapter,
        client,
        "one",
        privacy_lease=lease,
        ring_session_started=True,
        mic_track=SimpleNamespace(stop_capture=mic),
        pc=SimpleNamespace(close=peer),
    )
    await resources.cleanup()
    assert order == ["mic", "stream", "peer"] and policy._lease is None


@pytest.mark.asyncio
async def test_talkback_uses_existing_gate_and_stops_delayed_microphone_after_opt_out(monkeypatch):
    from halo_gtk import live_stream
    from tests.test_live_stream_lifecycle import FakeMicrophoneTrack

    client, _, _, policy = authority()
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    microphone = FakeMicrophoneTrack()
    resources = _StreamResources(
        1,
        policy.adapter,
        client,
        "one",
        privacy_lease=lease,
        mic_track=microphone,
        connected=True,
        talkback_generation=1,
        talkback_requested=True,
    )
    view = LiveStreamView.__new__(LiveStreamView)
    view._resources, view._stream_token, view._closing = resources, 1, False
    entered, release = asyncio.Event(), asyncio.Event()

    async def speaker(*_):
        entered.set()
        await release.wait()

    monkeypatch.setattr(live_stream, "activate_ring_camera_speaker", speaker)
    task = asyncio.create_task(view._async_start_talking(resources, 1))
    await entered.wait()
    policy.set_enabled(False)
    release.set()
    await task
    assert microphone.start_calls == 0 and not view._privacy_current(resources, "talkback")
    await resources.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,cancelled", [("mic", False), ("peer", False), ("mic", True), ("peer", True)]
)
async def test_failed_local_cleanup_keeps_intercom_capture_reserved(resource, cancelled):
    client, _, _, policy = authority()
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")

    async def fail():
        raise asyncio.CancelledError if cancelled else RuntimeError("synthetic cleanup failure")

    resources = _StreamResources(
        1,
        policy.adapter,
        client,
        "one",
        privacy_lease=lease,
        mic_track=SimpleNamespace(stop_capture=fail) if resource == "mic" else None,
        pc=SimpleNamespace(close=fail) if resource == "peer" else None,
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await resources.cleanup()
    else:
        await resources.cleanup()
    assert resources.cleanup_failed and policy.adapter._cleanup_incomplete
    assert policy._lease is lease
    second = media.IntercomMediaAuthority(client, 42, 3)
    second.set_enabled(True)
    assert second.acquire(second.adapter, "live") is None


def test_parent_inventory_refreshes_keep_one_explicit_call_until_five_minute_limit(monkeypatch):
    client, _, item, policy = authority()
    now = [privacy_time()]
    monkeypatch.setattr(media, "privacy_time", lambda: now[0])
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    # IntercomDetails' existing selected-device timer refreshes inventory every
    # 15 seconds even while the media child is open; there is no new call/release.
    for _ in range(19):
        now[0] += 15
        client.snapshot = replace(
            client.snapshot, devices=(replace(item, inventory_monotonic=now[0]),)
        )
        assert policy.valid(lease)
    now[0] += 16
    client.snapshot = replace(client.snapshot, devices=(replace(item, inventory_monotonic=now[0]),))
    assert not policy.valid(lease)
    policy.release(lease)


def test_fresh_metadata_after_a_gap_cannot_resurrect_suspended_media(monkeypatch):
    client, _, item, policy = authority()
    now = [privacy_time()]
    monkeypatch.setattr(media, "privacy_time", lambda: now[0])
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    now[0] += 31
    client.snapshot = replace(client.snapshot, devices=(replace(item, inventory_monotonic=now[0]),))
    assert not policy.valid(lease)
    assert not policy.valid(lease)
    policy.release(lease)


@pytest.mark.parametrize("raw_shared", [True, False, None])
def test_real_sdk_intercom_wrapper_uses_account_evidence_not_synthetic_shared(raw_shared):
    from ring_doorbell.ring import Ring

    from halo_gtk.intercom import parse_intercom_inventory

    row = {
        "id": 42,
        "description": "Synthetic account Intercom",
        "kind": media.VIDEO_KIND,
        "location_id": "home",
        "device_id": "fixture-hardware",
        "shared": raw_shared,
    }
    ring = Ring(object())  # No Auth and no HTTP: construct the actual SDK wrappers only.
    ring.devices_data = {"other": {42: row}}
    client, _, _ = fixture()
    client._ring = ring
    client.all_devices = list(ring.devices().all_devices)
    (item,) = parse_intercom_inventory({"other": [row]})
    item = replace(item, identity_epoch=3, stale=False, inventory_monotonic=privacy_time())
    client.snapshot = replace(client.snapshot, devices=(item,))
    source = client.all_devices[0]
    assert source.shared is True  # SDK constructor, regardless of raw data.
    assert media.qualified_intercom(client, 42) == (source, item)
    policy = media.IntercomMediaAuthority(client, 42, 3)
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    assert lease is not None and policy.valid(lease)
    client.snapshot = replace(client.snapshot, devices=(replace(item, denied=True),))
    assert not policy.valid(lease)
    policy.release(lease)


def test_worker_submission_failure_releases_unused_intercom_admission(monkeypatch):
    from halo_gtk import live_stream, ring_client

    client, _, _, policy = authority()
    retained, released = [], []
    client.retain_media_resources = retained.append
    client.release_media_resources = released.append

    def fail_submit(_coro):
        raise RuntimeError("worker closed")

    client.submit = fail_submit
    policy.set_enabled(True)
    view = LiveStreamView.__new__(LiveStreamView)
    view._stream_token, view._resources, view._pipeline = 0, None, None
    view._media_authority = policy
    view._paintable = None
    view._video_picture = SimpleNamespace(set_paintable=lambda *_: None)
    view._set_status = lambda *_: None
    monkeypatch.setattr(ring_client, "get_client", lambda: client)
    monkeypatch.setattr(live_stream.GLib, "idle_add", lambda *_: None)
    view.start_for_device(policy.adapter)
    assert retained == released and len(retained) == 1
    assert retained[0].cleanup_complete.is_set() and retained[0].cleanup_started
    assert view._resources is None and policy._lease is None and not policy._listeners
    second = media.IntercomMediaAuthority(client, 42, 3)
    second.set_enabled(True)
    lease = second.acquire(second.adapter, "live")
    assert lease is not None
    second.release(lease)


@pytest.mark.asyncio
async def test_partial_sdk_close_preserves_original_handles_for_fallback(streams):
    client, _, _, policy = authority()
    policy.set_enabled(True)
    lease = policy.acquire(policy.adapter, "live")
    await policy.adapter.generate_async_webrtc_stream("offer", "one", lambda *_: None)
    stream = streams.instances[-1]
    closed = []

    async def close_socket():
        closed.append("websocket")

    async def wait_forever():
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    await asyncio.sleep(0)
    stream.read_task = task
    stream.websocket = SimpleNamespace(close=close_socket)

    async def partial_close():
        stream.read_task = stream.ping_task = stream.websocket = None
        raise RuntimeError("SDK close failed after clearing handles")

    stream.close_hook = partial_close
    with pytest.raises(RuntimeError):
        await policy.adapter.close_webrtc_stream("one")
    assert task.cancelled() and closed == ["websocket"]
    policy.release(lease, resources_closed=False)
    assert policy._lease is lease
