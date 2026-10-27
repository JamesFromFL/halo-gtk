"""Alarm-only History presenter; no camera/media actions or durable storage."""

from __future__ import annotations

import threading
import weakref
from dataclasses import replace
from datetime import UTC, datetime

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk.alarm.history import (  # noqa: E402
    COVERAGE,
    AlarmHistoryEvent,
    AlarmHistoryPage,
    AlarmHistoryStatus,
)
from halo_gtk.ring_client import get_client  # noqa: E402

# No unbounded thread/executor queue when selection changes rapidly.
_WORKERS = threading.BoundedSemaphore(2)


class AlarmHistoryView(Gtk.Box):
    def __init__(self, *, on_camera_history=None, on_open_alarm=None) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._on_open_alarm = on_open_alarm
        self._client = None
        self._generation = 0
        self._cancel = threading.Event()
        self._active = False
        self._loading = False
        self._locations = ()
        self._location_id = None
        self._events: list[AlarmHistoryEvent] = []
        self._page_signatures: set[tuple[str, ...]] = set()
        self._next_offset = None
        self._updating = False
        self._device_ids = [None]
        self._snapshot_pending = None
        self._snapshot_idle = False
        self._watch_id = None
        self._account_generation = None
        header = Gtk.Box(spacing=8)
        back = Gtk.Button(label="Camera history")
        back.connect("clicked", lambda *_: on_camera_history() if on_camera_history else None)
        header.append(back)
        self._refresh_button = Gtk.Button(label="Refresh", halign=Gtk.Align.END, hexpand=True)
        self._refresh_button.connect("clicked", lambda *_: self.refresh(force=True))
        header.append(self._refresh_button)
        self.append(header)
        self.append(Gtk.Label(label="Alarm cloud history", xalign=0, css_classes=["title-2"]))
        self.append(Gtk.Label(label=COVERAGE, wrap=True, xalign=0, css_classes=["dim-label"]))
        self._location = Gtk.DropDown(model=Gtk.StringList.new([]))
        self._location.set_tooltip_text("Alarm location")
        self._location.update_property([Gtk.AccessibleProperty.LABEL], ["Alarm location"])
        self._location.connect("notify::selected", self._location_changed)
        self.append(self._location)
        filters = Gtk.Box(spacing=8, homogeneous=True)
        self._category = Gtk.DropDown.new_from_strings(
            ["All event types", "Mode", "Alarm", "Other"]
        )
        self._category.set_tooltip_text("Alarm event type")
        self._category.update_property([Gtk.AccessibleProperty.LABEL], ["Alarm event type"])
        self._category.connect("notify::selected", lambda *_: self._render())
        filters.append(self._category)
        self._device = Gtk.DropDown.new_from_strings(["All devices"])
        self._device.set_tooltip_text("Alarm device")
        self._device.update_property([Gtk.AccessibleProperty.LABEL], ["Alarm device"])
        self._device.connect("notify::selected", lambda *_: self._render())
        filters.append(self._device)
        self.append(filters)
        self._message = Gtk.Label(wrap=True, xalign=0, selectable=True)
        self.append(self._message)
        self._rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._rows.connect("row-activated", self._activate)
        self._empty = Adw.StatusPage(
            title="Choose an Alarm location", icon_name="security-high-symbolic"
        )
        self._rows.set_placeholder(self._empty)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroll.set_child(self._rows)
        self.append(scroll)
        self._older = Gtk.Button(label="Load older Alarm events", halign=Gtk.Align.CENTER)
        self._older.connect("clicked", lambda *_: self._fetch(append=True))
        self._older.set_visible(False)
        self.append(self._older)

    def refresh(self, location_id: str | None = None, *, force: bool = False) -> None:
        selected = location_id or self._location_id
        self.deactivate(clear=True)
        self._active = True
        client = get_client()
        if client is None or not client.is_authenticated:
            self._empty.set_title("Not signed in")
            self._message.set_label("Sign in to read Alarm history.")
            return
        self._client = client
        client.add_alarm_callback(self._on_snapshot)
        snapshot = client.get_alarm_snapshot()
        self._account_generation = snapshot.generation
        self._watch_id = GLib.timeout_add(250, self._watch_client)
        self._set_locations(snapshot, selected)
        self._fetch(append=False, force=force)

    def deactivate(self, *, clear: bool = False) -> None:
        self._active = False
        self._generation += 1
        self._cancel.set()
        self._loading = False
        self._refresh_button.set_sensitive(True)
        self._snapshot_pending = None
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None
        if self._client is not None:
            self._client.remove_alarm_callback(self._on_snapshot)
        self._client = None
        if clear:
            self._events.clear()
            self._page_signatures.clear()
            self._next_offset = None
            self._location_id = None
            self._locations = ()
            self._updating = True
            self._location.set_model(Gtk.StringList.new([]))
            self._location.set_selected(Gtk.INVALID_LIST_POSITION)
            self._device_ids = [None]
            self._device.set_model(Gtk.StringList.new(["All devices"]))
            self._device.set_selected(0)
            self._updating = False
            self._render()

    def _watch_client(self) -> bool:
        client = get_client()
        if (
            self._active
            and client is self._client
            and client is not None
            and (client.get_alarm_snapshot().generation == self._account_generation)
        ):
            return GLib.SOURCE_CONTINUE
        self._watch_id = None
        self.deactivate(clear=True)
        self._message.set_label("Account changed. Reopen Alarm history to continue.")
        return GLib.SOURCE_REMOVE

    def do_unroot(self) -> None:
        self.deactivate(clear=True)
        Gtk.Box.do_unroot(self)

    def _set_locations(self, snapshot, selected) -> None:
        device_index = self._device.get_selected()
        selected_device = (
            self._device_ids[device_index] if device_index < len(self._device_ids) else None
        )
        self._updating = True
        self._locations = tuple(snapshot.locations)
        self._location.set_model(
            Gtk.StringList.new(
                [location.name[:256] or "Alarm location" for location in self._locations]
            )
        )
        index = next(
            (i for i, location in enumerate(self._locations) if location.location_id == selected), 0
        )
        self._location.set_selected(index if self._locations else Gtk.INVALID_LIST_POSITION)
        self._location_id = self._locations[index].location_id if self._locations else None
        self._set_devices(selected_device)
        self._updating = False

    def _set_devices(self, selected: str | None = None) -> None:
        location = next(
            (loc for loc in self._locations if loc.location_id == self._location_id), None
        )
        devices = location.devices if location else ()
        counts: dict[str, int] = {}
        for device in devices:
            counts[device.zid] = counts.get(device.zid, 0) + 1
        devices = tuple(device for device in devices if counts[device.zid] == 1)
        self._device_ids = [None, *(device.zid for device in devices)]
        self._device.set_model(
            Gtk.StringList.new(
                ["All devices", *(device.name[:256] or "Alarm device" for device in devices)]
            )
        )
        self._device.set_selected(
            self._device_ids.index(selected) if selected in self._device_ids else 0
        )

    def _on_snapshot(self, snapshot) -> None:
        # The public client normally calls on GTK; still fence/coalesce delivery.
        self._snapshot_pending = snapshot
        if not self._snapshot_idle:
            self._snapshot_idle = True
            GLib.idle_add(self._apply_snapshot)

    def _apply_snapshot(self) -> bool:
        self._snapshot_idle = False
        snapshot, self._snapshot_pending = self._snapshot_pending, None
        if not self._active or snapshot is None or get_client() is not self._client:
            return GLib.SOURCE_REMOVE
        if snapshot.generation != self._account_generation:
            self.deactivate(clear=True)
            self._message.set_label("Account changed. Reopen Alarm history to continue.")
            return GLib.SOURCE_REMOVE
        ids = {location.location_id for location in snapshot.locations}
        if self._location_id not in ids:
            self._cancel.set()
            self._generation += 1
            self._loading = False
            self._refresh_button.set_sensitive(True)
            self._events.clear()
            self._next_offset = None
            self._set_locations(snapshot, None)
            self._message.set_label("Alarm access or location changed. Refresh to read history.")
            self._empty.set_title("Alarm access or location changed")
        else:
            self._set_locations(snapshot, self._location_id)
        self._render()
        return GLib.SOURCE_REMOVE

    def _location_changed(self, *_args) -> None:
        if self._updating:
            return
        index = self._location.get_selected()
        self._location_id = (
            self._locations[index].location_id if index < len(self._locations) else None
        )
        self._set_devices()
        self._fetch(append=False)

    def _fetch(self, *, append: bool, force: bool = False) -> None:
        if not self._active or self._client is None:
            return
        if append and (self._loading or self._next_offset is None):
            return
        self._cancel.set()
        self._generation += 1
        generation, client = self._generation, self._client
        cancel = self._cancel = threading.Event()
        self._loading = False
        self._refresh_button.set_sensitive(True)
        if not append:
            self._events.clear()
            self._page_signatures.clear()
            self._next_offset = None
        self._render()
        if self._location_id is None:
            self._empty.set_title("No Alarm locations available")
            self._message.set_label("Alarm history requires access to a Ring Alarm location.")
            return
        if not _WORKERS.acquire(blocking=False):
            self._message.set_label("Alarm history is busy. Try Refresh again.")
            return
        self._loading = True
        self._refresh_button.set_sensitive(False)
        self._older.set_sensitive(False)
        self._empty.set_title("Loading Alarm history…")
        self._message.set_label("Reading Ring cloud history…")
        location_id = self._location_id
        offset = self._next_offset if append else 0
        view_ref = weakref.ref(self)

        def worker():
            try:
                page = client.alarm_history(
                    location_id, offset=offset, limit=50, cancel_event=cancel, force=force
                )
                if not isinstance(page, AlarmHistoryPage):
                    page = AlarmHistoryPage(location_id)
            except Exception:
                page = AlarmHistoryPage(location_id)
            finally:
                _WORKERS.release()

            def publish():
                view = view_ref()
                if view is not None:
                    view._finish(page, client, generation, cancel, append)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(publish)

        try:
            threading.Thread(target=worker, daemon=True, name="alarm-history-ui").start()
        except Exception:
            _WORKERS.release()
            self._finish(AlarmHistoryPage(location_id), client, generation, cancel, append)

    def _finish(self, page, client, generation, cancel, append) -> None:
        if (
            not self._active
            or cancel.is_set()
            or generation != self._generation
            or (
                get_client() is not client
                or self._client is not client
                or page.location_id != self._location_id
            )
        ):
            return
        # Revocation can precede its queued snapshot callback. Re-read the
        # authoritative current snapshot at publication, not just at fetch.
        try:
            snapshot = client.get_alarm_snapshot()
        except Exception:
            snapshot = None
        if (
            snapshot is None
            or snapshot.generation != self._account_generation
            or not any(location.location_id == page.location_id for location in snapshot.locations)
        ):
            self.deactivate(clear=True)
            self._message.set_label("Alarm access or account changed. Refresh to continue.")
            self._empty.set_title("Alarm access or account changed")
            return
        self._loading = False
        self._refresh_button.set_sensitive(True)
        signature = tuple(event.key.rsplit(":", 1)[0] for event in page.events)
        repeated = append and signature and signature in self._page_signatures
        no_progress = append and not page.events and page.next_offset is not None
        if repeated or no_progress:
            page = replace(
                page,
                next_offset=None,
                truncated=True,
                status=AlarmHistoryStatus.PARTIAL,
                message="Ring history pagination made no observable progress; stopped. "
                "Coverage is uncertain.",
            )
        if signature:
            self._page_signatures.add(signature)
        if page.status in {
            AlarmHistoryStatus.PERMISSION_DENIED,
            AlarmHistoryStatus.AUTHENTICATION_REQUIRED,
        }:
            self._events.clear()
        elif append:
            if not repeated:
                keys = {event.key for event in self._events}
                self._events.extend(event for event in page.events if event.key not in keys)
        else:
            self._events = list(page.events)
        self._events.sort(
            key=lambda event: (event.occurred_at or datetime.min.replace(tzinfo=UTC), event.key),
            reverse=True,
        )
        self._events = self._events[:1000]
        self._next_offset = page.next_offset if len(self._events) < 1000 else None
        text = page.message
        if page.from_cache:
            text += " Cached results." if not page.stale else " Cached results may be stale."
        if len(self._events) >= 1000:
            text += " Display limit reached."
        self._message.set_label(text)
        self._empty.set_title(
            {
                AlarmHistoryStatus.EMPTY: "No events returned",
                AlarmHistoryStatus.UNSUPPORTED: "Unsupported Alarm history response",
                AlarmHistoryStatus.PERMISSION_DENIED: "No permission for Alarm history",
                AlarmHistoryStatus.AUTHENTICATION_REQUIRED: "Sign in again",
                AlarmHistoryStatus.TIMED_OUT: "Alarm history timed out",
                AlarmHistoryStatus.BUSY: "Alarm history is busy",
            }.get(page.status, "Alarm history unavailable")
        )
        self._render()

    def _render(self) -> None:
        if self._updating:
            return
        while (row := self._rows.get_first_child()) is not None:
            self._rows.remove(row)
        category_index = self._category.get_selected()
        category = (None, "mode", "alarm", "other")[min(category_index, 3)]
        index = self._device.get_selected()
        zid = self._device_ids[index] if index < len(self._device_ids) else None
        events = [
            event
            for event in self._events
            if (category is None or event.kind == category) and (zid is None or event.zid == zid)
        ]
        for event in events:
            when = (
                event.occurred_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                if (event.occurred_at is not None)
                else "Ring timestamp unavailable"
            )
            detail = f"{when} · {event.provenance}"
            if event.actor:
                detail += f" · {event.actor}"
            row = Adw.ActionRow(
                title=event.title, subtitle=detail, use_markup=False, activatable=True
            )
            row._alarm_history_event = event
            self._rows.append(row)
        if self._events and not events:
            self._empty.set_title("No matching Alarm events")
        self._older.set_visible(self._next_offset is not None)
        self._older.set_sensitive(not self._loading)

    def _activate(self, _list, row) -> None:
        event = getattr(row, "_alarm_history_event", None)
        if event is not None and self._on_open_alarm is not None:
            self._on_open_alarm(event)
