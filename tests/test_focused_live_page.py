"""Focused live ownership and failure-state regressions."""

from types import SimpleNamespace

from halo_gtk.focused_live_page import FocusedLivePage


class _FakeToggle:
    def __init__(self, active: bool):
        self.active = active
        self.icon_name = None
        self.tooltip = None

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = active

    def handler_block_by_func(self, _callback):
        pass

    def handler_unblock_by_func(self, _callback):
        pass

    def set_icon_name(self, icon_name):
        self.icon_name = icon_name

    def set_tooltip_text(self, tooltip):
        self.tooltip = tooltip


def test_stop_talking_ends_active_shared_session_before_reset():
    calls = []
    toggle = _FakeToggle(active=True)
    page = SimpleNamespace(
        _mic_btn=toggle,
        _session=SimpleNamespace(stop_talking=lambda: calls.append("stop")),
        _on_mic_toggled=lambda *_: None,
        _reset_microphone_control=lambda: FocusedLivePage._reset_microphone_control(page),
    )

    FocusedLivePage._stop_talking(page)

    assert calls == ["stop"]
    assert toggle.active is False
    assert toggle.icon_name == "microphone-disabled-symbolic"


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
