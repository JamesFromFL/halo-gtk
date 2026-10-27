"""Offline Light Groups service authority, scheduling and actuator regressions."""

import asyncio
from copy import deepcopy

import pytest

from halo_gtk.light_group_models import (
    LightGroupIntent,
    LightGroupPermission,
    LightGroupsReadStatus,
)
from halo_gtk.light_group_models import (
    LightGroupOperationStatus as Status,
)
from halo_gtk.light_groups import LightGroupsService
from halo_gtk.light_groups_rest import LightGroupsRestError
from halo_gtk.ring_json import RingWriteReceipt


def member(api_id=10, location="home", **changes):
    return {
        "doorbot_id": api_id,
        "location_id": location,
        "type": "hp_cam_v1",
        "name": "Lamp",
        "deleted_at": None,
        **changes,
    }


def group(group_id="garden", location="home", **changes):
    return {
        "device_group_id": group_id,
        "location_id": location,
        "name": "Garden",
        "devices": [member(location=location)],
        "deleted_at": None,
        **changes,
    }


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Actions:
    def __init__(self):
        self.token = None
        self.acquisitions = 0
        self.releases = []
        self.invalidations = []
        self.current = True

    def try_acquire(self):
        self.acquisitions += 1
        if self.token is not None or not self.current:
            return None
        self.token = object()
        return self.token

    def admission(self, token):
        if not self.current or token is not self.token:
            error = RuntimeError("retired")
            error.code = "session-changed"
            raise error

    def release(self, token):
        self.releases.append(token)
        if token is self.token:
            self.token = None
            return True
        return False

    def invalidate_group_members(self, location_id, members):
        self.invalidations.append((location_id, members))


class Gateway:
    def __init__(self):
        self.locations = {"user_locations": [{"location_id": "home", "name": "Home"}]}
        self.groups = {"home": {"device_groups": [group()]}}
        self.reports = {
            ("home", "garden"): {
                "device_group_id": "garden",
                "lights_on": False,
                "devices": [{"id": "10", "lights_on": False}],
            }
        }
        self.calls = []
        self.writes = []
        self.read_hook = None
        self.before_admission = None
        self.after_admission = None
        self.read_error = None
        self.write_error = None
        self.receipt = RingWriteReceipt(202)
        self.apply = True
        self.closed = False

    async def _read(self, key, payload):
        self.calls.append(key)
        if self.read_hook:
            await self.read_hook(key)
        if self.read_error and key[0] == "report":
            raise self.read_error
        return deepcopy(payload)

    async def read_locations(self, *, timeout):
        return await self._read(("locations",), self.locations)

    async def read_groups(self, location_id, *, timeout):
        return await self._read(("groups", location_id), self.groups[location_id])

    async def read_report(self, location_id, group_id, *, timeout):
        return await self._read(
            ("report", location_id, group_id), self.reports[(location_id, group_id)]
        )

    async def set_lights(
        self, location_id, group_id, on, *, duration_seconds, timeout, admission, on_admitted
    ):
        if self.before_admission:
            await self.before_admission()
        admission()
        on_admitted()
        self.writes.append((location_id, group_id, on, duration_seconds))
        if self.after_admission:
            await self.after_admission()
        if self.write_error:
            raise self.write_error
        if self.apply:
            self.reports[(location_id, group_id)]["lights_on"] = on
        return self.receipt

    async def close(self):
        self.closed = True


async def ready(gateway=None, **kwargs):
    gateway = gateway or Gateway()
    clock, actions = Clock(), Actions()
    service = LightGroupsService(
        gateway, generation=7, lighting_actions=actions, clock=clock, elapsed_clock=clock, **kwargs
    )
    await service.refresh_details("home", "garden")
    return service, gateway, clock, actions


def intent_for(service, location="home", group_id="garden"):
    selected = service.snapshot.find_group(location, group_id)
    return LightGroupIntent(
        service.snapshot.generation, location, group_id, selected.revision, selected.identity_epoch
    )


async def request(
    service, on=True, duration=60, *, location="home", group_id="garden", intent=None
):
    intent = intent or intent_for(service, location, group_id)
    return await service.request_lights(
        location, group_id, on, duration=duration, expected_revision=intent.revision, intent=intent
    )


@pytest.mark.asyncio
async def test_read_only_entry_supports_lighting_only_locations_no_commands():
    service, gateway, _, actions = await ready()
    snapshot = service.snapshot
    selected = snapshot.find_group("home", "garden")
    assert snapshot.complete and snapshot.has_inventory
    assert selected.can_write and selected.report is False
    assert selected.permission == LightGroupPermission.UNKNOWN
    assert selected.member_count == 1
    assert gateway.calls == [("locations",), ("groups", "home"), ("report", "home", "garden")]
    assert not gateway.writes and actions.acquisitions == 0
    await service.close()


@pytest.mark.asyncio
async def test_identical_receipts_do_not_change_semantic_revision_or_epoch():
    service, gateway, clock, _ = await ready()
    before = service.snapshot
    clock.now += 6
    await service.refresh_details("home", "garden", force=True)
    after = service.snapshot
    assert after.revision > before.revision
    assert (
        after.find_group("home", "garden").revision == before.find_group("home", "garden").revision
    )
    assert (
        after.find_group("home", "garden").identity_epoch
        == before.find_group("home", "garden").identity_epoch
    )
    assert after.find_group("home", "garden").report_monotonic == 106
    assert len(gateway.calls) == 6
    await service.close()


@pytest.mark.asyncio
async def test_inventory_and_report_caches_and_manual_cooldown():
    service, gateway, clock, _ = await ready()
    await service.refresh_details("home", "garden", force=True)
    assert len(gateway.calls) == 3
    clock.now += 5
    await service.refresh_details("home", "garden")
    assert gateway.calls[-1] == ("report", "home", "garden") and len(gateway.calls) == 4
    clock.now += 6
    await service.refresh_details("home", "garden", force=True)
    assert len(gateway.calls) == 7
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["locations", "groups", "report"])
async def test_invalid_schema_preserves_stale_evidence_and_blocks_writes(target):
    service, gateway, clock, _ = await ready()
    if target == "locations":
        gateway.locations = {}
    elif target == "groups":
        gateway.groups["home"] = {}
    else:
        gateway.reports[("home", "garden")] = {}
    clock.now += 6
    await service.refresh_details("home", "garden", force=True)
    selected = service.snapshot.find_group("home", "garden")
    assert selected is not None and selected.stale and not selected.can_write
    assert (await request(service)).status == Status.REJECTED
    assert not gateway.writes
    assert service.snapshot.has_inventory
    await service.close()


@pytest.mark.asyncio
async def test_empty_groups_only_remove_exact_location_and_do_not_revive_on_failure():
    gateway = Gateway()
    gateway.locations["user_locations"].append({"location_id": "other", "name": "Other"})
    gateway.groups["other"] = {"device_groups": [group(location="other")]}
    service, gateway, clock, _ = await ready(gateway)
    await service.refresh_groups("other")
    old = intent_for(service)
    clock.now += 6
    gateway.groups["home"] = {"device_groups": []}
    await service.refresh_groups("home", force=True)
    assert service.snapshot.find_group("home", "garden") is None
    assert service.snapshot.find_group("other", "garden") is not None
    clock.now += 6
    gateway.groups["home"] = {}
    await service.refresh_groups("home", force=True)
    assert not service.snapshot.find_location("home").groups
    assert service.snapshot.find_location("home").has_inventory
    assert (await request(service, intent=old)).code == "group-removed"
    await service.close()


@pytest.mark.asyncio
async def test_removed_location_and_reappeared_group_receive_new_epochs():
    service, gateway, clock, _ = await ready()
    old = intent_for(service)
    gateway.locations = {"user_locations": []}
    clock.now += 6
    await service.refresh_locations(force=True)
    assert not service.snapshot.locations
    assert (await request(service, intent=old)).code == "location-removed"
    gateway.locations = {"user_locations": [{"location_id": "home", "name": "Home"}]}
    clock.now += 6
    await service.refresh_details("home", "garden", force=True)
    assert service.snapshot.find_group("home", "garden").identity_epoch != old.identity_epoch
    assert (await request(service, intent=old)).code == "group-changed"
    await service.close()


@pytest.mark.asyncio
async def test_membership_change_invalidates_report_and_old_intent():
    service, gateway, clock, _ = await ready()
    old = intent_for(service)
    gateway.groups["home"]["device_groups"][0]["devices"].append(member(11))
    clock.now += 6
    await service.refresh_groups("home", force=True)
    selected = service.snapshot.find_group("home", "garden")
    assert selected.report is None and selected.stale
    assert selected.revision != old.revision
    assert (await request(service, intent=old)).code == "group-changed"
    await service.close()


@pytest.mark.asyncio
async def test_member_state_is_not_filled_from_group_aggregate():
    gateway = Gateway()
    gateway.reports[("home", "garden")]["lights_on"] = True
    gateway.reports[("home", "garden")]["devices"][0].pop("lights_on")
    service, _, _, _ = await ready(gateway)
    selected = service.snapshot.find_group("home", "garden")
    assert selected.report is True and selected.members[0].lights_on is None
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, 1, "false"])
async def test_unknown_group_report_is_readable_but_read_only(value):
    gateway = Gateway()
    gateway.reports[("home", "garden")]["lights_on"] = value
    service, gateway, _, _ = await ready(gateway)
    assert service.snapshot.find_group("home", "garden").report is None
    assert not service.snapshot.find_group("home", "garden").can_write
    assert (await request(service)).status == Status.REJECTED
    assert not gateway.writes
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,poll,delay",
    [
        (LightGroupsRestError("http-error", status=403), False, None),
        (LightGroupsRestError("authentication-required", status=401), False, None),
        (LightGroupsRestError("http-error", status=429, retry_after=1000), True, 300),
        (LightGroupsRestError("network-unavailable"), True, 15),
    ],
)
async def test_read_failure_stops_or_backs_off_without_hidden_reprobe(error, poll, delay):
    service, gateway, clock, _ = await ready()
    clock.now += 6
    gateway.read_error = error
    await service.refresh_details("home", "garden", force=True)
    selected = service.snapshot.find_group("home", "garden")
    assert selected.poll_allowed is poll and selected.read_failure_count == 1
    assert selected.read_retry_after == clock.now + (delay if delay is not None else 5)
    before = len(gateway.calls)
    clock.now += 6
    await service.refresh_details("home", "garden")
    assert len(gateway.calls) == before
    if error.status == 429:
        await service.refresh_details("home", "garden", force=True)
        # Location/group manual reads can proceed; the rate-limited report cannot.
        assert gateway.calls.count(("report", "home", "garden")) == 2
    gateway.read_error = None
    clock.now += 301
    await service.refresh_details("home", "garden", force=True)
    assert service.snapshot.find_group("home", "garden").poll_allowed
    assert service.snapshot.find_group("home", "garden").read_failure_count == 0
    assert service.snapshot.find_group("home", "garden").permission == LightGroupPermission.UNKNOWN
    await service.close()


@pytest.mark.asyncio
async def test_transient_poll_backoff_is_15_30_60_120_and_resets():
    service, gateway, clock, _ = await ready()
    gateway.read_error = LightGroupsRestError("network-unavailable")
    for delay in (15, 30, 60, 120, 120):
        clock.now += 121
        await service.refresh_details("home", "garden")
        assert service.snapshot.find_group("home", "garden").read_retry_after == clock.now + delay
    gateway.read_error = None
    clock.now += 121
    await service.refresh_details("home", "garden")
    assert service.snapshot.find_group("home", "garden").read_retry_after == clock.now + 5
    await service.close()


@pytest.mark.asyncio
async def test_explicit_on_and_off_one_post_each_reported_not_physical_confirmation():
    service, gateway, _, actions = await ready()
    result = await request(service)
    assert result.status == Status.REPORTED and result.reported is True
    assert gateway.writes == [("home", "garden", True, 60)]
    assert actions.token is None and len(actions.invalidations) == 1
    result = await request(service, False, None)
    assert result.status == Status.REPORTED and result.reported is False
    assert gateway.writes[-1] == ("home", "garden", False, None)
    assert len(gateway.writes) == 2
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "on,duration",
    [
        (1, 60),
        ("true", 60),
        (True, True),
        (True, 0),
        (True, -1),
        (True, "60"),
        (True, None),
        (True, 30),
        (False, 60),
    ],
)
async def test_invalid_inputs_never_reserve_or_send(on, duration):
    service, gateway, _, actions = await ready()
    result = await request(service, on, duration)
    assert result.code == "invalid-request" and not gateway.writes
    assert actions.acquisitions == 0
    await service.close()


@pytest.mark.asyncio
async def test_busy_is_immediate_before_stale_preflight_no_backlog():
    service, gateway, clock, actions = await ready()
    actions.token = object()  # An existing camera-light action owns the slot.
    clock.now += 31
    before = len(gateway.calls)
    result = await request(service)
    assert result.code == "busy"
    assert len(gateway.calls) == before and service._operation_task is None
    await service.close()


@pytest.mark.asyncio
async def test_stale_preflight_uses_reserved_slot_and_unchanged_receipt_revision():
    service, gateway, clock, actions = await ready()
    old = intent_for(service)
    clock.now += 31
    seen = []

    async def inspect(key):
        seen.append(actions.token is not None)

    gateway.read_hook = inspect
    result = await request(service, intent=old)
    assert result.status == Status.REPORTED and all(seen)
    assert actions.token is None
    await service.close()


@pytest.mark.asyncio
async def test_unmatched_readback_has_two_reads_no_optimism_replay_rollback_or_timer():
    service, gateway, _, actions = await ready()
    gateway.apply = False
    result = await request(service)
    assert result.status == Status.UNKNOWN
    assert len(gateway.writes) == 1
    assert gateway.calls.count(("report", "home", "garden")) == 3
    selected = service.snapshot.find_group("home", "garden")
    assert selected.report is False and selected.stale
    assert not selected.operation.active and actions.token is None
    await service.close()


@pytest.mark.asyncio
async def test_incomplete_2xx_reply_uses_readback_no_alternate_post():
    service, gateway, _, _ = await ready()
    gateway.receipt = RingWriteReceipt(202, False, "response-too-large")
    assert (await request(service)).status == Status.REPORTED
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, Status.REJECTED),
        (403, Status.REJECTED),
        (429, Status.REJECTED),
        (500, Status.UNKNOWN),
        (503, Status.UNKNOWN),
        (None, Status.UNKNOWN),
    ],
)
async def test_write_errors_do_not_replay_or_invent_state(status, expected):
    service, gateway, _, actions = await ready()
    gateway.write_error = LightGroupsRestError(
        "http-error", status=status, possibly_sent=True, retry_after=60
    )
    result = await request(service)
    assert result.status == expected and len(gateway.writes) == 1
    assert service.snapshot.find_group("home", "garden").report is False
    assert actions.token is None
    if status == 403:
        assert (
            service.snapshot.find_group("home", "garden").permission == LightGroupPermission.DENIED
        )
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("cancel", "cancelled"),
        ("session", "session-changed"),
        ("token", "session-changed"),
        ("age", "refresh-required"),
        ("membership", "group-changed"),
        ("remove", "group-removed"),
        ("permission", "group-changed"),
    ],
)
async def test_final_http_admission_rechecks_after_wait(mutation, code):
    current = [True]
    service, gateway, clock, actions = await ready(is_current=lambda: current[0])
    intent = intent_for(service)
    if mutation == "age":
        clock.now += 16

    async def mutate():
        if mutation == "cancel":
            intent.cancel()
        elif mutation == "session":
            current[0] = False
        elif mutation == "token":
            actions.current = False
        elif mutation == "age":
            clock.now += 15  # Evidence aged past30s within the action's30s budget.
        elif mutation == "permission":
            clock.now += 6
            gateway.read_error = LightGroupsRestError("http-error", status=403)
            await service.refresh_details("home", "garden", force=True)
        else:
            clock.now += 6
            if mutation == "membership":
                gateway.groups["home"]["device_groups"][0]["name"] = "Changed target"
            else:
                gateway.groups["home"] = {"device_groups": []}
            await service.refresh_groups("home", force=True)

    gateway.before_admission = mutate
    result = await request(service, intent=intent)
    assert result.status == Status.REJECTED and result.code == code
    assert not gateway.writes and not actions.invalidations
    assert actions.token is None
    await service.close()


@pytest.mark.asyncio
async def test_view_cancel_before_admission_releases_owned_preflight():
    service, gateway, _, actions = await ready()
    started = asyncio.Event()

    async def hold():
        started.set()
        await asyncio.Event().wait()

    gateway.before_admission = hold
    intent = intent_for(service)
    pending = asyncio.create_task(request(service, intent=intent))
    await started.wait()
    intent.cancel()
    result = await asyncio.wait_for(pending, 1)
    assert result.status == Status.REJECTED and result.code == "cancelled"
    assert actions.token is None and not gateway.writes
    await service.close()


@pytest.mark.asyncio
async def test_canceled_presentation_after_admission_does_not_release_owned_action():
    service, gateway, _, actions = await ready()
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        started.set()
        await release.wait()

    gateway.after_admission = hold
    intent = intent_for(service)
    caller = asyncio.create_task(request(service, intent=intent))
    await started.wait()
    assert service.snapshot.find_group("home", "garden").operation.possibly_sent
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert actions.token is not None and service._operation_task is not None
    release.set()
    await service._operation_task
    assert actions.token is None
    assert service.snapshot.find_group("home", "garden").operation.status == Status.REPORTED
    await service.close()


@pytest.mark.asyncio
async def test_whole_deadline_bounds_preflight_post_and_readback(monkeypatch):
    service, gateway, _, actions = await ready()
    real_timeout = asyncio.timeout
    budgets = []

    def shorter(seconds):
        budgets.append(seconds)
        return real_timeout(0.01)

    async def hold():
        await asyncio.Event().wait()

    gateway.after_admission = hold
    monkeypatch.setattr("halo_gtk.light_groups.asyncio.timeout", shorter)
    result = await request(service)
    assert result.status == Status.UNKNOWN and result.code == "outcome-unknown"
    assert budgets == [30.0] and actions.token is None and len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
async def test_joined_close_cancels_owned_work_and_does_not_leak_reservation():
    service, gateway, _, actions = await ready()
    started = asyncio.Event()

    async def hold():
        started.set()
        await asyncio.Event().wait()

    gateway.after_admission = hold
    caller = asyncio.create_task(request(service))
    await started.wait()
    await asyncio.gather(service.close(), service.close())
    assert (await caller).status == Status.UNKNOWN
    assert actions.token is None and gateway.closed
    assert not service._reads and service._operation_task is None
    assert service.snapshot.status == LightGroupsReadStatus.CLOSED
    assert not service.snapshot.find_group("home", "garden").operation.active


@pytest.mark.asyncio
async def test_publication_and_sdk_errors_never_expose_raw_private_text():
    service, gateway, _, _ = await ready(
        on_snapshot=lambda snapshot: (_ for _ in ()).throw(RuntimeError("presentation"))
    )
    gateway.write_error = RuntimeError("https://private.example/?token=secret")
    result = await request(service)
    assert result.status == Status.UNKNOWN and result.code == "request-failed"
    assert "secret" not in repr(service.snapshot)
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
async def test_coalesced_reads_detach_one_consumer_without_canceling_another():
    gateway = Gateway()
    service = LightGroupsService(gateway, generation=1, lighting_actions=Actions())
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(key):
        if key == ("locations",):
            started.set()
            await release.wait()

    gateway.read_hook = hold
    first = asyncio.create_task(service.refresh_locations())
    second = asyncio.create_task(service.refresh_locations())
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert gateway.calls == [("locations",)]
    release.set()
    assert (await second).complete
    assert not service._reads
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_final_consumer_cancel_rapid_reopen_and_late_result_fence(late_error):
    gateway = Gateway()
    service = LightGroupsService(gateway, generation=1, lighting_actions=Actions())
    started, canceled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold(key):
        if len(gateway.calls) == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.set()
                await release.wait()
                if late_error:
                    raise LightGroupsRestError("retired-error") from None

    gateway.read_hook = hold
    old = asyncio.create_task(service.refresh_locations())
    await started.wait()
    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old
    await canceled.wait()
    reopened = asyncio.create_task(service.refresh_locations())
    await asyncio.sleep(0)
    assert len(gateway.calls) == 1
    release.set()
    assert (await reopened).complete
    assert len(gateway.calls) == 2
    assert service.snapshot.revision == 1  # Only replacement publishes success/error.
    await service.close()


@pytest.mark.asyncio
async def test_authoritative_removal_retires_read_and_late_response_cannot_revive():
    service, gateway, clock, _ = await ready()
    clock.now += 6
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(key):
        if key[0] == "report":
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    gateway.read_hook = hold
    old = asyncio.create_task(service.refresh_details("home", "garden"))
    await started.wait()
    gateway.locations = {"user_locations": []}
    await service.refresh_locations(force=True)
    release.set()
    await old
    assert not service.snapshot.locations
    assert set(service._policies) <= {("locations",)}
    await service.close()


@pytest.mark.asyncio
async def test_membership_change_retires_inflight_report_revision():
    service, gateway, clock, _ = await ready()
    clock.now += 6
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(key):
        if key[0] == "report":
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    gateway.read_hook = hold
    old = asyncio.create_task(service.refresh_details("home", "garden"))
    await started.wait()
    gateway.groups["home"]["device_groups"][0]["name"] = "Revised group"
    await service.refresh_groups("home", force=True)
    release.set()
    await old
    selected = service.snapshot.find_group("home", "garden")
    assert selected.name == "Revised group" and selected.report is None
    assert selected.status == LightGroupsReadStatus.UNKNOWN
    await service.close()


@pytest.mark.asyncio
async def test_four_distinct_extra_read_jobs_bound_with_shared_discovery():
    gateway = Gateway()
    gateway.groups["home"] = {"device_groups": [group(str(i)) for i in range(5)]}
    for i in range(5):
        gateway.reports[("home", str(i))] = {
            "device_group_id": str(i),
            "lights_on": False,
            "devices": [{"id": "10"}],
        }
    service = LightGroupsService(gateway, generation=1, lighting_actions=Actions())
    await service.refresh_groups("home")
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(key):
        if key[0] == "report":
            if sum(call[0] == "report" for call in gateway.calls) == 4:
                started.set()
            await release.wait()

    gateway.read_hook = hold
    pending = [asyncio.create_task(service.refresh_details("home", str(i))) for i in range(4)]
    await started.wait()
    snapshot = await service.refresh_details("home", "4")
    assert snapshot.find_group("home", "4").code == "busy"
    assert len(service._reads) == 4
    release.set()
    await asyncio.gather(*pending)
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("constant", ["MAX_CACHED_GROUPS", "MAX_CACHED_MEMBERS"])
async def test_total_cache_limits_preserve_other_locations_without_partial_replacement(
    monkeypatch, constant
):
    monkeypatch.setattr("halo_gtk.light_groups." + constant, 1)
    gateway = Gateway()
    gateway.locations["user_locations"].append({"location_id": "other", "name": "Other"})
    gateway.groups["other"] = {"device_groups": [group(location="other")]}
    service, _, _, _ = await ready(gateway)
    await service.refresh_groups("other")
    assert service.snapshot.find_group("home", "garden") is not None
    assert not service.snapshot.find_location("other").groups
    assert service.snapshot.find_location("other").code == "cache-limit"
    assert not service.snapshot.find_location("other").complete
    await service.close()


@pytest.mark.asyncio
async def test_accepted_post_then_denied_readback_is_unknown_and_scoped_denial():
    service, gateway, _, actions = await ready()

    async def deny_readback():
        gateway.read_error = LightGroupsRestError("http-error", status=403)

    gateway.after_admission = deny_readback
    result = await request(service)
    selected = service.snapshot.find_group("home", "garden")
    assert result.status == Status.UNKNOWN
    assert selected.permission == LightGroupPermission.DENIED
    assert selected.report is False and not selected.poll_allowed
    assert gateway.calls.count(("report", "home", "garden")) == 2
    assert len(gateway.writes) == 1 and actions.token is None
    await service.close()


@pytest.mark.asyncio
async def test_post_readback_backoff_cannot_confirm_old_matching_report():
    service, gateway, clock, _ = await ready()

    async def block_readback():
        gateway.read_error = LightGroupsRestError("http-error", status=429, retry_after=60)

    gateway.after_admission = block_readback
    result = await request(service, False, None)
    # The desired false matched before the POST, but no fresh report succeeded.
    assert result.status == Status.UNKNOWN
    assert service.snapshot.find_group("home", "garden").read_retry_after == clock.now + 60
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
async def test_report_started_before_post_completion_cannot_confirm():
    service, gateway, clock, _ = await ready()
    gateway.apply = False
    write_started, finish_write, old_started, finish_old = (asyncio.Event() for _ in range(4))

    async def hold_write():
        write_started.set()
        await finish_write.wait()

    original = gateway.read_report
    intercepted = False

    async def old_report(location_id, group_id, *, timeout):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            gateway.calls.append(("report", location_id, group_id))
            old_started.set()
            await finish_old.wait()
            return {
                "device_group_id": group_id,
                "lights_on": True,
                "devices": [{"id": "10", "lights_on": False}],
            }
        return await original(location_id, group_id, timeout=timeout)

    gateway.read_report = old_report
    gateway.after_admission = hold_write
    operation = asyncio.create_task(request(service))
    await write_started.wait()
    clock.now += 6
    old = asyncio.create_task(service.refresh_details("home", "garden"))
    await old_started.wait()
    finish_write.set()
    await asyncio.sleep(0)
    finish_old.set()
    await old
    result = await operation
    assert result.status == Status.UNKNOWN
    assert gateway.calls.count(("report", "home", "garden")) == 4
    assert len(gateway.writes) == 1
    await service.close()


@pytest.mark.asyncio
async def test_location_read_denial_stops_child_poll_and_manual_recovery_unsticks():
    service, gateway, clock, _ = await ready()
    original = gateway.read_groups

    async def denied(location_id, *, timeout):
        raise LightGroupsRestError("http-error", status=403)

    gateway.read_groups = denied
    clock.now += 6
    await service.refresh_groups("home", force=True)
    assert not service.snapshot.find_group("home", "garden").poll_allowed
    assert not service.snapshot.find_group("home", "garden").can_write
    gateway.read_groups = original
    clock.now += 6
    await service.refresh_details("home", "garden", force=True)
    assert service.snapshot.find_group("home", "garden").poll_allowed
    assert service.snapshot.find_group("home", "garden").can_write
    await service.close()


@pytest.mark.asyncio
async def test_write_denial_is_scoped_to_exact_location_and_group():
    gateway = Gateway()
    gateway.locations["user_locations"].append({"location_id": "other", "name": "Other"})
    gateway.groups["other"] = {"device_groups": [group(location="other")]}
    gateway.reports[("other", "garden")] = {
        "device_group_id": "garden",
        "lights_on": False,
        "devices": [{"id": "10"}],
    }
    service, gateway, _, _ = await ready(gateway)
    await service.refresh_details("other", "garden")
    gateway.write_error = LightGroupsRestError("http-error", status=403, possibly_sent=True)
    assert (await request(service)).status == Status.REJECTED
    assert service.snapshot.find_group("home", "garden").permission == LightGroupPermission.DENIED
    assert service.snapshot.find_group("other", "garden").permission == LightGroupPermission.UNKNOWN
    assert service.snapshot.find_group("other", "garden").can_write
    assert service.snapshot.find_group("other", "garden").poll_allowed
    await service.close()


@pytest.mark.asyncio
async def test_late_response_after_generation_retirement_never_publishes():
    gateway, actions = Gateway(), Actions()
    current = [True]
    delivered = []
    service = LightGroupsService(
        gateway,
        delivered.append,
        generation=7,
        is_current=lambda: current[0],
        lighting_actions=actions,
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(key):
        started.set()
        await release.wait()

    gateway.read_hook = hold
    pending = asyncio.create_task(service.refresh_locations())
    await started.wait()
    current[0] = False
    release.set()
    await pending
    assert not delivered and not service.snapshot.locations
    assert service.snapshot.revision == 0
    await service.close()


@pytest.mark.asyncio
async def test_preflight_deadline_never_admits_write_and_releases_token(monkeypatch):
    service, gateway, clock, actions = await ready()
    clock.now += 31
    canceled = asyncio.Event()

    async def hold(key):
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    gateway.read_hook = hold
    real_timeout = asyncio.timeout
    monkeypatch.setattr("halo_gtk.light_groups.asyncio.timeout", lambda seconds: real_timeout(0.01))
    result = await request(service)
    assert result.status == Status.REJECTED and result.code == "timeout"
    assert actions.token is None and not gateway.writes
    await canceled.wait()
    await service.close()


@pytest.mark.asyncio
async def test_read_cooldown_is_exposed_but_does_not_fabricate_write_pause():
    service, _, clock, _ = await ready()
    selected = service.snapshot.find_group("home", "garden")
    assert selected.read_retry_after == clock.now + 5
    assert selected.can_write
    assert (await request(service)).status == Status.REPORTED
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after", "readback"])
async def test_synchronous_late_gateway_result_cannot_send_or_report_success(phase):
    service, gateway, clock, actions = await ready()

    async def late():
        clock.now += 31  # No yield: asyncio.timeout cannot run its timer callback.

    async def late_read(key):
        if key[0] == "report":
            clock.now += 31

    if phase == "before":
        gateway.before_admission = late
    elif phase == "after":
        gateway.after_admission = late
    else:
        gateway.read_hook = late_read
    result = await request(service)
    assert result.status == (Status.REJECTED if phase == "before" else Status.UNKNOWN)
    assert len(gateway.writes) == (0 if phase == "before" else 1)
    assert actions.token is None
    assert service.snapshot.find_group("home", "garden").operation.status != Status.REPORTED
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("uncancel", [False, True])
async def test_gateway_suppressed_deadline_cancel_cannot_send_or_confirm(
    monkeypatch, phase, uncancel
):
    service, gateway, clock, actions = await ready()
    settled = asyncio.Event()
    real_timeout = asyncio.timeout
    monkeypatch.setattr("halo_gtk.light_groups.asyncio.timeout", lambda seconds: real_timeout(0.01))

    async def suppress():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if uncancel:
                asyncio.current_task().uncancel()
                clock.now += 31
            await asyncio.sleep(0)  # Simulate completed cooperative HTTP cleanup.
            settled.set()

    if phase == "before":
        gateway.before_admission = suppress
    else:
        gateway.after_admission = suppress
    result = await request(service)
    assert settled.is_set()
    assert result.status == (Status.REJECTED if phase == "before" else Status.UNKNOWN)
    assert len(gateway.writes) == (0 if phase == "before" else 1)
    assert gateway.calls.count(("report", "home", "garden")) == 1
    assert actions.token is None
    await service.close()


@pytest.mark.asyncio
async def test_whole_deadline_is_captured_before_preparing_publication():
    clock = Clock()
    actions, gateway = Actions(), Gateway()

    def slow_publication(snapshot):
        selected = snapshot.find_group("home", "garden")
        if (
            selected is not None
            and selected.operation is not None
            and selected.operation.status == Status.PREPARING
        ):
            clock.now += 31

    service = LightGroupsService(
        gateway,
        slow_publication,
        generation=7,
        lighting_actions=actions,
        clock=clock,
        elapsed_clock=clock,
    )
    await service.refresh_details("home", "garden")
    result = await request(service)
    assert result.status == Status.REJECTED and result.code == "timeout"
    assert not gateway.writes and actions.token is None
    await service.close()
