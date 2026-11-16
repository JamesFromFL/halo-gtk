"""Application-level styling for Halo's GTK interface."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

_CSS = """
.page-scroll-content {
  padding: 28px 20px 36px;
}

.section-heading {
  margin-top: 10px;
}

.camera-grid {
  margin-top: 8px;
}

.camera-tile {
  border: 1px solid alpha(@borders, 0.52);
  border-radius: 14px;
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
  color: white;
}

.state-offline {
  color: #f4ad63;
}

.camera-footer {
  padding: 14px 16px;
  background: @card_bg_color;
}

.camera-name {
  font-weight: 700;
}

.camera-details {
  margin-top: 2px;
}

.monitor-canvas {
  background: @window_bg_color;
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
  background: @card_bg_color;
}

.video-frame {
  border-radius: 14px;
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
  border-radius: 14px;
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
  border-radius: 14px;
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
  border-radius: 14px;
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

.halo-page-heading {
  font-size: 2em;
  font-weight: 800;
  letter-spacing: -0.6px;
}
.halo-page-subtitle { color: alpha(currentColor, 0.65); }
.halo-section-heading { font-size: 1.12em; font-weight: 700; }
.halo-card {
  border-radius: 16px;
  background: @card_bg_color;
  box-shadow: 0 1px 3px alpha(black, 0.12);
  border: 1px solid alpha(@borders, 0.36);
}
.halo-overview-summary { padding: 22px; }
.halo-overview-icon, .halo-security-icon {
  padding: 16px;
  border-radius: 18px;
  background: alpha(@accent_bg_color, 0.10);
  color: @accent_color;
}
.halo-security-icon.success,
.halo-overview-icon.success { color: @success_color; background: alpha(@success_bg_color, 0.12); }
.halo-security-icon.warning,
.halo-overview-icon.warning { color: @warning_color; background: alpha(@warning_bg_color, 0.12); }
.halo-security-icon.error,
.halo-overview-icon.error { color: @error_color; background: alpha(@error_bg_color, 0.12); }
.halo-camera-card { border-radius: 16px; }
.halo-camera-toolbar { padding: 16px 20px; }
.halo-camera-footer { padding: 14px 16px; }
.halo-preview-age { border-radius: 7px; padding: 4px 7px; }
.halo-focus-canvas { background: #151719; border-radius: 14px; }
.halo-focus-controls { padding: 10px; border-radius: 20px; background: @card_bg_color; }
.halo-focus-health { padding: 12px 16px; }
.halo-timeline-list { background: @card_bg_color; }
.halo-timeline-date { padding: 14px 8px 8px; font-weight: 700; }
.halo-device-group { margin-top: 6px; }
.halo-event-icon {
  padding: 8px;
  border-radius: 12px;
  color: @accent_color;
  background: alpha(@accent_bg_color, 0.10);
}
.halo-security-state { font-size: 2em; font-weight: 800; }
.halo-security-summary { padding: 20px; }
.halo-mode-choice { padding: 14px 18px; min-height: 48px; border-radius: 12px; }
.halo-mode-selected { color: @accent_color; background: alpha(@accent_bg_color, 0.13); }

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
