"""Literal Intercom evidence and explicit, expiring entrance confirmation."""

from __future__ import annotations

import weakref

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from halo_gtk.chime_details import _info_row, _label, _received_text  # noqa: E402
from halo_gtk.device_view_session import DeviceViewSession  # noqa: E402
from halo_gtk.intercom import (  # noqa: E402
    FRESH_SECONDS,
    IntercomIntent,
    IntercomOperationStatus,
    light_group_monotonic_time,
)
from halo_gtk.ring_client import get_client  # noqa: E402


def _fresh(device):
    return (
        not device.stale
        and device.inventory_monotonic is not None
        and (0 <= light_group_monotonic_time() - device.inventory_monotonic <= FRESH_SECONDS)
    )


def _retry(snapshot):
    return max(0, (snapshot.read_retry_after or 0) - light_group_monotonic_time())


def _freshness(snapshot):
    return (_retry(snapshot) == 0, tuple((d.api_id, _fresh(d)) for d in snapshot.devices))


class IntercomViewSession(DeviceViewSession):
    snapshot_method = "get_intercom_snapshot"
    add_method = "add_intercom_callback"
    remove_method = "remove_intercom_callback"
    cancel_method = "cancel_intercom_intent"
    snapshot_freshness = staticmethod(_freshness)

    def __init__(self, changed, retired, *, provider=None):
        super().__init__(changed, retired, provider=provider or (lambda: get_client()))


def _operation_text(operation):
    if operation is None:
        return "No entrance release requested."
    return {
        IntercomOperationStatus.PREPARING: "Checking your approval; not sent yet.",
        IntercomOperationStatus.POSSIBLY_SENT: "Sending. The request may have reached Ring.",
        IntercomOperationStatus.ACCEPTED: (
            "Ring accepted the release request. Check the entrance; "
            "Halo cannot verify that it opened."
        ),
        IntercomOperationStatus.REJECTED: (
            "Request rejected or not sent. Reload before trying again."
        ),
        IntercomOperationStatus.UNKNOWN: (
            "Outcome unknown. The entrance may have released. Check it before another request."
        ),
    }[operation.status]


def _write_reason(device):
    if device.acknowledgment_id is not None:
        return "Check the entrance and acknowledge this request before requesting another release."
    if device.operation and device.operation.active:
        return "Wait for this request to settle."
    if device.denied:
        return "Ring denied this operation. Reload deliberately to check access."
    if not _fresh(device):
        return "Reload current Intercom evidence before requesting release."
    if not device.can_write:
        return "Entrance release is unavailable. Reload to check access or wait for the cooldown."
    return None


class IntercomDetails(Adw.Dialog):
    def __init__(self, client, api_id):
        super().__init__(title="Intercom details", content_width=480, content_height=700)
        self._api_id = api_id
        self._device = client.get_intercom_snapshot().find_device(api_id)
        self._epoch = self._device.identity_epoch if self._device else None
        self._closed = False
        self._confirmation = None
        self._approval = None
        self._poll_id = None
        self._session = IntercomViewSession(self._changed, self.retire)
        self._build_ui()
        self.connect("closed", lambda *_: self.retire(close=False))
        self._session.open(client)
        self._reload(force=False)

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
        self._name = _label("Intercom", css_classes=["title-2"])
        content.append(self._name)
        overview = Adw.PreferencesGroup(title="Reported evidence")
        self._rows = {
            key: _info_row(title)
            for key, title in (
                ("model", "Model"),
                ("connection", "Connection"),
                ("battery", "Battery"),
                ("firmware", "Firmware"),
                ("ring_to_open", "Buzz before release"),
                ("received", "Inventory received"),
                ("door", "Physical lock / door state"),
            )
        }
        for row in self._rows.values():
            overview.add(row)
        content.append(overview)
        content.append(
            _label(
                "Access permission is decided by Ring for each manual request. "
                "This integration is experimental. "
                "Some entrances release only after a visitor buzzes.",
                css_classes=["dim-label"],
            )
        )
        self._release = Gtk.Button(label="Request entrance release…")
        self._release.add_css_class("destructive-action")
        self._release.connect("clicked", lambda *_: self._confirm())
        self._ack = Gtk.Button(label="I checked the entrance and understand the result")
        self._ack.get_child().set_wrap(True)
        self._ack.get_child().set_max_width_chars(26)
        self._ack.connect("clicked", lambda *_: self._acknowledge())
        self._reload_button = Gtk.Button(label="Reload Intercom status")
        self._reload_button.connect("clicked", lambda *_: self._reload(force=True))
        self._reason = _label()
        self._operation = _label()
        self._message = _label()
        for child in (
            self._release,
            self._ack,
            self._reload_button,
            self._reason,
            self._operation,
            self._message,
        ):
            content.append(child)
        content.append(
            _label(
                "Release is a momentary request, not a lock toggle. Halo does not "
                "automatically release, retry, relock or infer door position. "
                "Calling, audio/video, "
                "guest keys and setup remain in the Ring app.",
                css_classes=["dim-label"],
            )
        )
        self._events = Adw.PreferencesGroup(
            title="Recent live observations",
            description="This session only. "
            "Missing events do not establish that no calls occurred.",
        )
        self._event_rows = []
        content.append(self._events)

    def _selected(self):
        snapshot = self._session.current()
        device = snapshot.find_device(self._api_id) if snapshot else None
        if device is None or device.identity_epoch != self._epoch:
            self.retire()
            return None
        return device

    def _cancel_confirmation(self):
        if self._approval is not None:
            self._approval.cancel()
            self._approval = None
        dialog, self._confirmation = self._confirmation, None
        if dialog is not None:
            dialog.force_close()

    def retire(self, *, close=True):
        if self._closed:
            return
        self._closed = True
        self._cancel_confirmation()
        self._session.close()
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        self._name.set_label("Intercom unavailable")
        for row in self._rows.values():
            row.set_subtitle("Unknown")
        for label in (self._reason, self._operation, self._message):
            label.set_label("")
        for row in self._event_rows:
            self._events.remove(row)
        self._event_rows.clear()
        for button in (self._release, self._ack, self._reload_button):
            button.set_sensitive(False)
        self._device = None
        if close:
            self.force_close()

    def _changed(self, snapshot, completed):
        if self._closed:
            return
        device = self._selected()
        if device is None:
            return
        if self._approval is not None and (
            device.revision != self._approval.revision
            or not _fresh(device)
            or light_group_monotonic_time() - self._approval.created_monotonic >= 30
        ):
            self._cancel_confirmation()
        self._device = device
        self._name.set_label(device.name)
        fields = {
            "model": device.model,
            "connection": "Reported " + device.connection,
            "battery": "Unknown" if device.battery is None else f"Reported {device.battery}%",
            "firmware": device.firmware or "Unknown",
            "ring_to_open": "Required by reported configuration"
            if device.ring_to_open is True
            else "Not required by reported configuration"
            if device.ring_to_open is False
            else "Unknown",
            "received": _received_text(device.inventory_received_at),
            "door": "Unknown; not reported",
        }
        for key, value in fields.items():
            self._rows[key].set_subtitle(
                value
                + (" · stale" if not _fresh(device) and key not in {"door", "received"} else "")
            )
        self._reason.set_label(_write_reason(device) or "Requesting release requires confirmation.")
        self._operation.set_label(_operation_text(device.operation))
        self._release.set_sensitive(
            _write_reason(device) is None
            and not self._session.pending("command")
            and self._confirmation is None
        )
        self._ack.set_visible(device.acknowledgment_id is not None)
        self._ack.set_sensitive(not self._session.pending("ack"))
        self._reload_button.set_sensitive(
            _retry(snapshot) == 0 and not self._session.pending("inventory")
        )
        for key, future in completed.items():
            try:
                if future is None:
                    raise RuntimeError
                result = future.result()
                if key == "inventory" and not result.complete:
                    raise RuntimeError
            except Exception:
                self._message.set_label(
                    "Request unavailable. A release may have reached Ring if already sent."
                )
            else:
                self._message.set_label(
                    "Updated reported evidence."
                    if key == "inventory"
                    else "Local acknowledgment recorded."
                    if key == "ack" and result
                    else ""
                )
        for row in self._event_rows:
            self._events.remove(row)
        self._event_rows = []
        for event in device.observations:
            row = _info_row("Call reported" if event.kind == "ding" else "Unlock event reported")
            row.set_subtitle("Received " + _received_text(event.received_at))
            self._events.add(row)
            self._event_rows.append(row)
        self._arm_poll(snapshot)

    def _arm_poll(self, snapshot):
        if (
            self._poll_id is not None
            or not snapshot.poll_allowed
            or (self._device is not None and self._device.denied)
        ):
            return
        delay = max(15, _retry(snapshot))
        self._poll_id = GLib.timeout_add(int(delay * 1000), self._poll, weakref.ref(self))

    @staticmethod
    def _poll(reference):
        owner = reference()
        if owner is None or owner._closed:
            return False
        owner._poll_id = None
        snapshot = owner._session.current()
        if snapshot is None:
            owner.retire()
        elif snapshot.poll_allowed and not (owner._device and owner._device.denied):
            if not owner._session.pending("inventory") and not owner._session.pending("command"):
                owner._reload(force=False)
            owner._arm_poll(snapshot)
        return False

    def _reload(self, *, force):
        snapshot = self._session.current()
        if snapshot is None or _retry(snapshot) > 0:
            return
        self._session.request(
            "inventory", lambda: self._session.client.refresh_intercoms(force=force)
        )

    def _confirm(self):
        device = self._selected()
        if (
            device is None
            or _write_reason(device) is not None
            or self._confirmation is not None
            or self._session.pending("command")
        ):
            return
        intent = IntercomIntent(
            self._session.generation,
            device.api_id,
            device.revision,
            device.identity_epoch,
            created_monotonic=light_group_monotonic_time(),
        )
        self._approval = intent
        dialog = Adw.AlertDialog(
            heading="Release this entrance?",
            body=f"Request a momentary entrance release through {device.name}? "
            "Only proceed when you intend to grant access. Halo cannot verify who is outside "
            "or whether the entrance opens. This confirmation expires after 30 seconds.",
        )
        dialog.set_body_use_markup(False)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("release", "Release entrance")
        dialog.set_response_appearance("release", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        self._confirmation = dialog
        dialog.connect("response", self._confirmed, intent)
        self._release.set_sensitive(False)
        dialog.present(self)

    def _confirmed(self, dialog, response, intent):
        if self._confirmation is not dialog or self._approval is not intent:
            intent.cancel()
            return
        self._confirmation = self._approval = None
        device = self._selected()
        if (
            response != "release"
            or intent.cancelled
            or device is None
            or device.revision != intent.revision
            or _write_reason(device) is not None
            or not 0 <= light_group_monotonic_time() - intent.created_monotonic < 30
        ):
            intent.cancel()
            if device is not None:
                self._changed(self._session.current(), {})
            return
        client = self._session.client
        self._session.request(
            "command",
            lambda: client.request_intercom_unlock(
                device.api_id, expected_revision=intent.revision, intent_id=intent
            ),
            intent=intent,
        )

    def _acknowledge(self):
        device = self._selected()
        if device is None or device.acknowledgment_id is None:
            return
        client = self._session.client
        self._session.request(
            "ack",
            lambda: client.acknowledge_intercom_request(device.api_id, device.acknowledgment_id),
        )
