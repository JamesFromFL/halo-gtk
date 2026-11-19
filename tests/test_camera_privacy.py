"""Offline qualification and race regressions; no Ring or hardware access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from halo_gtk import camera_privacy, ring_client
from halo_gtk.camera_privacy import CameraPrivacy, privacy_time, ring_privacy_receipt
from halo_gtk.ring_client import RingClient
from tests.privacy_helpers import grant, grant_current


def camera(*, live=False, motion=True, audio=True, device_id=1):
    return SimpleNamespace(
        id=device_id,
        name="Fixture camera",
        family="stickup_cams",
        _attrs={
            "settings": {
                "live_view_disabled": live,
                "motion_detection_enabled": motion,
                "enable_audio_recording": audio,
            }
        },
    )


def observe(policy, device, *, now=None):
    policy.observe(ring_privacy_receipt([device], privacy_time() if now is None else now))


@pytest.mark.parametrize("live", [False, True, None, 0, "false"])
@pytest.mark.parametrize("motion", [True, False, None, 1])
@pytest.mark.parametrize("audio", [True, False, None, 1])
def test_only_explicit_ring_booleans_grant_independent_permissions(live, motion, audio):
    device = camera(live=live, motion=motion, audio=audio)
    policy = CameraPrivacy()
    observe(policy, device)
    assert policy.allowed(device, "live") is (live is False)
    assert policy.allowed(device, "preview") is (live is False and motion is True)
    assert policy.allowed(device, "audio") is (live is False and audio is True)
    assert policy.allowed(device, "talkback") is (live is False and audio is True)
    # Preserve Ring's raw audio truth even while live or motion is disabled.
    assert policy.view(device).audio_allowed is (audio if type(audio) is bool else None)
    assert not policy.allowed(device, "unrecognized")


def test_missing_receipt_and_sdk_convenience_defaults_never_grant():
    policy = CameraPrivacy()
    device = SimpleNamespace(id=1, live_view=True, motion_detection=True)
    assert policy.view(device).reason == "unknown"
    observe(policy, device)
    assert policy.acquire(device, "live") is None
    assert policy.acquire(device, "preview") is None


def test_mutable_sdk_settings_are_not_an_authoritative_receipt():
    device = camera(live=True)
    policy = CameraPrivacy()
    observe(policy, device)
    device._attrs["settings"]["live_view_disabled"] = False
    assert not policy.allowed(device, "live")
    observe(policy, device)
    assert policy.allowed(device, "live")


def test_suspend_aware_default_clock_uses_boottime(monkeypatch):
    clocks = []
    monkeypatch.setattr(
        camera_privacy.time, "clock_gettime", lambda clock: clocks.append(clock) or 99
    )
    assert privacy_time() == 99
    assert clocks == [camera_privacy.time.CLOCK_BOOTTIME]


def test_expired_lease_cannot_revive_with_identical_settings_after_resume():
    now = [10.0]
    policy = CameraPrivacy(clock=lambda: now[0])
    device = camera()
    observe(policy, device, now=10.0)
    old = policy.acquire(device, "preview")
    now[0] += 3600
    assert policy.view(device).reason == "stale"
    assert not policy.valid(old)
    observe(policy, device, now=now[0])
    assert policy.allowed(device, "preview")
    assert not policy.valid(old)


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), True, 1000.0])
def test_invalid_or_future_receipts_fail_closed(stamp):
    policy = CameraPrivacy(clock=lambda: 10.0)
    device = camera()
    observe(policy, device, now=stamp)
    assert not policy.allowed(device, "live")


def test_unchanged_fresh_receipt_preserves_active_lease():
    device = camera()
    policy = CameraPrivacy(clock=lambda: 20.0)
    observe(policy, device, now=10.0)
    lease = policy.acquire(device, "live")
    observe(policy, device, now=20.0)
    assert policy.valid(lease)


def test_removed_identity_cannot_reuse_old_lease_and_revision_map_stays_bounded():
    policy = CameraPrivacy()
    first = camera()
    observe(policy, first)
    lease = policy.acquire(first, "live")
    for index in range(1, 500):
        observe(policy, camera(device_id=index))
        assert len(policy._revisions) == 1
    observe(policy, first)
    assert not policy.valid(lease)
    assert not policy.allowed(camera(), "live")


def test_duplicate_identifiers_are_not_qualified():
    first, duplicate = camera(), camera()
    receipt = ring_privacy_receipt([first, duplicate], privacy_time())
    assert receipt.rows == ()
    policy = CameraPrivacy()
    policy.observe(receipt)
    assert not policy.allowed(first, "live")


def test_one_broken_observer_does_not_abort_invalidation_or_other_observers(caplog):
    policy = CameraPrivacy()
    received = []

    def broken():
        raise RuntimeError("fixture observer")

    policy.subscribe(broken)
    policy.subscribe(lambda: received.append(True))
    observe(policy, camera())
    policy.invalidate()
    assert received == [True, True]
    assert "Camera privacy observer failed" in caplog.text


@pytest.mark.asyncio
async def test_cached_inventory_read_does_not_qualify_or_extend_privacy(monkeypatch):
    device = camera()
    client = RingClient()
    client._ring = SimpleNamespace(
        async_update_devices=AsyncMock(),
        async_update_dings=AsyncMock(),
        async_update_groups=AsyncMock(),
        devices=lambda: SimpleNamespace(all_devices=[device]),
    )
    client._last_device_update_at = privacy_time()
    await client._async_refresh_devices(None, 60)
    assert client.camera_privacy.view(device).reason == "unknown"
    client._ring.async_update_devices.assert_not_awaited()
    await client._async_refresh_devices(None, -1)
    assert client.camera_privacy.allowed(device, "live")
    stamp = client.camera_privacy._received_at
    await client._async_refresh_devices(None, 60)
    assert client.camera_privacy._received_at == stamp
    client._ring.async_update_devices.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_inventory_update_cannot_publish_partially_changed_settings():
    device = camera(live=True)
    client = RingClient()
    observe(client.camera_privacy, device)

    async def failed_update():
        device._attrs["settings"]["live_view_disabled"] = False
        raise RuntimeError("partial SDK update")

    client._ring = SimpleNamespace(async_update_devices=failed_update)
    with pytest.raises(RuntimeError, match="partial SDK"):
        await client._async_refresh_devices(None, -1)
    assert not client.camera_privacy.allowed(device, "live")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["snapshot", "latest", "notification"])
async def test_unknown_privacy_starts_no_camera_or_enrichment_requests(method):
    client = RingClient()
    device = camera()
    device.async_history = AsyncMock()
    device.async_recording_url = AsyncMock()
    client._ring = SimpleNamespace(
        async_query=AsyncMock(), devices=lambda: SimpleNamespace(all_devices=[device])
    )
    if method == "snapshot":
        assert await client._async_snapshot_bytes(device) is None
    elif method == "latest":
        assert await client._async_last_event_frame_for_device(device) is None
    else:
        lease = client.camera_privacy.acquire(device, "preview")
        assert (
            await client._async_event_notification_context(
                object(), lease, include_image=True, include_summary=True
            )
            == {}
        )
    client._ring.async_query.assert_not_awaited()
    device.async_history.assert_not_awaited()
    device.async_recording_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_completing_after_deny_then_restore_is_discarded(monkeypatch):
    client, device = RingClient(), camera()
    grant(client, device)
    device.async_recording_url = AsyncMock(return_value="https://fixture.invalid/video")
    started, release = asyncio.Event(), asyncio.Event()

    async def decode(_url):
        started.set()
        await release.wait()
        return b"old preview"

    monkeypatch.setattr(client._preview, "recording", decode)
    task = asyncio.create_task(client._async_first_recording_frame(device, 1))
    await asyncio.wait_for(started.wait(), 1)
    device._attrs["settings"]["live_view_disabled"] = True
    observe(client.camera_privacy, device)
    device._attrs["settings"]["live_view_disabled"] = False
    observe(client.camera_privacy, device)
    release.set()
    assert await task is None
    assert not client._preview_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [b"live frame", None])
async def test_active_live_session_uses_frame_or_cached_snapshot_without_wake(monkeypatch, frame):
    from halo_gtk import live_sessions

    client, device = RingClient(), camera()
    grant(client, device)
    client._ring = SimpleNamespace(async_query=AsyncMock())
    monkeypatch.setattr(
        live_sessions,
        "active_session_for_device",
        lambda target: SimpleNamespace(device=target, get_current_frame_png=lambda: frame),
    )
    cached = AsyncMock(return_value=b"cached frame")
    monkeypatch.setattr(client, "_async_cached_snapshot_bytes", cached)
    assert await client._async_snapshot_bytes(device) == (frame or b"cached frame")
    client._ring.async_query.assert_not_awaited()
    assert cached.await_count == (0 if frame else 1)


def test_current_privacy_does_not_revoke_access_to_existing_history_recordings(monkeypatch):
    client, device = RingClient(), camera(live=True, motion=False)
    observe(client.camera_privacy, device)
    device.async_recording_url = AsyncMock(return_value="https://fixture.invalid/history")
    monkeypatch.setattr(client, "_run", asyncio.run)
    assert client.recording_url(device, 8) == "https://fixture.invalid/history"
    device.async_recording_url.assert_awaited_once_with(8)


def test_cache_and_queued_gtk_result_cannot_survive_privacy_revocation(monkeypatch):
    from halo_gtk.cameras_page import CamerasPage

    device = camera()
    lease = grant_current(monkeypatch, device)
    cache, leases = {}, {}
    page = SimpleNamespace(
        _snapshot_generation=1, _snapshot_cache=cache, _snapshot_cache_leases=leases, _cards={}
    )
    CamerasPage._set_card_snapshot(page, 1, b"private image", generation=1, lease=lease)
    assert cache == {1: b"private image"}
    lease.policy.invalidate()
    CamerasPage._refresh_camera_privacy(page)
    assert cache == leases == {}
    observe(lease.policy, device)
    CamerasPage._set_card_snapshot(page, 1, b"late image", generation=1, lease=lease)
    assert cache == {}


def test_account_replacement_rejects_identical_camera_identifier(monkeypatch):
    device = camera()
    lease = grant_current(monkeypatch, device)
    assert camera_privacy.lease_is_current(lease)
    replacement = grant(SimpleNamespace(), camera())
    monkeypatch.setattr(ring_client, "get_client", lambda: replacement)
    assert not camera_privacy.lease_is_current(lease)
    assert camera_privacy.current_view(device).reason == "unknown"


def test_shared_session_never_reuses_another_device_wrapper():
    from halo_gtk.live_sessions import LiveSessionManager
    from tests.test_live_sessions import FakeView

    manager = LiveSessionManager(view_factory=FakeView)
    old = manager.acquire(camera(), owner="old")
    new = manager.acquire(camera(), owner="new")
    assert new is not old
    assert not old.active and old.owners == set()
    assert new.owners == {"new"}


@pytest.mark.asyncio
async def test_privacy_denied_microphone_never_opens_capture_pipeline(monkeypatch):
    from halo_gtk import ring_media

    def parse(*_args):
        pytest.fail("privacy-denied microphone opened")

    monkeypatch.setattr(ring_media.Gst, "parse_launch", parse)
    track = ring_media.MicrophoneTrack(capture_allowed=lambda: False)
    with pytest.raises(RuntimeError, match="privacy"):
        await track.start_capture()
    frame = await track.recv()
    assert not any(bytes(frame.planes[0]))


@pytest.mark.asyncio
async def test_audio_flag_false_allows_video_but_drops_incoming_audio(monkeypatch):
    from aiortc.mediastreams import MediaStreamError

    from halo_gtk import live_stream

    device = camera(audio=False)
    client = SimpleNamespace(camera_privacy=CameraPrivacy(), _preview_is_current=lambda: True)
    observe(client.camera_privacy, device)
    resources = live_stream._StreamResources(
        token=1,
        device=device,
        client=client,
        session_id="fixture",
        privacy_lease=client.camera_privacy.acquire(device, "live"),
    )
    view = live_stream.LiveStreamView.__new__(live_stream.LiveStreamView)
    view._resources, view._stream_token, view._closing = resources, 1, False
    received = []
    convert = []

    async def recv():
        if received:
            raise MediaStreamError
        received.append(True)
        return object()

    monkeypatch.setattr(live_stream, "audio_frame_to_s16le_interleaved", convert.append)
    assert view._privacy_current(resources)
    assert not view._privacy_current(resources, "audio")
    await view._receive_audio_frames(SimpleNamespace(recv=recv), resources)
    assert received == [True] and convert == []


@pytest.mark.asyncio
async def test_queued_microphone_samples_become_silence_after_revoke(monkeypatch):
    from halo_gtk import ring_media

    allowed = [True]
    track = ring_media.MicrophoneTrack(capture_allowed=lambda: allowed[0])
    track._active = True
    track._mic_appsink = object()

    async def pull(*_args):
        allowed[0] = False
        return b"\x01\x10" * 960

    with monkeypatch.context() as patch:
        patch.setattr(
            ring_media.asyncio,
            "get_running_loop",
            lambda: SimpleNamespace(run_in_executor=pull),
        )
        frame = await track.recv()
    assert not any(bytes(frame.planes[0]))


@pytest.mark.asyncio
async def test_live_revocation_flushes_pipeline_before_gtk_callback_and_cleans_once(monkeypatch):
    from halo_gtk import live_stream

    device = camera()
    client = grant(SimpleNamespace(), device)
    device.close_webrtc_stream = AsyncMock()
    lease = client.camera_privacy.acquire(device, "live")
    microphone = SimpleNamespace(stop_capture=AsyncMock())
    peer = SimpleNamespace(close=AsyncMock())
    resources = live_stream._StreamResources(
        token=1,
        device=device,
        client=client,
        session_id="fixture",
        privacy_lease=lease,
        ring_session_started=True,
        pc=peer,
        mic_track=microphone,
        talkback_requested=True,
    )
    view = live_stream.LiveStreamView.__new__(live_stream.LiveStreamView)
    view._resources, view._stream_token, view._closing = resources, 1, False
    view._last_frame_rgb = object()
    states, hidden, idle, cleanup = [], [], [], []
    view._pipeline = SimpleNamespace(set_state=states.append)
    view._video_picture = SimpleNamespace(set_paintable=hidden.append)
    view._submit = lambda _client, coroutine, _label: cleanup.append(asyncio.create_task(coroutine))
    monkeypatch.setattr(
        live_stream.GLib, "idle_add", lambda callback, *args: idle.append((callback, args))
    )
    resources.privacy_callback = lambda: view._privacy_changed(resources)
    client.camera_privacy.subscribe(resources.privacy_callback)

    device._attrs["settings"]["enable_audio_recording"] = False
    observe(client.camera_privacy, device)
    assert states == [live_stream.Gst.State.NULL]
    assert view._last_frame_rgb is None
    assert not resources.talkback_requested
    assert hidden == []  # GTK delivery is deferred; audio is already flushed.
    callback, args = idle[0]
    callback(*args)
    assert hidden == [None]
    await asyncio.gather(*cleanup)
    assert resources.cleanup_started
    microphone.stop_capture.assert_awaited_once()
    peer.close.assert_awaited_once()
    device.close_webrtc_stream.assert_awaited_once_with("fixture")
    assert view._resources is None
    assert view._on_connected(1) is False  # A queued old ready signal cannot revive it.


@pytest.mark.asyncio
async def test_motion_setting_withdraws_until_a_get_started_after_completion():
    device = camera()
    client = RingClient()
    grant(client, device)
    old = client.camera_privacy.acquire(device, "preview")
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def invoke(value):
        assert value is False
        order.append("write-start")
        assert not client.camera_privacy.allowed(device, "preview")
        entered.set()
        await release.wait()
        device._attrs["settings"]["motion_detection_enabled"] = False
        order.append("write-end")

    async def get():
        order.append("get")

    client._ring = SimpleNamespace(
        devices=lambda: SimpleNamespace(all_devices=[device]),
        async_update_devices=get,
        async_update_dings=AsyncMock(),
        async_update_groups=AsyncMock(),
    )
    device.async_set_motion_detection = invoke
    write = asyncio.create_task(client._async_camera_privacy_setting(device, False))
    await asyncio.wait_for(entered.wait(), 1)
    refresh = asyncio.create_task(client._async_refresh_devices(None, -1))
    await asyncio.sleep(0)
    assert order == ["write-start"]
    release.set()
    observed, _ = await asyncio.gather(write, refresh)
    assert observed is False
    assert order == ["write-start", "write-end", "get", "get"]
    assert not client.camera_privacy.valid(old)
    assert client.camera_privacy.view(device).reason == "motion_disabled"
    assert client.camera_privacy.allowed(device, "live")


@pytest.mark.asyncio
async def test_failed_motion_setting_retires_prewrite_grant_without_claiming_saved():
    client, device = RingClient(), camera()
    grant(client, device)
    client._ring = SimpleNamespace(devices=lambda: SimpleNamespace(all_devices=[device]))
    invoke = AsyncMock(side_effect=RuntimeError("fixture write failed"))
    device.async_set_motion_detection = invoke
    with pytest.raises(RuntimeError, match="write failed"):
        await client._async_camera_privacy_setting(device, False)
    assert client.camera_privacy.view(device).reason == "unknown"
    assert not client.camera_privacy._pending


@pytest.mark.asyncio
async def test_accepted_motion_write_with_failed_readback_is_explicitly_uncertain():
    client, device = RingClient(), camera()
    grant(client, device)
    get = AsyncMock(side_effect=RuntimeError("fixture readback failure"))
    client._ring = SimpleNamespace(
        devices=lambda: SimpleNamespace(all_devices=[device]),
        async_update_devices=get,
        async_update_dings=AsyncMock(),
        async_update_groups=AsyncMock(),
    )
    device.async_set_motion_detection = AsyncMock()
    with pytest.raises(camera_privacy.CameraPrivacyReadbackError) as raised:
        await client._async_camera_privacy_setting(device, False)
    assert raised.value.accepted is True
    device.async_set_motion_detection.assert_awaited_once_with(False)
    get.assert_awaited_once()
    assert client.camera_privacy.view(device).reason == "unknown"


@pytest.mark.parametrize("reported", [True, False, None, 1])
async def test_motion_setting_only_reports_exact_post_write_observation(reported):
    client, device = RingClient(), camera()
    grant(client, device)
    device.async_set_motion_detection = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def readback():
        entered.set()
        await release.wait()
        settings = device._attrs["settings"]
        if reported is None:
            settings.pop("motion_detection_enabled")
        else:
            settings["motion_detection_enabled"] = reported

    client._ring = SimpleNamespace(
        devices=lambda: SimpleNamespace(all_devices=[device]), async_update_devices=readback
    )
    write = asyncio.create_task(client._async_camera_privacy_setting(device, False))
    await asyncio.wait_for(entered.wait(), 1)
    assert not write.done()
    assert client.camera_privacy.view(device).motion_allowed is None
    assert not client.camera_privacy.allowed(device, "preview")
    release.set()
    if type(reported) is bool:
        assert (await write) is reported
    else:
        with pytest.raises(camera_privacy.CameraPrivacyReadbackError):
            await write
    device.async_set_motion_detection.assert_awaited_once_with(False)
    assert client.camera_privacy.view(device).motion_allowed is (
        reported if type(reported) is bool else None
    )


def test_non_camera_inventory_rows_cannot_collide_with_camera_privacy_ids():
    client = RingClient()
    device = camera()
    other = camera()
    other.family = "other"
    client._ring = SimpleNamespace(devices=lambda: SimpleNamespace(all_devices=[other, device]))
    client._record_camera_privacy_receipt()
    assert client.camera_privacy.allowed(device, "live")
    assert not client.camera_privacy.allowed(other, "live")


@pytest.mark.asyncio
async def test_client_shutdown_closes_retained_media_after_new_submissions_are_refused():
    from halo_gtk.live_stream import _StreamResources

    client = RingClient()
    events = []

    async def stop_mic():
        events.append("microphone stopped")

    async def close_peer():
        events.append("peer closed")

    async def close_auth():
        events.append("auth closed")

    client._ring = SimpleNamespace(auth=SimpleNamespace(async_close=close_auth))
    resources = _StreamResources(
        token=1,
        device=camera(),
        client=client,
        session_id="fixture",
        mic_track=SimpleNamespace(stop_capture=stop_mic),
        pc=SimpleNamespace(close=close_peer),
    )
    client.retain_media_resources(resources)
    client._stopped = True
    await client._async_close()
    assert events == ["microphone stopped", "peer closed", "auth closed"]
    assert resources.cleanup_complete.is_set()
    assert not client._media_resources


@pytest.mark.parametrize("audio", [False, None])
def test_focused_controls_keep_video_available_and_disable_unqualified_audio(monkeypatch, audio):
    from halo_gtk.focused_live_page import FocusedLivePage

    device = camera(audio=audio)
    client = SimpleNamespace(camera_privacy=CameraPrivacy(), _preview_is_current=lambda: True)
    observe(client.camera_privacy, device)
    monkeypatch.setattr(ring_client, "get_client", lambda: client)
    states = {}

    def button(name):
        return SimpleNamespace(set_sensitive=lambda value: states.update({name: value}))

    page = SimpleNamespace(
        _device=device,
        _intercom_supported=True,
        _play_btn=button("play"),
        _stop_btn=button("stop"),
        _mic_btn=button("microphone"),
        _volume_btn=button("volume"),
        _volume_scale=button("scale"),
    )
    FocusedLivePage._set_controls_sensitive(page, True)
    assert states == {
        "play": False,
        "stop": True,
        "microphone": False,
        "volume": False,
        "scale": False,
    }
    FocusedLivePage._set_controls_sensitive(page, False)
    assert states["play"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_sign_in_and_receipts_are_independent_of_dings_and_groups(monkeypatch, cached):
    import ring_doorbell

    from halo_gtk import ring_client

    device = camera()
    now = [100.0]
    monkeypatch.setattr(ring_client, "privacy_time", lambda: now[0])
    client = RingClient(token_updater=lambda _: None)
    client.camera_privacy = CameraPrivacy(clock=lambda: now[0])
    entered, release = asyncio.Event(), asyncio.Event()

    class FakeRing:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_devices(self):
            pass

        async def async_update_groups(self):
            entered.set()
            await release.wait()
            raise RuntimeError("unavailable")

        async def async_update_dings(self):
            raise RuntimeError("unavailable")

        def devices(self):
            return SimpleNamespace(all_devices=[device])

    auth = SimpleNamespace(
        async_fetch_token=AsyncMock(return_value={}),
        get_hardware_id=lambda: "fixture",
        async_close=AsyncMock(),
    )
    monkeypatch.setattr(ring_doorbell, "Auth", lambda *_: auth)
    monkeypatch.setattr(ring_doorbell, "Ring", FakeRing)
    if cached:
        await asyncio.wait_for(client._async_authenticate_from_token({}), 1)
    else:
        await asyncio.wait_for(client._async_authenticate("user", "fixture", None), 1)
    await asyncio.wait_for(entered.wait(), 1)
    assert client.is_authenticated
    lease = client.camera_privacy.acquire(device, "preview")
    assert lease is not None
    # A newer GET can revoke the old grant while the groups request is pending.
    now[0] += 10
    del device._attrs["settings"]["live_view_disabled"]
    await client._async_refresh_devices(None, -1)
    assert client.camera_privacy.view(device).live_allowed is None
    assert not client.camera_privacy.valid(lease)
    device._attrs["settings"]["live_view_disabled"] = True
    now[0] += 5
    await client._async_refresh_devices(None, -1)
    assert client.camera_privacy.view(device).live_allowed is False
    stamp = client.camera_privacy._received_at
    release.set()
    now[0] += 100
    await client._ancillary_task
    assert client.camera_privacy._received_at == stamp == 115
    await client._async_close()
