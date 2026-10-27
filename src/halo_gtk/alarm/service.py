"""Multi-location Ring Alarm supervision and conservative command policy."""

from __future__ import annotations

import asyncio
import contextlib
import random
import secrets
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Any

from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmCapability,
    AlarmConnectionStatus,
    AlarmDeviceKind,
    AlarmInventoryStatus,
    AlarmLocationSnapshot,
    AlarmMode,
    AlarmOperationSnapshot,
    AlarmOperationStatus,
    AlarmPhase,
    AlarmServiceStatus,
    AlarmWriteAuthorization,
    TriState,
    alarm_monotonic_time,
    empty_alarm_snapshot,
)
from halo_gtk.alarm.policy import asset_reason, authorization_reason, panel_reason
from halo_gtk.alarm.protocol import (
    AssetSessionInfo,
    ClapMessage,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    SessionInfo,
)
from halo_gtk.alarm.provider import (
    AlarmCommandResult,
    AlarmCommandStatus,
    AlarmCommandSupersededError,
    AlarmLocationSeed,
    AlarmModePreparation,
    AlarmRefreshResult,
    AlarmRefreshStatus,
    AlarmRestGateway,
)
from halo_gtk.alarm.store import AlarmStateBoundsError, AlarmStateStore
from halo_gtk.alarm.transport import (
    AlarmTicketAsset,
    AlarmTransportClosedError,
    AlarmTransportError,
    RingAlarmTransport,
)


@dataclass(slots=True)
class _LocationRuntime:
    seed: AlarmLocationSeed
    transport: RingAlarmTransport
    phase: str = "connecting"
    epoch: int = 0
    backoff: float = 1.0
    task: asyncio.Task[None] | None = None
    sync_task: asyncio.Task[None] | None = None
    stable_task: asyncio.Task[None] | None = None
    connected_at: float | None = None
    maintenance_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _InventoryWaiter:
    future: asyncio.Future[None]
    baseline: int = 0
    armed: bool = False


@dataclass(slots=True)
class _ModeApproval:
    token: str
    epoch: int
    revision: int
    panel_asset_id: str
    panel_zid: str
    issued_at: float
    target: AlarmMode | None = None


@dataclass(slots=True)
class _PendingCommand:
    target: AlarmMode
    panel_asset_id: str
    panel_zid: str
    epoch: int
    baseline_revision: int
    future: asyncio.Future[AlarmMode]
    armed: bool = False


class _CommandConnectionLost(RuntimeError):
    pass


class _CommandRevisionChanged(AlarmCommandSupersededError):
    pass


class _CommandAdmissionRejected(AlarmCommandSupersededError):
    def __init__(self, code: str) -> None:
        self.code = code


class RingAlarmService:
    """Own per-location transports and publish normalized immutable snapshots."""

    def __init__(
        self,
        request: AlarmRestGateway,
        locations: Iterable[AlarmLocationSeed],
        on_snapshot: Callable[[AlarmAccountSnapshot], None],
        *,
        generation: int = 0,
        transport_factory: Callable[..., RingAlarmTransport] = RingAlarmTransport,
        reconnect_min: float = 1.0,
        reconnect_max: float = 120.0,
        initial_sync_timeout: float = 15.0,
        stable_connection_time: float = 10.0,
        command_timeout: float = 15.0,
        reconcile_timeout: float = 5.0,
        jitter: Callable[[float], float] | None = None,
        poll_interval: float = 0.1,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = alarm_monotonic_time,
        refresh_timeout: float = 15.0,
        manual_refresh_cooldown: float = 5.0,
        health_refresh_interval: float = 300.0,
        initial_sync_retries: int = 2,
        approval_lifetime: float = 60.0,
        inventory_max_age: float = 30.0,
        command_admission: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> None:
        if reconnect_min < 0 or reconnect_max < reconnect_min:
            raise ValueError("invalid reconnect bounds")
        if (
            initial_sync_timeout <= 0
            or stable_connection_time < 0
            or command_timeout <= 0
            or reconcile_timeout < 0
        ):
            raise ValueError("service timeouts are invalid")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if (
            refresh_timeout <= 0
            or manual_refresh_cooldown < 0
            or health_refresh_interval <= 0
            or initial_sync_retries < 0
            or approval_lifetime <= 0
            or inventory_max_age <= 0
        ):
            raise ValueError("refresh and approval bounds are invalid")
        self._request = request
        self._on_snapshot = on_snapshot
        self._generation = generation
        self._transport_factory = transport_factory
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._initial_sync_timeout = initial_sync_timeout
        self._stable_connection_time = stable_connection_time
        self._command_timeout = command_timeout
        self._reconcile_timeout = reconcile_timeout
        self._jitter = jitter or _default_jitter
        self._poll_interval = poll_interval
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._refresh_timeout = refresh_timeout
        self._manual_refresh_cooldown = manual_refresh_cooldown
        self._health_refresh_interval = health_refresh_interval
        self._initial_sync_retries = initial_sync_retries
        self._approval_lifetime = approval_lifetime
        self._inventory_max_age = inventory_max_age
        self._command_admission = command_admission or contextlib.nullcontext
        self._store = AlarmStateStore(
            generation=generation, clock=wall_clock, monotonic_clock=monotonic_clock
        )
        self._seeds = _index_seeds(locations)
        self._runtimes: dict[str, _LocationRuntime] = {}
        self._mode_locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, _PendingCommand] = {}
        self._refresh_tasks: dict[str, asyncio.Task[AlarmRefreshResult]] = {}
        self._refresh_started: dict[str, float] = {}
        self._inventory_waiters: dict[tuple[str, str, int], _InventoryWaiter] = {}
        self._approvals: dict[str, _ModeApproval] = {}
        self._active_modes: set[str] = set()
        self._operations: dict[str, AlarmOperationSnapshot] = {}
        self._operation_counter = 0
        self._uncertain_inventory: dict[str, tuple[str, int, int]] = {}
        self._last_emitted = empty_alarm_snapshot(generation)
        self._change_event = asyncio.Event()
        self._stop_event: Any | None = None
        self._reconcile_revision = 0
        self._running = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        for seed in self._seeds.values():
            self._store.register_location(
                seed.location_id,
                name=seed.name,
                expected_assets=seed.asset_ids,
                write_authorization=seed.write_authorization,
            )

    @property
    def snapshot(self) -> AlarmAccountSnapshot:
        """Return the latest published snapshot."""

        return self._last_emitted

    async def run(self, stop_event: Any) -> None:
        """Supervise all discovered locations until stopped."""

        if self._closed:
            return
        if self._running:
            raise RuntimeError("Ring Alarm service is already running")
        self._running = True
        self._stop_event = stop_event
        self._publish()
        try:
            for seed in self._seeds.values():
                self._start_runtime(seed)
            while not self._closed and not _event_is_set(stop_event):
                await asyncio.sleep(self._poll_interval)
        finally:
            await self.close()

    async def close(self) -> None:
        """Idempotently stop supervisors and close every owned WSS session."""

        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(self._close_impl())
            self._close_task = task
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(_CommandConnectionLost("service-closed"))
        self._pending.clear()

        tasks: list[asyncio.Task[Any]] = []
        self._approvals.clear()
        for refresh in self._refresh_tasks.values():
            refresh.cancel()
            tasks.append(refresh)
        self._refresh_tasks.clear()
        transports: list[RingAlarmTransport] = []
        for runtime in self._runtimes.values():
            if runtime.maintenance_task is not None:
                runtime.maintenance_task.cancel()
                tasks.append(runtime.maintenance_task)
            if runtime.sync_task is not None:
                runtime.sync_task.cancel()
                tasks.append(runtime.sync_task)
            if runtime.stable_task is not None:
                runtime.stable_task.cancel()
                tasks.append(runtime.stable_task)
            if runtime.task is not None:
                runtime.task.cancel()
                tasks.append(runtime.task)
            transports.append(runtime.transport)
        if transports:
            await asyncio.gather(
                *(transport.close() for transport in transports),
                return_exceptions=True,
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runtimes.clear()
        self._running = False
        self._publish(status=AlarmServiceStatus.STOPPED)

    async def reconcile(self, locations: Iterable[AlarmLocationSeed]) -> None:
        """Reconcile REST discovery without disrupting healthy sockets."""

        if self._closed:
            return
        updated = _index_seeds(locations)
        removed = set(self._seeds) - set(updated)
        for location_id in removed:
            runtime = self._runtimes.pop(location_id, None)
            if runtime is not None:
                await self._stop_runtime(runtime)
            remover = getattr(self._store, "remove_location", None)
            if callable(remover):
                remover(location_id)
            self._fail_pending(location_id)
            self._mode_locks.pop(location_id, None)
            self._approvals.pop(location_id, None)
            self._operations.pop(location_id, None)
            self._uncertain_inventory.pop(location_id, None)
            self._refresh_started.pop(location_id, None)

        self._seeds = updated
        for seed in updated.values():
            runtime = self._runtimes.get(seed.location_id)
            connected_assets = (
                tuple((asset.asset_id, asset.kind) for asset in runtime.transport.assets)
                if runtime is not None and runtime.transport.assets
                else seed.asset_ids
            )
            self._store.register_location(
                seed.location_id,
                name=seed.name,
                expected_assets=connected_assets,
                write_authorization=seed.write_authorization,
            )
            if runtime is None and self._running:
                self._start_runtime(seed)
            elif runtime is not None:
                runtime.seed = seed
        self._reconcile_revision += 1
        self._publish()

    async def request_refresh(
        self,
        locations: Iterable[AlarmLocationSeed] | None = None,
    ) -> None:
        """Wake offline retries, optionally with refreshed discovery data."""

        if locations is not None:
            await self.reconcile(locations)
            return
        self._reconcile_revision += 1

    async def refresh(
        self, location_id: str | None = None, *, force: bool = False
    ) -> AlarmRefreshResult:
        """Join one bounded inventory reread per location, without replaying writes."""
        ids = (location_id,) if location_id is not None else tuple(self._seeds)
        if self._closed or not ids or any(item not in self._seeds for item in ids):
            return AlarmRefreshResult(AlarmRefreshStatus.UNAVAILABLE, code="location-unavailable")
        tasks = []
        results = []
        for item in ids:
            task = self._refresh_tasks.get(item)
            if task is None or task.done():
                now = self._monotonic_clock()
                last = self._refresh_started.get(item)
                if not force and last is not None and now - last < self._manual_refresh_cooldown:
                    results.append(
                        AlarmRefreshResult(AlarmRefreshStatus.COOLDOWN, (item,), "cooldown")
                    )
                    continue
                self._refresh_started[item] = now
                task = asyncio.create_task(self._refresh_location(item), name="ring-alarm-refresh")
                self._refresh_tasks[item] = task
            tasks.append(asyncio.shield(task))
        if tasks:
            results.extend(await asyncio.gather(*tasks))
        statuses = {result.status for result in results}
        if len(statuses) == 1:
            status = results[0].status
        elif AlarmRefreshStatus.REFRESHED in statuses or AlarmRefreshStatus.INCOMPLETE in statuses:
            status = AlarmRefreshStatus.INCOMPLETE
        elif AlarmRefreshStatus.TIMED_OUT in statuses:
            status = AlarmRefreshStatus.TIMED_OUT
        else:
            status = AlarmRefreshStatus.UNAVAILABLE
        code = results[0].code if len(results) == 1 else None
        return AlarmRefreshResult(status, ids, code)

    async def _refresh_location(self, location_id: str) -> AlarmRefreshResult:
        runtime = self._runtimes.get(location_id)
        if runtime is None:
            return AlarmRefreshResult(
                AlarmRefreshStatus.UNAVAILABLE, (location_id,), "connection-unavailable"
            )
        self._reconcile_revision += 1
        waiters: list[tuple[tuple[str, str, int], _InventoryWaiter]] = []
        try:
            async with asyncio.timeout(self._refresh_timeout):
                while not runtime.transport.connected:
                    if self._closed or self._runtimes.get(location_id) is not runtime:
                        return AlarmRefreshResult(
                            AlarmRefreshStatus.UNAVAILABLE, (location_id,), "connection-unavailable"
                        )
                    await asyncio.sleep(self._poll_interval)
                epoch = runtime.epoch
                assets = runtime.transport.assets
                if not assets:
                    return AlarmRefreshResult(
                        AlarmRefreshStatus.INCOMPLETE, (location_id,), "inventory-incomplete"
                    )
                for asset in assets:
                    key = (location_id, asset.asset_id, epoch)
                    waiter = _InventoryWaiter(asyncio.get_running_loop().create_future())
                    self._inventory_waiters[key] = waiter
                    waiters.append((key, waiter))

                    @contextlib.contextmanager
                    def admit_inventory(asset_id=asset.asset_id, waiter=waiter):
                        if (
                            self._closed
                            or self._runtimes.get(location_id) is not runtime
                            or runtime.epoch != epoch
                            or not runtime.transport.connected
                        ):
                            raise AlarmCommandSupersededError
                        current = self._store.snapshot.find_location(location_id)
                        current_asset = current.find_asset(asset_id) if current else None
                        if current_asset is None:
                            raise AlarmCommandSupersededError
                        waiter.baseline = current_asset.inventory_revision
                        waiter.armed = True
                        yield

                    await runtime.transport.request_inventory(
                        asset.asset_id, epoch=epoch, admission=admit_inventory
                    )
                await asyncio.gather(*(waiter.future for _, waiter in waiters))
                return AlarmRefreshResult(AlarmRefreshStatus.REFRESHED, (location_id,))
        except TimeoutError:
            location = self.snapshot.find_location(location_id)
            status = (
                AlarmRefreshStatus.INCOMPLETE
                if location is not None
                and any(
                    waiter.future.done()
                    and not waiter.future.cancelled()
                    and waiter.future.exception() is None
                    for _, waiter in waiters
                )
                else AlarmRefreshStatus.TIMED_OUT
            )
            return AlarmRefreshResult(status, (location_id,), "refresh-timeout")
        except (AlarmTransportError, AlarmCommandSupersededError):
            return AlarmRefreshResult(
                AlarmRefreshStatus.UNAVAILABLE, (location_id,), "connection-unavailable"
            )
        finally:
            for key, waiter in waiters:
                if self._inventory_waiters.get(key) is waiter:
                    self._inventory_waiters.pop(key, None)
                if not waiter.future.done():
                    waiter.future.cancel()

    def _complete_inventory_waiter(self, runtime: _LocationRuntime, message: ClapMessage) -> None:
        if not isinstance(message, DeviceInfoDocList):
            return
        key = (runtime.seed.location_id, message.asset_id, runtime.epoch)
        waiter = self._inventory_waiters.get(key)
        location = self._store.snapshot.find_location(runtime.seed.location_id)
        asset = location.find_asset(message.asset_id) if location is not None else None
        if (
            waiter is not None
            and waiter.armed
            and not waiter.future.done()
            and asset is not None
            and asset.epoch == runtime.epoch
            and asset.inventory_revision > waiter.baseline
            and not asset.stale
            and asset.inventory is AlarmInventoryStatus.COMPLETE
        ):
            waiter.future.set_result(None)

    def _fresh_inventory(self, location: AlarmLocationSnapshot) -> bool:
        panel = location.security_panel
        asset = location.find_asset(panel.asset_id) if panel is not None else None
        return bool(
            asset is not None
            and not asset.stale
            and asset.inventory_monotonic is not None
            and 0 <= self._monotonic_clock() - asset.inventory_monotonic <= self._inventory_max_age
        )

    async def prepare_mode(self, location_id: str) -> AlarmModePreparation:
        """Read-only preflight; issue one bounded approval token for a dialog."""
        if location_id in self._active_modes:
            return AlarmModePreparation(code="busy")
        location = self.snapshot.find_location(location_id)
        if location is None or self._closed:
            return AlarmModePreparation(code="location-unavailable")
        if not self._fresh_inventory(location) or location_id in self._uncertain_inventory:
            refreshed = await self.refresh(location_id, force=True)
            location = self.snapshot.find_location(location_id)
            # A missing optional bridge must not override a newly authoritative
            # panel response. Mode admission still checks the unique panel asset.
            if refreshed.status is not AlarmRefreshStatus.REFRESHED and (
                location is None or not self._fresh_inventory(location)
            ):
                return AlarmModePreparation(code=refreshed.code or "inventory-incomplete")
        if location_id in self._active_modes:
            return AlarmModePreparation(code="busy")
        if location is None or not location.can_set_mode or not self._fresh_inventory(location):
            return AlarmModePreparation(
                location=location,
                code=location.command_unavailable_reason if location else "location-unavailable",
            )
        panel = location.security_panel
        assert panel is not None
        uncertain_error = self._uncertain_error(location)
        if uncertain_error is not None:
            return AlarmModePreparation(location=location, code=uncertain_error)
        token = secrets.token_urlsafe(24)
        self._approvals[location_id] = _ModeApproval(
            token,
            location.epoch,
            location.revision,
            panel.asset_id,
            panel.zid,
            self._monotonic_clock(),
        )
        return AlarmModePreparation(location, token)

    async def request_mode(
        self,
        location_id: str,
        mode: AlarmMode,
        *,
        bypass_zids: Iterable[str] = (),
        expected_revision: int | None = None,
        approval_id: str | None = None,
    ) -> AlarmCommandResult:
        """Send one revision-checked command and confirm it from panel state."""

        if not isinstance(mode, AlarmMode) or mode is AlarmMode.UNKNOWN:
            return self._command_result(
                AlarmCommandStatus.UNAVAILABLE,
                location_id,
                AlarmMode.UNKNOWN,
                code="invalid-mode",
            )
        if self._closed or location_id not in self._seeds:
            return self._command_result(
                AlarmCommandStatus.UNAVAILABLE, location_id, mode, code="location-unavailable"
            )
        if location_id in self._active_modes:
            return self._command_result(AlarmCommandStatus.BUSY, location_id, mode, code="busy")
        # No await between admission and ownership: callers cannot accumulate
        # unbounded lock waiters while a mode's outcome is pending.
        self._active_modes.add(location_id)
        self._operation_counter += 1
        operation_id = self._operation_counter
        self._operations[location_id] = AlarmOperationSnapshot(
            operation_id, mode, AlarmOperationStatus.PREPARING
        )
        self._publish()
        try:
            result = await self._request_mode_locked(
                location_id,
                mode,
                bypass_zids,
                expected_revision,
                approval_id,
            )
            status = (
                AlarmOperationStatus.REPORTED_TARGET
                if result.confirmed
                else AlarmOperationStatus.OUTCOME_UNKNOWN
                if result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN
                else AlarmOperationStatus.REJECTED
            )
            if location_id in self._runtimes:
                self._operations[location_id] = AlarmOperationSnapshot(
                    operation_id, mode, status, result.code
                )
            if result.status is AlarmCommandStatus.TIMEOUT_UNKNOWN:
                self._remember_uncertain(location_id)
            if result.status is not AlarmCommandStatus.NEEDS_BYPASS:
                self._approvals.pop(location_id, None)
            return result
        except asyncio.CancelledError:
            if location_id in self._runtimes:
                self._operations[location_id] = AlarmOperationSnapshot(
                    operation_id, mode, AlarmOperationStatus.OUTCOME_UNKNOWN, "command-cancelled"
                )
                self._remember_uncertain(location_id)
            self._approvals.pop(location_id, None)
            raise
        except Exception:
            if location_id in self._runtimes:
                self._operations[location_id] = AlarmOperationSnapshot(
                    operation_id, mode, AlarmOperationStatus.OUTCOME_UNKNOWN, "command-failed"
                )
                self._remember_uncertain(location_id)
            self._approvals.pop(location_id, None)
            raise
        finally:
            self._active_modes.discard(location_id)
            self._publish()

    async def set_mode(
        self,
        location_id: str,
        target: AlarmMode,
        expected_revision: int,
        bypass_ids: Iterable[str] = (),
    ) -> AlarmCommandResult:
        """Provider-style alias for :meth:`request_mode`."""

        return await self.request_mode(
            location_id,
            target,
            bypass_zids=bypass_ids,
            expected_revision=expected_revision,
        )

    def _approval_error(
        self, location: AlarmLocationSnapshot, target: AlarmMode, approval_id: str | None
    ) -> str | None:
        if not self._fresh_inventory(location):
            return "state-too-old"
        uncertain_error = self._uncertain_error(location)
        if uncertain_error is not None:
            return uncertain_error
        panel = location.security_panel
        assert panel is not None
        if approval_id is None:
            # Compatibility for immediate provider callers. GTK uses a prepared
            # token so time spent in confirmation/bypass dialogs is bounded.
            return None
        approval = self._approvals.get(location.location_id)
        if (
            approval is None
            or approval.token != approval_id
            or not 0 <= self._monotonic_clock() - approval.issued_at <= self._approval_lifetime
        ):
            return "approval-expired"
        if (
            approval.epoch != location.epoch
            or approval.revision != location.revision
            or approval.panel_asset_id != panel.asset_id
            or approval.panel_zid != panel.zid
        ):
            return "approval-stale"
        if approval.target is not None and approval.target is not target:
            return "approval-stale"
        approval.target = target
        return None

    def _remember_uncertain(self, location_id: str) -> None:
        current = self._store.snapshot.find_location(location_id)
        panel = current.security_panel if current else None
        asset = current.find_asset(panel.asset_id) if panel else None
        if asset is not None:
            self._uncertain_inventory[location_id] = (
                asset.asset_id,
                asset.epoch,
                asset.inventory_revision,
            )

    def _uncertain_error(self, location: AlarmLocationSnapshot) -> str | None:
        panel = location.security_panel
        asset = location.find_asset(panel.asset_id) if panel else None
        if asset is None:
            return "panel-unavailable"
        uncertain = self._uncertain_inventory.get(location.location_id)
        if uncertain is not None:
            old_asset, old_epoch, old_revision = uncertain
            if (
                asset.asset_id == old_asset
                and asset.epoch == old_epoch
                and asset.inventory_revision <= old_revision
            ):
                return "fresh-inventory-required"
            self._uncertain_inventory.pop(location.location_id, None)
        return None

    def _start_runtime(self, seed: AlarmLocationSeed) -> None:
        if self._closed or seed.location_id in self._runtimes:
            return
        transport = self._transport_factory(self._request, seed)
        runtime = _LocationRuntime(
            seed=seed,
            transport=transport,
            backoff=self._reconnect_min,
        )
        self._runtimes[seed.location_id] = runtime
        runtime.task = asyncio.create_task(
            self._supervise_location(runtime),
            name="ring-alarm-location",
        )
        runtime.maintenance_task = asyncio.create_task(
            self._maintain_location(runtime), name="ring-alarm-health-refresh"
        )

    async def _maintain_location(self, runtime: _LocationRuntime) -> None:
        while not self._should_stop() and self._runtimes.get(runtime.seed.location_id) is runtime:
            await asyncio.sleep(self._health_refresh_interval)
            if not self._should_stop():
                await self.refresh(runtime.seed.location_id, force=True)

    async def _stop_runtime(self, runtime: _LocationRuntime) -> None:
        if runtime.maintenance_task is not None:
            runtime.maintenance_task.cancel()
        refresh = self._refresh_tasks.pop(runtime.seed.location_id, None)
        if refresh is not None:
            refresh.cancel()
        if runtime.sync_task is not None:
            runtime.sync_task.cancel()
        if runtime.stable_task is not None:
            runtime.stable_task.cancel()
        if runtime.task is not None:
            runtime.task.cancel()
        await runtime.transport.close()
        for task in (
            runtime.sync_task,
            runtime.stable_task,
            runtime.task,
            runtime.maintenance_task,
            refresh,
        ):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _supervise_location(self, runtime: _LocationRuntime) -> None:
        attempts = 0
        while not self._should_stop():
            runtime.phase = "connecting" if attempts == 0 else "reconnecting"
            runtime.epoch = self._store.begin_epoch(runtime.seed.location_id)
            self._publish()
            observed_reconcile = self._reconcile_revision
            try:
                await runtime.transport.connect_once(
                    epoch=runtime.epoch,
                    stop_event=self._stop_event,
                    on_open=lambda assets: self._location_opened(runtime, assets),
                    on_message=lambda message: self._location_message(runtime, message),
                )
                if self._should_stop():
                    return
                error = AlarmTransportError("websocket-closed")
            except asyncio.CancelledError:
                raise
            except AlarmTransportError as exc:
                error = exc
            except Exception:
                error = AlarmTransportError("transport-failed")

            if self._should_stop():
                return
            if runtime.stable_task is not None:
                runtime.stable_task.cancel()
                runtime.stable_task = None
            runtime.connected_at = None
            attempts += 1
            runtime.phase = (
                "authentication-required" if error.code == "authentication-required" else "offline"
            )
            self._store.mark_location_stale(runtime.seed.location_id, runtime.epoch)
            self._fail_pending(runtime.seed.location_id)
            self._publish()
            delay = (
                error.retry_after
                if error.retry_after is not None
                else max(0.0, float(self._jitter(runtime.backoff)))
            )
            runtime.backoff = min(
                max(self._reconnect_min, runtime.backoff * 2),
                self._reconnect_max,
            )
            await self._wait_for_retry(delay, observed_reconcile)

    async def _location_opened(
        self,
        runtime: _LocationRuntime,
        assets: tuple[AlarmTicketAsset, ...],
    ) -> None:
        if self._should_stop():
            return
        expected_assets = tuple((asset.asset_id, asset.kind) for asset in assets)
        self._store.register_location(
            runtime.seed.location_id,
            name=runtime.seed.name,
            expected_assets=expected_assets,
            write_authorization=runtime.seed.write_authorization,
        )
        sessions = tuple(
            AssetSessionInfo(
                asset_id=asset.asset_id,
                connection=(
                    AlarmConnectionStatus.ONLINE
                    if asset.status == "online"
                    else AlarmConnectionStatus.OFFLINE
                    if asset.status == "offline"
                    else AlarmConnectionStatus.UNKNOWN
                ),
                kind=asset.kind,
            )
            for asset in assets
        )
        self._store.apply_message(
            runtime.seed.location_id,
            SessionInfo(sessions=sessions),
            runtime.epoch,
        )
        for asset in assets:
            self._store.record_ticket_power(
                runtime.seed.location_id, asset.asset_id, runtime.epoch, asset.on_battery
            )
        runtime.phase = "syncing"
        runtime.connected_at = asyncio.get_running_loop().time()
        if runtime.sync_task is not None:
            runtime.sync_task.cancel()
        runtime.sync_task = asyncio.create_task(
            self._mark_sync_degraded(runtime, runtime.epoch),
            name="ring-alarm-initial-sync",
        )
        if runtime.stable_task is not None:
            runtime.stable_task.cancel()
        runtime.stable_task = asyncio.create_task(
            self._mark_connection_stable(runtime, runtime.epoch),
            name="ring-alarm-stability",
        )
        self._state_changed()
        self._publish()

    async def _location_message(
        self,
        runtime: _LocationRuntime,
        message: ClapMessage,
    ) -> None:
        if self._should_stop():
            return
        message = self._supported_asset_message(runtime, message)
        if message is None:
            return
        previous = self._store.snapshot.find_location(runtime.seed.location_id)
        try:
            self._store.apply_message(runtime.seed.location_id, message, runtime.epoch)
        except AlarmStateBoundsError:
            raise AlarmTransportError("retained-state-limit") from None
        self._complete_inventory_waiter(runtime, message)
        if isinstance(message, SessionInfo):
            for session in message.sessions:
                previous_asset = (
                    previous.find_asset(session.asset_id) if previous is not None else None
                )
                if session.connection is AlarmConnectionStatus.ONLINE and (
                    previous_asset is None
                    or previous_asset.connection is not AlarmConnectionStatus.ONLINE
                    or previous_asset.stale
                ):
                    self._store.mark_asset_stale(
                        runtime.seed.location_id,
                        session.asset_id,
                        runtime.epoch,
                        connection=AlarmConnectionStatus.ONLINE,
                    )
                    await runtime.transport.request_inventory(
                        session.asset_id,
                        epoch=runtime.epoch,
                    )
        location = self._store.snapshot.find_location(runtime.seed.location_id)
        if location is not None and location.inventory is AlarmInventoryStatus.COMPLETE:
            runtime.phase = (
                "online" if location.connection is AlarmConnectionStatus.ONLINE else "degraded"
            )
            if runtime.sync_task is not None:
                runtime.sync_task.cancel()
                runtime.sync_task = None
        elif runtime.phase in {"online", "degraded"}:
            # An established socket can remain open while the hub reports an
            # offline/stale asset. Reflect that loss of inventory immediately.
            runtime.phase = "degraded"
        if (
            location is not None
            and self._has_fresh_panel_asset(location)
            and runtime.connected_at is not None
            and asyncio.get_running_loop().time() - runtime.connected_at
            >= self._stable_connection_time
        ):
            runtime.backoff = self._reconnect_min
        self._confirm_pending(runtime, message)
        self._state_changed()
        self._publish()

    @staticmethod
    def _supported_asset_message(
        runtime: _LocationRuntime,
        message: ClapMessage,
    ) -> ClapMessage | None:
        allowed_asset_ids = {asset.asset_id for asset in runtime.transport.assets}
        if isinstance(message, DeviceInfoDocList | DeviceInfoDocUpdate):
            return message if message.asset_id in allowed_asset_ids else None
        if isinstance(message, SessionInfo):
            sessions = tuple(
                session for session in message.sessions if session.asset_id in allowed_asset_ids
            )
            if not sessions:
                return None
            if sessions != message.sessions:
                return SessionInfo(sessions=sessions, sequence=message.sequence)
        return message

    async def _mark_sync_degraded(self, runtime: _LocationRuntime, epoch: int) -> None:
        try:
            await asyncio.sleep(self._initial_sync_timeout)
        except asyncio.CancelledError:
            return
        if runtime.epoch == epoch and runtime.phase == "syncing" and not self._should_stop():
            runtime.phase = "degraded"
            self._publish()
            for _attempt in range(self._initial_sync_retries):
                if runtime.epoch != epoch or self._should_stop():
                    return
                result = await self.refresh(runtime.seed.location_id, force=True)
                if result.status is AlarmRefreshStatus.REFRESHED:
                    return
                await asyncio.sleep(self._initial_sync_timeout)

    async def _mark_connection_stable(
        self,
        runtime: _LocationRuntime,
        epoch: int,
    ) -> None:
        try:
            await asyncio.sleep(self._stable_connection_time)
        except asyncio.CancelledError:
            return
        location = self._store.snapshot.find_location(runtime.seed.location_id)
        if (
            runtime.epoch == epoch
            and runtime.transport.connected
            and location is not None
            and self._has_fresh_panel_asset(location)
        ):
            runtime.backoff = self._reconnect_min

    @staticmethod
    def _has_fresh_panel_asset(location: AlarmLocationSnapshot) -> bool:
        return any(
            not asset.stale
            and asset.connection is AlarmConnectionStatus.ONLINE
            and asset.inventory is AlarmInventoryStatus.COMPLETE
            and any(
                device.kind is AlarmDeviceKind.SECURITY_PANEL and not device.stale
                for device in asset.devices
            )
            for asset in location.assets
        )

    async def _wait_for_retry(self, delay: float, reconcile_revision: int) -> None:
        remaining = min(max(delay, 0.0), 300.0)
        while (
            remaining > 0
            and not self._should_stop()
            and reconcile_revision == self._reconcile_revision
        ):
            interval = min(self._poll_interval, remaining)
            await asyncio.sleep(interval)
            remaining -= interval

    async def _request_mode_locked(
        self,
        location_id: str,
        target: AlarmMode,
        bypass_ids: Iterable[str],
        expected_revision: int | None,
        approval_id: str | None = None,
    ) -> AlarmCommandResult:
        location = self.snapshot.find_location(location_id)
        runtime = self._runtimes.get(location_id)
        if location is None or runtime is None or self._closed:
            return self._command_result(
                AlarmCommandStatus.UNAVAILABLE,
                location_id,
                target,
                code="location-unavailable",
            )
        if expected_revision is None or expected_revision != location.revision:
            return self._command_result(
                AlarmCommandStatus.STALE_REVISION,
                location_id,
                target,
                confirmed_mode=location.mode,
                code="revision-mismatch",
            )
        panel = location.security_panel
        if not location.can_set_mode or panel is None:
            return self._command_result(
                (
                    AlarmCommandStatus.PERMISSION_DENIED
                    if location.write_authorization is AlarmWriteAuthorization.DENIED
                    else AlarmCommandStatus.UNAVAILABLE
                ),
                location_id,
                target,
                confirmed_mode=location.mode,
                code=location.command_unavailable_reason or "panel-unavailable",
            )
        approval_error = self._approval_error(location, target, approval_id)
        if approval_error is not None:
            return self._command_result(
                AlarmCommandStatus.UNAVAILABLE, location_id, target, code=approval_error
            )
        if target is not AlarmMode.DISARMED and location.phase in {
            AlarmPhase.ENTRY_DELAY,
            AlarmPhase.EXIT_DELAY,
        }:
            return self._command_result(
                AlarmCommandStatus.UNAVAILABLE,
                location_id,
                target,
                confirmed_mode=location.mode,
                code="transition-in-progress",
            )
        if location.mode is target:
            return self._command_result(
                AlarmCommandStatus.CONFIRMED,
                location_id,
                target,
                confirmed_mode=target,
                code="already-set",
            )

        required_bypass = (
            self._faulted_bypass_ids(location) if target is not AlarmMode.DISARMED else ()
        )
        if required_bypass is None:
            return self._command_result(
                AlarmCommandStatus.UNAVAILABLE,
                location_id,
                target,
                confirmed_mode=location.mode,
                code="faulted-device-unresolved",
            )
        requested_bypass = _validated_identifiers(bypass_ids)
        if requested_bypass is None:
            return self._command_result(
                AlarmCommandStatus.NEEDS_BYPASS,
                location_id,
                target,
                confirmed_mode=location.mode,
                bypass_ids=required_bypass,
                code="invalid-bypass-set",
            )
        if requested_bypass != required_bypass:
            return self._command_result(
                AlarmCommandStatus.NEEDS_BYPASS,
                location_id,
                target,
                confirmed_mode=location.mode,
                bypass_ids=required_bypass,
                code="bypass-confirmation-required",
            )

        loop = asyncio.get_running_loop()
        pending = _PendingCommand(
            target=target,
            panel_asset_id=panel.asset_id,
            panel_zid=panel.zid,
            epoch=runtime.epoch,
            baseline_revision=location.revision,
            future=loop.create_future(),
        )
        self._pending[location_id] = pending

        @contextlib.contextmanager
        def admit_send():
            with self._command_admission():
                # A session update can be awaiting an inventory request before
                # its UI snapshot is published. Admission uses the live store.
                current = self._store.snapshot.find_location(location_id)
                approval_error = (
                    self._approval_error(current, target, approval_id)
                    if current
                    else "location-unavailable"
                )
                if approval_error is not None:
                    raise _CommandAdmissionRejected(approval_error)
                if (
                    self._closed
                    or self._runtimes.get(location_id) is not runtime
                    or current is None
                    or current.revision != expected_revision
                    or not self._decorate_location(current).can_set_mode
                ):
                    raise _CommandRevisionChanged
                pending.armed = True
                operation = self._operations.get(location_id)
                if operation is not None:
                    self._operations[location_id] = replace(
                        operation, status=AlarmOperationStatus.WAITING
                    )
                    self._publish()
                yield

        try:
            try:
                await runtime.transport.send_mode(
                    panel.asset_id,
                    panel.zid,
                    target,
                    epoch=runtime.epoch,
                    bypass_ids=requested_bypass,
                    admission=admit_send,
                )
            except _CommandAdmissionRejected as exc:
                return self._command_result(
                    AlarmCommandStatus.UNAVAILABLE, location_id, target, code=exc.code
                )
            except _CommandRevisionChanged:
                current = self.snapshot.find_location(location_id)
                return self._command_result(
                    AlarmCommandStatus.STALE_REVISION,
                    location_id,
                    target,
                    confirmed_mode=current.mode if current is not None else AlarmMode.UNKNOWN,
                    code="revision-mismatch",
                )
            except AlarmCommandSupersededError:
                return self._command_result(
                    AlarmCommandStatus.SUPERSEDED,
                    location_id,
                    target,
                    confirmed_mode=location.mode,
                    code="session-superseded",
                )
            except AlarmTransportClosedError:
                return self._command_result(
                    AlarmCommandStatus.UNAVAILABLE,
                    location_id,
                    target,
                    confirmed_mode=location.mode,
                    code="connection-unavailable",
                )
            except AlarmTransportError as exc:
                return self._command_result(
                    AlarmCommandStatus.TIMEOUT_UNKNOWN,
                    location_id,
                    target,
                    confirmed_mode=location.mode,
                    code=exc.code,
                )

            try:
                confirmed = await asyncio.wait_for(
                    asyncio.shield(pending.future),
                    timeout=self._command_timeout,
                )
                return self._command_result(
                    AlarmCommandStatus.CONFIRMED,
                    location_id,
                    target,
                    confirmed_mode=confirmed,
                )
            except (TimeoutError, _CommandConnectionLost):
                return await self._reconcile_ambiguous_command(
                    runtime,
                    location_id,
                    panel.asset_id,
                    panel.zid,
                    target,
                    panel.revision,
                )
        finally:
            if self._pending.get(location_id) is pending:
                self._pending.pop(location_id, None)
            if not pending.future.done():
                pending.future.cancel()
            elif not pending.future.cancelled():
                pending.future.exception()

    async def _reconcile_ambiguous_command(
        self,
        runtime: _LocationRuntime,
        location_id: str,
        panel_asset_id: str,
        panel_zid: str,
        target: AlarmMode,
        baseline_panel_revision: int,
    ) -> AlarmCommandResult:
        if runtime.transport.connected:
            with contextlib.suppress(AlarmTransportError):
                await runtime.transport.request_inventory(
                    panel_asset_id,
                    epoch=runtime.epoch,
                )
        if self._reconcile_timeout:
            await self._wait_for_panel_revision(
                location_id,
                panel_asset_id,
                panel_zid,
                baseline_panel_revision,
                self._reconcile_timeout,
            )
        current = self.snapshot.find_location(location_id)
        current_mode = current.mode if current is not None else AlarmMode.UNKNOWN
        if current_mode is target:
            return self._command_result(
                AlarmCommandStatus.CONFIRMED,
                location_id,
                target,
                confirmed_mode=target,
                code="confirmed-after-reconcile",
            )
        return self._command_result(
            AlarmCommandStatus.TIMEOUT_UNKNOWN,
            location_id,
            target,
            confirmed_mode=current_mode,
            code="confirmation-timeout",
        )

    async def _wait_for_panel_revision(
        self,
        location_id: str,
        panel_asset_id: str,
        panel_zid: str,
        baseline_revision: int,
        timeout: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._should_stop():
            event = self._change_event
            location = self.snapshot.find_location(location_id)
            panel_asset = location.find_asset(panel_asset_id) if location is not None else None
            panel = panel_asset.find_device(panel_zid) if panel_asset is not None else None
            if panel is None or panel.revision > baseline_revision:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=remaining)

    def _confirm_pending(
        self,
        runtime: _LocationRuntime,
        message: ClapMessage,
    ) -> None:
        pending = self._pending.get(runtime.seed.location_id)
        if (
            pending is None
            or pending.future.done()
            or not pending.armed
            or pending.epoch != runtime.epoch
            or not isinstance(message, DeviceInfoDocUpdate)
            or message.asset_id != pending.panel_asset_id
        ):
            return
        location = self._store.snapshot.find_location(runtime.seed.location_id)
        if (
            location is None
            or location.epoch != pending.epoch
            or location.revision <= pending.baseline_revision
        ):
            return
        expected_raw = {
            AlarmMode.DISARMED: "none",
            AlarmMode.HOME: "some",
            AlarmMode.AWAY: "all",
        }[pending.target]
        if any(
            document.zid == pending.panel_zid and document.mode == expected_raw
            for document in message.devices
        ):
            pending.future.set_result(pending.target)

    def _fail_pending(self, location_id: str) -> None:
        pending = self._pending.pop(location_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_exception(_CommandConnectionLost("connection-replaced"))

    def _faulted_bypass_ids(
        self,
        location: AlarmLocationSnapshot,
    ) -> tuple[str, ...] | None:
        panel = location.security_panel
        bypassable_kinds = {
            AlarmDeviceKind.CONTACT_SENSOR,
            AlarmDeviceKind.MOTION_SENSOR,
            AlarmDeviceKind.GLASS_BREAK_SENSOR,
            AlarmDeviceKind.TILT_SENSOR,
            AlarmDeviceKind.RETROFIT_ZONE,
        }
        panel_devices = tuple(
            device
            for device in location.devices
            if not device.stale and panel is not None and device.asset_id == panel.asset_id
        )
        bypassable = {
            device.zid: device
            for device in panel_devices
            if device.kind in bypassable_kinds and AlarmCapability.BYPASS in device.capabilities
        }
        if panel is not None:
            if not panel.faulted_devices_valid:
                return None
            if panel.faulted_device_ids is not None:
                if len(panel.faulted_device_ids) > 256 or any(
                    zid not in bypassable for zid in panel.faulted_device_ids
                ):
                    return None
                return tuple(sorted(panel.faulted_device_ids))
        if any(
            device.faulted is TriState.ACTIVE
            and AlarmCapability.BYPASS in device.capabilities
            and device.kind not in bypassable_kinds
            for device in panel_devices
        ):
            return None
        faulted = tuple(
            sorted(zid for zid, device in bypassable.items() if device.faulted is TriState.ACTIVE)
        )
        return faulted if len(faulted) <= 256 else None

    def _publish(self, *, status: AlarmServiceStatus | None = None) -> None:
        base = self._store.snapshot
        decorated_locations = tuple(
            self._decorate_location(location) for location in base.locations
        )
        candidate = replace(
            base,
            generation=self._generation,
            status=status or self._derive_status(),
            locations=decorated_locations,
            error_code=self._derive_error_code(),
        )
        comparable = replace(
            candidate,
            revision=self._last_emitted.revision,
            updated_at=self._last_emitted.updated_at,
        )
        if comparable == self._last_emitted:
            return
        candidate = replace(
            candidate,
            revision=max(base.revision, self._last_emitted.revision + 1),
            updated_at=self._wall_clock(),
        )
        self._last_emitted = candidate
        self._state_changed()
        with contextlib.suppress(Exception):
            self._on_snapshot(candidate)

    def _decorate_location(self, location: AlarmLocationSnapshot) -> AlarmLocationSnapshot:
        runtime = self._runtimes.get(location.location_id)
        panels = tuple(
            device for device in location.devices if device.kind is AlarmDeviceKind.SECURITY_PANEL
        )
        panel = panels[0] if len(panels) == 1 else None
        reason: str | None = None
        if self._closed:
            reason = "service-stopped"
        elif runtime is None or not runtime.transport.connected:
            reason = "connection-unavailable"
        elif len(panels) > 1:
            reason = "multiple-panels"
        elif panel is None:
            reason = "security-panel-unavailable"
        elif authorization_reason(location.write_authorization) is not None:
            reason = authorization_reason(location.write_authorization)
        elif panel.stale:
            reason = "state-stale"
        else:
            panel_asset = location.find_asset(panel.asset_id)
            if panel_asset is None:
                reason = "panel-unavailable"
            else:
                reason = asset_reason(panel_asset, check_stale=False) or panel_reason(
                    location.mode,
                    location.phase,
                    location.signal,
                    faults_valid=panel.faulted_devices_valid,
                    alarming_first=True,
                )
                if reason == "panel-offline":
                    reason = "panel-not-online"
                if reason is None and self._faulted_bypass_ids(location) is None:
                    reason = "faulted-device-unresolved"
        return replace(
            location,
            can_set_mode=reason is None,
            command_unavailable_reason=reason,
            operation=self._operations.get(location.location_id),
        )

    def _derive_status(self) -> AlarmServiceStatus:
        if self._closed:
            return AlarmServiceStatus.STOPPED
        if not self._seeds:
            return AlarmServiceStatus.NO_SYSTEM
        phases = {runtime.phase for runtime in self._runtimes.values()}
        if not phases:
            return AlarmServiceStatus.DISCOVERING
        if "authentication-required" in phases:
            return AlarmServiceStatus.AUTHENTICATION_REQUIRED
        if phases == {"online"}:
            return AlarmServiceStatus.ONLINE
        if phases <= {"connecting"}:
            return AlarmServiceStatus.CONNECTING
        if phases <= {"reconnecting", "offline"}:
            return (
                AlarmServiceStatus.RECONNECTING
                if "reconnecting" in phases
                else AlarmServiceStatus.OFFLINE
            )
        if phases <= {"syncing"}:
            return AlarmServiceStatus.SYNCING
        if phases & {"online", "degraded", "syncing"}:
            return AlarmServiceStatus.DEGRADED
        if "reconnecting" in phases:
            return AlarmServiceStatus.RECONNECTING
        if "connecting" in phases:
            return AlarmServiceStatus.CONNECTING
        return AlarmServiceStatus.OFFLINE

    def _derive_error_code(self) -> str | None:
        if any(runtime.phase == "authentication-required" for runtime in self._runtimes.values()):
            return "authentication-required"
        return None

    def _state_changed(self) -> None:
        event, self._change_event = self._change_event, asyncio.Event()
        event.set()

    def _should_stop(self) -> bool:
        return self._closed or _event_is_set(self._stop_event)

    @staticmethod
    def _command_result(
        status: AlarmCommandStatus,
        location_id: str,
        requested_mode: AlarmMode,
        *,
        confirmed_mode: AlarmMode = AlarmMode.UNKNOWN,
        bypass_ids: tuple[str, ...] = (),
        code: str | None = None,
    ) -> AlarmCommandResult:
        return AlarmCommandResult(
            status=status,
            location_id=location_id,
            requested_mode=requested_mode,
            confirmed_mode=confirmed_mode,
            bypass_ids=bypass_ids,
            code=code,
        )


def _index_seeds(locations: Iterable[AlarmLocationSeed]) -> dict[str, AlarmLocationSeed]:
    result: dict[str, AlarmLocationSeed] = {}
    for seed in locations:
        if not isinstance(seed, AlarmLocationSeed):
            raise TypeError("locations must contain AlarmLocationSeed values")
        if seed.location_id in result:
            raise ValueError("locations must contain unique location ids")
        result[seed.location_id] = seed
    return result


def _validated_identifiers(values: Iterable[str]) -> tuple[str, ...] | None:
    if isinstance(values, str | bytes):
        return None
    result: list[str] = []
    try:
        for value in values:
            if len(result) >= 256:
                return None
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 512
                or any(ord(character) < 32 for character in value)
                or value in result
            ):
                return None
            result.append(value)
    except Exception:
        return None
    return tuple(sorted(result))


def _event_is_set(event: Any | None) -> bool:
    checker = getattr(event, "is_set", None)
    return bool(checker()) if callable(checker) else False


def _default_jitter(delay: float) -> float:
    return random.uniform(delay * 0.8, delay * 1.2)
