"""Shared camera/group reservation, cancellation and delayed-presentation races."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from halo_gtk import device_commands, lighting_actions
from halo_gtk.lighting_actions import LightingActionError, LightingActions


def fixture():
    calls = []

    async def setter(value):
        calls.append(value)

    camera = SimpleNamespace(
        id=12,
        kind="stickup_cam",
        location_id="home",
        family="stickup_cams",
        light=False,
        siren=0,
        has_capability=lambda kind: kind == "light",
        async_set_light=setter,
    )
    client = SimpleNamespace(
        _stopped=False,
        _stop_event=threading.Event(),
        is_authenticated=True,
        all_devices=[camera],
        active=True,
    )
    actions = LightingActions(client, is_current=lambda c, generation: c.active and generation == 7)
    actions.initialize(7)
    return client, camera, actions, calls


def test_reservation_has_no_queue_and_old_token_cannot_release_new_action():
    _client, _camera, actions, _calls = fixture()
    first = actions.try_acquire()
    assert first is not None and actions.try_acquire() is None
    assert actions.release(object()) is False
    assert actions.release(first)
    second = actions.try_acquire()
    assert second is not first and not actions.release(first)
    actions.admission(second)
    assert actions.release(second)


def test_retirement_between_global_check_and_local_acquire_is_fenced():
    _client, _camera, actions, _calls = fixture()

    def race(*_args):
        actions.invalidate(8)
        return True

    actions._is_current = race
    assert actions.try_acquire() is None


@pytest.mark.asyncio
async def test_camera_busy_rejects_without_entering_sdk_or_retained_task():
    _client, camera, actions, calls = fixture()
    token = actions.try_acquire()
    with pytest.raises(LightingActionError, match="lighting-busy"):
        await actions.camera_light(camera, True)
    assert calls == [] and not actions._tasks
    actions.release(token)
    await actions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, None, "on", []])
async def test_camera_state_requires_bool(value):
    _client, camera, actions, calls = fixture()
    with pytest.raises(LightingActionError, match="invalid-request"):
        await actions.camera_light(camera, value)
    assert not calls and actions._active is None


@pytest.mark.asyncio
async def test_replaced_camera_is_not_a_command_target():
    client, camera, actions, calls = fixture()
    client.all_devices = [SimpleNamespace(**vars(camera))]
    with pytest.raises(LightingActionError, match="camera-changed"):
        await actions.camera_light(camera, True)
    assert not calls and not actions._tasks


@pytest.mark.asyncio
async def test_camera_commit_precedes_release_and_group_invalidates_exact_member_only(monkeypatch):
    client, camera, actions, calls = fixture()
    state = device_commands.DeviceCommandState()
    monkeypatch.setattr(device_commands, "command_state", state)
    other = SimpleNamespace(**{**vars(camera), "id": 13})
    other_location = SimpleNamespace(**{**vars(camera), "location_id": "other"})
    client.all_devices += [other, other_location]
    state.record_light_success(client, other, True)
    state.record_light_success(client, other_location, True)
    state.record_siren_success(client, camera, 30)
    await actions.camera_light(camera, True)
    assert calls == [True] and actions._active is None
    assert state.light_enabled(client, camera)
    token = actions.try_acquire()
    member = SimpleNamespace(api_id="12", kind="stickup_cam", location_id="home")
    actions.invalidate_group_members("home", [member])
    assert not state.light_enabled(client, camera)
    assert state.light_enabled(client, other) and state.siren_active(client, camera)
    assert state.light_enabled(client, other_location)
    actions.release(token)
    await actions.close()


@pytest.mark.asyncio
async def test_presentation_cancel_after_entry_does_not_release_running_camera(monkeypatch):
    client, camera, actions, _calls = fixture()
    started, release = asyncio.Event(), asyncio.Event()

    async def setter(_value):
        started.set()
        await release.wait()

    camera.async_set_light = setter
    consumer = asyncio.create_task(actions.camera_light(camera, True))
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert actions.try_acquire() is None
    release.set()
    async with asyncio.timeout(1):
        while actions._tasks:
            await asyncio.sleep(0)
    assert actions._active is None
    await actions.close()


@pytest.mark.asyncio
async def test_whole_camera_deadline_cancels_stalled_auth_and_releases_reservation(monkeypatch):
    _client, camera, actions, _calls = fixture()
    cleaned = asyncio.Event()
    monkeypatch.setattr(lighting_actions, "CAMERA_ACTION_TIMEOUT", 0.01)

    async def setter(_value):
        try:
            await asyncio.Event().wait()  # Synthetic SDK auth/serialization wait.
        finally:
            cleaned.set()

    camera.async_set_light = setter
    with pytest.raises(LightingActionError) as caught:
        await actions.camera_light(camera, True)
    assert caught.value.code == "outcome-unknown" and caught.value.possibly_sent
    assert cleaned.is_set() and actions._active is None
    token = actions.try_acquire()
    assert token is not None
    actions.release(token)
    await actions.close()


@pytest.mark.asyncio
async def test_sdk_suppressed_timeout_cannot_publish_late_camera_success(monkeypatch):
    client, camera, actions, _calls = fixture()
    state = device_commands.DeviceCommandState()
    monkeypatch.setattr(device_commands, "command_state", state)
    monkeypatch.setattr(lighting_actions, "CAMERA_ACTION_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def setter(_value):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()

    camera.async_set_light = setter
    with pytest.raises(LightingActionError) as caught:
        await actions.camera_light(camera, True)
    assert caught.value.code == "outcome-unknown" and caught.value.possibly_sent
    assert cancelled.is_set() and not state.light_enabled(client, camera)
    assert actions._active is None
    await actions.close()


@pytest.mark.asyncio
async def test_suspend_or_synchronous_sdk_completion_cannot_publish_expired_success(monkeypatch):
    client, camera, actions, _calls = fixture()
    state = device_commands.DeviceCommandState()
    monkeypatch.setattr(device_commands, "command_state", state)
    clock = [100.0]
    monkeypatch.setattr(lighting_actions, "_elapsed_time", lambda: clock[0])

    async def setter(_value):
        clock[0] += 31  # No event-loop yield, like completion immediately after suspend.

    camera.async_set_light = setter
    with pytest.raises(LightingActionError) as caught:
        await actions.camera_light(camera, True)
    assert caught.value.code == "outcome-unknown" and caught.value.possibly_sent
    assert not state.light_enabled(client, camera) and actions._active is None
    await actions.close()


@pytest.mark.asyncio
async def test_close_joins_camera_cancellation_and_preserves_unknown_outcome():
    _client, camera, actions, _calls = fixture()
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def setter(_value):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    camera.async_set_light = setter
    consumer = asyncio.create_task(actions.camera_light(camera, True))
    await started.wait()
    await asyncio.gather(actions.close(), actions.close())
    with pytest.raises(LightingActionError) as caught:
        await consumer
    assert caught.value.possibly_sent
    assert cleaned.is_set() and actions._active is None and not actions._tasks
    assert actions.try_acquire() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["grid", "focused"])
async def test_delayed_camera_gtk_completion_cannot_revive_group_invalidated_override(
    monkeypatch, surface
):
    from halo_gtk import cameras_page, focused_live_page
    from tests.test_device_commands import _Button

    client, camera, actions, _calls = fixture()
    camera.name = "Synthetic camera"
    state = device_commands.DeviceCommandState()
    monkeypatch.setattr(device_commands, "command_state", state)
    monkeypatch.setattr(cameras_page, "get_client", lambda: client)
    monkeypatch.setattr(focused_live_page, "_ring_client", lambda: client)
    await actions.camera_light(camera, True)
    assert state.light_enabled(client, camera)
    token = actions.try_acquire()
    actions.invalidate_group_members(
        "home", [SimpleNamespace(api_id="12", kind="stickup_cam", location_id="home")]
    )
    button = _Button(active=True)
    if surface == "grid":
        tile = SimpleNamespace(
            _light_btn=button, _on_light_toggled=lambda *_: None, _refresh_light_icon=lambda: None
        )
        page = cameras_page.CamerasPage.__new__(cameras_page.CamerasPage)
        page._cards = {12: tile}
        page._present_device_command_error = lambda *_: None
        page._finish_tile_light_command(camera, True, None, tile, client=client)
    else:
        page = focused_live_page.FocusedLivePage.__new__(focused_live_page.FocusedLivePage)
        page._device = camera
        page._light_btn = button
        page._on_light_toggled = lambda *_: None
        page._refresh_light_icon = lambda: None
        page._present_device_command_error = lambda *_: None
        page._finish_light_command(camera, True, None, client=client)
    assert not state.light_enabled(client, camera)
    assert not button.active and button.sensitive
    actions.release(token)
    await actions.close()


def test_busy_callback_restores_current_light_state_instead_of_inverting_request(monkeypatch):
    from halo_gtk import cameras_page
    from tests.test_device_commands import _Button

    client, camera, _actions, _calls = fixture()
    camera.light = True
    monkeypatch.setattr(cameras_page, "get_client", lambda: client)
    button = _Button(active=True)
    tile = SimpleNamespace(
        _light_btn=button, _on_light_toggled=lambda *_: None, _refresh_light_icon=lambda: None
    )
    page = cameras_page.CamerasPage.__new__(cameras_page.CamerasPage)
    page._cards = {12: tile}
    page._present_device_command_error = lambda *_: None
    page._finish_tile_light_command(
        camera, True, LightingActionError("lighting-busy"), tile, client=client
    )
    assert button.active  # Old inverse-of-request rollback would fabricate Off.


def test_retired_camera_completion_does_not_touch_new_account_widgets(monkeypatch):
    from halo_gtk import cameras_page
    from tests.test_device_commands import _Button

    client, camera, _actions, _calls = fixture()
    monkeypatch.setattr(cameras_page, "get_client", lambda: object())
    button = _Button(active=False)
    tile = SimpleNamespace(_light_btn=button)
    page = cameras_page.CamerasPage.__new__(cameras_page.CamerasPage)
    page._cards = {12: tile}
    page._finish_tile_light_command(camera, True, None, tile, client=client)
    assert not button.active and not button.sensitive


def test_uncertain_camera_failure_does_not_claim_device_rejected_request():
    error = LightingActionError("outcome-unknown", possibly_sent=True)
    message = device_commands.command_failure_summary("Garden camera", error)
    assert "may have reached Ring" in message and "unknown" in message
    assert "not sent" in device_commands.command_failure_summary(
        "Garden camera", LightingActionError("lighting-busy")
    )
