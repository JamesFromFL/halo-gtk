"""Devices cache rendering and visible-only Chime lifecycle."""

from types import SimpleNamespace

import pytest

from halo_gtk import devices_page as page_module
from halo_gtk.chime import ChimeAccountSnapshot
from halo_gtk.devices_page import DevicesPage, _device_counts
from tests import test_chime_details as detail_tests
from tests.test_chime_details import Client, Widget, account, device

harness = detail_tests.harness


class PageHarness:
    def __init__(self, context):
        self._visible = False
        self._dialog = None
        self._chime_rows = []
        self._account_group = Widget()
        self._summary_row = Widget()
        self._device_count_label = Widget()
        self._camera_row = Widget()
        self._chime_group = Widget()
        self._refresh_button = Widget()
        self._chime_message = Widget()
        self._session = page_module._ChimeViewSession(
            self._snapshot_changed, self._account_retired, provider=lambda: context.current[0]
        )


for name in (
    "refresh",
    "on_page_shown",
    "on_page_hidden",
    "_account_retired",
    "_close_dialog",
    "_fetch",
    "_snapshot_changed",
    "_render_chimes",
    "_open_chime",
    "_dialog_closed",
):
    setattr(PageHarness, name, getattr(DevicesPage, name))


@pytest.fixture
def page(harness, monkeypatch):
    monkeypatch.setattr(page_module, "get_client", lambda: harness.current[0])
    monkeypatch.setattr(page_module.Adw, "ActionRow", Widget)
    monkeypatch.setattr(page_module.Gtk, "Image", Widget)
    return PageHarness(harness)


def test_hidden_refresh_is_cache_only_and_does_not_bind(page, harness):
    for _ in range(10):
        page.refresh()
    assert not harness.client.calls
    assert not harness.client.callbacks
    assert not harness.scheduler.sources
    assert len(page._chime_rows) == 1


def test_shown_is_idempotent_and_hide_cancels_read_consumer(page, harness):
    page.on_page_shown()
    page.on_page_shown()
    assert harness.client.calls == [("inventory", (), {"force": False})]
    assert len(harness.client.callbacks) == 1
    read = harness.client.futures[-1]
    page.on_page_hidden()
    assert read.cancelled()
    assert not harness.client.callbacks
    assert not harness.scheduler.sources


def test_counts_replace_sdk_chimes_without_altering_other_families():
    sdk = [
        SimpleNamespace(family=family) for family in ("doorbots", "chimes", "chimes", "intercoms")
    ]
    assert _device_counts(sdk, account(device())) == (3, 1, ["chimes", "doorbots", "intercoms"])
    empty = ChimeAccountSnapshot(3, complete=True)
    assert _device_counts(sdk, empty) == (2, 1, ["doorbots", "intercoms"])
    assert _device_counts(sdk, ChimeAccountSnapshot(3)) == (
        4,
        1,
        ["chimes", "doorbots", "intercoms"],
    )


def test_valid_empty_then_failed_read_does_not_revive_removed_sdk_chimes():
    sdk = [SimpleNamespace(family="chimes"), SimpleNamespace(family="doorbots")]
    from halo_gtk.chime import ChimeReadStatus

    failed_after_empty = ChimeAccountSnapshot(
        3, complete=False, devices=(), has_inventory=True, status=ChimeReadStatus.STALE
    )
    assert _device_counts(sdk, failed_after_empty) == (1, 1, ["doorbots"])


def test_unknown_model_remains_activatable_literal_read_only_row(page, harness):
    harness.client.snapshot = account(
        device(
            kind="future_chime",
            name="<b>Private</b>",
            can_write=False,
            unavailable_reason="unsupported-model",
        )
    )
    page.refresh()
    row = page._chime_rows[0]
    assert row.title == "<b>Private</b>" and row.use_markup is False
    assert row.activatable and "Read only" in row.subtitle
    assert not harness.client.calls


def test_explicit_empty_is_distinct_from_unloaded_inventory(page, harness):
    harness.client.snapshot = ChimeAccountSnapshot(3, complete=True)
    page.refresh()
    assert page._chime_rows[0].title == "No Chimes reported"
    harness.client.snapshot = ChimeAccountSnapshot(3)
    page.refresh()
    assert page._chime_rows[0].title == "Chime inventory not loaded"


def test_hidden_refresh_retires_changed_account_and_dialog_without_network(page, harness):
    page.on_page_shown()
    retired = []
    page._dialog = SimpleNamespace(retire=lambda: retired.append(True))
    harness.current[0] = Client(account(device(name="New account"), generation=4))
    page.refresh()
    assert retired == [True] and page._dialog is None
    assert harness.client.futures[0].cancelled()
    assert not harness.current[0].calls
    assert page._chime_rows[0].title == "New account"


def test_stale_row_activation_cannot_open_reused_id(page, harness, monkeypatch):
    page.on_page_shown()
    harness.client.snapshot = account(device(identity_epoch=3))
    monkeypatch.setattr(
        page_module, "ChimeDetails", lambda *args: pytest.fail("old identity opened")
    )
    page._open_chime(None, 42, 2, 3)


def test_hide_clears_rows_and_closes_parented_dialog(page, harness):
    page.on_page_shown()
    retired = []
    page._dialog = SimpleNamespace(retire=lambda: retired.append(True))
    page.on_page_hidden()
    assert retired == [True]
    assert page._chime_rows[0].title == "Chime inventory not loaded"


def test_late_old_snapshot_callback_reads_only_current_cache(page, harness):
    page.on_page_shown()
    old = harness.client.snapshot
    harness.client.snapshot = account(device(name="Newest"))
    page._session._on_snapshot(old)
    harness.scheduler.drain()
    assert page._chime_rows[0].title == "Newest"


def test_refresh_failure_shows_safe_error_and_releases_button(page, harness):
    page.on_page_shown()
    harness.client.futures[0].set_exception(RuntimeError("owner=private&token=SECRET"))
    harness.scheduler.drain()
    assert page._refresh_button.sensitive
    assert "unavailable" in page._chime_message.label
    assert "SECRET" not in page._chime_message.label
