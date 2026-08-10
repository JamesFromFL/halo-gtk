#!/usr/bin/env bash
set -euo pipefail

APP_ID="io.github.JamesFromFL.HaloGtk"
APP_NAME="halo-gtk"
CONFIG_HOME="${XDG_CONFIG_HOME:-"$HOME/.config"}"
DATA_HOME="${XDG_DATA_HOME:-"$HOME/.local/share"}"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$CONFIG_HOME/autostart"
DESKTOP_DIR="$DATA_HOME/applications"
ICON_THEME_DIR="$DATA_HOME/icons/hicolor"
SCHEMA_DIR="$DATA_HOME/glib-2.0/schemas"

log() {
  printf '%s\n' "$*"
}

refresh_caches() {
  if command -v glib-compile-schemas >/dev/null 2>&1 && [ -d "$SCHEMA_DIR" ]; then
    glib-compile-schemas "$SCHEMA_DIR"
  fi

  if command -v update-desktop-database >/dev/null 2>&1 && [ -d "$DESKTOP_DIR" ]; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  fi

  if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -f "$ICON_THEME_DIR/index.theme" ]; then
    gtk-update-icon-cache -q "$ICON_THEME_DIR" >/dev/null 2>&1 || true
  fi
}

main() {
  local icon

  log "Removing launcher"
  rm -f "$BIN_DIR/$APP_NAME"

  log "Removing autostart entry"
  rm -f "$AUTOSTART_DIR/$APP_ID.desktop"

  log "Removing desktop file"
  rm -f "$DESKTOP_DIR/$APP_ID.desktop"

  log "Removing icons"
  for icon in "$ICON_THEME_DIR"/*/apps/"$APP_ID.png"; do
    [ -e "$icon" ] || continue
    rm -f "$icon"
  done

  log "Removing GSettings schema"
  rm -f "$SCHEMA_DIR/$APP_ID.gschema.xml"

  log "Refreshing desktop caches"
  refresh_caches

  log "Uninstalled Halo desktop integration"
  log "Ring credentials and user data were preserved"
}

main "$@"
