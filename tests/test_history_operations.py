"""Save feedback outlives navigation while page actions remain current."""

import gc
import weakref
from types import SimpleNamespace

import pytest

from halo_gtk import history_page
from halo_gtk.history_page import HistoryPage


class Toast:
    def __init__(self, title, **kwargs):
        self.title = title
        self.callback = kwargs.get("callback")
        self.button = kwargs.get("button_label")
        self.dismissed = False
        self.timeout = None

    def set_title(self, value):
        self.title = value

    def set_button_label(self, value):
        self.button = value

    def set_timeout(self, value):
        self.timeout = value

    def dismiss(self):
        self.dismissed = True


class Window:
    def __init__(self):
        self.toasts = []

    def show_toast(self, title, **kwargs):
        toast = Toast(title, **kwargs)
        self.toasts.append(toast)
        return toast


class Page:
    _submit_save_job = HistoryPage._submit_save_job
    _show_save_toast = HistoryPage._show_save_toast
    _queue_history_result = HistoryPage._queue_history_result
    _copy_url_to_clipboard = HistoryPage._copy_url_to_clipboard
    _load_and_play = HistoryPage._load_and_play
    _do_delete = HistoryPage._do_delete

    def __init__(self, window):
        self.window = window
        self._save_ui_generation = 3
        self._playback_request_generation = 2
        self.actions = []

    def get_root(self):
        return self.window

    def _do_copy_clipboard(self, value):
        self.actions.append(("copy", value))

    def _after_delete(self, event):
        self.actions.append(("delete", event))

    def _load_url_if_current(self, *args):
        self.actions.append(("play", args))


@pytest.mark.parametrize("change", ["hidden", "unrooted", "collected"])
def test_save_outcome_survives_navigation_but_page_action_does_not(monkeypatch, change):
    submitted = []

    def submit(_owner, key, label, operation, **kwargs):
        job = SimpleNamespace(
            key=key,
            label=label,
            state="queued",
            result="saved",
            error=None,
            total=None,
            downloaded=0,
            cancel=lambda: True,
        )
        submitted.append((job, kwargs["on_update"], operation))
        return job

    monkeypatch.setattr(
        history_page.save_jobs, "get_manager", lambda: SimpleNamespace(submit=submit)
    )
    window = Window()
    page = Page(window)
    actions = []

    async def operation(_job):
        return "saved"

    page._submit_save_job(
        object(),
        ("download", "1"),
        "Saving",
        operation,
        on_success=lambda _page, result: actions.append(result),
    )
    job, callback, _operation = submitted[0]
    callback(job)
    assert window.toasts[0].button == "Cancel"
    assert window.toasts[0].timeout == 0
    reference = weakref.ref(page)
    if change == "hidden":
        page._save_ui_generation += 1
    elif change == "unrooted":
        page.window = None
    else:
        del page
        gc.collect()
        assert reference() is None
    job.state = "succeeded"
    callback(job)
    assert actions == []
    assert window.toasts[0].dismissed
    assert window.toasts[-1].title == "Saved"


def test_save_progress_cancel_and_timeout_are_visible(monkeypatch):
    submitted = []

    def submit(_owner, key, label, _operation, **kwargs):
        job = SimpleNamespace(
            key=key,
            label=label,
            state="running",
            total=100,
            downloaded=25,
            cancel=lambda: True,
            error=None,
        )
        submitted.append((job, kwargs["on_update"]))
        return job

    monkeypatch.setattr(
        history_page.save_jobs, "get_manager", lambda: SimpleNamespace(submit=submit)
    )
    window = Window()
    page = Page(window)
    page._submit_save_job(object(), ("download", "1"), "Saving", None)
    job, callback = submitted[0]
    callback(job)
    assert window.toasts[0].title == "Saving — 25%"
    window.toasts[0].callback()
    assert window.toasts[0].title == "Cancelling save…"
    job.state, job.error = "failed", TimeoutError()
    callback(job)
    assert window.toasts[-1].title == "Saving timed out. Try again."


@pytest.mark.parametrize("operation", ["play", "share", "delete"])
@pytest.mark.parametrize("failure", ["missing", "error", "timeout"])
def test_operation_failures_are_visible_and_stale_callbacks_are_dropped(
    monkeypatch, operation, failure
):
    pending = []
    monkeypatch.setattr(history_page.GLib, "idle_add", lambda callback: pending.append(callback))

    def recording_url(*_args):
        if failure == "missing":
            return None
        if failure == "timeout":
            raise TimeoutError()
        raise RuntimeError("private URL must not be shown")

    client = SimpleNamespace(recording_url=recording_url, delete_recording=recording_url)
    monkeypatch.setattr(history_page, "get_client", lambda: client)
    window = Window()
    page = Page(window)
    event = {"id": 1, "_device": object()}
    if operation == "play":
        page._load_and_play(event, 2)
    elif operation == "share":
        page._copy_url_to_clipboard(event)
    else:
        page._do_delete(event)
    for callback in pending:
        callback()
    assert window.toasts
    if operation == "delete" and failure == "timeout":
        assert "may have completed" in window.toasts[-1].title
    assert "private URL" not in window.toasts[-1].title
    page._save_ui_generation += 1
    window.toasts.clear()
    for callback in pending:
        callback()
    assert window.toasts == []


def test_deletion_does_not_start_after_account_changed(monkeypatch):
    old = SimpleNamespace(delete_recording=lambda *_args: pytest.fail("stale account command"))
    monkeypatch.setattr(history_page, "get_client", lambda: object())
    monkeypatch.setattr(history_page.GLib, "idle_add", lambda callback: None)
    Page(Window())._do_delete({"id": 1, "_device": object()}, 3, old)
