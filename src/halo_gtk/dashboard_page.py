"""Overview: cached security status, camera previews and recent live events."""

from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk, Pango  # noqa: E402

from halo_gtk import device_names  # noqa: E402
from halo_gtk.overview_state import security_summary  # noqa: E402
from halo_gtk.ring_client import get_client  # noqa: E402
from halo_gtk.ring_events import normalized_event_kind  # noqa: E402


class DashboardPage(Gtk.ScrolledWindow):
    """Overview uses existing caches and callbacks; it never polls Ring history."""

    def __init__(self, *, camera_grid=None, on_navigate=None, on_open_event=None) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        self._camera_grid = camera_grid
        self._on_navigate = on_navigate
        self._on_open_event = on_open_event
        self._client = None
        self._callback_generation = 0
        self._alarm_callback = None
        self._event_callback = None
        self._account_client = None
        self._events = []
        self._active = False
        self._inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self._inner.add_css_class("page-scroll-content")
        self.set_child(Adw.Clamp(maximum_size=1240, tightening_threshold=1000, child=self._inner))
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading.append(Gtk.Label(label="Your home", xalign=0, css_classes=["halo-page-heading"]))
        self._subtitle = Gtk.Label(xalign=0, wrap=True, css_classes=["halo-page-subtitle"])
        heading.append(self._subtitle)
        self._inner.append(heading)

        summary = Gtk.Box(spacing=18, css_classes=["halo-card", "halo-overview-summary"])
        self._status_icon = Gtk.Image(
            icon_name="security-high-symbolic",
            pixel_size=30,
            css_classes=["halo-overview-icon"],
            valign=Gtk.Align.CENTER,
        )
        summary.append(self._status_icon)
        status_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self._status_title = Gtk.Label(xalign=0, wrap=True, css_classes=["title-2"])
        self._status_detail = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        status_text.append(self._status_title)
        status_text.append(self._status_detail)
        summary.append(status_text)
        status_button = Gtk.Button(
            icon_name="go-next-symbolic",
            tooltip_text="Manage security",
            valign=Gtk.Align.CENTER,
            css_classes=["flat"],
        )
        status_button.connect("clicked", lambda *_: self._navigate("alarm"))
        summary.append(status_button)
        self._inner.append(summary)
        self._inner.append(self._section("Your cameras", "Cameras", "cameras"))
        if camera_grid is not None:
            self._inner.append(camera_grid)

        lower = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            row_spacing=22,
            column_spacing=24,
            min_children_per_line=1,
            max_children_per_line=2,
            valign=Gtk.Align.START,
        )
        recent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        recent.set_size_request(260, -1)
        recent.append(self._section("Recent activity", "View all", "history"))
        self._events_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        recent.append(self._events_list)
        self._render_events()
        lower.append(recent)
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        controls.set_size_request(260, -1)
        controls.append(
            Gtk.Label(
                label="Quick access",
                xalign=0,
                height_request=34,
                css_classes=["halo-section-heading"],
            )
        )
        actions = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for title, detail, icon, destination in (
            (
                "Lights and devices",
                "Chimes, lighting groups and connected devices",
                "preferences-system-symbolic",
                "devices",
            ),
            (
                "Security",
                "Alarm mode, sensors and reported health",
                "security-high-symbolic",
                "alarm",
            ),
            (
                "Live monitoring",
                "Choose cameras and a saved layout",
                "camera-video-symbolic",
                "cameras",
            ),
        ):
            row = Adw.ActionRow(title=title, subtitle=detail, activatable=True)
            row.set_title_lines(1)
            row.set_subtitle_lines(2)
            row.add_prefix(Gtk.Image(icon_name=icon, css_classes=["event-symbol"]))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _, target=destination: self._navigate(target))
            actions.append(row)
        controls.append(actions)
        lower.append(controls)
        self._inner.append(lower)

    def _section(self, title, action, destination):
        row = Gtk.Box(spacing=12)
        row.append(
            Gtk.Label(
                label=title,
                xalign=0,
                hexpand=True,
                ellipsize=Pango.EllipsizeMode.END,
                css_classes=["halo-section-heading"],
            )
        )
        button = Gtk.Button(label=action, css_classes=["flat"])
        button.connect("clicked", lambda *_: self._navigate(destination))
        row.append(button)
        return row

    def _navigate(self, destination):
        if self._on_navigate is not None:
            self._on_navigate(destination)

    def _bind_client(self, client):
        if client is self._client:
            return
        self._callback_generation += 1
        generation = self._callback_generation
        if self._client is not None:
            self._client.remove_alarm_callback(self._alarm_callback)
            self._client.remove_event_callback(self._event_callback)
        self._client = client
        self._alarm_callback = None
        self._event_callback = None
        if client is not None:

            def on_alarm(snapshot):
                if generation == self._callback_generation and self._client is client:
                    self._on_alarm(snapshot)

            def on_event(event):
                if generation == self._callback_generation and self._client is client:
                    self._on_event(event)

            self._alarm_callback = on_alarm
            self._event_callback = on_event
            client.add_alarm_callback(on_alarm)
            client.add_event_callback(on_event)

    def refresh(self):
        self._active = True
        client = get_client()
        signed_in = client is not None and client.is_authenticated
        client = client if signed_in else None
        if client is not self._account_client:
            self._events.clear()
            self._account_client = client
            self._render_events()
        self._bind_client(client)
        count = len(client.all_devices) if client else 0
        self._subtitle.set_label(
            f"{count} Ring devices · Cameras, activity and security at a glance"
            if client
            else "Cameras, activity and security in one place"
        )
        self._show_security(client.get_alarm_snapshot() if client else None, signed_in)
        if self._camera_grid is not None:
            self._camera_grid.refresh()

    def _show_security(self, snapshot, signed_in):
        state = security_summary(snapshot, signed_in=signed_in)
        self._status_title.set_label(state.title)
        self._status_detail.set_label(state.detail)
        for css in ("success", "warning", "error"):
            self._status_icon.remove_css_class(css)
        if state.severity != "neutral":
            self._status_icon.add_css_class(state.severity)

    def _on_alarm(self, snapshot):
        if self._active and self._client is not None and self._client is get_client():
            self._show_security(snapshot, True)

    def _on_event(self, event):
        if not self._active or self._client is None or self._client is not get_client():
            return
        kind = normalized_event_kind(event)
        if kind not in {"ding", "motion"}:
            return

        def field(key):
            return event.get(key) if isinstance(event, dict) else getattr(event, key, None)

        try:
            device_id = int(field("doorbot_id"))
        except (ValueError, TypeError):
            return
        device = self._client.get_device_by_id(device_id)
        if device is None:
            return
        event_id = field("id")
        try:
            event_id = int(event_id) if event_id is not None else None
        except (ValueError, TypeError):
            event_id = None
        item = (
            device_id,
            event_id,
            "Doorbell rang" if kind == "ding" else "Motion detected",
            device_names.display_name(device),
            datetime.now().astimezone().strftime("%H:%M"),
        )
        if event_id is not None:
            self._events = [old for old in self._events if old[:2] != item[:2]]
        self._events.insert(0, item)
        del self._events[3:]
        self._render_events()

    def _render_events(self):
        while child := self._events_list.get_first_child():
            self._events_list.remove(child)
        if not self._events:
            row = Adw.ActionRow(
                title="No new activity in this view",
                subtitle="Open Activity for your full event history.",
                activatable=True,
            )
            row.set_subtitle_lines(2)
            row.connect("activated", lambda *_: self._navigate("history"))
            self._events_list.append(row)
            return
        for device_id, event_id, title, name, received in self._events:
            row = Adw.ActionRow(
                title=title,
                subtitle=f"{name} · Received at {received}",
                activatable=True,
                use_markup=False,
            )
            row.set_title_lines(1)
            row.set_subtitle_lines(2)
            row.add_prefix(
                Gtk.Image(icon_name="camera-video-symbolic", css_classes=["event-symbol"])
            )
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect(
                "activated", lambda _, did=device_id, eid=event_id: self._open_event(did, eid)
            )
            self._events_list.append(row)

    def _open_event(self, device_id, event_id):
        if event_id is not None and self._on_open_event is not None:
            self._on_open_event(device_id, event_id)
        else:
            self._navigate("history")

    def on_page_hidden(self):
        self._active = False
        self._bind_client(None)

    def do_unroot(self):
        self.on_page_hidden()
        Gtk.ScrolledWindow.do_unroot(self)
