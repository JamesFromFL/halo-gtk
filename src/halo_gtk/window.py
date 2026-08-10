"""Main application window — Halo console layout.

Left sidebar: primary product areas.
Right content: Gtk.Stack switching between dashboard, cameras, events, devices, and alarm.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gtk  # noqa: E402

from halo_gtk import APP_ID, device_names  # noqa: E402
from halo_gtk.alarm_page import AlarmPage  # noqa: E402
from halo_gtk.cameras_page import CamerasPage  # noqa: E402
from halo_gtk.dashboard_page import DashboardPage  # noqa: E402
from halo_gtk.devices_page import DevicesPage  # noqa: E402
from halo_gtk.focused_live_page import FocusedLivePage  # noqa: E402
from halo_gtk.history_page import HistoryPage  # noqa: E402
from halo_gtk.live_sessions import get_live_session_manager  # noqa: E402
from halo_gtk.ring_client import (  # noqa: E402
    LISTENER_CONNECTED,
    LISTENER_CONNECTING,
    add_connection_state_callback,
    get_client,
    listener_state,
    remove_connection_state_callback,
)
from halo_gtk.ui_style import install_ui_style  # noqa: E402

_log = logging.getLogger(__name__)

# Navigation entries: (page_name, label, icon_name)
_NAV_ITEMS = [
    ("dashboard", "Dashboard", "view-grid-symbolic"),
    ("cameras", "Live Monitoring", "camera-video-symbolic"),
    ("history", "Event History", "document-open-recent-symbolic"),
    ("devices", "Devices", "preferences-system-devices-symbolic"),
    ("alarm", "Alarm", "security-high-symbolic"),
]


_PAGE_LABELS: dict[str, str] = {
    "dashboard": "Dashboard",
    "cameras": "Live Monitoring",
    "history": "Event History",
    "devices": "Devices",
    "alarm": "Alarm",
    "focused_live": "Live",
}


class RingWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            title="Halo",
            default_width=1360,
            default_height=880,
            **kwargs,
        )
        # Absolute minimum the app will attempt to render into.  Tiled
        # Wayland compositors (e.g. Hyprland) can force any size; setting a
        # size_request gives GTK a floor so it never allocates zero pixels
        # to widgets.
        self.set_size_request(360, 480)
        self.connect("close-request", self._on_close_request)
        self._build_ui()
        add_connection_state_callback(self._on_listener_state)
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        install_ui_style()

        # Full-height navigation ribbon which becomes an overlay on narrow windows.
        self._split_view = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.START,
            min_sidebar_width=218,
            max_sidebar_width=248,
            sidebar_width_fraction=0.20,
            collapsed=False,
            show_sidebar=True,
        )
        self._split_view.connect("notify::collapsed", self._on_collapsed_changed)
        self._split_view.connect("notify::show-sidebar", self._on_sidebar_visibility_changed)
        self.set_content(self._split_view)

        content_toolbar = Adw.ToolbarView()

        self._content_header = Adw.HeaderBar()
        self._header_title = Adw.WindowTitle(title="Dashboard")
        self._content_header.set_title_widget(self._header_title)

        self._sidebar_toggle_btn = Gtk.Button(
            icon_name="sidebar-show-symbolic",
            tooltip_text="Show navigation",
            visible=False,
        )
        self._sidebar_toggle_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Show navigation"],
        )
        self._sidebar_toggle_btn.connect("clicked", self._on_sidebar_toggle_clicked)
        self._content_header.pack_start(self._sidebar_toggle_btn)

        self._back_btn = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text="Back",
            sensitive=False,
            visible=False,
        )
        self._back_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Back"],
        )
        self._back_btn.connect("clicked", self._on_back_clicked)
        self._content_header.pack_start(self._back_btn)

        menu_btn = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            tooltip_text="Main menu",
            menu_model=self._build_menu(),
        )
        menu_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Main menu"],
        )
        self._content_header.pack_end(menu_btn)
        content_toolbar.add_top_bar(self._content_header)

        self._banner = Adw.Banner(title="Not signed in to Ring", button_label="Sign In")
        self._banner.connect("button-clicked", self._on_sign_in)
        content_toolbar.add_top_bar(self._banner)

        self._events_banner = Adw.Banner(title="Reconnecting to Ring events…")
        self._events_banner.set_revealed(False)
        content_toolbar.add_top_bar(self._events_banner)

        sidebar_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            width_request=218,
        )
        sidebar_box.add_css_class("navigation-ribbon")

        brand = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            valign=Gtk.Align.CENTER,
        )
        brand.add_css_class("ribbon-brand")
        self._brand_icon = self._make_sidebar_logo(64)
        brand.append(self._brand_icon)
        brand_name = Gtk.Label(label="Halo", xalign=0, hexpand=True)
        brand_name.add_css_class("title-3")
        brand.append(brand_name)
        sidebar_box.append(brand)
        sidebar_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._nav_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            activate_on_single_click=True,
        )
        self._nav_list.add_css_class("navigation-sidebar")
        self._nav_selected_handler_id = self._nav_list.connect(
            "row-activated",
            self._on_nav_selected,
        )
        sidebar_box.append(self._nav_list)

        self._nav_rows: dict[str, Gtk.ListBoxRow] = {}
        for name, label, icon in _NAV_ITEMS:
            row = self._make_nav_row(label, icon)
            self._nav_list.append(row)
            self._nav_rows[name] = row

        sidebar_box.append(Gtk.Box(vexpand=True))

        settings_btn = Gtk.Button(css_classes=["flat"])
        settings_btn.add_css_class("ribbon-settings")
        settings_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        settings_content.append(Gtk.Image.new_from_icon_name("preferences-system-symbolic"))
        settings_content.append(Gtk.Label(label="Settings", xalign=0, hexpand=True))
        settings_btn.set_child(settings_content)
        settings_btn.set_tooltip_text("Open Settings")
        settings_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Open Settings"],
        )
        settings_btn.set_action_name("app.settings")
        sidebar_box.append(settings_btn)

        self._split_view.set_sidebar(sidebar_box)

        self._content_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            hhomogeneous=False,
            vhomogeneous=False,
            hexpand=True,
            vexpand=True,
        )
        content_toolbar.set_content(self._content_stack)
        self._split_view.set_content(content_toolbar)

        self._dashboard_cameras_page = CamerasPage(
            on_open_live_focus=self._open_focused_live,
        )
        self._dashboard_page = DashboardPage(camera_grid=self._dashboard_cameras_page)
        self._content_stack.add_named(self._dashboard_page, "dashboard")

        self._cameras_page = CamerasPage(
            on_open_live_focus=self._open_focused_live,
            on_open_history=self.open_history,
            show_monitoring_controls=True,
            grid_size_config_key="live_monitoring_grid_size",
            camera_order_config_key="live_monitoring_camera_order",
        )
        self._content_stack.add_named(self._cameras_page, "cameras")

        self._focused_live_page = FocusedLivePage(
            on_history=self.open_history,
            on_nickname_changed=self._refresh_camera_names,
        )
        self._content_stack.add_named(self._focused_live_page, "focused_live")

        self._history_page = HistoryPage(on_title_change=self._on_history_title_change)
        self._content_stack.add_named(self._history_page, "history")

        self._devices_page = DevicesPage(on_show_cameras=lambda: self._show_page("dashboard"))
        self._content_stack.add_named(self._devices_page, "devices")

        self._alarm_page = AlarmPage()
        self._content_stack.add_named(self._alarm_page, "alarm")

        # Default to Dashboard.
        self._active_page_name = "dashboard"
        self._focused_source_page: str | None = None
        self._page_history: list[str] = []
        self._nav_list.select_row(self._nav_rows["dashboard"])
        self._sync_sidebar_buttons()
        self._update_back_button()

        compact = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 900px"),
        )
        compact.add_setter(self._split_view, "collapsed", True)
        compact.add_setter(self._sidebar_toggle_btn, "visible", True)
        compact.add_setter(self._history_page.split_view, "collapsed", True)
        self.add_breakpoint(compact)

    def _make_nav_row(self, label: str, icon: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True)
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=11,
        )
        box.add_css_class("ribbon-row")
        box.append(Gtk.Image(icon_name=icon))
        box.append(Gtk.Label(label=label, halign=Gtk.Align.START, hexpand=True))
        row.set_child(box)
        return row

    @staticmethod
    def _make_sidebar_logo(pixel_size: int) -> Gtk.Picture:
        """Load the app logo for the full-height sidebar."""
        icon_path = Path(__file__).resolve().parent / "assets" / "icons" / "apps" / f"{APP_ID}.png"
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(icon_path),
            pixel_size,
            pixel_size,
            True,
        )
        icon = Gtk.Picture(
            paintable=Gdk.Texture.new_for_pixbuf(pixbuf),
            content_fit=Gtk.ContentFit.CONTAIN,
            can_shrink=False,
        )
        icon.set_size_request(pixel_size, pixel_size)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        return icon

    def _build_menu(self):
        from gi.repository import Gio

        menu = Gio.Menu()
        menu.append("Settings", "app.settings")
        menu.append("About", "app.about")
        menu.append("Quit", "app.quit")
        return menu

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _update_title(self, page: str, sub: str | None = None) -> None:
        """Update the window title (breadcrumb) and header bar label."""
        page_label = _PAGE_LABELS.get(page, page)
        if sub is not None:
            self._header_title.set_title(sub)
            self.set_title(f"Halo \u2022 {page_label} \u2022 {sub}")
        elif page == "dashboard":
            self._header_title.set_title(page_label)
            self.set_title("Halo")
        else:
            self._header_title.set_title(page_label)
            self.set_title(f"Halo \u2022 {page_label}")

    def _on_history_title_change(self, name: str | None) -> None:
        if getattr(self, "_active_page_name", None) == "history":
            self._update_title("history", name)

    def _on_nav_selected(self, list_box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        for name, nav_row in self._nav_rows.items():
            if nav_row is row:
                self._show_page(name)
                break

    def _show_page(self, name: str, *, record_history: bool = True) -> None:
        previous_page = self._active_page_name
        returning_to_monitoring = (
            previous_page == "focused_live"
            and name == "cameras"
            and self._focused_source_page == "cameras"
        )
        if record_history and previous_page != name and previous_page != "focused_live":
            self._page_history.append(previous_page)

        if previous_page == "focused_live" and name != "focused_live":
            self._leave_focused_live(name)
        if previous_page == "dashboard" and name != "dashboard":
            self._dashboard_cameras_page.on_page_hidden()
        elif previous_page == "cameras" and name != "cameras":
            self._cameras_page.on_page_hidden()
        elif previous_page == "history" and name != "history":
            self._history_page.on_page_hidden()

        self._active_page_name = name
        if name in self._nav_rows:
            self._select_nav_row(name)
        else:
            self._clear_nav_selection()
        self._content_stack.set_visible_child_name(name)
        self._update_title(name)
        if name == "dashboard":
            self._dashboard_page.refresh()
        elif name == "cameras":
            if returning_to_monitoring:
                self._cameras_page.resume_after_focused()
            else:
                self._cameras_page.refresh()
        elif name == "history":
            self._history_page.refresh()
        elif name == "devices":
            self._devices_page.refresh()
        elif name == "alarm":
            self._alarm_page.refresh()
        split_view = getattr(self, "_split_view", None)
        if split_view is not None and split_view.get_collapsed():
            split_view.set_show_sidebar(False)
        self._update_back_button()

    def _on_back_clicked(self, *_args) -> None:
        if not self._page_history:
            return
        previous_page = self._page_history.pop()
        self._show_page(previous_page, record_history=False)

    def _update_back_button(self) -> None:
        can_go_back = bool(self._page_history)
        self._back_btn.set_sensitive(can_go_back)
        self._back_btn.set_visible(can_go_back)

    def open_history(self, device_id: int | None = None) -> None:
        """Show Event History, optionally filtered to one camera."""
        self._enter_history_page(lambda: self._history_page.refresh(filter_device_id=device_id))

    def open_history_event(self, device_id: int, event_id: int) -> None:
        """Show Event History and play a specific event."""
        self._enter_history_page(lambda: self._history_page.show_event_by_id(device_id, event_id))

    def _enter_history_page(self, populate) -> None:
        """Switch to Event History (with page-history bookkeeping) and populate it."""
        if self._active_page_name != "history":
            append_history = True
            if self._active_page_name == "dashboard":
                self._dashboard_cameras_page.on_page_hidden()
            elif self._active_page_name == "cameras":
                self._cameras_page.on_page_hidden()
            elif self._active_page_name == "focused_live":
                self._leave_focused_live("history")
                append_history = False
            if append_history:
                self._page_history.append(self._active_page_name)
        self._select_nav_row("history")
        self._active_page_name = "history"
        self._content_stack.set_visible_child_name("history")
        self._update_title("history")
        populate()
        self._update_back_button()

    def open_camera_live(self, device_id: int) -> bool:
        """Show focused live monitoring for a device."""
        client = get_client()
        device = client.get_device_by_id(device_id) if client is not None else None
        if device is None:
            return False
        self._open_focused_live(device, self._active_page_name)
        self._update_back_button()
        return True

    def _select_nav_row(self, name: str) -> None:
        self._nav_list.handler_block(self._nav_selected_handler_id)
        try:
            self._nav_list.select_row(self._nav_rows[name])
        finally:
            self._nav_list.handler_unblock(self._nav_selected_handler_id)

    def _clear_nav_selection(self) -> None:
        self._nav_list.handler_block(self._nav_selected_handler_id)
        try:
            self._nav_list.select_row(None)
        finally:
            self._nav_list.handler_unblock(self._nav_selected_handler_id)

    def _open_focused_live(
        self,
        device,
        source_page: str,
        initial_snapshot: bytes | None = None,
    ) -> None:
        if self._active_page_name == "focused_live":
            source_page = self._focused_source_page or source_page
            self._focused_live_page.leave()
        elif source_page == "dashboard":
            self._dashboard_cameras_page.deactivate_snapshot_updates()
        elif source_page == "cameras":
            self._cameras_page.deactivate_snapshot_updates()
        elif self._active_page_name == "history":
            self._history_page.on_page_hidden()
        if self._active_page_name != "focused_live":
            self._page_history.append(self._active_page_name)
        self._focused_source_page = source_page
        self._active_page_name = "focused_live"
        self._clear_nav_selection()
        self._content_stack.set_visible_child_name("focused_live")
        self._update_title("focused_live", device_names.display_name(device))
        self._focused_live_page.show_device(
            device,
            max_streams=self._cameras_page._max_live_monitoring_streams(),
            initial_snapshot=initial_snapshot,
        )
        self._update_back_button()

    def _leave_focused_live(self, destination: str) -> None:
        """Release focused ownership and settle its source page lifecycle."""
        source_page = self._focused_source_page
        self._focused_live_page.leave()
        if destination == "cameras" and source_page == "cameras":
            self._cameras_page.reattach_live_monitoring_sessions()
        elif source_page == "cameras":
            self._cameras_page.on_page_hidden()
        elif source_page == "dashboard" and destination != "dashboard":
            self._dashboard_cameras_page.on_page_hidden()
        self._focused_source_page = None

    def _refresh_camera_names(self) -> None:
        self._dashboard_cameras_page._refresh_device_names()
        self._cameras_page._refresh_device_names()

    def _on_sidebar_toggle_clicked(self, *_args) -> None:
        self._split_view.set_show_sidebar(not self._split_view.get_show_sidebar())

    def _on_collapsed_changed(self, split_view: Adw.OverlaySplitView, _) -> None:
        """Hide the sidebar by default when the window enters narrow overlay mode."""
        if split_view.get_collapsed():
            split_view.set_show_sidebar(False)
        else:
            split_view.set_show_sidebar(True)
        self._sync_sidebar_buttons()

    def _on_sidebar_visibility_changed(self, *_args) -> None:
        self._sync_sidebar_buttons()

    def _sync_sidebar_buttons(self) -> None:
        show_sidebar = self._split_view.get_show_sidebar()
        self._sidebar_toggle_btn.set_icon_name("sidebar-show-symbolic")
        label = "Hide navigation" if show_sidebar else "Show navigation"
        self._sidebar_toggle_btn.set_tooltip_text(label)
        self._sidebar_toggle_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [label],
        )

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        """Adapt the Live Monitoring inspector around its docking threshold."""
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        inspector = self._cameras_page.inspector_split_view
        if inspector is not None:
            collapse_inspector = width <= 1100
            if inspector.get_collapsed() != collapse_inspector:
                inspector.set_collapsed(collapse_inspector)
                inspector.set_show_sidebar(not collapse_inspector)

    def _on_close_request(self, *_) -> bool:
        app = self.get_application()
        if hasattr(app, "is_quitting") and app.is_quitting():
            self.stop_runtime_streams()
            return False
        if hasattr(app, "should_hide_on_close") and app.should_hide_on_close():
            self.stop_runtime_streams()
            self.set_visible(False)
            return True
        self.stop_runtime_streams()
        return False

    def stop_runtime_streams(self) -> None:
        """Stop any camera stream that could otherwise survive window teardown."""
        self._focused_live_page.leave()
        self._dashboard_cameras_page.deactivate_snapshot_updates()
        self._cameras_page.deactivate_snapshot_updates()
        self._cameras_page.stop_live_monitoring()
        self._history_page.on_page_hidden()
        get_live_session_manager().stop_all()

    # ------------------------------------------------------------------
    # Refresh — called after auth and by the refresh button
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        client = get_client()
        if client is None or not client.is_authenticated:
            self._banner.set_revealed(True)
            self._update_events_banner(listener_state())
            self._dashboard_page.refresh()
            self._cameras_page.refresh()
            self._history_page.refresh()
            self._devices_page.refresh()
            self._alarm_page.refresh()
            return

        self._banner.set_revealed(False)
        self._update_events_banner(listener_state())
        # Only refresh the page that's actually visible — navigating to a page
        # refreshes it via _show_page, so refreshing a hidden page just burns
        # snapshot fetches. (Non-visible cameras still enforce stream caps.)
        if self._active_page_name == "dashboard":
            self._dashboard_page.refresh()
        if self._active_page_name == "cameras":
            self._cameras_page.refresh()
        else:
            self._cameras_page.enforce_live_monitoring_limits()
        self._devices_page.refresh()
        self._alarm_page.refresh()

    def _on_listener_state(self, state: str) -> None:
        self._update_events_banner(state)

    def _update_events_banner(self, state: str) -> None:
        client = get_client()
        signed_in = client is not None and client.is_authenticated
        if not signed_in or state == LISTENER_CONNECTED:
            self._events_banner.set_revealed(False)
            return
        if state == LISTENER_CONNECTING:
            self._events_banner.set_title("Reconnecting to Ring events…")
        else:
            self._events_banner.set_title("Live Ring events are offline")
        self._events_banner.set_revealed(True)

    def do_unroot(self) -> None:
        remove_connection_state_callback(self._on_listener_state)
        Adw.ApplicationWindow.do_unroot(self)

    def enforce_live_monitoring_limits(self) -> None:
        self._cameras_page.enforce_live_monitoring_limits()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _on_sign_in(self, *_) -> None:
        from halo_gtk.auth_dialog import AuthDialog

        dialog = AuthDialog()
        dialog.present(self)
