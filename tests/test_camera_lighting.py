"""Camera light evidence, acknowledgement fences and send-once outcomes."""

from types import SimpleNamespace

import pytest

from halo_gtk.camera_lighting import (
    CameraLightingView,
    camera_lighting_receipt,
    receipt_row,
)
from halo_gtk.camera_privacy import privacy_time
from halo_gtk.lighting_actions import LightingActionError
from halo_gtk.ring_json import RingJsonError, RingWriteReceipt
from tests.test_lighting_actions import fixture, observe


def camera(status="on", settings=None, **kwargs):
    return SimpleNamespace(
        id=1,
        family="stickup_cams",
        _attrs={"led_status": status, "settings": settings or {}},
        **kwargs,
    )


def row(device):
    return camera_lighting_receipt([device], received_at=100, revision=0).rows[0]


@pytest.mark.parametrize("value", [True, False, 0, 1, None, "ON", " off ", "active", {}, []])
def test_status_requires_exact_on_or_off(value):
    assert row(camera(value)).light is None


@pytest.mark.parametrize("value,expected", [("on", True), ("off", False)])
def test_exact_led_status_is_copied_without_sdk_defaults(value, expected):
    device = camera(value, light=not expected)
    receipt = camera_lighting_receipt([device], received_at=100, revision=0)
    device._attrs["led_status"] = "off" if expected else "on"
    assert receipt.rows[0].light is expected


@pytest.mark.parametrize("duration", [False, True, None, 0, 29, 901, 60.0, "60", -1])
def test_motion_duration_rejects_coercion_and_indefinite_values(duration):
    assert (
        row(
            camera(settings={"floodlight_settings": {"duration": duration}})
        ).motion_duration_seconds
        is None
    )


@pytest.mark.parametrize("duration", [30, 60, 900])
def test_native_motion_duration_is_read_only_and_bounded(duration):
    settings = {
        "floodlight_settings": {
            "duration": duration,
            "always_on": True,
            "always_on_duration": 0,
            "always_on_duration_secs": 0,
        }
    }
    assert row(camera(settings=settings)).motion_duration_seconds == duration


@pytest.mark.parametrize("alternate", [None, [], {"duration": 90}, {"duration": "60"}])
def test_duplicate_timer_surface_conflict_is_unknown(alternate):
    settings = {"floodlight_settings": {"duration": 60}, "light_settings": alternate}
    assert row(camera(settings=settings)).motion_duration_seconds is None


def test_alternate_timer_cannot_rescue_absent_primary():
    assert (
        row(camera(settings={"light_settings": {"duration": 60}})).motion_duration_seconds is None
    )


def test_duplicate_camera_id_is_not_an_arbitrary_observation():
    receipt = camera_lighting_receipt([camera(), camera("off")], received_at=100, revision=0)
    assert receipt.rows == ()


def test_receipt_expiry_identity_and_revision_are_independent_fences():
    device = camera()
    receipt = camera_lighting_receipt([device], received_at=100, revision=2)
    assert receipt_row(receipt, device, 2, now=190).light is True
    assert receipt_row(receipt, device, 2, now=191) is None
    assert receipt_row(receipt, camera(), 2, now=101) is None
    assert receipt_row(receipt, device, 3, now=101) is None
    assert receipt_row(receipt, device, 2, now=99) is None


def test_send_and_ack_are_idempotent_and_old_token_cannot_mutate_revision():
    _, device, actions, _ = fixture()
    assert actions.view(device).light is False
    token = actions.try_acquire()
    assert actions.revision == 0
    assert actions.mark_sent(token) and actions.mark_sent(token)
    assert actions.revision == 1 and actions.view(device).light is None
    during = camera_lighting_receipt([device], received_at=privacy_time(), revision=1)
    assert actions.acknowledge(token) == actions.acknowledge(token) == 2
    actions.observe(during)
    assert actions.view(device).light is None
    actions.release(token)
    current = actions.try_acquire()
    assert not actions.mark_sent(token) and actions.acknowledge(token) is None
    assert actions.revision == 2
    actions.release(current)


@pytest.mark.asyncio
async def test_accepted_then_observed_requires_get_started_after_acknowledgement():
    client, device, actions, calls = fixture()
    original = client._transport.request_write
    phases = []

    async def write(*args, **kwargs):
        phases.append(actions.view(device).phase)
        result = await original(*args, **kwargs)
        # A successful inventory GET during the PUT is still insufficient.
        observe(actions, [device])
        assert actions.revision == 1
        return result

    async def read(*args):
        phases.append(actions.view(device).phase)
        assert actions.revision == 2
        assert actions.view(device).light is None
        device._attrs["led_status"] = "on"
        observe(actions, [device])

    client._transport.request_write = write
    client._async_refresh_devices = read
    result = await actions.camera_light(device, True)
    assert phases == ["pending", "accepted"]
    assert result.phase == "observed" and result.light is True and result.accepted
    assert calls == [True] and actions._active is None
    await actions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_accepted_write_with_missing_or_different_readback_stays_uncertain(failure):
    client, device, actions, calls = fixture()

    async def read(*_):
        if failure:
            raise RuntimeError("synthetic read failed")
        observe(actions, [device])  # Still Off.

    client._async_refresh_devices = read
    result = await actions.camera_light(device, True)
    assert result.phase == "uncertain" and result.accepted
    assert result.light is (None if failure else False)
    assert "accepted On" in result.message and "unconfirmed" in result.message
    assert calls == [True] and result.button_active  # Explicit follow-up Off remains possible.
    device._attrs["led_status"] = "on"
    observe(actions, [device])
    assert actions.view(device).phase == "observed"
    await actions.close()


@pytest.mark.asyncio
async def test_presend_auth_failure_has_no_mutation_or_readback():
    client, device, actions, calls = fixture()

    async def blocked(*_, **__):
        raise RingJsonError("authentication-failed")

    client._transport.request_write = blocked
    client._async_refresh_devices = lambda *_: pytest.fail("No GET after rejected preflight")
    with pytest.raises(LightingActionError) as caught:
        await actions.camera_light(device, True)
    assert not caught.value.possibly_sent and actions.revision == 0 and calls == []
    assert actions.view(device).phase == "rejected"
    await actions.close()


@pytest.mark.asyncio
async def test_partial_accepted_body_is_not_retried_and_can_be_observed():
    client, device, actions, calls = fixture()
    original = client._transport.request_write

    async def write(*args, **kwargs):
        await original(*args, **kwargs)
        return RingWriteReceipt(202, body_complete=False, body_error="body-timeout")

    client._transport.request_write = write
    result = await actions.camera_light(device, True)
    assert result.phase == "observed" and calls == [True]
    await actions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [None, True, 0, -1, "../x", "1?on=off", "1/2", "1%2f2"])
async def test_invalid_api_id_never_enters_transport(identifier):
    client, device, actions, calls = fixture()
    device.device_api_id = identifier
    with pytest.raises(LightingActionError):
        await actions.camera_light(device, True)
    assert calls == [] and not actions._tasks
    await actions.close()


def test_unknown_presentation_does_not_claim_off_or_hold_support():
    view = CameraLightingView()
    assert "unknown" in view.message and "Off" not in view.message
    assert "hold" not in view.message


@pytest.mark.asyncio
async def test_cancellation_before_owned_task_starts_cannot_leave_pending(monkeypatch):
    import asyncio

    from halo_gtk import lighting_actions

    client, device, actions, calls = fixture()
    create = asyncio.create_task

    def cancelled(coroutine):
        task = create(coroutine)
        task.cancel()
        return task

    monkeypatch.setattr(lighting_actions.asyncio, "create_task", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await actions.camera_light(device, True)
    await asyncio.sleep(0)
    assert actions.view(device).phase == "rejected"
    assert calls == [] and actions._active is None and not actions._tasks
    monkeypatch.setattr(lighting_actions.asyncio, "create_task", create)
    await actions.close()
