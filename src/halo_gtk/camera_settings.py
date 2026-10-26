"""Per-camera configurable Ring settings."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402
from ring_doorbell.exceptions import RingTimeout  # noqa: E402

from halo_gtk import device_names  # noqa: E402
from halo_gtk.camera_info import CameraInfoContent  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402

_log = logging.getLogger(__name__)

_CHIME_TYPES = ("Mechanical", "Digital", "Not Present")
_SETTING_TIMEOUT = 20.0
_setting_queues_lock = threading.Lock()
_setting_queues: dict[tuple, _SettingQueue] = {}


@dataclass
class _SettingRequest:
    key: str | None
    invoke: Callable[[], Any]
    current: Callable[[], bool]
    wanted: Callable[[], bool]
    finished: Callable[[str | None, bool], None]


class _SettingQueue:
    """One in-flight write per device/session, including after a wait timeout.

    Only callers explicitly providing an idempotent setting key may replace an
    unsent value. Never cancel/retry a submitted remote write on a UI timeout.
    """

    def __init__(self, key: tuple, client) -> None:
        self.key = key
        self.client = client
        self.pending: deque[_SettingRequest] = deque()

    @classmethod
    def enqueue(cls, key: tuple, client, request: _SettingRequest) -> None:
        replaced = []
        with _setting_queues_lock:
            queue = _setting_queues.get(key)
            start = queue is None
            if queue is None:
                queue = cls(key, client)
                _setting_queues[key] = queue
            if request.key is not None:
                replaced = [waiting for waiting in queue.pending if waiting.key == request.key]
                queue.pending = deque(
                    waiting for waiting in queue.pending if waiting.key != request.key
                )
                queue.pending.append(request)
            else:
                queue.pending.append(request)
        for waiting in replaced:
            if waiting.current() and waiting.wanted():
                waiting.finished("Not sent; a newer pending value replaced this change.", False)
        if start:
            try:
                threading.Thread(target=queue._run, daemon=True).start()
            except Exception as exc:
                _log.debug("Could not start camera setting worker: %s", exc)
                with _setting_queues_lock:
                    _setting_queues.pop(key, None)
                    waiting = list(queue.pending)
                    queue.pending.clear()
                for queued in waiting:
                    if queued.current() and queued.wanted():
                        queued.finished(
                            "Not sent; the save worker could not start. Try again.", False
                        )

    async def _invoke(self, request: _SettingRequest):
        if not request.current() or not request.wanted():
            raise RuntimeError("The Ring session or settings window changed")
        return await request.invoke()

    def _run(self) -> None:
        while True:
            with _setting_queues_lock:
                if not self.pending:
                    _setting_queues.pop(self.key, None)
                    return
                request = self.pending.popleft()
            if not request.current() or not request.wanted():
                continue
            try:
                command = self._invoke(request)
                try:
                    future = self.client.submit(command)
                except BaseException:
                    command.close()
                    raise
                try:
                    future.result(timeout=_SETTING_TIMEOUT)
                except FutureTimeout:
                    if future.done():
                        # Completion can race the local wait deadline.
                        future.result()
                    else:
                        request.finished(
                            "Ring has not confirmed this change. Waiting for its outcome.", True
                        )
                        # Hold subsequent writes until the actual request resolves.
                        # Closing a window must not admit overlapping writes.
                        while request.current():
                            try:
                                future.result(timeout=0.25)
                                break
                            except FutureTimeout:
                                if future.done():
                                    future.result()
                                    break
                        else:
                            continue
                if request.current():
                    request.finished(None, False)
            except Exception as exc:
                _log.debug("Camera setting write failed: %s", exc)
                if request.current():
                    uncertain = isinstance(exc, TimeoutError | RingTimeout)
                    request.finished(
                        "Ring timed out; the change may have been applied. "
                        "Check the camera before trying again."
                        if uncertain
                        else "Ring could not confirm this change. "
                        "Check the connection and camera, then try again.",
                        uncertain,
                    )
                    if uncertain:
                        with _setting_queues_lock:
                            waiting = list(self.pending)
                            self.pending.clear()
                        for queued in waiting:
                            if queued.current() and queued.wanted():
                                queued.finished(
                                    "Not sent because an earlier change has an uncertain outcome. "
                                    "Check the camera before trying again.",
                                    False,
                                )


@dataclass(frozen=True)
class CameraSetting:
    """Description of a setting that applies to a Ring camera-like device."""

    key: str
    title: str
    description: str


def camera_setting_specs(device: Any) -> list[CameraSetting]:
    """Return configurable settings supported by *device* through ring-doorbell."""
    specs: list[CameraSetting] = []
    if device_names.device_key(device) is not None:
        specs.append(
            CameraSetting(
                "nickname",
                "Device Nickname",
                "Halo-only name used throughout the application.",
            )
        )
    if _supports_motion_detection(device):
        specs.append(
            CameraSetting(
                "motion_detection",
                "Motion Detection",
                "Ring motion detection is enabled for this camera.",
            )
        )

    if _is_doorbell(device):
        if callable(getattr(device, "async_set_volume", None)):
            specs.append(
                CameraSetting(
                    "doorbell_volume",
                    "Doorbell Volume",
                    "Volume used by this Ring doorbell.",
                )
            )
        chime_type = _safe_getattr(device, "existing_doorbell_type")
        if chime_type in _CHIME_TYPES and callable(
            getattr(device, "async_set_existing_doorbell_type", None)
        ):
            specs.append(
                CameraSetting(
                    "chime_type",
                    "In-Home Chime Type",
                    "Existing in-home chime wiring connected to this doorbell.",
                )
            )
        if chime_type in {"Mechanical", "Digital"} and callable(
            getattr(device, "async_set_existing_doorbell_type_enabled", None)
        ):
            specs.append(
                CameraSetting(
                    "chime_enabled",
                    "In-Home Chime",
                    "Ring may ring the existing in-home chime.",
                )
            )
        if chime_type == "Digital" and callable(
            getattr(device, "async_set_existing_doorbell_type_duration", None)
        ):
            specs.append(
                CameraSetting(
                    "chime_duration",
                    "Digital Chime Duration",
                    "How long the digital in-home chime rings.",
                )
            )

    return specs


class CameraSettingsWindow(Adw.ApplicationWindow):
    """Non-modal settings window for one Ring camera or doorbell."""

    def __init__(
        self,
        device: Any,
        *,
        on_nickname_changed: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            title="Camera Settings",
            default_width=560,
            default_height=620,
            **kwargs,
        )
        self.set_size_request(500, 420)
        self.device = device
        self._on_nickname_changed = on_nickname_changed
        self._loading = False
        self._closed = False
        self._setting_serial = 0
        self._status_row: Adw.ActionRow | None = None
        self.connect("close-request", self._on_close_request)
        self._build_ui()
        self._show_settings()

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        self._header = Adw.HeaderBar()
        toolbar_view.add_top_bar(self._header)

        self._back_btn = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text="Back to Camera Settings",
            visible=False,
        )
        self._back_btn.connect("clicked", lambda *_: self._show_settings())
        self._header.pack_start(self._back_btn)

        self._info_btn = Gtk.Button(
            icon_name="dialog-information-symbolic",
            tooltip_text="Device Info",
        )
        self._info_btn.connect("clicked", lambda *_: self._show_device_info())
        self._header.pack_end(self._info_btn)

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
            hexpand=True,
            vexpand=True,
        )
        toolbar_view.set_content(self._stack)

    def _show_settings(self) -> None:
        self.set_title("Camera Settings")
        self._back_btn.set_visible(False)
        self._info_btn.set_visible(True)
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=14,
            margin_bottom=14,
            margin_start=14,
            margin_end=14,
        )
        scrolled.set_child(content)

        overview = Adw.PreferencesGroup(title=device_names.display_name(self.device, "Camera"))
        model = _safe_getattr(self.device, "model") or "Ring camera"
        overview.add(_info_row("Device", model))
        overview.add(_info_row("Ring Device Name", device_names.ring_name(self.device)))
        content.append(overview)

        specs = camera_setting_specs(self.device)
        if specs:
            group = Adw.PreferencesGroup(title="Settings")
            self._loading = True
            for spec in specs:
                row = self._build_setting_row(spec)
                if row is not None:
                    group.add(row)
            self._loading = False
            content.append(group)
        else:
            status = Adw.StatusPage(
                icon_name="preferences-system-symbolic",
                title="No configurable camera settings",
                description="Ring does not expose editable settings for this device through Halo.",
                vexpand=True,
            )
            content.append(status)

        status_group = Adw.PreferencesGroup(title="Status")
        self._status_row = _info_row("Last Change", "No changes made")
        status_group.add(self._status_row)
        content.append(status_group)

        self._set_stack_child(scrolled, "settings")

    def _show_device_info(self) -> None:
        self.set_title("Device Info")
        self._back_btn.set_visible(True)
        self._info_btn.set_visible(False)

        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scrolled.set_child(CameraInfoContent(self.device))
        self._set_stack_child(scrolled, "info")

    def _set_stack_child(self, widget: Gtk.Widget, name: str) -> None:
        old = self._stack.get_child_by_name(name)
        if old is not None:
            self._stack.remove(old)
        self._stack.add_named(widget, name)
        self._stack.set_visible_child_name(name)

    def _build_setting_row(self, spec: CameraSetting) -> Gtk.Widget | None:
        if spec.key == "nickname":
            row = Adw.ActionRow(title=spec.title, subtitle=spec.description)
            entry = Gtk.Entry(
                text=device_names.get_nickname(self.device),
                placeholder_text=device_names.ring_name(self.device),
                hexpand=True,
                valign=Gtk.Align.CENTER,
            )
            entry.connect("activate", self._on_nickname_activate)
            row.add_suffix(entry)
            save_btn = Gtk.Button(
                icon_name="document-save-symbolic",
                tooltip_text="Save nickname",
                css_classes=["flat"],
                valign=Gtk.Align.CENTER,
            )
            save_btn.connect("clicked", self._on_nickname_save_clicked, entry)
            row.add_suffix(save_btn)
            reset_btn = Gtk.Button(
                icon_name="edit-clear-symbolic",
                tooltip_text="Clear nickname",
                css_classes=["flat"],
                valign=Gtk.Align.CENTER,
            )
            reset_btn.connect("clicked", self._on_nickname_reset, entry)
            row.add_suffix(reset_btn)
            row.set_activatable_widget(entry)
            return row
        if spec.key == "motion_detection":
            row = Adw.SwitchRow(title=spec.title, subtitle=spec.description)
            row.set_active(bool(_safe_getattr(self.device, "motion_detection")))
            row.connect("notify::active", self._on_motion_detection_changed)
            return row
        if spec.key == "doorbell_volume":
            return self._spin_row(
                spec,
                int(_safe_getattr(self.device, "volume") or 0),
                0,
                11,
                self._on_volume_changed,
            )
        if spec.key == "chime_type":
            row = Adw.ActionRow(title=spec.title, subtitle=spec.description)
            dropdown = Gtk.DropDown.new_from_strings(list(_CHIME_TYPES))
            selected = _chime_type_index(_safe_getattr(self.device, "existing_doorbell_type"))
            dropdown.set_selected(selected)
            dropdown.connect("notify::selected", self._on_chime_type_changed)
            row.add_suffix(dropdown)
            row.set_activatable_widget(dropdown)
            return row
        if spec.key == "chime_enabled":
            row = Adw.SwitchRow(title=spec.title, subtitle=spec.description)
            row.set_active(bool(_safe_getattr(self.device, "existing_doorbell_type_enabled")))
            row.connect("notify::active", self._on_chime_enabled_changed)
            return row
        if spec.key == "chime_duration":
            return self._spin_row(
                spec,
                int(_safe_getattr(self.device, "existing_doorbell_type_duration") or 0),
                0,
                10,
                self._on_chime_duration_changed,
            )
        return None

    def _spin_row(
        self,
        spec: CameraSetting,
        value: int,
        lower: int,
        upper: int,
        callback: Callable[[Gtk.SpinButton], None],
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=spec.title, subtitle=spec.description)
        adjustment = Gtk.Adjustment(
            value=value,
            lower=lower,
            upper=upper,
            step_increment=1,
            page_increment=1,
        )
        spin = Gtk.SpinButton(adjustment=adjustment, numeric=True, valign=Gtk.Align.CENTER)
        spin.connect("value-changed", callback)
        row.add_suffix(spin)
        row.set_activatable_widget(spin)
        return row

    def _on_motion_detection_changed(self, row: Adw.SwitchRow, _param) -> None:
        if not self._loading:
            value = row.get_active()
            self._apply_setting(
                "Motion Detection",
                lambda: self.device.async_set_motion_detection(value),
                lambda: _set_nested_attr(
                    self.device,
                    ("settings", "motion_detection_enabled"),
                    value,
                ),
                setting_key="motion_detection",
            )

    def _on_nickname_activate(self, entry: Gtk.Entry) -> None:
        self._save_nickname(entry.get_text())

    def _on_nickname_save_clicked(self, _button: Gtk.Button, entry: Gtk.Entry) -> None:
        self._save_nickname(entry.get_text())

    def _on_nickname_reset(self, _button: Gtk.Button, entry: Gtk.Entry) -> None:
        entry.set_text("")
        self._save_nickname("")

    def _save_nickname(self, value: str) -> None:
        try:
            device_names.set_nickname(self.device, value)
        except Exception as exc:
            self._set_status(f"Device Nickname failed: {exc}")
            return
        if self._on_nickname_changed is not None:
            self._on_nickname_changed()
        self._show_settings()
        self._set_status("Device Nickname saved" if value.strip() else "Device Nickname cleared")

    def _on_volume_changed(self, spin: Gtk.SpinButton) -> None:
        if not self._loading:
            value = int(spin.get_value())
            self._apply_setting(
                "Doorbell Volume",
                lambda: self.device.async_set_volume(value),
                lambda: _set_nested_attr(self.device, ("settings", "doorbell_volume"), value),
                setting_key="doorbell_volume",
            )

    def _on_chime_type_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if not self._loading:
            selected = dropdown.get_selected()
            value = int(selected)
            label = _CHIME_TYPES[value]
            self._apply_setting(
                "In-Home Chime Type",
                lambda: self.device.async_set_existing_doorbell_type(value),
                lambda: _set_nested_attr(
                    self.device, ("settings", "chime_settings", "type"), value
                ),
                refresh_settings=True,
                success_detail=f"Saved: {label}",
                setting_key="chime_type",
            )

    def _on_chime_enabled_changed(self, row: Adw.SwitchRow, _param) -> None:
        if not self._loading:
            value = row.get_active()
            self._apply_setting(
                "In-Home Chime",
                lambda: self.device.async_set_existing_doorbell_type_enabled(value),
                lambda: _set_nested_attr(
                    self.device,
                    ("settings", "chime_settings", "enable"),
                    value,
                ),
                setting_key="chime_enabled",
            )

    def _on_chime_duration_changed(self, spin: Gtk.SpinButton) -> None:
        if not self._loading:
            value = int(spin.get_value())
            self._apply_setting(
                "Digital Chime Duration",
                lambda: self.device.async_set_existing_doorbell_type_duration(value),
                lambda: _set_nested_attr(
                    self.device, ("settings", "chime_settings", "duration"), value
                ),
                setting_key="chime_duration",
            )

    def _apply_setting(
        self,
        title: str,
        coroutine_factory: Callable[[], Any],
        local_update: Callable[[], None],
        *,
        refresh_settings: bool = False,
        success_detail: str | None = None,
        setting_key: str | None = None,
    ) -> None:
        client = get_client()
        if client is None:
            self._set_status(f"{title} was not saved: not signed in to Ring")
            return
        generation = getattr(client, "_published_generation", None)
        self._setting_serial += 1
        serial = self._setting_serial
        self._set_status(f"Waiting to save {title}...")

        def current() -> bool:
            return (
                get_client() is client
                and getattr(client, "_published_generation", None) == generation
                and not getattr(client, "_stopped", False)
            )

        def finished(error: str | None, uncertain: bool) -> None:
            GLib.idle_add(
                self._finish_queued_setting,
                serial,
                current,
                title,
                error,
                uncertain,
                local_update,
                refresh_settings,
                success_detail,
            )

        key = (id(client), generation, device_names.device_key(self.device) or id(self.device))
        _SettingQueue.enqueue(
            key,
            client,
            _SettingRequest(
                setting_key,
                coroutine_factory,
                current,
                lambda: not self._closed,
                finished,
            ),
        )

    def _finish_queued_setting(
        self,
        serial,
        current,
        title,
        error,
        uncertain,
        local_update,
        refresh_settings,
        success_detail,
    ) -> bool:
        if self._closed or not current():
            return GLib.SOURCE_REMOVE
        if error is None:
            try:
                local_update()
            except Exception as exc:
                _log.debug("Ring saved a setting but the local display could not refresh: %s", exc)
                error = (
                    "Ring saved the change, but the local display could not refresh. "
                    "Reopen settings."
                )
        # An old request must not replace a newer pending/error status or
        # rebuild controls while a later edit is still queued.
        if uncertain:
            self._set_status(f"{title}: {error} No automatic retry.")
            return GLib.SOURCE_REMOVE
        if serial != self._setting_serial:
            return GLib.SOURCE_REMOVE
        return self._finish_apply(title, error, refresh_settings, success_detail)

    def _on_close_request(self, *_args) -> bool:
        self._closed = True
        return False

    def _finish_apply(
        self,
        title: str,
        error: str | None,
        refresh_settings: bool,
        success_detail: str | None,
    ) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        if error:
            if error.startswith("Not sent"):
                self._set_status(f"{title}: {error}")
            else:
                self._set_status(f"{title} could not be confirmed: {error}")
        else:
            if refresh_settings:
                self._show_settings()
            self._set_status(success_detail or f"{title} saved")
        return GLib.SOURCE_REMOVE

    def _set_status(self, text: str) -> None:
        if self._status_row is not None:
            self._status_row.set_subtitle(GLib.markup_escape_text(text))


def _supports_motion_detection(device: Any) -> bool:
    setter = getattr(device, "async_set_motion_detection", None)
    if not callable(setter):
        return False
    checker = getattr(device, "has_capability", None)
    if not callable(checker):
        return True
    try:
        return bool(checker("motion_detection"))
    except Exception:
        return True


def _is_doorbell(device: Any) -> bool:
    family = _safe_getattr(device, "family")
    return family in {"doorbots", "authorized_doorbots"}


def _chime_type_index(value: Any) -> int:
    try:
        return _CHIME_TYPES.index(value)
    except ValueError:
        return _CHIME_TYPES.index("Not Present")


def _safe_getattr(device: Any, name: str) -> Any:
    try:
        return getattr(device, name, None)
    except Exception:
        return None


def _set_nested_attr(device: Any, path: tuple[str, ...], value: Any) -> None:
    attrs = getattr(device, "_attrs", None)
    if not isinstance(attrs, dict):
        return
    cursor = attrs
    for key in path[:-1]:
        next_value = cursor.setdefault(key, {})
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[key] = next_value
        cursor = next_value
    cursor[path[-1]] = value


def _info_row(title: str, value: Any) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    row.set_subtitle(GLib.markup_escape_text(_format_value(value)))
    return row


def _format_value(value: Any) -> str:
    if value is None:
        return "Unknown"
    text = str(value)
    return text if text else "Unknown"
