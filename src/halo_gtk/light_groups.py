"""Scoped Light Group evidence and explicit, bounded, session-owned operations."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace

from halo_gtk.light_group_models import (
    MAX_CACHED_GROUPS,
    MAX_CACHED_MEMBERS,
    ON_DURATIONS,
    LightGroupIntent,
    LightGroupOperationSnapshot,
    LightGroupOperationStatus,
    LightGroupPermission,
    LightGroupResult,
    LightGroupSchemaError,
    LightGroupSnapshot,
    LightGroupsReadStatus,
    LightGroupsSnapshot,
    parse_group_report,
    parse_groups,
    parse_locations,
    validate_identifier,
)
from halo_gtk.light_groups_rest import LightGroupsRestError

INVENTORY_FRESH_SECONDS = 30.0
INVENTORY_CACHE_SECONDS = 60.0
READ_COOLDOWN_SECONDS = 5.0
REPORT_CACHE_SECONDS = 5.0


def light_group_monotonic_time() -> float:
    """Include Linux suspend when aging a command's supporting evidence."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    return time.clock_gettime(clock) if clock is not None else time.monotonic()


@dataclass(slots=True)
class _ReadPolicy:
    attempted: float | None = None
    succeeded: float | None = None
    failures: int = 0
    retry_after: float | None = None
    poll_allowed: bool = True
    server_backoff: bool = False
    receipts: int = 0


@dataclass(slots=True)
class _ReadJob:
    task: asyncio.Task
    consumers: int = 0


class LightGroupsService:
    """One asyncio owner; shared lighting reservation precedes all preflight.

    Automatic/manual reads obey local admission cooldowns. The single owned
    command's preflight and at most two post-POST readbacks may bypass local
    caches/cooldowns, never server backoff or stopped auth/permission recovery.
    A PossiblySent marker is an HTTP admission boundary, not a wire-level fence.
    """

    def __init__(
        self,
        gateway,
        on_snapshot: Callable[[LightGroupsSnapshot], None] | None = None,
        *,
        generation: int,
        is_current: Callable[[], bool] = lambda: True,
        lighting_actions,
        clock: Callable[[], float] = time.time,
        elapsed_clock: Callable[[], float] = light_group_monotonic_time,
    ):
        self._gateway = gateway
        self._on_snapshot = on_snapshot
        self._generation = generation
        self._is_current = is_current
        self._lighting_actions = lighting_actions
        self._clock = clock
        self._elapsed_clock = elapsed_clock
        self._snapshot = LightGroupsSnapshot(generation=generation)
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._reads: dict[tuple, _ReadJob] = {}
        self._policies: dict[tuple, _ReadPolicy] = {}
        self._revision = 0
        self._epoch = 0
        self._operation_serial = 0
        self._operation_task: asyncio.Task | None = None
        self._operation_token = None

    def _current(self) -> bool:
        return not self._closed and self._is_current()

    @property
    def snapshot(self) -> LightGroupsSnapshot:
        return replace(
            self._snapshot,
            locations=tuple(
                replace(location, groups=tuple(self._derive(group) for group in location.groups))
                for location in self._snapshot.locations
            ),
        )

    def _emit(self, **changes) -> None:
        if not self._current():
            return
        self._snapshot = replace(self._snapshot, revision=self._snapshot.revision + 1, **changes)
        if self._on_snapshot is not None:
            # A GTK presentation callback cannot interrupt HTTP admission.
            with suppress(Exception):
                self._on_snapshot(self.snapshot)

    def _next_revision(self) -> int:
        self._revision += 1
        return self._revision

    def _next_epoch(self) -> int:
        self._epoch += 1
        return self._epoch

    def _fresh(self, stamp) -> bool:
        return stamp is not None and 0 <= self._elapsed_clock() - stamp <= INVENTORY_FRESH_SECONDS

    def _reason(self, group: LightGroupSnapshot, *, allow_pending=False) -> str | None:
        if not self._current():
            return "session-changed"
        location = self._snapshot.find_location(group.location_id)
        if not self._snapshot.complete or self._snapshot.status != LightGroupsReadStatus.READY:
            return "refresh-required"
        if location is None:
            return "location-removed"
        if not location.complete or location.status != LightGroupsReadStatus.READY:
            return (
                "permission-denied" if location.code == "permission-denied" else "refresh-required"
            )
        if group.permission == LightGroupPermission.DENIED:
            return "permission-denied"
        if not group.membership_complete:
            return "membership-unavailable"
        if (
            group.stale
            or group.status != LightGroupsReadStatus.READY
            or not self._fresh(group.inventory_monotonic)
            or not self._fresh(group.report_monotonic)
        ):
            return "refresh-required"
        if not isinstance(group.report, bool):
            return "state-unknown"
        if not allow_pending and group.operation is not None and group.operation.active:
            return "busy"
        return None

    def _derive(self, group: LightGroupSnapshot) -> LightGroupSnapshot:
        location = self._snapshot.find_location(group.location_id)
        allowed = group.poll_allowed and self._snapshot.poll_allowed
        deadlines = [group.read_retry_after, self._snapshot.read_retry_after]
        if location is not None:
            allowed = allowed and location.poll_allowed
            deadlines.append(location.read_retry_after)
        reason = self._reason(group)
        return replace(
            group,
            can_write=reason is None,
            unavailable_reason=reason,
            stale=group.stale
            or not self._snapshot.complete
            or location is None
            or not location.complete
            or not self._fresh(group.inventory_monotonic)
            or not self._fresh(group.report_monotonic),
            poll_allowed=allowed,
            read_retry_after=max((stamp for stamp in deadlines if stamp is not None), default=None),
        )

    def _replace_location(self, location) -> None:
        self._emit(
            locations=tuple(
                location if old.location_id == location.location_id else old
                for old in self._snapshot.locations
            )
        )

    def _replace_group(self, group) -> None:
        location = self._snapshot.find_location(group.location_id)
        if location is not None:
            self._replace_location(
                replace(
                    location,
                    groups=tuple(
                        group if old.group_id == group.group_id else old for old in location.groups
                    ),
                )
            )

    def _policy(self, key) -> _ReadPolicy:
        return self._policies.setdefault(key, _ReadPolicy())

    def _read_allowed(self, key, ttl, *, force, internal) -> bool:
        policy, now = self._policy(key), self._elapsed_clock()
        if (
            policy.retry_after is not None
            and now < policy.retry_after
            and (policy.server_backoff or not (force or internal))
        ):
            return False
        if not policy.poll_allowed and not force:
            return False
        if (
            not internal
            and policy.attempted is not None
            and 0 <= now - policy.attempted < READ_COOLDOWN_SECONDS
        ):
            return False
        return not (
            not (force or internal)
            and policy.failures == 0
            and policy.succeeded is not None
            and 0 <= now - policy.succeeded < ttl
        )

    def _read_success(self, key):
        policy = self._policy(key)
        policy.succeeded = self._elapsed_clock()
        policy.receipts += 1
        policy.failures = 0
        policy.retry_after = None
        policy.poll_allowed = True
        policy.server_backoff = False
        return dict(
            read_retry_after=policy.succeeded + READ_COOLDOWN_SECONDS,
            poll_allowed=True,
            read_failure_count=0,
        )

    def _read_failure(self, key, exc):
        policy = self._policy(key)
        code = (
            exc.code
            if isinstance(exc, (LightGroupsRestError, LightGroupSchemaError))
            else "read-failed"
        )
        status = getattr(exc, "status", None)
        policy.failures = min(policy.failures + 1, 4)
        policy.server_backoff = status == 429
        auth = status == 401 or code in {
            "authentication-required",
            "token-refresh-failed",
            "authentication-coordination-unavailable",
            "unsupported-auth-contract",
        }
        denied = status == 403
        policy.poll_allowed = not (auth or denied)
        if auth or denied:
            policy.retry_after = None
        elif policy.server_backoff:
            delay = getattr(exc, "retry_after", None)
            delay = (
                float(delay)
                if isinstance(delay, (int, float))
                and not isinstance(delay, bool)
                and math.isfinite(delay)
                and delay >= 0
                else 15.0
            )
            policy.retry_after = self._elapsed_clock() + min(300.0, delay)
        else:
            policy.retry_after = self._elapsed_clock() + 15.0 * 2 ** (policy.failures - 1)
        return dict(
            status=LightGroupsReadStatus.ERROR,
            code="permission-denied" if denied else "authentication-required" if auth else code,
            read_retry_after=policy.retry_after,
            poll_allowed=policy.poll_allowed,
            read_failure_count=policy.failures,
        )

    async def _consume(self, key, factory) -> None:
        if not self._current():
            return
        job = self._reads.get(key)
        if job is not None and job.task.cancelling() and not job.task.done():
            try:
                await asyncio.shield(job.task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass
            if not self._current():
                return
            job = self._reads.get(key)
        if job is not None and job.task.done():
            self._reads.pop(key, None)
            job = None
        if job is None:
            if key != ("locations",) and sum(item != ("locations",) for item in self._reads) >= 4:
                raise LightGroupsRestError("busy")
            task = asyncio.create_task(factory())
            job = _ReadJob(task)
            self._reads[key] = job

            def finished(done):
                if self._reads.get(key) is job:
                    self._reads.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        job.consumers += 1
        try:
            await asyncio.shield(job.task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        finally:
            job.consumers -= 1
            if job.consumers == 0 and not job.task.done():
                job.task.cancel()

    def _read_current(self) -> bool:
        return self._current() and not asyncio.current_task().cancelling()

    async def refresh_locations(self, force=False, *, _internal=False):
        if not self._current():
            return self.snapshot
        key = ("locations",)
        if key not in self._reads and not self._read_allowed(
            key, INVENTORY_CACHE_SECONDS, force=force, internal=_internal
        ):
            return self.snapshot
        await self._consume(key, self._read_locations)
        return self.snapshot

    async def _read_locations(self):
        key = ("locations",)
        self._policy(key).attempted = self._elapsed_clock()
        try:
            locations = parse_locations(await self._gateway.read_locations(timeout=10.0))
        except Exception as exc:
            if self._read_current():
                self._emit(complete=False, **self._read_failure(key, exc))
            return
        if not self._read_current():
            return
        old = {location.location_id: location for location in self._snapshot.locations}
        merged = []
        for location in locations:
            previous = old.get(location.location_id)
            if previous is None:
                location = replace(
                    location, revision=self._next_revision(), identity_epoch=self._next_epoch()
                )
            else:
                location = replace(
                    previous,
                    name=location.name,
                    revision=self._next_revision()
                    if previous.name != location.name
                    else previous.revision,
                )
            merged.append(location)
        self._emit(
            locations=tuple(merged),
            complete=True,
            has_inventory=True,
            status=LightGroupsReadStatus.READY,
            code=None,
            inventory_received_at=self._clock(),
            inventory_monotonic=self._elapsed_clock(),
            **self._read_success(key),
        )
        self._retire_reads()
        self._prune_policies()

    async def refresh_groups(self, location_id, force=False, *, _internal=False):
        try:
            validate_identifier(location_id)
        except LightGroupSchemaError:
            return self.snapshot
        await self.refresh_locations(force=force, _internal=False)
        location = self._snapshot.find_location(location_id)
        if not self._current() or location is None or not self._snapshot.complete:
            return self.snapshot
        key = ("groups", location_id, location.identity_epoch)
        policy_key = ("groups", location_id)
        if key not in self._reads and not self._read_allowed(
            policy_key, INVENTORY_CACHE_SECONDS, force=force, internal=_internal
        ):
            return self.snapshot
        try:
            await self._consume(
                key, lambda: self._read_groups(location_id, location.identity_epoch)
            )
        except LightGroupsRestError as exc:
            self._groups_failed(location_id, location.identity_epoch, exc)
        return self.snapshot

    @staticmethod
    def _membership(group):
        return (
            group.name,
            group.membership_complete,
            tuple(
                (member.api_id, member.location_id, member.kind, member.name)
                for member in group.members
            ),
        )

    async def _read_groups(self, location_id, epoch):
        key = ("groups", location_id)
        self._policy(key).attempted = self._elapsed_clock()
        try:
            groups = parse_groups(
                await self._gateway.read_groups(location_id, timeout=10.0), location_id
            )
            others = [
                group
                for location in self._snapshot.locations
                if location.location_id != location_id
                for group in location.groups
            ]
            if (
                len(others) + len(groups) > MAX_CACHED_GROUPS
                or sum(len(group.members) for group in (*others, *groups)) > MAX_CACHED_MEMBERS
            ):
                raise LightGroupSchemaError("cache-limit")
        except Exception as exc:
            self._groups_failed(location_id, epoch, exc)
            return
        location = self._snapshot.find_location(location_id)
        if not self._read_current() or location is None or location.identity_epoch != epoch:
            return
        old = {group.group_id: group for group in location.groups}
        merged = []
        for group in groups:
            previous = old.get(group.group_id)
            if previous is None:
                group = replace(
                    group, revision=self._next_revision(), identity_epoch=self._next_epoch()
                )
            elif self._membership(group) == self._membership(previous):
                group = previous
            else:
                group = replace(
                    group,
                    revision=self._next_revision(),
                    identity_epoch=previous.identity_epoch,
                    operation=previous.operation,
                    permission=previous.permission,
                )
            merged.append(
                replace(
                    group,
                    inventory_received_at=self._clock(),
                    inventory_monotonic=self._elapsed_clock(),
                )
            )
        self._replace_location(
            replace(
                location,
                groups=tuple(merged),
                complete=True,
                has_inventory=True,
                status=LightGroupsReadStatus.READY,
                code=None,
                inventory_received_at=self._clock(),
                inventory_monotonic=self._elapsed_clock(),
                **self._read_success(key),
            )
        )
        self._retire_reads()
        self._prune_policies()

    def _groups_failed(self, location_id, epoch, exc):
        location = self._snapshot.find_location(location_id)
        if self._read_current() and location is not None and location.identity_epoch == epoch:
            self._replace_location(
                replace(
                    location, complete=False, **self._read_failure(("groups", location_id), exc)
                )
            )

    async def refresh_details(self, location_id, group_id, force=False, *, _internal=False):
        try:
            validate_identifier(group_id)
        except LightGroupSchemaError:
            return self.snapshot
        await self.refresh_groups(location_id, force=force, _internal=_internal)
        group = self._snapshot.find_group(location_id, group_id)
        location = self._snapshot.find_location(location_id)
        if (
            not self._current()
            or group is None
            or location is None
            or not self._snapshot.complete
            or not location.complete
        ):
            return self.snapshot
        await self._refresh_report(group, force=force, internal=_internal)
        return self.snapshot

    async def _refresh_report(self, group, *, force=False, internal=False):
        location = self._snapshot.find_location(group.location_id)
        if location is None:
            return
        for parent in (("locations",), ("groups", group.location_id)):
            policy = self._policies.get(parent)
            if policy is not None and (
                (not policy.poll_allowed and not force)
                or (
                    policy.server_backoff
                    and policy.retry_after is not None
                    and self._elapsed_clock() < policy.retry_after
                )
            ):
                return
        key = (
            "report",
            group.location_id,
            location.identity_epoch,
            group.group_id,
            group.identity_epoch,
            group.revision,
        )
        policy_key = ("report", group.location_id, group.group_id)
        if key not in self._reads and not self._read_allowed(
            policy_key, REPORT_CACHE_SECONDS, force=force, internal=internal
        ):
            return
        try:
            await self._consume(
                key, lambda: self._read_report(group, location.identity_epoch, force=force)
            )
        except LightGroupsRestError as exc:
            self._report_failed(group, location.identity_epoch, exc)

    def _same_group(self, group, location_epoch, *, revision=True):
        location = self._snapshot.find_location(group.location_id)
        current = self._snapshot.find_group(group.location_id, group.group_id)
        return (
            current
            if location is not None
            and location.identity_epoch == location_epoch
            and current is not None
            and current.identity_epoch == group.identity_epoch
            and (not revision or current.revision == group.revision)
            else None
        )

    async def _read_report(self, group, location_epoch, *, force):
        key = ("report", group.location_id, group.group_id)
        self._policy(key).attempted = self._elapsed_clock()
        try:
            report = parse_group_report(
                await self._gateway.read_report(group.location_id, group.group_id, timeout=10.0),
                group.location_id,
                group.group_id,
                members=group.members,
            )
        except Exception as exc:
            self._report_failed(group, location_epoch, exc)
            return
        current = self._same_group(group, location_epoch)
        if not self._read_current() or current is None:
            return
        states = {member.api_id: member.lights_on for member in report.members}
        members = tuple(
            replace(member, lights_on=states.get(member.api_id)) for member in current.members
        )
        changed = current.report != report.report or current.members != members
        permission = LightGroupPermission.UNKNOWN if force else current.permission
        if permission != current.permission:
            changed = True
        self._replace_group(
            replace(
                current,
                report=report.report,
                members=members,
                stale=False,
                status=LightGroupsReadStatus.READY,
                code=None,
                report_received_at=self._clock(),
                report_monotonic=self._elapsed_clock(),
                revision=self._next_revision() if changed else current.revision,
                permission=permission,
                **self._read_success(key),
            )
        )

    def _report_failed(self, group, location_epoch, exc):
        current = self._same_group(group, location_epoch)
        if self._read_current() and current is not None:
            changes = self._read_failure(("report", group.location_id, group.group_id), exc)
            denied = getattr(exc, "status", None) == 403
            self._replace_group(
                replace(
                    current,
                    stale=True,
                    permission=LightGroupPermission.DENIED if denied else current.permission,
                    revision=self._next_revision()
                    if denied and current.permission != LightGroupPermission.DENIED
                    else current.revision,
                    **changes,
                )
            )

    def _retire_reads(self):
        for key, job in tuple(self._reads.items()):
            if key == ("locations",):
                continue
            location = self._snapshot.find_location(key[1])
            valid = location is not None and location.identity_epoch == key[2]
            if valid and key[0] == "report":
                group = location.find_group(key[3])
                valid = (
                    group is not None
                    and group.identity_epoch == key[4]
                    and group.revision == key[5]
                )
            if not valid and job.task is not asyncio.current_task():
                job.task.cancel()

    def _prune_policies(self):
        for key in tuple(self._policies):
            if key == ("locations",):
                continue
            location = self._snapshot.find_location(key[1])
            if location is None or (key[0] == "report" and location.find_group(key[2]) is None):
                self._policies.pop(key, None)

    def _intent_reason(self, location_id, group_id, expected_revision, intent):
        if not isinstance(intent, LightGroupIntent):
            return "invalid-intent"
        if not self._current() or intent.generation != self._generation:
            return "session-changed"
        if intent.cancelled:
            return "cancelled"
        location = self._snapshot.find_location(location_id)
        if location is None:
            return "location-removed"
        group = location.find_group(group_id)
        if group is None:
            return "group-removed"
        if (
            intent.location_id != location_id
            or intent.group_id != group_id
            or intent.revision != expected_revision
            or group.revision != expected_revision
            or intent.identity_epoch != group.identity_epoch
        ):
            return "group-changed"
        return None

    async def request_lights(
        self, location_id, group_id, on, *, duration, expected_revision, intent
    ) -> LightGroupResult:
        invalid = (
            not isinstance(on, bool)
            or (on and (type(duration) is not int or duration not in ON_DURATIONS))
            or (not on and duration is not None)
        )
        if invalid:
            return LightGroupResult(
                LightGroupOperationStatus.REJECTED, location_id, group_id, code="invalid-request"
            )
        reason = self._intent_reason(location_id, group_id, expected_revision, intent)
        if reason is not None:
            return LightGroupResult(
                LightGroupOperationStatus.REJECTED, location_id, group_id, code=reason
            )
        # No async preflight or task exists until the global camera/group slot
        # is reserved. Additional clicks fail Busy, never join an actuator queue.
        token = self._lighting_actions.try_acquire()
        if token is None:
            return LightGroupResult(
                LightGroupOperationStatus.REJECTED,
                location_id,
                group_id,
                code="busy" if self._current() else "session-changed",
            )
        if self._operation_task is not None:
            self._lighting_actions.release(token)
            return LightGroupResult(
                LightGroupOperationStatus.REJECTED, location_id, group_id, code="busy"
            )
        group = self._snapshot.find_group(location_id, group_id)
        deadline = self._elapsed_clock() + 30.0
        self._operation_serial += 1
        operation = LightGroupOperationSnapshot(
            self._operation_serial, LightGroupOperationStatus.PREPARING, True, on, duration
        )
        self._replace_group(replace(group, operation=operation))
        self._operation_token = token
        task = asyncio.create_task(
            self._run_operation(group, expected_revision, intent, token, operation, deadline)
        )
        self._operation_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            intent.cancel()
            raise

    def _set_operation(self, target, operation, *, stale=None):
        group = self._snapshot.find_group(target.location_id, target.group_id)
        if group is not None and group.identity_epoch == target.identity_epoch:
            changes = dict(operation=operation)
            if stale is not None:
                changes["stale"] = stale
            self._replace_group(replace(group, **changes))

    def _command_block(self, target, expected_revision, intent):
        reason = self._intent_reason(target.location_id, target.group_id, expected_revision, intent)
        group = self._snapshot.find_group(target.location_id, target.group_id)
        if reason is None:
            reason = self._reason(group, allow_pending=True)
        # A stopped auth/permission scope or Retry-After is not bypassed by the
        # command's local cache exception, including after a slot/token wait.
        if reason is None:
            for key in (
                ("locations",),
                ("groups", target.location_id),
                ("report", target.location_id, target.group_id),
            ):
                policy = self._policies.get(key)
                if policy is not None and not policy.poll_allowed:
                    reason = (
                        "permission-denied"
                        if group.permission == LightGroupPermission.DENIED
                        else "refresh-required"
                    )
                    break
                if (
                    policy is not None
                    and policy.server_backoff
                    and policy.retry_after is not None
                    and self._elapsed_clock() < policy.retry_after
                ):
                    reason = "retry-later"
                    break
        return reason

    async def _run_operation(self, target, expected_revision, intent, token, operation, deadline):
        task = asyncio.current_task()
        admitted = False

        async def cancel_unsent():
            while not admitted:
                reason = self._intent_reason(
                    target.location_id, target.group_id, expected_revision, intent
                )
                if reason is not None:
                    task.cancel()
                    return
                await asyncio.sleep(0.05)

        cancellation = asyncio.create_task(cancel_unsent())

        def expired():
            return self._elapsed_clock() >= deadline or task.cancelling() > 0

        def require_budget():
            if expired():
                raise LightGroupsRestError("timeout", possibly_sent=admitted)

        def admission():
            require_budget()
            reason = self._command_block(target, expected_revision, intent)
            if reason is not None:
                raise LightGroupsRestError(reason)
            try:
                self._lighting_actions.admission(token)
            except Exception as exc:
                raise LightGroupsRestError(getattr(exc, "code", "session-changed")) from None

        def on_admitted():
            nonlocal admitted, operation
            admission()
            admitted = True
            operation = replace(
                operation, status=LightGroupOperationStatus.POSSIBLY_SENT, possibly_sent=True
            )
            self._set_operation(target, operation)
            # Invalidate only independently joined current camera members. The
            # aggregate is never copied into camera or member state.
            current = self._snapshot.find_group(target.location_id, target.group_id)
            self._lighting_actions.invalidate_group_members(target.location_id, current.members)
            require_budget()

        try:
            async with asyncio.timeout(max(0.0, deadline - self._elapsed_clock())):
                require_budget()
                reason = self._intent_reason(
                    target.location_id, target.group_id, expected_revision, intent
                )
                if reason is not None:
                    raise LightGroupsRestError(reason)
                group = self._snapshot.find_group(target.location_id, target.group_id)
                if self._reason(group, allow_pending=True) == "refresh-required":
                    await self.refresh_details(target.location_id, target.group_id, _internal=True)
                admission()
                receipt = await self._gateway.set_lights(
                    target.location_id,
                    target.group_id,
                    operation.on,
                    duration_seconds=operation.duration,
                    timeout=10.0,
                    admission=admission,
                    on_admitted=on_admitted,
                )
                require_budget()
                group = self._snapshot.find_group(target.location_id, target.group_id)
                if (
                    not self._current()
                    or group is None
                    or group.identity_epoch != target.identity_epoch
                ):
                    operation = replace(
                        operation,
                        status=LightGroupOperationStatus.UNKNOWN,
                        active=False,
                        code="group-changed",
                    )
                elif not receipt.accepted:
                    operation = replace(
                        operation,
                        status=LightGroupOperationStatus.REJECTED,
                        active=False,
                        code="http-rejected",
                    )
                else:
                    operation = replace(operation, status=LightGroupOperationStatus.ACCEPTED)
                    self._set_operation(target, operation)
                    # Join and retire any report GET already admitted before the
                    # POST completed. It cannot count as either fresh readback.
                    existing = [
                        job.task
                        for key, job in self._reads.items()
                        if key[0] == "report"
                        and key[1] == target.location_id
                        and key[3] == target.group_id
                    ]
                    if existing:
                        await asyncio.gather(
                            *(asyncio.shield(read) for read in existing), return_exceptions=True
                        )
                        require_budget()
                    for _ in range(2):
                        require_budget()
                        group = self._snapshot.find_group(target.location_id, target.group_id)
                        if (
                            not self._current()
                            or group is None
                            or group.identity_epoch != target.identity_epoch
                        ):
                            break
                        policy_key = ("report", target.location_id, target.group_id)
                        previous_receipt = self._policy(policy_key).receipts
                        await self._refresh_report(group, internal=True)
                        require_budget()
                        current = self._snapshot.find_group(target.location_id, target.group_id)
                        if (
                            self._current()
                            and current is not None
                            and current.identity_epoch == target.identity_epoch
                            and current.status == LightGroupsReadStatus.READY
                            and not current.stale
                            and current.report is operation.on
                            and self._policy(policy_key).receipts > previous_receipt
                        ):
                            operation = replace(
                                operation,
                                status=LightGroupOperationStatus.REPORTED,
                                active=False,
                                code=None,
                            )
                            break
                    else:
                        operation = replace(
                            operation,
                            status=LightGroupOperationStatus.UNKNOWN,
                            active=False,
                            code="readback-unconfirmed",
                        )
                    if operation.active:
                        operation = replace(
                            operation,
                            status=LightGroupOperationStatus.UNKNOWN,
                            active=False,
                            code="group-changed",
                        )
        except LightGroupsRestError as exc:
            sent = admitted or exc.possibly_sent
            rejected = exc.status is not None and 400 <= exc.status < 500
            operation = replace(
                operation,
                status=LightGroupOperationStatus.REJECTED
                if rejected or not sent
                else LightGroupOperationStatus.UNKNOWN,
                active=False,
                possibly_sent=sent,
                code=exc.code,
            )
            if self._current() and exc.status in (401, 403, 429):
                location = self._snapshot.find_location(target.location_id)
                group = self._snapshot.find_group(target.location_id, target.group_id)
                if (
                    location is not None
                    and group is not None
                    and group.identity_epoch == target.identity_epoch
                ):
                    self._report_failed(group, location.identity_epoch, exc)
        except (TimeoutError, asyncio.CancelledError):
            code = self._intent_reason(
                target.location_id, target.group_id, expected_revision, intent
            ) or ("closed" if self._closed else "timeout")
            operation = replace(
                operation,
                status=LightGroupOperationStatus.UNKNOWN
                if admitted
                else LightGroupOperationStatus.REJECTED,
                active=False,
                code="outcome-unknown" if admitted else code,
            )
        except Exception:
            operation = replace(
                operation,
                status=LightGroupOperationStatus.UNKNOWN
                if admitted
                else LightGroupOperationStatus.REJECTED,
                active=False,
                code="request-failed",
            )
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
            self._lighting_actions.release(token)
            if self._operation_token is token:
                self._operation_token = None
            if self._operation_task is task:
                self._operation_task = None
        if operation.status == LightGroupOperationStatus.REPORTED and expired():
            operation = replace(
                operation, status=LightGroupOperationStatus.UNKNOWN, code="outcome-unknown"
            )
        self._set_operation(
            target,
            operation,
            stale=True
            if operation.possibly_sent and operation.status == LightGroupOperationStatus.UNKNOWN
            else None,
        )
        group = self._snapshot.find_group(target.location_id, target.group_id)
        return LightGroupResult(
            operation.status,
            target.location_id,
            target.group_id,
            operation.operation_id,
            operation.code,
            group.report
            if group is not None and group.identity_epoch == target.identity_epoch
            else None,
        )

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)

    async def _close_resources(self):
        tasks = {job.task for job in self._reads.values()}
        if self._operation_task is not None:
            tasks.add(self._operation_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reads.clear()
        self._policies.clear()
        if self._operation_token is not None:
            self._lighting_actions.release(self._operation_token)
            self._operation_token = None
        self._operation_task = None
        locations = []
        for location in self._snapshot.locations:
            groups = []
            for group in location.groups:
                operation = group.operation
                if operation is not None and operation.active:
                    operation = replace(
                        operation,
                        active=False,
                        status=LightGroupOperationStatus.UNKNOWN
                        if operation.possibly_sent
                        else LightGroupOperationStatus.REJECTED,
                        code="closed",
                    )
                groups.append(
                    replace(
                        group,
                        stale=True,
                        can_write=False,
                        poll_allowed=False,
                        unavailable_reason="closed",
                        operation=operation,
                    )
                )
            locations.append(
                replace(
                    location,
                    groups=tuple(groups),
                    complete=False,
                    poll_allowed=False,
                    status=LightGroupsReadStatus.CLOSED,
                )
            )
        self._snapshot = replace(
            self._snapshot,
            locations=tuple(locations),
            complete=False,
            poll_allowed=False,
            status=LightGroupsReadStatus.CLOSED,
        )
        await self._gateway.close()
