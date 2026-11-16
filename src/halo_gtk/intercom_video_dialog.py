"""Explicit Intercom Video session controls; no automatic calls or capture."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from halo_gtk.intercom_media import IntercomMediaAuthority, qualified_intercom  # noqa: E402


class IntercomVideoDialog(Adw.Dialog):
    def __init__(self, client, api_id, identity_epoch):
        super().__init__(title="Experimental Intercom Video", content_width=640, content_height=720)
        self._client, self._api_id, self._epoch = client, api_id, identity_epoch
        self._view = self._authority = None
        self._running = self._talking = self._closed = False
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        toolbar.set_content(content)
        self.set_child(toolbar)
        note = Gtk.Label(
            label=(
                "Experimental · hardware not yet verified\n"
                "For Ring Intercom Video only. Starting may open or answer the building "
                "intercom's media path. Sessions end after 5 minutes. Entrance release "
                "remains a separate confirmed action."
            ),
            xalign=0,
            wrap=True,
            css_classes=["dim-label"],
        )
        content.append(note)
        self._opt_in = Gtk.CheckButton(label="Enable Intercom Video for this view")
        self._opt_in.connect("toggled", self._enabled_changed)
        content.append(self._opt_in)
        self._media = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, vexpand=True, height_request=240
        )
        self._placeholder = Gtk.Label(
            label="Start when you intend to open the Intercom media session.",
            wrap=True,
            vexpand=True,
            css_classes=["dim-label"],
        )
        self._media.append(self._placeholder)
        content.append(self._media)
        controls = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        self._start_button = Gtk.Button(label="Start", css_classes=["suggested-action"])
        self._end_button = Gtk.Button(label="End", sensitive=False)
        self._listen = Gtk.ToggleButton(label="Listen", sensitive=False)
        self._talk = Gtk.Button(label="Hold to talk", sensitive=False)
        self._start_button.connect("clicked", self._start)
        self._end_button.connect("clicked", self._end)
        self._listen.connect("toggled", self._listen_changed)
        for index, widget in enumerate(
            (self._start_button, self._end_button, self._listen, self._talk)
        ):
            controls.attach(widget, index % 2, index // 2, 1, 1)
        content.append(controls)
        self._status = Gtk.Label(
            label="Not started. Listening is muted; the microphone is off.", xalign=0, wrap=True
        )
        content.append(self._status)
        gesture = Gtk.GestureClick(button=1)
        gesture.connect("pressed", lambda *_: self._press_talk())
        gesture.connect("released", lambda *_: self._release_talk())
        gesture.connect("cancel", lambda *_: self._release_talk())
        self._talk.add_controller(gesture)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        keys.connect("key-released", self._key_released)
        self._talk.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._release_talk())
        self._talk.add_controller(focus)
        self.connect("closed", lambda *_: self.retire())
        self._timer = GLib.timeout_add(250, self._tick)
        self._tick()

    def _enabled_changed(self, *_):
        if not self._opt_in.get_active():
            self._end()
        self._tick()

    def _tick(self):
        if self._closed:
            return False
        qualified = qualified_intercom(self._client, self._api_id, epoch=self._epoch) is not None
        if self._running and (not qualified or not self._opt_in.get_active()):
            self._end()
            self._status.set_label(
                "Session ended because current Intercom access could not be verified."
            )
        closing = (
            self._authority is not None and self._authority._lease is not None and not self._running
        )
        self._start_button.set_sensitive(
            qualified and self._opt_in.get_active() and not self._running and not closing
        )
        if closing and self._authority.adapter._cleanup_incomplete:
            self._status.set_label(
                "Local media cleanup could not be verified. Close Halo before trying again."
            )
        elif not qualified and not self._running:
            self._status.set_label(
                "Current Intercom Video account evidence is unavailable. "
                "Reload the Intercom status."
            )
        return True

    def _start(self, *_):
        if self._closed or self._running or not self._opt_in.get_active():
            return
        if self._authority is not None and self._authority._lease is not None:
            return
        if qualified_intercom(self._client, self._api_id, epoch=self._epoch) is None:
            self._tick()
            return
        from halo_gtk.live_stream import LiveStreamView

        try:
            authority = IntercomMediaAuthority(self._client, self._api_id, self._epoch)
            authority.set_enabled(True)
            view = LiveStreamView(media_authority=authority)
            view.set_volume(0)
            view.set_on_stream_state_changed(self._stream_state)
        except Exception:
            self._status.set_label(
                "Intercom media could not be prepared. No microphone was enabled."
            )
            return
        self._authority, self._view = authority, view
        self._running = True
        self._placeholder.set_visible(False)
        self._media.append(view)
        self._listen.set_active(False)
        self._start_button.set_sensitive(False)
        self._end_button.set_sensitive(True)
        self._status.set_label("Starting experimental media. Listening muted; microphone off.")
        try:
            view.start_for_device(authority.adapter)
        except Exception:
            self._end()
            self._status.set_label(
                "Intercom media could not start. Reload current access before retrying."
            )

    def _stream_state(self, state):
        if self._closed or not self._running:
            return
        if state == "active":
            self._listen.set_sensitive(True)
            self._talk.set_sensitive(True)
            self._status.set_label(
                "Media received · hardware behavior remains unverified. Microphone off."
            )
        elif state in {"failed", "stopped", "ended"}:
            self._end()
            self._status.set_label(
                "Intercom media ended. Start again only when you intend to connect."
            )

    def _listen_changed(self, *_):
        if self._view is not None:
            self._view.set_volume(1.0 if self._listen.get_active() else 0.0)

    def _press_talk(self):
        if self._closed or not self._running or self._talking or not self._talk.get_sensitive():
            return
        self._talking = True
        self._status.set_label("Starting microphone while held…")
        self._view.start_talking(on_state_changed=self._talk_state)

    def _talk_state(self, state):
        if self._closed:
            return
        if state == "active" and self._talking:
            self._status.set_label("Microphone on while held. Release to stop transmitting.")
        elif state == "failed":
            self._talking = False
            self._status.set_label("Microphone could not start; it remains off.")

    def _release_talk(self):
        self._talking = False
        if self._view is not None:
            self._view.stop_talking()
        if self._running:
            self._status.set_label("Microphone off.")

    def _key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval in {Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            self._press_talk()
            return True
        return False

    def _key_released(self, _controller, keyval, _keycode, _state):
        if keyval in {Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            self._release_talk()

    def _end(self, *_):
        self._release_talk()
        self._running = False
        if self._authority is not None:
            self._authority.set_enabled(False)
        view, self._view = self._view, None
        if view is not None:
            view.set_on_stream_state_changed(None)
            view.set_volume(0)
            view.stop()
            self._media.remove(view)
        self._placeholder.set_visible(True)
        self._listen.set_active(False)
        for button in (self._end_button, self._listen, self._talk):
            button.set_sensitive(False)
        self._status.set_label("Ended. Listening muted; microphone off.")

    def retire(self):
        if self._closed:
            return
        self._end()
        self._closed = True
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        self._start_button.set_sensitive(False)
