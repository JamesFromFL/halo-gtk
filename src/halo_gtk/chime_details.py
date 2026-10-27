"""Literal, adaptive Chime details and bounded view-owned asynchronous reads."""

from __future__ import annotations

import math
from datetime import datetime

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk  # noqa: E402
from gi.repository import GLib as GLib  # noqa: E402

from halo_gtk.chime import (  # noqa: E402
    HEALTH_CACHE_SECONDS,
    INVENTORY_FRESH_SECONDS,
    ChimeConnectionStatus,
    ChimeIntent,
    ChimeOperationStatus,
    ChimeReadStatus,
    chime_monotonic_time,
)
from halo_gtk.device_view_session import DeviceViewSession  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402


def _fresh(device) -> bool:
    received = device.inventory_monotonic
    return (
        not device.stale
        and received is not None
        and 0 <= chime_monotonic_time() - received <= INVENTORY_FRESH_SECONDS
    )


def _connection_text(device) -> str:
    label = {
        ChimeConnectionStatus.ONLINE: "Reported Online",
        ChimeConnectionStatus.OFFLINE: "Reported Offline",
    }.get(device.connection, "Connection unknown")
    return label + (" · stale" if not _fresh(device) else "")


def _health_fresh(health) -> bool:
    return (
        health is not None
        and not health.stale
        and health.observed_monotonic is not None
        and 0 <= chime_monotonic_time() - health.observed_monotonic <= HEALTH_CACHE_SECONDS
    )


def _freshness(snapshot):
    return tuple((d.api_id, _fresh(d), _health_fresh(d.health)) for d in snapshot.devices)


def _volume_text(value) -> str:
    return (
        f"Legacy Ring level {value} of 11"
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 11
        else "Unknown"
    )


def _received_text(value) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        return "Not available"
    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%x %X %Z")
    except (ValueError, OverflowError, OSError):
        return "Not available"


def _write_reason(device) -> str | None:
    if device.sound_operation_id is not None:
        return "Check the physical Chime, then acknowledge when the sound has stopped."
    if device.operation is not None and device.operation.active:
        return "Wait for the current Chime request to settle."
    reasons = {
        "unsupported-model": "This model's Chime controls have not been validated.",
        "unsupported-kind": "This model's Chime controls have not been validated.",
        "permission-unverified": "Chime control permission has not been verified for this account.",
        "ownership-unverified": "Chime control permission has not been verified for this account.",
        "permission-denied": "Ring denied this Chime operation for this account.",
        "volume-unknown": "Ring has not reported a valid legacy volume. Reload to check.",
        "unsupported-volume": "Ring has not reported a valid legacy volume. Reload to check.",
        "offline": "Ring reports this Chime offline.",
        "not-online": "Ring has not reported this Chime Online.",
        "connection-unknown": "Ring has not reported this Chime Online.",
        "refresh-required": "Reload to obtain current Chime status before making changes.",
        "stale": "Reload to obtain current Chime status before making changes.",
    }
    if not device.can_write:
        return reasons.get(device.unavailable_reason, "Legacy Chime controls are unavailable.")
    if not _fresh(device):
        return "Reload to obtain current Chime status before making changes."
    return None


def _operation_text(operation) -> str:
    if operation is None:
        return "No request made."
    if operation.status is ChimeOperationStatus.REPORTED:
        return f"Ring now reports legacy level {operation.requested_volume}."
    if operation.status is ChimeOperationStatus.ACCEPTED:
        return (
            "Sound request accepted. Halo cannot confirm that it played or finished."
            if operation.kind == "test-sound"
            else "Request accepted. Waiting for Ring to report the volume."
        )
    return {
        ChimeOperationStatus.PREPARING: "Checking current Chime status; not sent yet.",
        ChimeOperationStatus.POSSIBLY_SENT: "Sending. The request may already have reached Ring.",
        ChimeOperationStatus.REJECTED: "Request rejected or not sent. Reload before trying again.",
        ChimeOperationStatus.UNKNOWN: (
            "Outcome unknown. The request may have reached Ring. No automatic retry."
        ),
    }.get(operation.status, "Chime request status unavailable.")


class _ChimeViewSession(DeviceViewSession):
    snapshot_freshness = staticmethod(_freshness)

    def __init__(self, changed, retired, *, provider=None):
        super().__init__(changed, retired, provider=provider or (lambda: get_client()))


def _label(text="", **kwargs):
    return Gtk.Label(label=text, wrap=True, xalign=0, use_markup=False, **kwargs)


def _info_row(title):
    row = Adw.ActionRow(title=title, use_markup=False)
    row.set_subtitle_lines(0)
    return row


class ChimeDetails(Adw.Dialog):
    """A parented dialog; all writes require an explicit deliberate action."""

    def __init__(self, client, api_id: int):
        super().__init__(title="Chime details", content_width=480, content_height=700)
        self._api_id = api_id
        self._device = client.get_chime_snapshot().find_device(api_id)
        self._identity_epoch = self._device.identity_epoch if self._device is not None else None
        self._draft = self._device.volume if self._device is not None else None
        self._draft_revision = self._device.revision if self._device is not None else None
        self._updating = False
        self._closed = False
        self._reset_draft = False
        self._session = _ChimeViewSession(self._snapshot_changed, self._account_retired)
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
        self._name = _label("Chime", css_classes=["title-2"])
        content.append(self._name)
        content.append(
            _label(
                "Experimental legacy Chime controls. "
                "Compatibility has not been verified on your hardware.",
                css_classes=["dim-label"],
            )
        )
        self._info = Adw.PreferencesGroup(title="Reported details")
        self._rows = {
            key: _info_row(title)
            for key, title in (
                ("model", "Model"),
                ("connection", "Connection"),
                ("received", "Inventory received"),
                ("firmware", "Firmware"),
                ("snooze", "Snooze"),
                ("health", "Wi-Fi signal"),
            )
        }
        for row in self._rows.values():
            self._info.add(row)
        content.append(self._info)
        self._message = _label()
        content.append(self._message)
        group = Adw.PreferencesGroup(
            title="Legacy volume",
            description=(
                "One Ring level from 0 to 11; not separate Ring and Motion volume. "
                "This controls the household Chime, not your PC speaker."
            ),
        )
        self._reported = _info_row("Reported level")
        group.add(self._reported)
        draft_row = _info_row("Local draft")
        self._volume = Gtk.SpinButton.new_with_range(0, 11, 1)
        self._volume.set_digits(0)
        self._volume.set_numeric(True)
        self._volume.set_valign(Gtk.Align.CENTER)
        self._volume.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Local legacy Chime volume, 0 to 11"]
        )
        self._volume.connect("value-changed", self._draft_changed)
        draft_row.add_suffix(self._volume)
        group.add(draft_row)
        content.append(group)
        controls = Gtk.Box(spacing=8, homogeneous=True)
        self._apply = Gtk.Button(label="Apply volume")
        self._apply.add_css_class("suggested-action")
        self._apply.connect("clicked", self._apply_volume)
        self._reload_button = Gtk.Button(label="Reload")
        self._reload_button.connect("clicked", lambda *_: self._reload(force=True))
        controls.append(self._apply)
        controls.append(self._reload_button)
        content.append(controls)
        self._reason = _label(css_classes=["dim-label"])
        content.append(self._reason)
        content.append(
            _label(
                "Play a sound on this physical Chime using its current tone and volume. "
                "Snooze may affect what you hear. Wait until any sound has stopped "
                "before changing settings.",
                css_classes=["dim-label"],
            )
        )
        self._test_buttons = {}
        for kind, title in (("ding", "Play doorbell sound"), ("motion", "Play motion sound")):
            button = Gtk.Button(label=title)
            button.connect("clicked", self._test_sound, kind)
            content.append(button)
            self._test_buttons[kind] = button
        self._operation = _label()
        content.append(self._operation)
        self._ack = Gtk.Button(label="The sound has stopped")
        self._ack.connect("clicked", self._acknowledge)
        content.append(self._ack)
        content.append(
            _label(
                "This acknowledgment only clears Halo's local sound gate after you check "
                "the device. Halo cannot confirm that a sound played or finished.",
                css_classes=["caption", "dim-label"],
            )
        )
        content.append(
            _label(
                "Linked device details are unavailable: their response format has not been "
                "validated. Manage links, tones and other settings in the Ring app.",
                css_classes=["dim-label"],
            )
        )
        self._clear_text()

    def _clear_text(self):
        self._name.set_label("Chime unavailable")
        for row in self._rows.values():
            row.set_subtitle("Unknown")
        self._reported.set_subtitle("Unknown")
        self._volume.set_visible(False)
        self._updating = True
        self._volume.set_value(0)
        self._updating = False
        self._apply.set_sensitive(False)
        self._reload_button.set_sensitive(False)
        for button in self._test_buttons.values():
            button.set_sensitive(False)
        self._ack.set_visible(False)
        self._reason.set_label("")
        self._operation.set_label("")
        self._message.set_label("")

    def retire(self, *, close=True):
        if self._closed:
            return
        self._closed = True
        self._session.close()
        self._device = self._draft = self._draft_revision = None
        self._clear_text()
        if close:
            self.force_close()

    def _account_retired(self):
        self.retire()

    def _selected(self):
        snapshot = self._session.current()
        device = snapshot.find_device(self._api_id) if snapshot is not None else None
        if device is None or device.identity_epoch != self._identity_epoch:
            self.retire()
            return None
        return device

    def _snapshot_changed(self, snapshot, completed):
        if self._closed:
            return
        device = self._selected()
        if device is None:
            return
        self._device = device
        for key, future in completed.items():
            try:
                if future is None:
                    raise RuntimeError
                result = future.result()
            except Exception:
                self._message.set_label(
                    "Chime request unavailable. Reload to check current status."
                )
            else:
                if key == "details":
                    if self._reset_draft:
                        self._draft, self._draft_revision = device.volume, device.revision
                        self._reset_draft = False
                    failed = (
                        result.status
                        in {ChimeReadStatus.ERROR, ChimeReadStatus.STALE, ChimeReadStatus.CLOSED}
                        or result.code is not None
                        or (
                            device.health is not None
                            and device.health.status
                            in {ChimeReadStatus.ERROR, ChimeReadStatus.STALE}
                        )
                    )
                    self._message.set_label(
                        "Chime reload failed. Last-reported values may be stale."
                        if failed
                        else "Ring's reported details do not prove the speaker is reachable."
                    )
                elif key == "command" and result.status is ChimeOperationStatus.REJECTED:
                    self._message.set_label(
                        "Request rejected or not sent. Reload before trying again."
                    )
        self._name.set_label(device.name)
        self._rows["model"].set_subtitle(device.model)
        self._rows["connection"].set_subtitle(_connection_text(device))
        self._rows["received"].set_subtitle(_received_text(device.inventory_received_at))
        self._rows["firmware"].set_subtitle(device.firmware or "Unknown")
        seconds = device.snooze_seconds_left
        self._rows["snooze"].set_subtitle(
            "Unknown"
            if seconds is None
            else f"Last reported {seconds} seconds remaining; not a playback timer"
        )
        health = device.health
        health_text = "Not reported"
        if health is not None:
            signal = health.wifi_signal_strength
            health_text = "Unknown" if signal is None else f"Reported RSSI {signal}"
            if health.wifi_signal_category:
                health_text += f" · {health.wifi_signal_category}"
            health_text += f" · received {_received_text(health.observed_at)}"
            if not _health_fresh(health):
                health_text += " · stale"
        self._rows["health"].set_subtitle(health_text)
        self._reported.set_subtitle(_volume_text(device.volume))
        if self._draft is None or self._draft == device.volume:
            self._draft, self._draft_revision = device.volume, device.revision
        self._updating = True
        if self._draft is not None:
            self._volume.set_value(self._draft)
        self._updating = False
        self._operation.set_label(_operation_text(device.operation))
        self._update_actions()

    def _update_actions(self):
        device = self._device
        if device is None:
            return
        reason = _write_reason(device)
        busy = self._session.pending("command") or self._session.pending("ack")
        draft_stale = self._draft_revision != device.revision
        self._reason.set_label(
            reason
            or (
                "Settings changed. Reload before applying your draft."
                if draft_stale
                else "Edits stay local until Apply. Level 0 is not timed snooze."
            )
        )
        self._volume.set_visible(self._draft is not None)
        self._volume.set_sensitive(reason is None and not busy)
        self._apply.set_sensitive(
            reason is None and not busy and not draft_stale and self._draft != device.volume
        )
        self._reload_button.set_sensitive(not self._session.pending("details"))
        for button in self._test_buttons.values():
            button.set_sensitive(reason is None and not busy)
        gate = device.sound_operation_id is not None
        self._ack.set_visible(gate)
        self._ack.set_sensitive(
            gate and not busy and not (device.operation and device.operation.active)
        )

    def _draft_changed(self, button):
        if not self._updating and not self._closed:
            self._draft = button.get_value_as_int()
            self._update_actions()

    def _reload(self, *, force):
        device = self._selected()
        if device is None:
            return
        self._draft, self._draft_revision = device.volume, device.revision
        self._reset_draft = force
        client = self._session.client
        self._session.request(
            "details", lambda: client.refresh_chime_details(self._api_id, force=force)
        )
        self._update_actions()

    def _command(self, *, value=None, kind=None):
        device = self._selected()
        if (
            device is None
            or _write_reason(device) is not None
            or self._session.pending("command")
            or self._session.pending("ack")
        ):
            return
        if value is not None and self._draft_revision != device.revision:
            return
        intent = ChimeIntent(
            self._session.generation, device.api_id, device.revision, device.identity_epoch
        )
        client = self._session.client
        if kind is None:

            def invoke():
                return client.request_chime_volume(
                    device.api_id, value, expected_revision=device.revision, intent_id=intent
                )
        else:

            def invoke():
                return client.request_chime_test_sound(
                    device.api_id, kind, expected_revision=device.revision, intent_id=intent
                )

        self._message.set_label("")
        self._session.request("command", invoke, intent=intent)
        self._update_actions()

    def _apply_volume(self, *_):
        if (
            self._draft is not None
            and self._device is not None
            and self._draft != self._device.volume
        ):
            self._command(value=self._draft)

    def _test_sound(self, _button, kind):
        if kind in {"ding", "motion"}:
            self._command(kind=kind)

    def _acknowledge(self, *_):
        device = self._selected()
        if (
            device is None
            or device.sound_operation_id is None
            or (device.operation and device.operation.active)
        ):
            return
        client = self._session.client
        self._session.request(
            "ack",
            lambda: client.acknowledge_chime_sound_finished(
                device.api_id, device.sound_operation_id
            ),
        )
        self._update_actions()
