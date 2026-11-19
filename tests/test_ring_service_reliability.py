"""Offline regression tests for push renewal, camera history and credential ordering."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from halo_gtk import history_page
from halo_gtk import ring_client as rc


@pytest.mark.asyncio
async def test_push_renews_repeatedly_and_recovers_without_restarting_receiver(monkeypatch):
    import ring_doorbell

    client = rc.RingClient()
    client._published_generation = 4
    client._ring = SimpleNamespace(async_create_session=AsyncMock())
    monkeypatch.setattr(rc, "_client", client)
    monkeypatch.setattr(rc, "_active_client_generation", 4)
    monkeypatch.setattr(client, "_load_owned_event_credentials", lambda: None)
    now = [100]
    monkeypatch.setattr(rc, "privacy_time", lambda: now[0])
    states, listeners, emitted, renewals = [], [], [], []
    monkeypatch.setattr(client, "_on_ring_event", emitted.append)
    monkeypatch.setattr(client, "_set_owned_listener_state", states.append)

    class Listener:
        SESSION_REFRESH_INTERVAL = 10

        def __init__(self, *_args, **_kwargs):
            listeners.append(self)
            self.callbacks = []
            self.subscribed = True
            self.fcm_token = "fixture"
            self._receiver = SimpleNamespace(run_state=SimpleNamespace(name="STARTED"))
            self.stopped = False

        def add_notification_callback(self, callback):
            self.callbacks.append(callback)

        async def start(self):
            self.session_refresh_task = asyncio.create_task(asyncio.sleep(3600))
            self.sdk_refresh = self.session_refresh_task
            return True

        async def add_subscription_to_ring(self, token):
            assert token == self.fcm_token
            renewals.append(now[0])
            self.subscribed = len(renewals) != 2

        async def stop(self):
            self.stopped = True

    async def advance(_seconds):
        listener = listeners[0]
        for callback in listener.callbacks:
            callback(now[0])
        if len(renewals) == 4:
            client._stop_event.set()
        now[0] += 1

    monkeypatch.setattr(client, "_sleep_or_stop", advance)
    monkeypatch.setattr(ring_doorbell, "RingEventListener", Listener)
    await client._async_listen()
    assert renewals == [110, 120, 125, 135]
    assert client._ring.async_create_session.await_count == 4
    assert len(listeners) == 1
    listener = listeners[0]
    assert listener.stopped and listener.sdk_refresh.cancelled()
    assert len(listener.callbacks) == 1 and len(set(emitted)) == len(emitted)
    assert rc.LISTENER_DEGRADED in states and states[-2:] == [
        rc.LISTENER_CONNECTED,
        rc.LISTENER_OFFLINE,
    ]
    listener.callbacks[0]("retired")
    assert "retired" not in emitted


def active_history_page(client):
    page = history_page.HistoryPage.__new__(history_page.HistoryPage)
    page._history_client = client
    page._history_request_generation = 1
    page._history_exhausted_device_ids = set()
    page._history_failed_device_ids = set()
    page._history_older_than_by_device = {}
    page._history_seen_keys = set()
    page._selected_device_id = None
    page._selected_device_index = 0
    page._device_options = []
    page._active_filter_keys = set()
    page._clear_event_list = Mock()
    page._rebuild_device_filter = Mock()
    page._fill_event_rows = Mock()
    return page


def test_failed_history_camera_retries_its_exact_cursor_and_preserves_other_rows(monkeypatch):
    client = rc.RingClient()
    first = SimpleNamespace(
        id=1,
        family="doorbots",
        async_history=AsyncMock(side_effect=[[{"id": 30}, {"id": 20}], [{"id": 10}]]),
    )
    second = SimpleNamespace(
        id=2,
        family="doorbots",
        async_history=AsyncMock(side_effect=[[{"id": 29}], OSError("unavailable"), [{"id": 19}]]),
    )
    client._ring = SimpleNamespace(devices=lambda: SimpleNamespace(all_devices=[first, second]))
    monkeypatch.setattr(history_page, "get_client", lambda: client)
    monkeypatch.setattr(client, "_run", asyncio.run)
    pending = []
    monkeypatch.setattr(history_page.GLib, "idle_add", lambda cb, *args: pending.append((cb, args)))
    workers = []

    class Worker:
        def __init__(self, *, target, kwargs, **_):
            self.target, self.kwargs = target, kwargs

        def start(self):
            workers.append(self)

    monkeypatch.setattr(history_page.threading, "Thread", Worker)
    ui = active_history_page(client)

    def populate(**kwargs):
        ui._start_history_fetch(**kwargs)
        worker = workers.pop()
        worker.target(**worker.kwargs)
        callback, args = pending.pop()
        callback(*args)

    populate(append=False)
    assert ui._history_older_than_by_device == {1: 20, 2: 29}
    populate(append=True)
    assert ui._history_older_than_by_device == {1: 10, 2: 29}
    assert ui._history_failed_device_ids == {2} and not ui._history_exhausted
    assert {row["id"] for row in ui._events} == {10, 20, 29, 30}
    populate(append=True, retry_failed=True)
    assert ui._history_failed_device_ids == set()
    assert {row["id"] for row in ui._events} == {10, 19, 20, 29, 30}
    assert [call.kwargs.get("older_than") for call in second.async_history.await_args_list] == [
        None,
        29,
        29,
    ]
    assert first.async_history.await_count == 2


def test_retired_history_worker_cannot_fetch_or_publish_to_replacement(monkeypatch):
    client = Mock()
    account = [client]
    monkeypatch.setattr(history_page, "get_client", lambda: account[0])
    ui = active_history_page(client)
    data = rc.CameraHistoryPage([], events=[{"id": 1}])
    client.event_history_page.return_value = data
    pending = []
    monkeypatch.setattr(history_page.GLib, "idle_add", lambda cb, *args: pending.append((cb, args)))
    kwargs = dict(
        client=client,
        generation=1,
        append=False,
        device_ids=None,
        backend_kind=None,
        older_than=None,
    )
    ui._fetch_history(**kwargs)
    account[0] = Mock()
    callback, args = pending.pop()
    callback(*args)
    ui._fetch_history(**kwargs)
    client.event_history_page.assert_called_once()
    account[0].event_history_page.assert_not_called()
    assert ui._history_seen_keys == set() and not pending


@pytest.mark.parametrize("kind", ["token", "push"])
def test_blocked_credential_write_cannot_hold_ui_or_restore_logged_out_secrets(monkeypatch, kind):
    client = rc.RingClient()
    client._published_generation = 6
    monkeypatch.setattr(rc, "_client", client)
    monkeypatch.setattr(rc, "_session_generation", 6)
    monkeypatch.setattr(rc, "_active_client_generation", 6)
    monkeypatch.setattr(rc, "_account_email_cache", "synthetic")
    monkeypatch.setattr(rc, "_account_email_loaded", True)
    started, released, stopped = threading.Event(), threading.Event(), threading.Event()
    order = []

    def save(_):
        started.set()
        assert released.wait(3)
        order.append("save")

    monkeypatch.setattr(rc, "_save_token", save)
    monkeypatch.setattr(rc, "save_ring_event_credentials", save)
    monkeypatch.setattr(rc, "_clear_token", lambda: order.append("clear-token"))
    monkeypatch.setattr(rc, "clear_ring_event_credentials", lambda: order.append("clear-push"))
    monkeypatch.setattr(rc, "clear_ring_account_email", lambda: order.append("clear-email"))
    monkeypatch.setattr(rc, "_stop_client_quietly", lambda _: stopped.set())
    monkeypatch.setattr(client, "_invalidate_alarm_state", lambda _: None)
    with ThreadPoolExecutor(max_workers=3) as pool:
        if kind == "token":
            saving = pool.submit(rc._save_token_for_client, client, 6, {})
        else:
            gen, rev, _ = client._event_listener_context()
            saving = client._on_event_credentials_updated(
                {"registration": {"token": "fixture"}}, generation=gen, listener_revision=rev
            )
        try:
            assert started.wait(1)
            assert pool.submit(rc.get_client).result(timeout=1) is client
            logout = pool.submit(rc.logout_client, expected_client=client)
            assert stopped.wait(1)
            assert pool.submit(rc.get_client).result(timeout=1) is None
            late = pool.submit(rc._save_token_for_client, client, 6, {"late": True})
            assert rc.get_cached_account_email() is None
        finally:
            released.set()
        saving.result(timeout=2)
        assert logout.result(timeout=2)
        with pytest.raises(rc.SessionSupersededError):
            late.result(timeout=2)
    assert order == ["save", "clear-token", "clear-email", "clear-push"]
