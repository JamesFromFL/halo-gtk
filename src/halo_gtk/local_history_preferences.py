"""Native opt-in local history settings, scoped to the currently signed-in client."""

from __future__ import annotations

import threading
import weakref

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk.local_alarm_history import DAYS  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402

_WORKERS = threading.BoundedSemaphore(2)


class LocalHistoryPreferences(Adw.PreferencesDialog):
    def __init__(self, client, *, on_changed=None):
        super().__init__(title="Local Alarm History", content_width=460, content_height=540)
        self._client = client
        self._generation = client.get_alarm_snapshot().generation
        self._on_changed = on_changed
        self._closed = False
        self._watch_id = None
        self._busy = False
        self._request = 0
        self._available = False
        page = Adw.PreferencesPage()
        self.add(page)
        group = Adw.PreferencesGroup(
            title="Save copies on this computer",
            description="Off by default. Saves only Alarm cloud history you explicitly request "
            "for this signed-in Ring account. It does not fetch in the background or provide "
            "a complete history or current Alarm status.",
        )
        page.add(group)
        self._retention = Adw.ComboRow(
            title="Keep new copies",
            model=Gtk.StringList.new(["Off", "7 days after retrieval", "30 days after retrieval"]),
        )
        group.add(self._retention)
        note = Adw.PreferencesGroup(
            description="Changing this setting applies to new copies. Existing copies retain "
            "their original expiry, even when switched Off. Expired copies are removed when "
            "local history is next accessed. Use Clear to remove them now.",
        )
        page.add(note)
        self._message = Gtk.Label(wrap=True, xalign=0, selectable=True)
        note.add(self._message)
        self._save = Gtk.Button(label="Save retention", css_classes=["suggested-action"])
        self._save.connect("clicked", self._save_clicked)
        note.add(self._save)
        self._clear = Gtk.Button(label="Clear saved history…", css_classes=["destructive-action"])
        self._clear.connect("clicked", self._clear_clicked)
        note.add(self._clear)
        self.connect("closed", self._on_closed)
        self._set_sensitive(False)
        self._watch_id = GLib.timeout_add(250, self._watch_client)
        self._start(client.local_history_preferences, self._loaded)

    def _start(self, operation, completed):
        if not self._current() or self._busy:
            return
        if not _WORKERS.acquire(blocking=False):
            self._message.set_label("Local history is busy. Reopen settings to try again.")
            return
        self._busy = True
        self._request += 1
        request = self._request
        self._set_sensitive(False)
        self._message.set_label("Working with local history…")
        reference = weakref.ref(self)

        def worker():
            value, succeeded = None, False
            try:
                owner = reference()
                if owner is not None and owner._current():
                    value = operation()
                    succeeded = True
            except Exception:
                pass
            finally:
                _WORKERS.release()

            def publish():
                owner = reference()
                if owner is not None:
                    owner._finish(request, completed, succeeded, value)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(publish)

        try:
            threading.Thread(target=worker, daemon=True, name="local-history-settings").start()
        except Exception:
            _WORKERS.release()
            self._finish(request, completed, False, None)

    def _finish(self, request, completed, succeeded, value):
        if request != self._request or not self._current():
            return
        self._busy = False
        self._set_sensitive(self._available)
        completed(succeeded, value)

    def _loaded(self, succeeded, value):
        if not succeeded:
            self._message.set_label("Local history settings are unavailable. Reopen to try again.")
            return
        self._available = value["available"]
        self._retention.set_selected(DAYS.index(value["days"]))
        self._message.set_label(value.get("reason", "Applies only to local copies."))
        self._set_sensitive(self._available)

    def _set_sensitive(self, value):
        for widget in (self._retention, self._save, self._clear):
            widget.set_sensitive(value)

    def _current(self):
        return (
            not self._closed
            and get_client() is self._client
            and self._client.get_alarm_snapshot().generation == self._generation
        )

    def _watch_client(self):
        if self._current():
            return GLib.SOURCE_CONTINUE
        self._watch_id = None
        self._set_sensitive(False)
        self._message.set_label("Account changed. Reopen local history settings to continue.")
        return GLib.SOURCE_REMOVE

    def _on_closed(self, *_):
        self._closed = True
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None

    def _save_clicked(self, *_):
        if not self._current():
            self._watch_client()
            return
        index = self._retention.get_selected()
        if index >= len(DAYS):
            return
        self._start(lambda: self._client.set_local_history_retention(DAYS[index]), self._saved)

    def _saved(self, succeeded, _value):
        if not succeeded:
            self._message.set_label(
                "Retention could not be confirmed. Reopen settings to check it."
            )
            return
        self._message.set_label("Retention saved. Existing copies keep their original expiry.")
        if self._on_changed is not None:
            self._on_changed()

    def _clear_clicked(self, *_):
        if not self._current():
            self._watch_client()
            return
        dialog = Adw.AlertDialog(
            heading="Clear saved Alarm history?",
            body="Remove local event copies for this Ring account on this computer. "
            "Ring cloud history and Alarm settings are unchanged. Saved retention is unchanged, "
            "so later explicit cloud requests may save new copies.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear saved history")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._clear_response)
        dialog.present(self)

    def _clear_response(self, _dialog, response):
        if response != "clear":
            return
        if not self._current():
            self._watch_client()
            return
        self._start(self._client.clear_local_alarm_history, self._cleared)

    def _cleared(self, succeeded, _value):
        if not succeeded:
            self._message.set_label("Saved history could not be fully cleared. Try again.")
            return
        self._message.set_label("Saved history cleared. Retention is unchanged.")
        if self._on_changed is not None:
            self._on_changed()
