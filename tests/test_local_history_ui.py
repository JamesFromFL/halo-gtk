"""No-network saved-history routing and account-bound native dialog callbacks."""

import threading
from types import MethodType, SimpleNamespace

import pytest

from halo_gtk import alarm_history as ui
from halo_gtk import local_history_preferences as preferences
from halo_gtk.alarm.history import AlarmHistoryPage, AlarmHistoryStatus
from halo_gtk.alarm_history import AlarmHistoryView
from halo_gtk.local_history_preferences import LocalHistoryPreferences
from tests.test_alarm_history_view import Client, Widget, event, view


def test_saved_source_runs_only_local_read_even_when_refresh_forced(monkeypatch):
    calls, queued = [], []

    class SavedClient(Client):
        def saved_alarm_history(self, location):
            calls.append(location)
            return AlarmHistoryPage(
                location, (event(),), AlarmHistoryStatus.PARTIAL, from_cache=True
            )

        def saved_alarm_history_available(self, location):
            return True

        def alarm_history(self, *args, **kwargs):
            pytest.fail("Saved source must never fetch Ring cloud history")

    client = SavedClient()
    obj = view(client)
    obj._saved_mode = True
    monkeypatch.setattr(ui, "get_client", lambda: client)
    monkeypatch.setattr(ui, "_WORKERS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    monkeypatch.setattr(ui.GLib, "idle_add", lambda callback: queued.append(callback))
    AlarmHistoryView._fetch(obj, append=False, force=True)
    assert calls == ["loc"]
    queued.pop()()
    assert obj._events and not obj._loading and obj._next_offset is None
    assert "Read 0 source rows" not in obj._message.text


@pytest.mark.parametrize("checkpoint", ["watch", "publish"])
def test_saved_access_revocation_clears_display_or_discards_queued_page(monkeypatch, checkpoint):
    client = Client()
    client.saved_alarm_history_available = lambda _: False
    obj = view(client)
    obj._saved_mode = True
    obj._events = [event()]
    monkeypatch.setattr(ui, "get_client", lambda: client)
    if checkpoint == "watch":
        AlarmHistoryView._watch_client(obj)
    else:
        obj._finish(
            AlarmHistoryPage("loc", (event(),), AlarmHistoryStatus.PARTIAL),
            client,
            1,
            obj._cancel,
            False,
        )
    assert not obj._events and not obj._loading


@pytest.mark.parametrize("checkpoint", ["watch", "publish"])
def test_cloud_access_revocation_clears_display_or_discards_queued_page(monkeypatch, checkpoint):
    client = Client()
    client.alarm_history_result_available = lambda _: False
    obj = view(client)
    obj._events = [event()]
    monkeypatch.setattr(ui, "get_client", lambda: client)
    if checkpoint == "watch":
        AlarmHistoryView._watch_client(obj)
    else:
        obj._finish(
            AlarmHistoryPage("loc", (event(),), AlarmHistoryStatus.READY),
            client,
            1,
            obj._cancel,
            False,
        )
    assert not obj._events and not obj._loading


def test_source_change_cancels_previous_work_and_updates_coverage(monkeypatch):
    calls = []
    obj = SimpleNamespace(
        _source=Widget(), _coverage=Widget(), _fetch=lambda **kw: calls.append(kw)
    )
    obj._source.set_selected(1)
    AlarmHistoryView._source_changed(obj)
    assert obj._saved_mode and "Saved copies" in obj._coverage.text
    assert calls == [{"append": False}]
    obj._source.set_selected(0)
    AlarmHistoryView._source_changed(obj)
    assert not obj._saved_mode and obj._coverage.text == ui.COVERAGE


def test_settings_change_does_not_fetch_cloud_history():
    calls = []
    obj = SimpleNamespace(_active=True, _saved_mode=False, _fetch=lambda **kw: calls.append(kw))
    AlarmHistoryView._local_settings_changed(obj)
    assert not calls
    obj._saved_mode = True
    AlarmHistoryView._local_settings_changed(obj)
    assert calls == [{"append": False}]


class DialogHarness(SimpleNamespace):
    pass


@pytest.fixture
def inline_workers(monkeypatch):
    monkeypatch.setattr(preferences, "_WORKERS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(
        preferences.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"])
    )
    monkeypatch.setattr(preferences.GLib, "idle_add", lambda callback: callback())


def dialog(client):
    obj = DialogHarness(
        _client=client,
        _generation=1,
        _closed=False,
        _watch_id=None,
        _retention=Widget(),
        _save=Widget(),
        _clear=Widget(),
        _message=Widget(),
        _on_changed=None,
        _busy=False,
        _available=True,
        _request=0,
    )
    for name in (
        "_current",
        "_set_sensitive",
        "_watch_client",
        "_save_clicked",
        "_clear_response",
        "_on_closed",
        "_start",
        "_finish",
        "_loaded",
        "_saved",
        "_cleared",
    ):
        setattr(obj, name, MethodType(getattr(LocalHistoryPreferences, name), obj))
    return obj


def test_dialog_retention_apply_and_clear_are_explicit(monkeypatch, inline_workers):
    calls = []
    client = SimpleNamespace(
        get_alarm_snapshot=lambda: SimpleNamespace(generation=1),
        set_local_history_retention=lambda days: calls.append(("save", days)),
        clear_local_alarm_history=lambda: calls.append(("clear",)),
    )
    obj = dialog(client)
    monkeypatch.setattr(preferences, "get_client", lambda: client)
    obj._retention.set_selected(2)
    assert not calls
    obj._save_clicked()
    assert calls == [("save", 30)]
    obj._clear_response(None, "cancel")
    assert calls == [("save", 30)]
    obj._clear_response(None, "clear")
    assert calls == [("save", 30), ("clear",)]
    assert "Retention is unchanged" in obj._message.text


@pytest.mark.parametrize("retire", ["client", "generation", "closed"])
def test_dialog_cannot_mutate_after_account_change_or_close(monkeypatch, retire, inline_workers):
    client = SimpleNamespace(
        get_alarm_snapshot=lambda: SimpleNamespace(generation=1),
        set_local_history_retention=lambda *_: pytest.fail("stale save"),
        clear_local_alarm_history=lambda: pytest.fail("stale clear"),
    )
    obj = dialog(client)
    monkeypatch.setattr(
        preferences, "get_client", lambda: client if retire != "client" else object()
    )
    if retire == "generation":
        obj._generation = 2
    elif retire == "closed":
        obj._closed = True
    obj._save_clicked()
    obj._clear_response(None, "clear")
    assert not obj._save.sensitive and "Account changed" in obj._message.text


def test_dialog_disk_failure_keeps_dialog_open_and_does_not_claim_success(
    monkeypatch, inline_workers
):
    def fail(*_):
        raise OSError("private path must not appear")

    client = SimpleNamespace(
        get_alarm_snapshot=lambda: SimpleNamespace(generation=1),
        set_local_history_retention=fail,
        clear_local_alarm_history=fail,
    )
    obj = dialog(client)
    monkeypatch.setattr(preferences, "get_client", lambda: client)
    obj._save_clicked()
    assert "could not be confirmed" in obj._message.text and not obj._closed
    obj._clear_response(None, "clear")
    assert "could not be fully cleared" in obj._message.text
    assert "private path" not in obj._message.text


@pytest.mark.parametrize("when", ["before-worker", "before-completion", "account-change"])
def test_settings_worker_discards_closed_or_replaced_dialog_work(monkeypatch, when):
    workers, callbacks, operations, published = [], [], [], []
    client = SimpleNamespace(get_alarm_snapshot=lambda: SimpleNamespace(generation=1))
    obj = dialog(client)
    monkeypatch.setattr(preferences, "get_client", lambda: client)
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(preferences, "_WORKERS", semaphore)
    monkeypatch.setattr(
        preferences.threading,
        "Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )
    monkeypatch.setattr(preferences.GLib, "idle_add", callbacks.append)
    obj._start(lambda: operations.append("read"), lambda *args: published.append(args))
    assert not operations and obj._busy and not obj._save.sensitive
    if when == "before-worker":
        obj._on_closed()
    workers.pop()()
    if when == "before-completion":
        obj._on_closed()
    elif when == "account-change":
        monkeypatch.setattr(preferences, "get_client", lambda: object())
    callbacks.pop()()
    assert not published
    assert operations == ([] if when == "before-worker" else ["read"])
    assert semaphore.acquire(blocking=False)


def test_settings_worker_does_not_block_main_thread_while_storage_waits(monkeypatch):
    started, release = threading.Event(), threading.Event()
    queued, threads, shown = [], [], []
    original_thread = threading.Thread
    client = SimpleNamespace(get_alarm_snapshot=lambda: SimpleNamespace(generation=1))
    obj = dialog(client)
    monkeypatch.setattr(preferences, "get_client", lambda: client)
    monkeypatch.setattr(preferences, "_WORKERS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(preferences.GLib, "idle_add", queued.append)

    def thread(**kw):
        worker = original_thread(**kw)
        threads.append(worker)
        return worker

    def read():
        started.set()
        assert release.wait(1)
        return {"available": True, "days": 7}

    monkeypatch.setattr(preferences.threading, "Thread", thread)
    obj._start(read, lambda *args: shown.append(args))
    try:
        assert started.wait(1)
        assert obj._busy and not shown
        obj._on_closed()
    finally:
        release.set()
        threads[0].join(1)
    assert not threads[0].is_alive()
    queued.pop()()
    assert not shown


@pytest.mark.parametrize("failure", ["busy", "thread-start"])
def test_settings_worker_limits_and_start_failure_are_visible(monkeypatch, failure):
    client = SimpleNamespace(get_alarm_snapshot=lambda: SimpleNamespace(generation=1))
    obj = dialog(client)
    monkeypatch.setattr(preferences, "get_client", lambda: client)
    semaphore = threading.BoundedSemaphore(0 if failure == "busy" else 1)
    monkeypatch.setattr(preferences, "_WORKERS", semaphore)
    monkeypatch.setattr(
        preferences.threading, "Thread", lambda **_: (_ for _ in ()).throw(RuntimeError())
    )
    obj._start(lambda: pytest.fail("must not run"), obj._loaded)
    assert not obj._busy and ("busy" in obj._message.text or "unavailable" in obj._message.text)
    if failure == "thread-start":
        assert semaphore.acquire(blocking=False)
