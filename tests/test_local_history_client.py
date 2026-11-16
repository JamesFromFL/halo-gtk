"""History ownership uses synthetic sessions and temporary normalized records only."""

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from halo_gtk import local_alarm_history as storage
from halo_gtk import ring_client as rc
from halo_gtk.alarm.history import AlarmHistoryEvent, AlarmHistoryPage, AlarmHistoryStatus
from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmConnectionStatus,
    AlarmLocationSnapshot,
    AlarmServiceStatus,
)
from halo_gtk.local_alarm_history import LocalAlarmHistory, LocalHistoryError
from halo_gtk.secret_store import BoundRingToken


def page(location="home", status=AlarmHistoryStatus.READY):
    now = datetime.now(UTC)
    return AlarmHistoryPage(
        location,
        events=(AlarmHistoryEvent("row", location, "mode", "Disarmed", now, now),),
        status=status,
        retrieved_at=now,
    )


def snapshot(generation=1, locations=("home",), status=AlarmServiceStatus.ONLINE):
    return AlarmAccountSnapshot(
        generation=generation,
        status=status,
        locations=tuple(
            AlarmLocationSnapshot(identity, connection=AlarmConnectionStatus.ONLINE)
            for identity in locations
        ),
    )


@pytest.fixture
def active(monkeypatch, tmp_path):
    client = rc.RingClient()
    client._published_generation = 1
    client._alarm_snapshot = snapshot()
    client._history_access_receipts["home"] = rc.alarm_monotonic_time()
    client._local_alarm_history = LocalAlarmHistory("a" * 32, root=tmp_path / "saved")
    monkeypatch.setattr(rc, "_client", client)
    monkeypatch.setattr(rc, "_active_client_generation", 1)
    monkeypatch.setattr(rc, "_session_generation", 1)
    return client


def test_default_off_and_only_explicit_fresh_cloud_fetch_is_saved(active):
    assert active.local_history_preferences()["days"] == 0
    original = page()
    assert active._save_local_history_result(original, 1) == original
    assert not active.saved_alarm_history("home").events
    active.set_local_history_retention(7)
    active._save_local_history_result(replace(original, from_cache=True), 1)
    assert not active.saved_alarm_history("home").events
    active._save_local_history_result(original, 1)
    assert active.saved_alarm_history("home").events
    active.set_local_history_retention(0)
    assert active.saved_alarm_history("home").events
    active.clear_local_alarm_history()
    assert not active.saved_alarm_history("home").events


@pytest.mark.parametrize("action", ["preferences", "retention", "clear", "load", "save"])
def test_retired_client_cannot_read_write_or_clear(active, monkeypatch, action):
    active.set_local_history_retention(7)
    active._save_local_history_result(page(), 1)
    monkeypatch.setattr(rc, "_client", object())
    operations = {
        "preferences": active.local_history_preferences,
        "retention": lambda: active.set_local_history_retention(0),
        "clear": active.clear_local_alarm_history,
        "load": lambda: active.saved_alarm_history("home"),
        "save": lambda: active._save_local_history_result(page(), 1),
    }
    with pytest.raises(rc.SessionSupersededError):
        operations[action]()
    assert active._local_alarm_history.load("home").events


@pytest.mark.parametrize(
    "status",
    [
        AlarmServiceStatus.OFFLINE,
        AlarmServiceStatus.AUTHENTICATION_REQUIRED,
        AlarmServiceStatus.STOPPED,
        AlarmServiceStatus.DISCOVERING,
    ],
)
def test_unavailable_account_inventory_never_grants_saved_location_access(active, status):
    active.set_local_history_retention(7)
    active._save_local_history_result(page(), 1)
    active._alarm_snapshot = snapshot(status=status)
    with pytest.raises(LocalHistoryError, match="permission verification"):
        active.saved_alarm_history("home")


@pytest.mark.parametrize("change", ["removed", "generation", "offline-location"])
def test_location_and_generation_are_rechecked(active, change):
    state = snapshot()
    if change == "removed":
        state = replace(state, locations=())
    elif change == "generation":
        state = replace(state, generation=2)
    else:
        state = replace(
            state,
            locations=(replace(state.locations[0], connection=AlarmConnectionStatus.OFFLINE),),
        )
    active._alarm_snapshot = state
    with pytest.raises(LocalHistoryError):
        active.saved_alarm_history("home")


def test_denial_purges_and_prevents_cached_success_from_regranting(active):
    active.set_local_history_retention(7)
    active._save_local_history_result(page(), 1)
    active._save_local_history_result(page(status=AlarmHistoryStatus.PERMISSION_DENIED), 1)
    assert not active._local_alarm_history.load("home").events
    active._save_local_history_result(replace(page(), from_cache=True), 1)
    with pytest.raises(LocalHistoryError):
        active.saved_alarm_history("home")
    active._save_local_history_result(page(), 1)
    assert active.saved_alarm_history("home").events


def test_authentication_failure_blocks_all_saved_reads_until_fresh_success(active):
    active.set_local_history_retention(7)
    active._save_local_history_result(page(), 1)
    active._save_local_history_result(page(status=AlarmHistoryStatus.AUTHENTICATION_REQUIRED), 1)
    with pytest.raises(LocalHistoryError):
        active.saved_alarm_history("home")
    active._save_local_history_result(page(), 1)
    assert active.saved_alarm_history("home").events


def test_authoritative_inventory_purges_old_session_locations_but_offline_does_not(active):
    store = active._local_alarm_history
    store.set_retention(7)
    store.save(page("gone"))
    offline = snapshot(status=AlarmServiceStatus.OFFLINE)
    active._alarm_snapshot = offline
    active._reconcile_local_history(offline, 1)
    assert store.load("gone").events
    current = snapshot()
    active._alarm_snapshot = current
    active._reconcile_local_history(current, 1)
    assert not store.load("gone").events


def test_retired_or_superseded_snapshot_cannot_purge_new_state(active, monkeypatch):
    store = active._local_alarm_history
    store.set_retention(7)
    store.save(page())
    old = snapshot(locations=())
    active._reconcile_local_history(old, 1)
    assert store.load("home").events
    active._alarm_snapshot = old
    monkeypatch.setattr(rc, "_client", object())
    active._reconcile_local_history(old, 1)
    assert store.load("home").events


@pytest.mark.asyncio
async def test_delayed_cloud_result_cannot_cross_account_switch(active, monkeypatch):
    active.set_local_history_retention(7)
    release = asyncio.Event()

    async def fetch(*args, **kwargs):
        await release.wait()
        return page()

    active._alarm_history_service = SimpleNamespace(fetch_page=fetch)
    task = asyncio.create_task(active._async_alarm_history("home"))
    await asyncio.sleep(0)
    monkeypatch.setattr(rc, "_client", object())
    release.set()
    with pytest.raises(rc.SessionSupersededError):
        await task
    assert not active._local_alarm_history.load("home").events


def test_store_failure_does_not_discard_successful_cloud_response(active, monkeypatch):
    active.set_local_history_retention(7)
    monkeypatch.setattr(
        active._local_alarm_history, "save", lambda *_, **__: (_ for _ in ()).throw(OSError())
    )
    result = active._save_local_history_result(page(), 1)
    assert result.events and result.status is AlarmHistoryStatus.READY
    assert "could not be saved" in result.message


@pytest.fixture
def publication(monkeypatch, tmp_path):
    saved, bound, authenticated = [], [], []
    monkeypatch.setattr(rc, "_client", None)
    monkeypatch.setattr(rc, "_active_client_generation", 0)
    monkeypatch.setattr(rc, "_session_generation", 0)
    monkeypatch.setattr(
        rc, "LocalAlarmHistory", lambda profile: LocalAlarmHistory(profile, root=tmp_path / "saved")
    )
    monkeypatch.setattr(
        rc, "history_profile_for_account", lambda name: (bound.append(name), "b" * 32)[1]
    )
    monkeypatch.setattr(rc, "save_ring_token", lambda token, **kw: saved.append((dict(token), kw)))
    monkeypatch.setattr(rc, "_save_token", lambda token: saved.append((dict(token), {})))
    monkeypatch.setattr(rc.RingClient, "stop", lambda self: None)

    def authenticate(client, *args):
        authenticated.append(args)
        client._authenticated = True
        client._ring = SimpleNamespace(auth=SimpleNamespace(token_updater=None))
        client._pending_auth_token = {"access_token": "synthetic"}

    monkeypatch.setattr(rc.RingClient, "authenticate", authenticate)
    monkeypatch.setattr(rc.RingClient, "authenticate_from_token", authenticate)
    return saved, bound, authenticated


def test_fresh_login_and_refresh_preserve_same_authenticated_binding(publication):
    saved, bound, _ = publication
    client = rc.init_client("current@example.test", "synthetic")
    client._ring.auth.token_updater({"access_token": "refreshed"})
    assert bound == ["current@example.test"]
    assert [row[1] for row in saved] == [{"history_profile": "b" * 32}] * 2
    assert client._history_username is None


@pytest.mark.parametrize("profile", [None, "c" * 32])
def test_cached_restore_never_uses_display_email_and_sdk_gets_token_only(
    publication, monkeypatch, profile
):
    saved, bound, authenticated = publication
    token = {"access_token": "cached"}
    monkeypatch.setattr(rc, "_load_token", lambda: BoundRingToken(token, profile))
    monkeypatch.setattr(
        rc, "get_cached_account_email", lambda: pytest.fail("display email is not authority")
    )
    client = rc.init_client_from_cache()
    assert not bound and authenticated == [(token,)]
    assert type(authenticated[0][0]) is dict
    if profile:
        assert saved[0][1] == {"history_profile": profile}
        assert client.local_history_preferences()["available"]
    else:
        assert not saved[0][1]
        assert not client.local_history_preferences()["available"]
        assert "Sign out and sign in" in client.local_history_preferences()["reason"]


def test_superseded_candidate_cannot_create_profile_or_save_credentials(publication):
    saved, bound, _ = publication
    client = rc.RingClient()
    client._prepare_history_identity(username="old@example.test")
    assert rc._publish_session_candidate(client, 99) == (False, None)
    assert not saved and not bound and client._local_alarm_history is None


def test_failed_login_cannot_create_profile(publication, monkeypatch):
    saved, bound, _ = publication
    monkeypatch.setattr(
        rc.RingClient, "authenticate", lambda *_: (_ for _ in ()).throw(ValueError("login failed"))
    )
    with pytest.raises(ValueError):
        rc.init_client("failed@example.test", "synthetic")
    assert not saved and not bound


def test_optional_profile_failure_preserves_live_login_and_plain_token(publication, monkeypatch):
    saved, _, _ = publication
    monkeypatch.setattr(
        rc, "history_profile_for_account", lambda _: (_ for _ in ()).throw(OSError())
    )
    client = rc.init_client("current@example.test", "synthetic")
    assert client.is_authenticated and saved[0][1] == {}
    assert not client.local_history_preferences()["available"]


def test_only_fresh_history_permission_extends_bounded_access_receipt(active, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(rc, "alarm_monotonic_time", lambda: now[0])
    active._history_access_receipts.clear()
    assert not active.saved_alarm_history_available("home")
    active._save_local_history_result(replace(page(), from_cache=True), 1)
    assert not active.saved_alarm_history_available("home")
    active._save_local_history_result(page(), 1)
    now[0] = 309.9
    assert active.saved_alarm_history_available("home")
    active._save_local_history_result(replace(page(), from_cache=True), 1)
    now[0] = 310.0
    assert not active.saved_alarm_history_available("home")
    active._save_local_history_result(page(status=AlarmHistoryStatus.TIMED_OUT), 1)
    assert not active.saved_alarm_history_available("home")
    active._save_local_history_result(page(status=AlarmHistoryStatus.EMPTY), 1)
    assert active.saved_alarm_history_available("home")


def test_ordinary_sensor_snapshots_do_not_repeat_disk_reconciliation(active, monkeypatch):
    active.set_local_history_retention(7)
    calls = []
    monkeypatch.setattr(
        active._local_alarm_history,
        "retain_locations",
        lambda locations, **_: calls.append(locations),
    )
    for revision in range(5):
        current = replace(snapshot(), revision=revision)
        active._alarm_snapshot = current
        active._reconcile_local_history(current, 1)
    assert calls == [frozenset({"home"})]
    current = snapshot(locations=())
    active._alarm_snapshot = current
    active._reconcile_local_history(current, 1)
    assert calls[-1] == frozenset()


def test_failed_reconciliation_is_not_acknowledged_and_can_retry(active, monkeypatch):
    active.set_local_history_retention(7)
    calls = []

    def reconcile(locations, **_):
        calls.append(locations)
        if len(calls) == 1:
            raise OSError("temporary failure")

    monkeypatch.setattr(active._local_alarm_history, "retain_locations", reconcile)
    current = active._alarm_snapshot
    active._reconcile_local_history(current, 1)
    active._reconcile_local_history(current, 1)
    active._reconcile_local_history(current, 1)
    assert calls == [frozenset({"home"})] * 2


@pytest.mark.parametrize("operation", ["read", "write"])
def test_slow_storage_releases_shared_locks_and_logout_fences_result_or_commit(
    active, monkeypatch, operation
):
    active.set_local_history_retention(7)
    active._save_local_history_result(page(), 1)
    started, release = threading.Event(), threading.Event()
    results, errors, probes = [], [], []
    original = storage._read if operation == "read" else storage._write
    delayed = False

    def slow(*args, **kwargs):
        nonlocal delayed
        if not delayed:
            delayed = True
            started.set()
            assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "_read" if operation == "read" else "_write", slow)

    def work():
        try:
            results.append(
                active.saved_alarm_history("home")
                if operation == "read"
                else active.set_local_history_retention(30)
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        assert started.wait(1)
        probe = threading.Thread(
            target=lambda: probes.append((rc.get_client(), active.get_alarm_snapshot())),
            daemon=True,
        )
        probe.start()
        probe.join(0.5)
        assert not probe.is_alive(), "storage must not hold the GTK/session or Alarm lock"
        monkeypatch.setattr(rc, "_client", object())
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and len(probes) == 1
    assert not results and isinstance(errors[0], rc.SessionSupersededError)
    assert active._local_alarm_history.preferences()["days"] == 7
    assert active._local_alarm_history.load("home").events


@pytest.mark.asyncio
async def test_cloud_history_storage_does_not_block_the_ring_event_loop(active, monkeypatch):
    active.set_local_history_retention(7)
    started, release = threading.Event(), threading.Event()
    original = storage._write
    progress = []

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(1), "a blocked event loop cannot release the disk worker"
        return original(*args, **kwargs)

    async def fetch(*args, **kwargs):
        return page()

    active._alarm_history_service = SimpleNamespace(fetch_page=fetch)
    monkeypatch.setattr(storage, "_write", slow)
    monkeypatch.setattr(rc, "_LOCAL_HISTORY_WORKERS", threading.BoundedSemaphore(1))
    task = asyncio.create_task(active._async_alarm_history("home"))
    try:
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0.001)
        progress.append("Ring loop remained available")
        assert rc.get_client() is active
    finally:
        release.set()
    result = await asyncio.wait_for(task, 1)
    assert result.events and progress


@pytest.mark.asyncio
async def test_cancelled_cloud_waiter_releases_bounded_worker_after_completion(active, monkeypatch):
    tasks = []
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(rc, "_LOCAL_HISTORY_WORKERS", semaphore)
    monkeypatch.setattr(
        rc.threading,
        "Thread",
        lambda **kw: SimpleNamespace(start=lambda: tasks.append(kw["target"])),
    )

    async def fetch(*args, **kwargs):
        return page()

    active._alarm_history_service = SimpleNamespace(fetch_page=fetch)
    task = asyncio.create_task(active._async_alarm_history("home"))
    await asyncio.sleep(0)
    assert len(tasks) == 1 and not semaphore.acquire(blocking=False)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    tasks.pop()()
    assert semaphore.acquire(blocking=False)


def test_background_reconciliation_coalesces_snapshots_and_defers_storage(active, monkeypatch):
    active.set_local_history_retention(7)
    workers, calls = [], []
    monkeypatch.setattr(rc, "_LOCAL_HISTORY_WORKERS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        rc.threading,
        "Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )

    def retain(locations, *, guard):
        with guard():
            calls.append(locations)

    monkeypatch.setattr(active._local_alarm_history, "retain_locations", retain)
    current = active._alarm_snapshot
    active._queue_history_reconciliation(current, 1)
    active._queue_history_reconciliation(current, 1)
    assert len(workers) == 1 and not calls
    workers.pop()()
    assert calls == [frozenset({"home"})] and not active._history_reconcile_pending
    active._queue_history_reconciliation(current, 1)
    assert not workers


def test_cleanup_commit_rechecks_membership_after_prepared_disk_scan(active, monkeypatch):
    store = active._local_alarm_history
    store.set_retention(7)
    store.save(page("regranted"))
    # Change membership while preparing the deletion, before its actual guard.
    original_locations = store._locations

    def scan():
        paths = original_locations()
        active._alarm_snapshot = snapshot(locations=("home", "regranted"))
        return paths

    monkeypatch.setattr(store, "_locations", scan)
    active._reconcile_local_history(active._alarm_snapshot, 1)
    assert store._location_path("regranted").exists()
    assert active._history_reconciled_locations is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["denied", "replaced", "removed-regranted"])
async def test_prepared_cloud_page_cannot_publish_after_newer_authority(
    active, monkeypatch, change
):
    active.set_local_history_retention(7)
    workers = []
    monkeypatch.setattr(rc, "_LOCAL_HISTORY_WORKERS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        rc.threading,
        "Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )

    async def fetch(*args, **kwargs):
        return page()

    active._alarm_history_service = SimpleNamespace(fetch_page=fetch)
    task = asyncio.create_task(active._async_alarm_history("home"))
    await asyncio.sleep(0)
    assert len(workers) == 1
    old = active._history_result_revisions["home"]
    if change == "denied":
        active._admit_local_history_result(page(status=AlarmHistoryStatus.PERMISSION_DENIED), 1)
    else:
        if change == "removed-regranted":
            active._alarm_snapshot = snapshot(locations=())
            active._reconcile_local_history(active._alarm_snapshot, 1)
            active._alarm_snapshot = snapshot()
        active._admit_local_history_result(page(), 1)
    assert active._history_result_revisions["home"] > old
    workers.pop()()
    with pytest.raises(LocalHistoryError):
        await task
    assert not active._local_alarm_history.load("home").events


@pytest.mark.asyncio
async def test_denied_cloud_outcome_remains_displayable_after_cleanup(active):
    async def fetch(*args, **kwargs):
        return replace(page(status=AlarmHistoryStatus.PERMISSION_DENIED), events=())

    active._alarm_history_service = SimpleNamespace(fetch_page=fetch)
    result = await active._async_alarm_history("home")
    assert result.status is AlarmHistoryStatus.PERMISSION_DENIED and not result.events


def test_authoritative_removal_revokes_receipt_before_disk_worker_available(active, monkeypatch):
    from gi.repository import GLib

    from halo_gtk import config

    active._admit_local_history_result(page(), 1)
    monkeypatch.setattr(config, "load", lambda: {})
    monkeypatch.setattr(GLib, "idle_add", lambda *_: None)
    semaphore = threading.BoundedSemaphore(1)
    semaphore.acquire()
    monkeypatch.setattr(rc, "_LOCAL_HISTORY_WORKERS", semaphore)
    active._on_alarm_snapshot(snapshot(locations=()))
    assert not active._history_access_receipts and not active._history_result_revisions
    active._on_alarm_snapshot(snapshot())
    assert not active.alarm_history_result_available("home")


def test_cloud_result_authority_does_not_require_local_storage(active):
    active._local_alarm_history = None
    active._admit_local_history_result(page(), 1)
    assert active.alarm_history_result_available("home")
    assert not active.saved_alarm_history_available("home")
    active._admit_local_history_result(page(status=AlarmHistoryStatus.PERMISSION_DENIED), 1)
    assert not active.alarm_history_result_available("home")
