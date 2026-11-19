"""Focused live ownership and failure-state regressions."""

import threading
from types import SimpleNamespace

import pytest

from halo_gtk.focused_live_page import FocusedLivePage
from tests.privacy_helpers import grant_current


def test_focused_still_passes_cancel_token_and_skips_fallback(monkeypatch):
    from halo_gtk import focused_live_page, ring_client

    calls = []

    def snapshot(_device, *, cancel_event):
        calls.append(cancel_event)
        cancel_event.set()
        return None

    client = SimpleNamespace(
        is_authenticated=True,
        snapshot_for_device=snapshot,
        last_event_frame_for_device=lambda *_args, **_kwargs: calls.append("fallback"),
    )
    monkeypatch.setattr(ring_client, "get_client", lambda: client)
    monkeypatch.setattr(
        focused_live_page.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"])
    )
    page = SimpleNamespace(_view_generation=1)
    device = SimpleNamespace(id=1, name="fixture")
    grant_current(monkeypatch, device, client)
    FocusedLivePage._load_snapshot_still(page, device)
    assert calls == [page._still_cancel_event]
    assert page._still_cancel_event.is_set()


def test_leaving_focus_cancels_pending_preview(monkeypatch):

    cancelled = threading.Event()
    page = SimpleNamespace(
        _still_cancel_event=cancelled,
        _view_generation=1,
        _device=None,
        _session=None,
        _set_controls_sensitive=lambda _: None,
        _cancel_live_ready_poll=lambda: None,
        _clear_video_box=lambda: None,
    )
    FocusedLivePage.leave(page)
    assert cancelled.is_set()
    assert page._view_generation == 2


@pytest.mark.parametrize("failure", [None, "write", "folder"])
def test_screenshot_distinguishes_save_and_folder_failures(monkeypatch, tmp_path, failure):
    from halo_gtk import focused_live_page

    messages = []

    def write(*_args, **_kwargs):
        if failure == "write":
            raise OSError("disk full")
        return tmp_path / "saved.png"

    def folder(*_args):
        if failure == "folder":
            raise RuntimeError("unavailable")

    monkeypatch.setattr(focused_live_page.media_paths, "snapshot_dir", lambda _: tmp_path)
    monkeypatch.setattr(focused_live_page.media_paths, "write_unique_bytes", write)
    monkeypatch.setattr(focused_live_page.media_paths, "should_open_after_save", lambda: True)
    monkeypatch.setattr(focused_live_page.media_paths, "open_folder", folder)
    monkeypatch.setattr(
        focused_live_page.threading, "Thread", lambda *, target, **_: SimpleNamespace(start=target)
    )
    monkeypatch.setattr(focused_live_page.GLib, "idle_add", lambda callback, *args: callback(*args))
    page = SimpleNamespace(
        _zoom=1.0,
        _view_generation=1,
        _screenshot_pending=False,
        _session=SimpleNamespace(get_current_frame_png=lambda viewport: b"png"),
        _video_view=SimpleNamespace(zoomed_frame_png=lambda _: None),
        _device=SimpleNamespace(id=1),
        get_root=lambda: SimpleNamespace(show_toast=messages.append),
    )
    grant_current(monkeypatch, page._device)
    FocusedLivePage._on_screenshot(page)
    if failure == "write":
        assert len(messages) == 1 and messages[0].startswith("Could not save screenshot")
    else:
        assert messages[0] == "Screenshot saved"
        assert len(messages) == (2 if failure == "folder" else 1)


def test_stop_talking_releases_the_held_control():
    calls = []
    page = SimpleNamespace(_mic_btn=SimpleNamespace(release=lambda: calls.append("stop")))
    FocusedLivePage._stop_talking(page)
    assert calls == ["stop"]


def test_failed_session_stops_live_frame_poll():
    sensitivity = []
    talkback = []
    page = SimpleNamespace(
        _session=SimpleNamespace(state="failed"),
        _live_ready_source_id=42,
        _stop_talking=lambda: talkback.append("stop"),
        _set_controls_sensitive=sensitivity.append,
    )

    result = FocusedLivePage._promote_live_when_ready(page)

    assert result is False
    assert page._live_ready_source_id is None
    assert talkback == ["stop"]
    assert sensitivity == [False]


def test_leaving_rejects_a_delayed_still_for_the_previous_camera(monkeypatch):
    from halo_gtk import focused_live_page

    calls = []
    page = SimpleNamespace(
        _device=SimpleNamespace(id=7),
        _view_generation=3,
        _session=None,
        _stop_talking=lambda: None,
        _set_controls_sensitive=lambda _value: None,
        _cancel_live_ready_poll=lambda: None,
        _clear_video_box=lambda: calls.append("clear"),
        _set_still_paintable=lambda _png: calls.append("still"),
    )
    monkeypatch.setattr(
        focused_live_page,
        "get_live_session_manager",
        lambda: SimpleNamespace(release=lambda *_args, **_kwargs: calls.append("release")),
    )

    FocusedLivePage.leave(page)
    FocusedLivePage._set_loaded_still(page, 7, b"old-frame", 3)

    assert page._view_generation == 4
    assert calls == ["release", "clear"]


def test_siren_confirmation_stays_bound_to_the_shown_camera_and_client(monkeypatch):
    from halo_gtk import focused_live_page

    client = object()
    camera = object()
    sent = []
    page = SimpleNamespace(
        _device=camera,
        _view_generation=3,
        _set_siren=lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    monkeypatch.setattr(focused_live_page, "_ring_client", lambda: client)

    FocusedLivePage._on_siren_confirmed(page, None, "activate", camera, client, 3)
    page._device = object()
    FocusedLivePage._on_siren_confirmed(page, None, "activate", camera, client, 3)
    page._device = camera
    page._view_generation += 1
    FocusedLivePage._on_siren_confirmed(page, None, "activate", camera, client, 3)
    monkeypatch.setattr(focused_live_page, "_ring_client", object)
    FocusedLivePage._on_siren_confirmed(page, None, "activate", camera, client, 4)

    assert sent == [((30,), {"client": client})]


def test_revocation_during_screenshot_write_discards_private_staging(monkeypatch, tmp_path):
    from halo_gtk import focused_live_page

    device = SimpleNamespace(id=19)
    lease = grant_current(monkeypatch, device)

    def revoke(file, payload):
        file.write(payload)
        lease.policy.invalidate()

    monkeypatch.setattr(focused_live_page.media_paths, "snapshot_dir", lambda _: tmp_path)
    monkeypatch.setattr(focused_live_page.media_paths, "_write_all", revoke)
    monkeypatch.setattr(
        focused_live_page.threading, "Thread", lambda *, target, **_: SimpleNamespace(start=target)
    )
    monkeypatch.setattr(focused_live_page.GLib, "idle_add", lambda fn, *args: fn(*args))
    page = SimpleNamespace(
        _zoom=1.0,
        _screenshot_pending=False,
        _view_generation=1,
        _device=device,
        _session=SimpleNamespace(get_current_frame_png=lambda _: b"private"),
    )
    FocusedLivePage._on_screenshot(page)
    assert list(tmp_path.iterdir()) == [] and not page._screenshot_pending
