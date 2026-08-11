"""Application-level styling for Halo's GTK interface."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

_CSS = """
.navigation-ribbon {
  background: mix(@window_bg_color, @view_bg_color, 0.58);
  border-right: 1px solid alpha(@borders, 0.48);
}

.ribbon-brand {
  padding: 12px 16px 11px;
}

.navigation-ribbon list {
  background: transparent;
}

.navigation-ribbon row {
  margin: 3px 9px;
  border-radius: 8px;
}

.navigation-ribbon row:selected {
  background: alpha(@accent_bg_color, 0.15);
}

.navigation-ribbon row:selected image {
  color: @accent_color;
}

.ribbon-row {
  padding: 10px 12px;
}

.ribbon-settings {
  margin: 5px 9px 11px;
  padding: 9px 11px;
}

.page-scroll-content {
  padding: 24px 28px 36px;
}

.section-heading {
  margin-top: 10px;
}

.camera-grid {
  margin-top: 8px;
}

.camera-tile {
  border: 1px solid alpha(@borders, 0.52);
  border-radius: 8px;
  background: @card_bg_color;
  box-shadow: 0 1px 3px alpha(black, 0.12);
}

.camera-tile:hover,
.camera-tile:focus-visible {
  border-color: alpha(@accent_color, 0.80);
}

.camera-tile.selected {
  border: 2px solid @accent_color;
  box-shadow: 0 2px 5px alpha(@accent_color, 0.18);
}

.camera-picture {
  background: #151719;
}

.camera-overlay {
  padding: 6px 8px;
  color: white;
  background: alpha(#111315, 0.72);
}

.state-live {
  color: #73e6a2;
}

.state-snapshot {
  color: #f2dc55;
}

.state-offline {
  color: #f4ad63;
}

.camera-footer {
  padding: 9px 11px;
  border-top: 1px solid alpha(@borders, 0.45);
  background: mix(@card_bg_color, @view_bg_color, 0.38);
}

.camera-name {
  font-weight: 700;
}

.camera-details {
  margin-top: 2px;
}

.monitor-canvas {
  background: mix(@window_bg_color, #101214, 0.30);
}

.monitor-inspector {
  background: @view_bg_color;
  border-left: 1px solid alpha(@borders, 0.58);
}

.inspector-content {
  padding: 20px;
}

.monitor-controls {
  padding: 8px 10px;
  border-top: 1px solid alpha(@borders, 0.45);
  background: mix(@card_bg_color, @view_bg_color, 0.38);
}

.video-frame {
  border-radius: 8px;
  background: #111315;
}

.video-toolbar {
  padding: 8px 10px;
  border-radius: 7px;
  background: alpha(#111315, 0.88);
}

.event-symbol {
  min-width: 24px;
  min-height: 24px;
  padding: 8px;
  border-radius: 8px;
  color: @accent_color;
  background: alpha(@accent_bg_color, 0.12);
}

.command-button {
  min-height: 42px;
}

.siren-button {
  color: @error_color;
  background: alpha(@error_bg_color, 0.12);
}

.health-strip {
  padding: 9px 14px;
  border-top: 1px solid alpha(@borders, 0.45);
}

.alarm-overview {
  border: 1px solid alpha(@borders, 0.52);
  border-radius: 8px;
  background: @card_bg_color;
  box-shadow: 0 1px 3px alpha(black, 0.10);
}

.alarm-overview-content {
  padding: 20px;
}

.alarm-state-symbol {
  min-width: 34px;
  min-height: 34px;
  padding: 10px;
  border-radius: 8px;
  background: alpha(@accent_bg_color, 0.12);
}

.alarm-state-symbol.success {
  background: alpha(@success_bg_color, 0.14);
}

.alarm-state-symbol.warning {
  background: alpha(@warning_bg_color, 0.14);
}

.alarm-state-symbol.error {
  background: alpha(@error_bg_color, 0.14);
}
"""


_installed = False


def install_ui_style() -> None:
    """Install Halo's shared CSS provider once for the current display."""
    global _installed
    if _installed:
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _installed = True
