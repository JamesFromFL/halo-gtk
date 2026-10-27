"""Location-scoped group evidence and explicit household light actions."""

from __future__ import annotations

import threading
import weakref

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk.chime_details import _info_row, _label, _received_text  # noqa: E402
from halo_gtk.light_group_models import (  # noqa: E402
    ON_DURATIONS,
    LightGroupIntent,
    LightGroupOperationStatus,
    LightGroupPermission,
    LightGroupsReadStatus,
)
from halo_gtk.light_groups import (  # noqa: E402
    INVENTORY_FRESH_SECONDS,
    light_group_monotonic_time,
)
from halo_gtk.ring_client import get_client  # noqa: E402


def _fresh(group):
    now = light_group_monotonic_time()
    return not group.stale and all(
        value is not None and 0 <= now - value <= INVENTORY_FRESH_SECONDS
        for value in (group.inventory_monotonic, group.report_monotonic)
    )


def _freshness(snapshot):
    return (
        _retry_delay(snapshot) == 0,
        tuple(
            (
                location.location_id,
                _retry_delay(location) == 0,
                tuple(
                    (group.group_id, _fresh(group), _retry_delay(group) == 0)
                    for group in location.groups
                ),
            )
            for location in snapshot.locations
        ),
    )


def _report_text(group):
    state = "On" if group.report is True else "Off" if group.report is False else None
    if state is None:
        return "Group report unknown"
    return f"Group reports {state}" if _fresh(group) else f"Last group report: {state} · stale"


def _retry_delay(group):
    return max(0, (group.read_retry_after or 0) - light_group_monotonic_time())


def _write_reason(group):
    if group.operation is not None and group.operation.active:
        return "Wait for the current lighting request to settle."
    if group.permission is LightGroupPermission.DENIED:
        return "Ring denied this group operation. Reload deliberately to check access."
    if not group.membership_complete:
        return "Membership is incomplete. Reload before requesting a group action."
    if not isinstance(group.report, bool):
        return "The group report is unknown. Reload before requesting a group action."
    if not _fresh(group):
        return "Group evidence is stale. Reload before requesting a group action."
    if not group.can_write:
        return "Group control is unavailable. Reload to check current evidence."
    return None


def _operation_text(operation):
    if operation is None:
        return "No request made."
    if operation.status is LightGroupOperationStatus.REPORTED:
        state = "On" if operation.on else "Off"
        return f"Requested group report observed: {state}. This does not prove every lamp changed."
    return {
        LightGroupOperationStatus.PREPARING: "Checking current evidence; not sent yet.",
        LightGroupOperationStatus.POSSIBLY_SENT: (
            "Sending. The request may already have reached Ring."
        ),
        LightGroupOperationStatus.ACCEPTED: "Request accepted. Waiting for a fresh group report.",
        LightGroupOperationStatus.REJECTED: (
            "Request rejected or not sent. Reload before trying again."
        ),
        LightGroupOperationStatus.UNKNOWN: (
            "Outcome unknown. The request may have reached Ring. No automatic retry."
        ),
    }.get(operation.status, "Lighting request status unavailable.")


class _LightViewSession:
    """One subscription, one GTK delivery and a bounded set of view consumers.

    Read futures are cancelled on retirement. Commands remain service-owned;
    cancelling their intent only prevents work that has not been admitted.
    """

    def __init__(self, changed, retired, *, provider=None):
        self._changed = changed
        self._retired = retired
        self._provider = provider or (lambda: get_client())
        self._lock = threading.Lock()
        self.client = None
        self.generation = None
        self.active = False
        self._epoch = 0
        self._futures = {}
        self._completed = {}
        self._intent = None
        self._idle_pending = False
        self._idle_id = None
        self._watch_id = None
        self._freshness = ()

    def current(self):
        client = self.client
        if (
            not self.active
            or client is None
            or self._provider() is not client
            or not client.is_authenticated
        ):
            return None
        snapshot = client.get_light_group_snapshot()
        return snapshot if snapshot.generation == self.generation else None

    def open(self, client) -> bool:
        if self.active and client is self.client and self.current() is not None:
            return False
        self.close()
        snapshot = client.get_light_group_snapshot()
        self.client, self.generation, self.active = client, snapshot.generation, True
        client.add_light_group_callback(self._on_snapshot)
        reference = weakref.ref(self)
        self._watch_id = GLib.timeout_add(250, self._watch, reference)
        self._queue()
        return True

    def close(self) -> None:
        with self._lock:
            self.active = False
            self._epoch += 1
            client, self.client = self.client, None
            reads = [
                future
                for key, future in self._futures.items()
                if key in {"locations", "groups", "details"}
            ]
            intent, self._intent = self._intent, None
            self._futures.clear()
            self._completed.clear()
            idle, self._idle_id = self._idle_id, None
            self._idle_pending = False
            watch, self._watch_id = self._watch_id, None
        if client is not None:
            client.remove_light_group_callback(self._on_snapshot)
        if intent is not None:
            intent.cancel()
            if client is not None:
                client.cancel_light_group_intent(intent)
        for future in reads:
            future.cancel()
        for source in (idle, watch):
            if source is not None:
                GLib.source_remove(source)

    def cancel_read(self, key):
        if key not in {"locations", "groups", "details"}:
            return
        with self._lock:
            future = self._futures.pop(key, None)
            self._completed.pop(key, None)
        if future is not None:
            future.cancel()

    def pending(self, key) -> bool:
        with self._lock:
            return key in self._futures

    def request(self, key, invoke, *, intent=None) -> bool:
        if key not in {"locations", "groups", "details", "command"}:
            return False
        if self.current() is None or self.pending(key):
            return False
        if intent is not None:
            self._intent = intent
        try:
            future = invoke()
        except Exception:
            if intent is not None:
                intent.cancel()
            with self._lock:
                self._completed[key] = None
            self._queue()
            return False
        with self._lock:
            self._futures[key] = future
            epoch = self._epoch
        reference = weakref.ref(self)

        def done(completed):
            owner = reference()
            if owner is None:
                return
            with owner._lock:
                if (
                    not owner.active
                    or owner._epoch != epoch
                    or owner._futures.get(key) is not completed
                ):
                    return
                owner._completed[key] = completed
            owner._queue()

        future.add_done_callback(done)
        self._queue()
        return True

    def _on_snapshot(self, _snapshot) -> None:
        # Do not retain a queued payload containing a retired account's names.
        self._queue()

    def _queue(self) -> None:
        with self._lock:
            if not self.active or self._idle_pending:
                return
            self._idle_pending = True
            epoch = self._epoch
        source = GLib.idle_add(self._deliver, weakref.ref(self), epoch)
        with self._lock:
            if self.active and self._epoch == epoch and self._idle_pending:
                self._idle_id = source

    @staticmethod
    def _deliver(reference, epoch):
        owner = reference()
        if owner is None:
            return False
        with owner._lock:
            if not owner.active or owner._epoch != epoch:
                return False
            owner._idle_pending = False
            owner._idle_id = None
            completed, owner._completed = owner._completed, {}
            for key, future in completed.items():
                if future is not None and owner._futures.get(key) is future:
                    owner._futures.pop(key)
        snapshot = owner.current()
        if snapshot is None:
            owner.close()
            owner._retired()
        else:
            owner._freshness = _freshness(snapshot)
            owner._changed(snapshot, completed)
        return False

    @staticmethod
    def _watch(reference):
        owner = reference()
        if owner is None or not owner.active:
            return False
        snapshot = owner.current()
        if snapshot is None:
            owner._watch_id = None
            owner.close()
            owner._retired()
            return False
        if _freshness(snapshot) != owner._freshness:
            owner._queue()
        return True


class LightGroupDetails(Adw.Dialog):
    """Only the visible selected group is polled; On/Off are explicit actions."""

    def __init__(self, client, location_id, group_id):
        super().__init__(title="Light Group", content_width=480, content_height=700)
        self._location_id, self._group_id = location_id, group_id
        snapshot = client.get_light_group_snapshot()
        group = snapshot.find_group(location_id, group_id)
        location = snapshot.find_location(location_id)
        self._identity_epoch = group.identity_epoch if group else None
        self._location_epoch = location.identity_epoch if location else None
        self._group = group
        self._closed = False
        self._poll_id = None
        self._poll_at = None
        self._session = _LightViewSession(self._snapshot_changed, self._account_retired)
        self._build_ui()
        self.connect("closed", lambda *_: self.retire(close=False))
        self._session.open(client)
        self._read(force=False)

    def _build_ui(self):
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=12,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )
        clamp = Adw.Clamp(maximum_size=540)
        clamp.set_child(content)
        scroll.set_child(clamp)
        toolbar.set_content(scroll)
        self.set_child(toolbar)
        self._name = _label("Light Group", css_classes=["title-2"])
        self._location = _label(css_classes=["dim-label"])
        content.append(self._name)
        content.append(self._location)
        content.append(
            _label(
                "A group report does not establish every lamp's state. "
                "These controls use an experimental Ring integration.",
                css_classes=["dim-label"],
            )
        )
        overview = Adw.PreferencesGroup(title="Reported evidence")
        self._report = _info_row("Group report")
        self._report_received = _info_row("Report received")
        self._inventory_received = _info_row("Membership received")
        self._membership = _info_row("Members")
        for row in (
            self._report,
            self._report_received,
            self._inventory_received,
            self._membership,
        ):
            overview.add(row)
        content.append(overview)
        self._permission = _label(css_classes=["dim-label"])
        content.append(self._permission)
        controls = Adw.PreferencesGroup(title="Group controls")
        duration_row = _info_row("Requested On duration")
        self._duration = Gtk.DropDown.new_from_strings(["1 minute", "5 minutes", "15 minutes"])
        self._duration.set_valign(Gtk.Align.CENTER)
        self._duration.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Requested Light Group On duration"]
        )
        duration_row.add_suffix(self._duration)
        controls.add(duration_row)
        content.append(controls)
        content.append(
            _label(
                "Duration stays local until On. Its expiry does not prove the lights are Off.",
                css_classes=["dim-label"],
            )
        )
        self._on = Gtk.Button(label="Turn group On")
        self._on.add_css_class("suggested-action")
        self._on.connect("clicked", lambda *_: self._command(True))
        self._off = Gtk.Button(label="Turn group Off now")
        self._off.connect("clicked", lambda *_: self._command(False))
        self._reload = Gtk.Button(label="Reload group report")
        self._reload.connect("clicked", lambda *_: self._read(force=True))
        for button in (self._on, self._off, self._reload):
            content.append(button)
        self._reason = _label(css_classes=["dim-label"])
        self._operation = _label()
        self._message = _label()
        self._poll_status = _label(css_classes=["dim-label"])
        for label in (self._reason, self._operation, self._message, self._poll_status):
            content.append(label)
        content.append(
            _label(
                "Off now does not disable motion rules, schedules or linked-device actions. "
                "They may turn the lights on again. "
                "No automatic retry or later Off command is sent.",
                css_classes=["dim-label"],
            )
        )
        self._members = Adw.PreferencesGroup(title="Reported members")
        self._member_rows = []
        content.append(self._members)
        self._clear_text()

    def _clear_text(self):
        self._name.set_label("Light Group unavailable")
        self._location.set_label("")
        for row in (
            self._report,
            self._report_received,
            self._inventory_received,
            self._membership,
        ):
            row.set_subtitle("Unknown")
        for label in (
            self._permission,
            self._reason,
            self._operation,
            self._message,
            self._poll_status,
        ):
            label.set_label("")
        for button in (self._on, self._off, self._reload, self._duration):
            button.set_sensitive(False)
        self._duration.set_selected(0)
        for row in self._member_rows:
            self._members.remove(row)
        self._member_rows.clear()

    def _stop_poll(self):
        source, self._poll_id = self._poll_id, None
        self._poll_at = None
        if source is not None:
            GLib.source_remove(source)

    def retire(self, *, close=True):
        if self._closed:
            return
        self._closed = True
        self._stop_poll()
        self._session.close()
        self._group = None
        self._clear_text()
        if close:
            self.force_close()

    def _account_retired(self):
        self.retire()

    def _selected(self):
        snapshot = self._session.current()
        group = snapshot.find_group(self._location_id, self._group_id) if snapshot else None
        location = snapshot.find_location(self._location_id) if snapshot else None
        if (
            group is None
            or location is None
            or group.identity_epoch != self._identity_epoch
            or location.identity_epoch != self._location_epoch
        ):
            self.retire()
            return None
        return group

    def _snapshot_changed(self, snapshot, completed):
        if self._closed:
            return
        group = self._selected()
        if group is None:
            return
        self._group = group
        self._name.set_label(group.name)
        self._location.set_label(snapshot.find_location(self._location_id).name)
        self._report.set_subtitle(_report_text(group))
        self._report_received.set_subtitle(_received_text(group.report_received_at))
        self._inventory_received.set_subtitle(_received_text(group.inventory_received_at))
        count = group.member_count
        self._membership.set_subtitle(
            "Unknown; membership incomplete" if count is None else f"{count} reported members"
        )
        self._permission.set_label(
            "Ring denied this group's operation; access needs a deliberate Reload."
            if group.permission is LightGroupPermission.DENIED
            else "Write permission is not reported. Ring decides each deliberate manual request."
        )
        self._operation.set_label(_operation_text(group.operation))
        for key, future in completed.items():
            try:
                if future is None:
                    raise RuntimeError
                result = future.result()
            except Exception:
                self._message.set_label(
                    "Lighting request unavailable. Its outcome may be unknown if already sent."
                )
            else:
                if key == "details":
                    location = snapshot.find_location(self._location_id)
                    failed = (
                        result.status is not LightGroupsReadStatus.READY
                        or not result.complete
                        or location is None
                        or not location.complete
                        or location.status is not LightGroupsReadStatus.READY
                        or group.status is not LightGroupsReadStatus.READY
                        or group.code is not None
                        or group.stale
                    )
                    self._message.set_label(
                        "Group refresh failed; last-reported evidence may be stale."
                        if failed
                        else "Showing Ring's last reported evidence; "
                        "physical member states are not inferred."
                    )
                elif key == "command" and result.status is LightGroupOperationStatus.REJECTED:
                    self._message.set_label(
                        "Request rejected or not sent. Reload before trying again."
                    )
        self._render_members(group)
        self._update_actions()
        self._arm_poll()

    def _render_members(self, group):
        for row in self._member_rows:
            self._members.remove(row)
        self._member_rows.clear()
        self._members.set_description(
            "Reported names and types only; individual light states are not inferred."
        )
        for member in group.members:
            row = _info_row(member.name)
            row.set_subtitle(member.kind)
            self._members.add(row)
            self._member_rows.append(row)

    def _update_actions(self):
        group = self._group
        if group is None:
            return
        busy = self._session.pending("command")
        reason = _write_reason(group)
        self._reason.set_label(
            reason or "On and Off send a request for this logical group only when clicked."
        )
        for button in (self._on, self._off):
            button.set_sensitive(reason is None and not busy)
        self._duration.set_sensitive(reason is None and not busy)
        self._reload.set_sensitive(
            not self._session.pending("details") and _retry_delay(group) == 0
        )
        self._poll_status.set_label(
            "Automatic updates paused. Use Reload to check access."
            if not group.poll_allowed
            else "Updates wait for the request cooldown."
            if _retry_delay(group) > 0
            else "This visible group is checked no more often than every 15 seconds."
        )

    def _arm_poll(self):
        group = self._selected()
        if group is None:
            return
        if not group.poll_allowed:
            self._stop_poll()
            return
        deadline = max(light_group_monotonic_time() + 15, group.read_retry_after or 0)
        if self._poll_id is not None:
            if self._poll_at is not None and self._poll_at >= (group.read_retry_after or 0):
                return
            self._stop_poll()
        self._poll_at = deadline
        self._poll_id = GLib.timeout_add(
            max(15000, int((deadline - light_group_monotonic_time()) * 1000)),
            self._poll,
            weakref.ref(self),
        )

    @staticmethod
    def _poll(reference):
        owner = reference()
        if owner is None or owner._closed:
            return False
        owner._poll_id = owner._poll_at = None
        group = owner._selected()
        if group is None or not group.poll_allowed:
            return False
        if (
            _retry_delay(group) > 0
            or owner._session.pending("details")
            or (group.operation and group.operation.active)
        ):
            owner._arm_poll()
            return False
        owner._read(force=False)
        return False

    def _read(self, *, force):
        group = self._selected()
        if group is None or _retry_delay(group) > 0:
            return
        self._stop_poll()
        client = self._session.client
        self._session.request(
            "details",
            lambda: client.refresh_light_group_details(
                self._location_id, self._group_id, force=force
            ),
        )
        self._update_actions()

    def _command(self, on):
        group = self._selected()
        if group is None or _write_reason(group) is not None or self._session.pending("command"):
            return
        selected = self._duration.get_selected()
        if on and selected >= len(ON_DURATIONS):
            return
        duration = ON_DURATIONS[selected] if on else None
        intent = LightGroupIntent(
            self._session.generation,
            self._location_id,
            self._group_id,
            group.revision,
            group.identity_epoch,
        )
        client = self._session.client
        self._message.set_label("")
        self._session.request(
            "command",
            lambda: client.request_light_group_lights(
                self._location_id,
                self._group_id,
                on,
                duration=duration,
                expected_revision=group.revision,
                intent_id=intent,
            ),
            intent=intent,
        )
        self._update_actions()
