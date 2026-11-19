"""Momentary camera talkback, shared by the focused view and camera wall."""

from __future__ import annotations

import weakref

from gi.repository import Gdk, Gtk

from halo_gtk.ui_feedback import show_toast


class HoldToTalkButton(Gtk.Button):
    def __init__(self, get_session, *, compact=False):
        super().__init__()
        self._get_session = get_session
        self._held_session = None
        self._press_revision = 0
        self._compact = compact
        self._window = None
        self._window_handler = None
        self._show_state("stopped")

        gesture = Gtk.GestureClick(button=1)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._pressed)
        gesture.connect("released", lambda *_: self.release())
        gesture.connect("cancel", lambda *_: self.release())
        self.add_controller(gesture)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        keys.connect("key-released", self._key_released)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self.release())
        self.add_controller(focus)
        self.connect("map", self._mapped)
        self.connect("unmap", self._unmapped)
        self.connect("state-flags-changed", self._sensitivity_changed)

    def _pressed(self, gesture, *_):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.grab_focus()
        self._press()

    def _key_pressed(self, _keys, key, _code, _state):
        if key in {Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            self._press()
            return True
        return False

    def _key_released(self, _keys, key, _code, _state):
        if key in {Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            self.release()

    def _mapped(self, *_):
        window = self.get_root()
        if isinstance(window, Gtk.Window):
            self._window = window
            self._window_handler = window.connect("notify::is-active", self._window_active)

    def _window_active(self, window, _property):
        if not window.is_active():
            self.release()

    def _unmapped(self, *_):
        self.release()
        if self._window is not None:
            self._window.disconnect(self._window_handler)
            self._window_handler = self._window = None

    def _sensitivity_changed(self, *_):
        if not self.is_sensitive():
            self.release()

    def _press(self):
        if self._held_session is not None or not self.is_sensitive():
            return
        session = self._get_session()
        if session is None:
            return
        self._held_session = session
        self._press_revision += 1
        revision = self._press_revision
        button_ref = weakref.ref(self)
        self._show_state("starting")

        def changed(state):
            button = button_ref()
            if button is None or button._press_revision != revision:
                return
            if state in {"stopped", "failed"}:
                button.release()
            else:
                button._show_state(state)
            if state == "failed":
                show_toast(button, "Microphone unavailable. Check your input device.")

        try:
            session.start_talking(on_state_changed=changed)
        except Exception:
            changed("failed")

    def release(self):
        session, self._held_session = self._held_session, None
        if session is None:
            return
        self._press_revision += 1
        try:
            session.stop_talking()
        finally:
            self._show_state("stopped")

    def _show_state(self, state):
        text = {
            "stopped": "Hold to talk",
            "starting": "Starting microphone…",
            "active": "Transmitting",
        }[state]
        if state == "active":
            self.add_css_class("destructive-action")
        else:
            self.remove_css_class("destructive-action")
        if self._compact:
            self.set_icon_name(
                "audio-input-microphone-symbolic"
                if state == "active"
                else "microphone-disabled-symbolic"
            )
        else:
            self.set_label(text)
        self.set_tooltip_text(text)
        self.update_property([Gtk.AccessibleProperty.LABEL], [text])
