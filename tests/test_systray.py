"""Async StatusNotifier lifecycle; no session bus is contacted."""

from types import SimpleNamespace

import pytest

from halo_gtk import systray


class Scheduler:
    def __init__(self):
        self.idles = []
        self.timeouts = {}
        self.next_id = 0

    def idle(self, callback, *args):
        self.idles.append((callback, args))
        return len(self.idles)

    def timeout(self, seconds, callback, *args):
        self.next_id += 1
        self.timeouts[self.next_id] = (seconds, callback, args)
        return self.next_id

    def remove(self, source):
        self.timeouts.pop(source, None)
        return True

    def run_idle(self):
        while self.idles:
            callback, args = self.idles.pop(0)
            callback(*args)


class Connection:
    def __init__(self):
        self.calls = []
        self.exports = []
        self.removed = []

    def is_closed(self):
        return False

    def register_object(self, path, *_args):
        self.exports.append(path)
        return len(self.exports)

    def unregister_object(self, token):
        self.removed.append(token)

    def signal_subscribe(self, *_args):
        return 11

    def signal_unsubscribe(self, token):
        self.removed.append(token)

    def call(self, *args):
        self.calls.append(args)

    def call_finish(self, result):
        if isinstance(result, Exception):
            raise result
        return result

    def call_sync(self, *_args):
        raise AssertionError("tray registration must not block on D-Bus")

    def reply(self, index, result):
        self.calls[index][-1](self, result, None)


@pytest.fixture
def bus(monkeypatch):
    scheduler = Scheduler()
    acquired, owned, unowned = [], [], []
    monkeypatch.setattr(systray.GLib, "idle_add", scheduler.idle)
    monkeypatch.setattr(systray.GLib, "timeout_add_seconds", scheduler.timeout)
    monkeypatch.setattr(systray.GLib, "source_remove", scheduler.remove)
    monkeypatch.setattr(systray.Gio, "bus_get", lambda *args: acquired.append(args))

    def finish(result):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(systray.Gio, "bus_get_finish", finish)
    monkeypatch.setattr(
        systray.Gio, "bus_get_sync", lambda *_: pytest.fail("synchronous bus acquisition")
    )
    monkeypatch.setattr(
        systray.Gio, "bus_own_name_on_connection", lambda *args: owned.append(args) or len(owned)
    )
    monkeypatch.setattr(systray.Gio, "bus_unown_name", unowned.append)
    tray = systray.SystemTray(object())
    yield SimpleNamespace(
        tray=tray, scheduler=scheduler, acquired=acquired, owned=owned, unowned=unowned
    )
    tray.shutdown()


def begin(bus):
    bus.tray.setup()
    bus.scheduler.run_idle()
    connection = Connection()
    bus.acquired[-1][-1](None, connection, None)
    assert connection.calls == []  # wait for actual bus-name ownership
    bus.owned[-1][-2](connection, "owned-name")
    assert connection.calls[0][3] == "NameHasOwner"
    return connection


def test_setup_is_async_and_repeated_requests_share_one_attempt(bus):
    assert bus.tray.setup() is True
    bus.tray.setup()
    assert not bus.acquired and len(bus.scheduler.idles) == 1
    bus.scheduler.run_idle()
    assert len(bus.acquired) == 1 and bus.tray._setup_pending


def test_async_registration_waits_for_reply_then_cancels_retry(bus):
    connection = begin(bus)
    connection.reply(0, systray.GLib.Variant("(b)", (True,)))
    assert connection.calls[1][3] == "RegisterStatusNotifierItem"
    assert not bus.tray._registered_with_watcher
    assert connection.calls[1][-2] is bus.tray._cancellable
    connection.reply(1, systray.GLib.Variant("()", ()))
    assert bus.tray._registered_with_watcher and not bus.tray._setup_pending
    assert not bus.scheduler.timeouts
    bus.tray.setup()
    assert not bus.scheduler.idles


def test_missing_watcher_schedules_one_retry(bus):
    connection = begin(bus)
    connection.reply(0, systray.GLib.Variant("(b)", (False,)))
    retry = bus.tray._retry_source_id
    assert retry is not None
    bus.tray._schedule_retry()
    assert bus.tray._retry_source_id == retry and len(bus.scheduler.timeouts) == 1


def test_shutdown_cancels_acquisition_and_ignores_late_result(bus):
    bus.tray.setup()
    bus.scheduler.run_idle()
    cancel = bus.tray._cancellable
    bus.tray.shutdown()
    connection = Connection()
    bus.acquired[-1][-1](None, connection, None)
    assert cancel.is_cancelled() and not connection.exports
    assert bus.tray._connection is None and not bus.scheduler.timeouts


def test_late_registration_reply_after_shutdown_cannot_restore_tray(bus):
    connection = begin(bus)
    connection.reply(0, systray.GLib.Variant("(b)", (True,)))
    cancel = bus.tray._cancellable
    bus.tray.shutdown()
    connection.reply(1, systray.GLib.Variant("()", ()))
    assert cancel.is_cancelled() and not bus.tray._registered_with_watcher
    assert set(connection.removed) == {1, 2, 11} and bus.unowned == [1]


def test_watcher_replacement_rejects_old_registration_completion(bus):
    connection = begin(bus)
    connection.reply(0, systray.GLib.Variant("(b)", (True,)))
    old_cancel = bus.tray._cancellable
    bus.tray._on_watcher_name_owner_changed(
        connection,
        None,
        None,
        None,
        None,
        systray.GLib.Variant("(sss)", (systray._WATCHER_BUS, ":1.1", ":1.2")),
    )
    bus.scheduler.run_idle()
    connection.reply(1, systray.GLib.Variant("()", ()))
    assert old_cancel.is_cancelled() and not bus.tray._registered_with_watcher
    assert bus.tray._setup_pending
    connection.reply(2, systray.GLib.Variant("(b)", (True,)))
    connection.reply(3, systray.GLib.Variant("()", ()))
    assert bus.tray._registered_with_watcher


def test_stalled_acquisition_has_attempt_deadline_and_retry(bus):
    bus.tray.setup()
    bus.scheduler.run_idle()
    cancel = bus.tray._cancellable
    timer = bus.tray._attempt_timeout_id
    _, callback, args = bus.scheduler.timeouts.pop(timer)
    callback(*args)
    assert cancel.is_cancelled() and bus.tray._retry_source_id is not None
    connection = Connection()
    bus.acquired[-1][-1](None, connection, None)
    assert not connection.exports


def test_stale_idle_from_old_enable_cycle_does_not_start_another_attempt(bus):
    bus.tray.setup()
    bus.tray.shutdown()
    bus.tray.setup()
    bus.scheduler.run_idle()
    assert len(bus.acquired) == 1


def test_async_bus_failure_retries_without_disabling_tray(bus):
    bus.tray.setup()
    bus.scheduler.run_idle()
    bus.acquired[-1][-1](None, RuntimeError("no bus"), None)
    assert bus.tray._enabled and not bus.tray._setup_pending
    assert bus.tray._retry_source_id is not None


def test_null_name_loss_on_bus_disconnect_releases_registration_and_retries(bus):
    connection = begin(bus)
    connection.reply(0, systray.GLib.Variant("(b)", (True,)))
    connection.reply(1, systray.GLib.Variant("()", ()))
    bus.owned[-1][-1](None, "owned-name")
    assert not bus.tray._registered_with_watcher
    assert bus.tray._connection is None
    assert bus.tray._retry_source_id is not None
    assert set(connection.removed) == {1, 2, 11}


def test_old_null_name_loss_cannot_remove_new_ownership(bus):
    begin(bus)
    old_lost = bus.owned[-1][-1]
    bus.tray.shutdown()
    connection = begin(bus)
    connection.reply(0, systray.GLib.Variant("(b)", (True,)))
    connection.reply(1, systray.GLib.Variant("()", ()))
    old_lost(None, "old-name")
    assert bus.tray._connection is connection
    assert bus.tray._registered_with_watcher
    assert not bus.tray._retry_source_id


def test_glib_keeps_dispatching_while_bus_reply_is_pending(monkeypatch):
    callbacks = []
    monkeypatch.setattr(systray.Gio, "bus_get", lambda *args: callbacks.append(args))
    tray = systray.SystemTray(object())
    loop = systray.GLib.MainLoop()
    ticks = []
    ticker = systray.GLib.timeout_add(5, lambda: ticks.append(True) or True)
    systray.GLib.timeout_add(75, lambda: loop.quit() or False)
    try:
        tray.setup()
        loop.run()
        assert callbacks and tray._setup_pending and len(ticks) >= 5
    finally:
        systray.GLib.source_remove(ticker)
        tray.shutdown()
