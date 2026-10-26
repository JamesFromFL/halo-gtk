"""Ordering and ambiguous-result behavior without Ring or GTK windows."""

import asyncio
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from halo_gtk import camera_settings as module


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("queue did not reach the expected state")


class Client:
    def __init__(self):
        self.values = []
        self.futures = []
        self._published_generation = 4

    def submit(self, coroutine):
        future = Future()
        try:
            asyncio.run(coroutine)
        except Exception as error:
            future.set_exception(error)
        self.futures.append(future)
        return future


@pytest.fixture
def queue_fixture(monkeypatch):
    client = Client()
    key = (id(client), 4, "fixture-camera")
    monkeypatch.setattr(module, "_SETTING_TIMEOUT", 0.03)
    yield client, key
    for future in client.futures:
        if not future.done():
            future.set_result(None)
    wait_for(lambda: key not in module._setting_queues)


def request(
    client, value, *, key="volume", current=lambda: True, wanted=lambda: True, finished=None
):
    async def invoke():
        client.values.append(value)

    return module._SettingRequest(key, invoke, current, wanted, finished or (lambda *_: None))


def test_writes_are_serial_and_only_unsent_absolute_values_coalesce(queue_fixture):
    client, key = queue_fixture
    module._SettingQueue.enqueue(key, client, request(client, 1))
    wait_for(lambda: len(client.futures) == 1)
    module._SettingQueue.enqueue(key, client, request(client, 2))
    module._SettingQueue.enqueue(key, client, request(client, "chime", key="chime"))
    module._SettingQueue.enqueue(key, client, request(client, 3))
    assert client.values == [1]
    client.futures[0].set_result(None)
    wait_for(lambda: len(client.futures) == 2)
    assert client.values == [1, "chime"]
    client.futures[1].set_result(None)
    wait_for(lambda: len(client.futures) == 3)
    assert client.values == [1, "chime", 3]
    client.futures[2].set_result(None)


def test_unkeyed_commands_are_never_coalesced(queue_fixture):
    client, key = queue_fixture
    for value in (1, 2, 3):
        module._SettingQueue.enqueue(key, client, request(client, value, key=None))
    for count in (1, 2, 3):
        wait_for(lambda count=count: len(client.futures) == count)
        client.futures[count - 1].set_result(None)
    wait_for(lambda: key not in module._setting_queues)
    assert client.values == [1, 2, 3]


def test_ui_wait_timeout_keeps_real_request_in_flight(queue_fixture):
    client, key = queue_fixture
    uncertain = threading.Event()
    outcomes = []

    def finished(error, unknown):
        outcomes.append((error, unknown))
        if unknown:
            uncertain.set()

    module._SettingQueue.enqueue(key, client, request(client, 1, finished=finished))
    wait_for(lambda: len(client.futures) == 1)
    module._SettingQueue.enqueue(key, client, request(client, 2))
    assert uncertain.wait(1)
    assert len(client.futures) == 1
    assert not client.futures[0].cancelled()
    client.futures[0].set_result(None)
    wait_for(lambda: len(client.futures) == 2)
    assert outcomes[-1] == (None, False)
    client.futures[1].set_result(None)


def test_terminal_remote_timeout_does_not_send_queued_changes(queue_fixture):
    client, key = queue_fixture
    outcomes = []
    module._SettingQueue.enqueue(
        key, client, request(client, 1, finished=lambda *args: outcomes.append(args))
    )
    wait_for(lambda: len(client.futures) == 1)
    module._SettingQueue.enqueue(
        key, client, request(client, 2, finished=lambda *args: outcomes.append(args))
    )
    client.futures[0].set_exception(TimeoutError("server outcome unknown"))
    wait_for(lambda: key not in module._setting_queues)
    assert client.values == [1]
    assert any(uncertain for _, uncertain in outcomes)
    assert "Not sent" in outcomes[-1][0]


def test_generation_change_drops_unsent_work_and_late_outcomes(queue_fixture):
    client, key = queue_fixture
    current = [True]
    outcomes = []
    for value in (1, 2):
        module._SettingQueue.enqueue(
            key,
            client,
            request(
                client,
                value,
                key=None,
                current=lambda: current[0],
                finished=lambda *args: outcomes.append(args),
            ),
        )
    wait_for(lambda: len(client.futures) == 1)
    current[0] = False
    client.futures[0].set_result(None)
    wait_for(lambda: key not in module._setting_queues)
    assert client.values == [1]
    assert outcomes == []


def test_closed_window_does_not_release_an_inflight_write(queue_fixture):
    client, key = queue_fixture
    open_window = [True]
    uncertain = threading.Event()
    module._SettingQueue.enqueue(
        key,
        client,
        request(
            client,
            1,
            wanted=lambda: open_window[0],
            finished=lambda _error, unknown: uncertain.set() if unknown else None,
        ),
    )
    wait_for(lambda: len(client.futures) == 1)
    open_window[0] = False
    module._SettingQueue.enqueue(key, client, request(client, 2))
    assert uncertain.wait(1)
    assert client.values == [1]
    client.futures[0].set_result(None)
    wait_for(lambda: len(client.futures) == 2)
    assert client.values == [1, 2]
    client.futures[1].set_result(None)


def test_dispatch_checks_generation_before_creating_sdk_coroutine():
    called = []
    req = module._SettingRequest(
        None, lambda: called.append(True), lambda: False, lambda: True, lambda *_: None
    )
    queue = module._SettingQueue(("unused",), object())
    with pytest.raises(RuntimeError, match="changed"):
        asyncio.run(queue._invoke(req))
    assert called == []


def test_unsent_change_replacement_notifies_its_original_window(queue_fixture):
    client, key = queue_fixture
    replaced = []
    module._SettingQueue.enqueue(key, client, request(client, 1))
    wait_for(lambda: len(client.futures) == 1)
    module._SettingQueue.enqueue(
        key,
        client,
        request(client, 2, finished=lambda *args: replaced.append(args)),
    )
    module._SettingQueue.enqueue(key, client, request(client, 3))
    assert len(replaced) == 1 and "newer pending" in replaced[0][0]
    client.futures[0].set_result(None)
    wait_for(lambda: len(client.futures) == 2)
    client.futures[1].set_result(None)


def test_failed_worker_start_reports_unsent_and_releases_queue(monkeypatch):
    outcomes = []
    client = Client()
    key = ("failed-thread",)

    def fail():
        raise RuntimeError("thread resources unavailable")

    monkeypatch.setattr(module.threading, "Thread", lambda **_kwargs: SimpleNamespace(start=fail))
    module._SettingQueue.enqueue(
        key,
        client,
        request(client, 1, finished=lambda *args: outcomes.append(args)),
    )
    assert key not in module._setting_queues and client.values == []
    assert len(outcomes) == 1 and "could not start" in outcomes[0][0]


def test_completion_racing_wait_deadline_is_reported_as_success():
    class RacedFuture:
        first = True

        def result(self, timeout=None):
            if self.first:
                self.first = False
                raise TimeoutError
            return None

        def done(self):
            return True

    outcomes = []
    client = SimpleNamespace(submit=lambda coroutine: (coroutine.close(), RacedFuture())[1])
    key = ("raced-deadline",)
    queue = module._SettingQueue(key, client)
    queue.pending.append(request(client, 1, finished=lambda *args: outcomes.append(args)))
    module._setting_queues[key] = queue
    queue._run()
    assert outcomes == [(None, False)]


def test_sdk_exception_text_is_not_sent_to_ui(queue_fixture):
    client, key = queue_fixture
    outcomes = []
    module._SettingQueue.enqueue(
        key,
        client,
        request(client, 1, finished=lambda *args: outcomes.append(args)),
    )
    wait_for(lambda: len(client.futures) == 1)
    client.futures[0].set_exception(RuntimeError("https://camera.invalid/?token=secret"))
    wait_for(lambda: key not in module._setting_queues)
    assert len(outcomes) == 1 and "secret" not in outcomes[0][0]
    assert "could not confirm" in outcomes[0][0]


def test_stopped_client_is_rejected_even_if_pointer_still_matches(monkeypatch):
    client = SimpleNamespace(_published_generation=4, _stopped=False)
    window = module.CameraSettingsWindow.__new__(module.CameraSettingsWindow)
    window.device = SimpleNamespace(id=1, family="doorbots")
    window._setting_serial = 0
    window._closed = False
    window._set_status = lambda _text: None
    captured = []
    monkeypatch.setattr(module, "get_client", lambda: client)
    monkeypatch.setattr(
        module._SettingQueue, "enqueue", lambda _key, _client, req: captured.append(req)
    )
    window._apply_setting("Volume", lambda: None, lambda: None, setting_key="volume")
    assert captured[0].current()
    client._stopped = True
    assert not captured[0].current()


def test_old_completion_does_not_replace_new_pending_status():
    updates = []
    page = SimpleNamespace(_closed=False, _setting_serial=2)
    page._set_status = lambda text: updates.append(text)
    page._finish_apply = lambda *_: updates.append("finished")
    module.CameraSettingsWindow._finish_queued_setting(
        page,
        1,
        lambda: True,
        "Volume",
        None,
        False,
        lambda: updates.append("cache"),
        False,
        None,
    )
    assert updates == ["cache"]
    module.CameraSettingsWindow._finish_queued_setting(
        page,
        2,
        lambda: False,
        "Volume",
        None,
        False,
        lambda: updates.append("stale cache"),
        False,
        None,
    )
    assert updates == ["cache"]
