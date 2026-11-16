"""Adaptive GNOME shell with five primary views and an isolated focused player."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gtk  # noqa: E402

from halo_gtk import APP_ID, config, device_names  # noqa: E402
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

_PAGE_LABELS: dict[str, str] = {
    "dashboard": "Overview",
    "cameras": "Cameras",
    "history": "Activity",
    "devices": "Devices",
    "alarm": "Security",
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
        self._resume_after_hide = False
        self.connect("close-request", self._on_close_request)
        self.connect("map", self._on_map)
        self._build_ui()
        add_connection_state_callback(self._on_listener_state)
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        install_ui_style()

        self._compact = False
        self._active_page_name = "dashboard"
        self._content_stack = Adw.ViewStack(hhomogeneous=False, vhomogeneous=False)
        self._focus_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=180,
            hhomogeneous=False,
            vhomogeneous=False,
        )
        self._focus_stack.add_named(self._content_stack, "main")
        content_toolbar = Adw.ToolbarView(content=self._focus_stack)
        self._toast_overlay = Adw.ToastOverlay(child=content_toolbar)
        self.set_content(self._toast_overlay)
        self._content_header = Adw.HeaderBar()
        self._brand = Gtk.Box(spacing=8)
        self._brand.append(self._make_sidebar_logo(28))
        brand_name = Gtk.Label(label="Halo", css_classes=["heading"])
        self._brand.append(brand_name)
        self._content_header.pack_start(self._brand)
        self._back_btn = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text="Back",
            sensitive=False,
            visible=False,
        )
        self._back_btn.connect("clicked", self._on_back_clicked)
        self._content_header.pack_start(self._back_btn)
        self._header_title = Adw.WindowTitle(title="Overview")
        self._view_switcher = Adw.ViewSwitcher(
            stack=self._content_stack,
            policy=Adw.ViewSwitcherPolicy.WIDE,
        )
        self._header_stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self._header_stack.add_named(self._view_switcher, "navigation")
        self._header_stack.add_named(self._header_title, "title")
        self._content_header.set_title_widget(self._header_stack)
        self._content_header.pack_end(
            Gtk.MenuButton(
                icon_name="open-menu-symbolic",
                tooltip_text="Main menu",
                menu_model=self._build_menu(),
            )
        )
        self._content_header.pack_end(self._build_appearance_menu())
        content_toolbar.add_top_bar(self._content_header)
        self._banner = Adw.Banner(title="Not signed in to Ring", button_label="Sign In")
        self._banner.connect("button-clicked", self._on_sign_in)
        content_toolbar.add_top_bar(self._banner)
        self._events_banner = Adw.Banner(title="Reconnecting to Ring events…", revealed=False)
        content_toolbar.add_top_bar(self._events_banner)
        self._bottom_navigation = Adw.ViewSwitcherBar(stack=self._content_stack)
        content_toolbar.add_bottom_bar(self._bottom_navigation)

        self._dashboard_cameras_page = CamerasPage(
            on_open_live_focus=self._open_focused_live,
            embedded=True,
        )
        self._dashboard_page = DashboardPage(
            camera_grid=self._dashboard_cameras_page,
            on_navigate=self._show_page,
            on_open_event=self.open_history_event,
        )
        self._content_stack.add_titled_with_icon(
            self._dashboard_page,
            "dashboard",
            "Overview",
            "view-grid-symbolic",
        )

        self._cameras_page = CamerasPage(
            on_open_live_focus=self._open_focused_live,
            on_open_history=self.open_history,
            show_monitoring_controls=True,
            grid_size_config_key="live_monitoring_grid_size",
            camera_order_config_key="live_monitoring_camera_order",
        )
        self._content_stack.add_titled_with_icon(
            self._cameras_page,
            "cameras",
            "Cameras",
            "camera-video-symbolic",
        )

        self._focused_live_page = FocusedLivePage(
            on_history=self.open_history,
            on_nickname_changed=self._refresh_camera_names,
        )
        self._focus_stack.add_named(self._focused_live_page, "focused_live")

        self._history_page = HistoryPage(on_title_change=self._on_history_title_change)
        self._content_stack.add_titled_with_icon(
            self._history_page,
            "history",
            "Activity",
            "document-open-recent-symbolic",
        )

        self._devices_page = DevicesPage(
            on_show_cameras=lambda: self._show_page("cameras"),
            on_show_security=lambda: self._show_page("alarm"),
        )
        self._content_stack.add_titled_with_icon(
            self._devices_page,
            "devices",
            "Devices",
            "preferences-system-symbolic",
        )

        self._alarm_page = AlarmPage()
        self._content_stack.add_titled_with_icon(
            self._alarm_page,
            "alarm",
            "Security",
            "security-high-symbolic",
        )

        self._focused_source_page: str | None = None
        self._page_history: list[str] = []
        self._navigation_handler = self._content_stack.connect(
            "notify::visible-child-name",
            self._on_view_changed,
        )
        self._update_back_button()
        self._sync_navigation()
        inspector = self._cameras_page.inspector_split_view
        if inspector is not None:
            inspector_compact = Adw.Breakpoint.new(
                Adw.BreakpointCondition.parse("max-width: 1100px")
            )
            inspector_compact.add_setter(inspector, "collapsed", True)
            inspector_compact.add_setter(inspector, "show-sidebar", False)
            self.add_breakpoint(inspector_compact)
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 980px"))
        compact.add_setter(self._history_page.split_view, "collapsed", True)
        if inspector is not None:
            # Only the last matching breakpoint is active, so both width
            # ranges need the inspector's overlay behavior.
            compact.add_setter(inspector, "collapsed", True)
            compact.add_setter(inspector, "show-sidebar", False)
        compact.connect("apply", lambda *_: self._set_compact(True))
        compact.connect("unapply", lambda *_: self._set_compact(False))
        self.add_breakpoint(compact)

    def _set_compact(self, compact: bool) -> None:
        self._compact = compact
        self._sync_navigation()

    def _sync_navigation(self) -> None:
        focused = self._active_page_name == "focused_live"
        self._header_stack.set_visible_child_name(
            "title" if self._compact or focused else "navigation"
        )
        self._bottom_navigation.set_reveal(self._compact and not focused)
        self._brand.set_visible(not focused)

    def _on_view_changed(self, stack, _property) -> None:
        name = stack.get_visible_child_name()
        if name and name != self._active_page_name:
            self._show_page(name)

    def show_toast(self, message: str, *, button_label=None, callback=None):
        """Show a bounded operation result without interrupting the current view."""
        toast = Adw.Toast(title=message, timeout=6, use_markup=False)
        if button_label and callback is not None:
            toast.set_button_label(button_label)
            toast.connect("button-clicked", lambda *_args: callback())
        self._toast_overlay.add_toast(toast)
        return toast

    @staticmethod
    def _make_sidebar_logo(pixel_size: int) -> Gtk.Picture:
        """Load the packaged app mark for the native header bar."""
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

    def _build_appearance_menu(self):
        button = Gtk.MenuButton(icon_name="weather-clear-night-symbolic", tooltip_text="Appearance")
        popover = Gtk.Popover()
        choices = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=14,
            margin_bottom=14,
            margin_start=14,
            margin_end=14,
        )
        choices.append(Gtk.Label(label="Appearance", xalign=0, css_classes=["heading"]))
        self._appearance_choices = {}
        selected = config.load().get("appearance", "system")
        group = None
        for value, label in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            choice = Gtk.CheckButton(label=label)
            if group is not None:
                choice.set_group(group)
            else:
                group = choice
            choice.set_active(value == selected)
            self._appearance_choices[value] = choice
            choice.connect("toggled", self._on_appearance_changed, value)
            choices.append(choice)
        self._apply_appearance(selected)
        popover.set_child(choices)
        button.set_popover(popover)
        return button

    @staticmethod
    def _apply_appearance(value):
        scheme = {
            "system": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        Adw.StyleManager.get_default().set_color_scheme(scheme.get(value, Adw.ColorScheme.DEFAULT))

    def _on_appearance_changed(self, choice, value):
        if getattr(self, "_syncing_appearance", False) or not choice.get_active():
            return
        settings = config.load()
        previous = settings.get("appearance", "system")
        if previous == value:
            self._apply_appearance(value)
            return
        settings["appearance"] = value
        try:
            config.save(settings)
        except (OSError, ValueError):
            self._appearance_choices[previous].set_active(True)
            self.show_toast("Could not save the appearance setting.")
            return
        self._apply_appearance(value)

    def _sync_appearance(self):
        """Reflect settings reset/import without saving from radio-button callbacks."""
        value = config.load().get("appearance", "system")
        self._syncing_appearance = True
        try:
            self._appearance_choices[value].set_active(True)
            self._apply_appearance(value)
        finally:
            self._syncing_appearance = False

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
            self._dashboard_page.on_page_hidden()
        elif previous_page == "cameras" and name != "cameras":
            self._cameras_page.on_page_hidden()
        elif previous_page == "history" and name != "history":
            self._history_page.on_page_hidden()
        elif previous_page == "alarm" and name != "alarm":
            self._alarm_page.on_page_hidden()
        elif previous_page == "devices" and name != "devices":
            self._devices_page.on_page_hidden()

        self._active_page_name = name
        self._select_nav_row(name)
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
            shown = getattr(self._devices_page, "on_page_shown", self._devices_page.refresh)
            shown()
        elif name == "alarm":
            self._alarm_page.refresh()
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

    def open_alarm_location(self, location_id=None, *, asset_id=None, zid=None, event_id=None):
        self._show_page("alarm")
        self._alarm_page.open_location(location_id, asset_id=asset_id, zid=zid, event_id=event_id)

    def open_alarm_history(self, location_id=None):
        self._enter_history_page(lambda: self._history_page.show_alarm_history(location_id))

    def open_history_event(self, device_id: int, event_id: int) -> None:
        """Show Event History and play a specific event."""
        self._enter_history_page(lambda: self._history_page.show_event_by_id(device_id, event_id))

    def _enter_history_page(self, populate) -> None:
        """Switch to Event History (with page-history bookkeeping) and populate it."""
        if self._active_page_name != "history":
            append_history = True
            if self._active_page_name == "dashboard":
                self._dashboard_cameras_page.on_page_hidden()
                self._dashboard_page.on_page_hidden()
            elif self._active_page_name == "cameras":
                self._cameras_page.on_page_hidden()
            elif self._active_page_name == "alarm":
                self._alarm_page.on_page_hidden()
            elif self._active_page_name == "devices":
                self._devices_page.on_page_hidden()
            elif self._active_page_name == "focused_live":
                self._leave_focused_live("history")
                append_history = False
            if append_history:
                self._page_history.append(self._active_page_name)
        self._active_page_name = "history"
        self._select_nav_row("history")
        self._content_stack.set_visible_child_name("history")
        self._update_title("history")
        populate()
        self._update_back_button()

    def open_intercom(self, api_id, epoch, generation):
        self._show_page("devices")
        return self._devices_page.open_intercom(api_id, epoch, generation)

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
        self._content_stack.handler_block(self._navigation_handler)
        try:
            self._content_stack.set_visible_child_name(name)
            self._focus_stack.set_visible_child_name("main")
        finally:
            self._content_stack.handler_unblock(self._navigation_handler)
        self._sync_navigation()

    def _clear_nav_selection(self) -> None:
        self._focus_stack.set_visible_child_name("focused_live")
        self._sync_navigation()

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
            self._dashboard_page.on_page_hidden()
        elif source_page == "cameras":
            self._cameras_page.deactivate_snapshot_updates()
        elif self._active_page_name == "history":
            self._history_page.on_page_hidden()
        elif self._active_page_name == "devices":
            self._devices_page.on_page_hidden()
        elif self._active_page_name == "alarm":
            self._alarm_page.on_page_hidden()
        if self._active_page_name != "focused_live":
            self._page_history.append(self._active_page_name)
        self._focused_source_page = source_page
        self._active_page_name = "focused_live"
        self._clear_nav_selection()
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

    def _on_close_request(self, *_) -> bool:
        app = self.get_application()
        if hasattr(app, "is_quitting") and app.is_quitting():
            self.stop_runtime_streams()
            return False
        if hasattr(app, "should_hide_on_close") and app.should_hide_on_close():
            self.stop_runtime_streams()
            self._resume_after_hide = True
            self.set_visible(False)
            return True
        self.stop_runtime_streams()
        return False

    def _on_map(self, *_):
        if not self._resume_after_hide:
            return
        self._resume_after_hide = False
        if self._active_page_name == "focused_live":
            # Closing ended this stream. Reopen its source, not a frozen player
            # or an automatically restarted camera connection.
            self._on_back_clicked()
        self.refresh()

    def stop_runtime_streams(self) -> None:
        """Stop any camera stream that could otherwise survive window teardown."""
        self._focused_live_page.leave()
        self._dashboard_cameras_page.deactivate_snapshot_updates()
        self._cameras_page.deactivate_snapshot_updates()
        self._cameras_page.stop_live_monitoring()
        self._history_page.on_page_hidden()
        for name in ("_dashboard_page", "_devices_page", "_alarm_page"):
            page = getattr(self, name, None)
            hidden = getattr(page, "on_page_hidden", None)
            if callable(hidden):
                hidden()
        get_live_session_manager().stop_all()

    # ------------------------------------------------------------------
    # Refresh — called after auth and by the refresh button
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._sync_appearance()
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
        if self._active_page_name == "devices":
            shown = getattr(self._devices_page, "on_page_shown", self._devices_page.refresh)
            shown()
        else:
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
