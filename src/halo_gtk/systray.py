"""GTK4-safe StatusNotifierItem tray support.

GTK4 removed Gtk.Menu/Gtk.MenuItem, so the old AppIndicator3 menu path is not
usable in this process.  This module registers a minimal
org.kde.StatusNotifierItem over D-Bus instead, which is the protocol consumed by
modern tray hosts on KDE, GNOME extensions, wlroots panels, and similar shells.
"""

from __future__ import annotations

import logging
import os

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from halo_gtk import APP_ID  # noqa: E402

_log = logging.getLogger(__name__)

_OBJECT_PATH = "/StatusNotifierItem"
_WATCHER_BUS = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_WATCHER_IFACE = "org.kde.StatusNotifierWatcher"
_MENU_PATH = f"{_OBJECT_PATH}/Menu"

_MENU_ROOT_ID = 0
_MENU_SHOW_ID = 1
_MENU_QUIT_ID = 2

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
  </interface>
</node>
"""

_DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/>
      <arg name="timestamp" type="u"/>
    </signal>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
  </interface>
</node>
"""

_SNI_INTERFACE_INFO = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).interfaces[0]
_DBUSMENU_INTERFACE_INFO = Gio.DBusNodeInfo.new_for_xml(_DBUSMENU_XML).interfaces[0]


class SystemTray:
    """Register a minimal tray icon through the StatusNotifierItem protocol."""

    def __init__(self, app) -> None:
        self._app = app
        self._connection: Gio.DBusConnection | None = None
        self._registration_id: int | None = None
        self._menu_registration_id: int | None = None
        self._bus_name_id: int | None = None
        self._watcher_signal_id: int | None = None
        self._retry_source_id: int | None = None
        self._item_bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-Halo"
        self._enabled = False
        self._registered_with_watcher = False
        self._setup_pending = False

    def setup(self) -> bool:
        """Enable the tray and register asynchronously, off the startup path."""
        self._enabled = True
        # Defer the synchronous D-Bus connection + registration to an idle
        # callback so a slow or contended session bus can't block app startup.
        if not self._setup_pending:
            self._setup_pending = True
            GLib.idle_add(self._setup_now)
        return True

    def _setup_now(self) -> bool:
        self._setup_pending = False
        if not self._enabled:
            return GLib.SOURCE_REMOVE

        if not self._ensure_connection():
            self._schedule_retry()
            return GLib.SOURCE_REMOVE

        if not self._export_objects() or not self._own_bus_name():
            self.shutdown()
            return GLib.SOURCE_REMOVE

        self._watch_watcher_owner()
        if self._register_with_watcher():
            self._cancel_retry()
            return GLib.SOURCE_REMOVE

        self._schedule_retry()
        return GLib.SOURCE_REMOVE

    def shutdown(self) -> None:
        """Unregister the D-Bus object if it was exported."""
        self._enabled = False
        self._cancel_retry()
        if (
            self._connection is not None
            and self._watcher_signal_id is not None
            and not self._connection.is_closed()
        ):
            self._connection.signal_unsubscribe(self._watcher_signal_id)
        if self._connection is not None and self._registration_id is not None:
            self._connection.unregister_object(self._registration_id)
        if self._connection is not None and self._menu_registration_id is not None:
            self._connection.unregister_object(self._menu_registration_id)
        if self._bus_name_id is not None:
            Gio.bus_unown_name(self._bus_name_id)
        self._registration_id = None
        self._menu_registration_id = None
        self._bus_name_id = None
        self._watcher_signal_id = None
        self._registered_with_watcher = False
        self._connection = None

    def _ensure_connection(self) -> bool:
        if self._connection is not None and not self._connection.is_closed():
            return True
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            return True
        except Exception as exc:
            _log.warning("Failed to connect to the session bus for tray icon: %s", exc)
            self._connection = None
            return False

    def _export_objects(self) -> bool:
        if self._connection is None:
            return False
        try:
            if self._menu_registration_id is None:
                self._menu_registration_id = self._connection.register_object(
                    _MENU_PATH,
                    _DBUSMENU_INTERFACE_INFO,
                    self._on_menu_method_call,
                    self._on_menu_get_property,
                    None,
                )
            if self._registration_id is None:
                self._registration_id = self._connection.register_object(
                    _OBJECT_PATH,
                    _SNI_INTERFACE_INFO,
                    self._on_method_call,
                    self._on_get_property,
                    None,
                )
            return True
        except Exception as exc:
            _log.warning("Failed to export tray icon D-Bus objects: %s", exc)
            return False

    def _own_bus_name(self) -> bool:
        if self._connection is None:
            return False
        if self._bus_name_id is not None:
            return True
        try:
            self._bus_name_id = Gio.bus_own_name_on_connection(
                self._connection,
                self._item_bus_name,
                Gio.BusNameOwnerFlags.NONE,
                None,
                None,
            )
            return True
        except Exception as exc:
            _log.warning("Failed to own tray icon bus name: %s", exc)
            self._bus_name_id = None
            return False

    def _watch_watcher_owner(self) -> None:
        if self._connection is None or self._watcher_signal_id is not None:
            return
        self._watcher_signal_id = self._connection.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            _WATCHER_BUS,
            Gio.DBusSignalFlags.NONE,
            self._on_watcher_name_owner_changed,
        )

    def _on_watcher_name_owner_changed(
        self,
        _connection,
        _sender_name,
        _object_path,
        _interface_name,
        _signal_name,
        parameters,
    ) -> None:
        _name, _old_owner, new_owner = parameters.unpack()
        self._registered_with_watcher = False
        if not self._enabled:
            return
        if new_owner:
            # Watcher reappeared — retry now, but cancel any pending timeout retry
            # first so its orphaned source can't fire a duplicate setup() later.
            self._cancel_retry()
            GLib.idle_add(self._retry_setup)
        else:
            self._schedule_retry()

    def _register_with_watcher(self) -> bool:
        if self._registered_with_watcher:
            return True
        if self._connection is None:
            return False
        if not self._watcher_available():
            _log.info("No StatusNotifierWatcher found; tray icon registration will retry")
            return False
        try:
            self._connection.call_sync(
                _WATCHER_BUS,
                _WATCHER_PATH,
                _WATCHER_IFACE,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._item_bus_name,)),
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            self._registered_with_watcher = True
            _log.info("StatusNotifier tray icon registered")
            return True
        except Exception as exc:
            self._registered_with_watcher = False
            _log.warning("Failed to register tray icon: %s", exc)
            return False

    def _schedule_retry(self) -> None:
        if not self._enabled or self._retry_source_id is not None:
            return
        self._retry_source_id = GLib.timeout_add_seconds(2, self._retry_setup)

    def _cancel_retry(self) -> None:
        if self._retry_source_id is None:
            return
        GLib.source_remove(self._retry_source_id)
        self._retry_source_id = None

    def _retry_setup(self) -> bool:
        self._retry_source_id = None
        if not self._enabled:
            return GLib.SOURCE_REMOVE
        self.setup()
        return GLib.SOURCE_REMOVE

    def _watcher_available(self) -> bool:
        if self._connection is None:
            return False
        try:
            reply = self._connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (_WATCHER_BUS,)),
                GLib.VariantType.new("(b)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            return bool(reply.unpack()[0])
        except Exception:
            return False

    def _on_method_call(
        self,
        connection,
        sender,
        object_path,
        interface_name,
        method_name,
        parameters,
        invocation,
    ) -> None:
        if method_name in {"Activate", "SecondaryActivate"}:
            GLib.idle_add(self._activate_app)
        invocation.return_value(None)

    def _on_get_property(self, connection, sender, object_path, interface_name, property_name):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "halo-gtk"),
            "Title": GLib.Variant("s", "Halo"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", APP_ID),
            "IconThemePath": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], "Halo", "Ring home security")),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", _MENU_PATH),
        }
        return values.get(property_name)

    def _activate_app(self) -> bool:
        self._app.activate()
        return GLib.SOURCE_REMOVE

    def _quit_app(self) -> bool:
        if hasattr(self._app, "request_quit"):
            self._app.request_quit()
        else:
            self._app.quit()
        return GLib.SOURCE_REMOVE

    def _on_menu_method_call(
        self,
        connection,
        sender,
        object_path,
        interface_name,
        method_name,
        parameters,
        invocation,
    ) -> None:
        if method_name == "GetLayout":
            parent_id, _recursion_depth, property_names = parameters.unpack()
            invocation.return_value(
                GLib.Variant("(u(ia{sv}av))", (1, self._menu_layout(parent_id, property_names)))
            )
        elif method_name == "GetGroupProperties":
            ids, property_names = parameters.unpack()
            invocation.return_value(
                GLib.Variant("(a(ia{sv}))", (self._menu_group_properties(ids, property_names),))
            )
        elif method_name == "GetProperty":
            item_id, name = parameters.unpack()
            invocation.return_value(GLib.Variant("(v)", (self._menu_property(item_id, name),)))
        elif method_name == "Event":
            item_id, event_id, _data, _timestamp = parameters.unpack()
            self._handle_menu_event(item_id, event_id)
            invocation.return_value(None)
        elif method_name == "EventGroup":
            events = parameters.unpack()[0]
            for item_id, event_id, _data, _timestamp in events:
                self._handle_menu_event(item_id, event_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method_name == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method_name == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown menu method: {method_name}",
            )

    def _on_menu_get_property(self, connection, sender, object_path, interface_name, property_name):
        values = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return values.get(property_name)

    def _menu_layout(self, item_id: int = _MENU_ROOT_ID, names=None):
        if item_id == _MENU_ROOT_ID:
            return self._menu_item(
                _MENU_ROOT_ID,
                names=names,
                children=[_MENU_SHOW_ID, _MENU_QUIT_ID],
            )
        return self._menu_item(item_id, names=names)

    def _menu_item(self, item_id: int, names=None, children: list[int] | None = None):
        props = self._menu_properties(item_id, names)
        child_nodes = [
            GLib.Variant("(ia{sv}av)", self._menu_item(child_id)) for child_id in children or []
        ]
        return (item_id, props, child_nodes)

    def _menu_group_properties(self, ids, property_names):
        item_ids = ids if ids else [_MENU_ROOT_ID, _MENU_SHOW_ID, _MENU_QUIT_ID]
        return [(item_id, self._menu_properties(item_id, property_names)) for item_id in item_ids]

    def _menu_properties(self, item_id: int, names=None) -> dict:
        labels = {
            _MENU_ROOT_ID: "",
            _MENU_SHOW_ID: "Show Halo",
            _MENU_QUIT_ID: "Quit",
        }
        all_props = {
            "type": GLib.Variant("s", "standard"),
            "label": GLib.Variant("s", labels.get(item_id, "")),
            "enabled": GLib.Variant("b", item_id != _MENU_ROOT_ID),
            "visible": GLib.Variant("b", True),
        }
        if not names:
            return all_props
        return {name: value for name, value in all_props.items() if name in names}

    def _menu_property(self, item_id: int, name: str) -> GLib.Variant:
        props = self._menu_properties(item_id)
        return props.get(name, GLib.Variant("s", ""))

    def _handle_menu_event(self, item_id: int, event_id: str) -> None:
        if event_id != "clicked":
            return
        if item_id == _MENU_SHOW_ID:
            GLib.idle_add(self._activate_app)
        elif item_id == _MENU_QUIT_ID:
            GLib.idle_add(self._quit_app)
