#!/usr/bin/env bash
set -euo pipefail

APP_ID="io.github.JamesFromFL.HaloGtk"
APP_NAME="halo-gtk"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_HOME="${XDG_DATA_HOME:-"$HOME/.local/share"}"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$DATA_HOME/applications"
ICON_THEME_DIR="$DATA_HOME/icons/hicolor"
SCHEMA_DIR="$DATA_HOME/glib-2.0/schemas"

log() {
  printf '%s\n' "$*"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

require_gi_namespace() {
  local namespace="$1"
  local version="$2"
  local package_hint="$3"

  if ! "$REPO_ROOT/.venv/bin/python" -c "import gi; gi.require_version('$namespace', '$version'); repo = __import__('gi.repository', fromlist=['$namespace']); getattr(repo, '$namespace')" >/dev/null 2>&1; then
    printf 'Missing required GI namespace: %s-%s\n' "$namespace" "$version" >&2
    printf 'Install the system package first: %s\n' "$package_hint" >&2
    exit 1
  fi
}

require_gst_element() {
  local element="$1"
  local package_hint="$2"

  if ! "$REPO_ROOT/.venv/bin/python" -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); raise SystemExit(0 if Gst.ElementFactory.find('$element') else 1)" >/dev/null 2>&1; then
    printf 'Missing required GStreamer element: %s\n' "$element" >&2
    printf 'Install the system package first: %s\n' "$package_hint" >&2
    exit 1
  fi
}

require_any_gst_element() {
  local package_hint="$1"
  shift
  local elements="$*"

  if ! "$REPO_ROOT/.venv/bin/python" -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); raise SystemExit(0 if any(Gst.ElementFactory.find(name) for name in '$elements'.split()) else 1)" >/dev/null 2>&1; then
    printf 'Missing required GStreamer audio sink (tried: %s)\n' "$elements" >&2
    printf 'Install the system package first: %s\n' "$package_hint" >&2
    exit 1
  fi
}

refresh_caches() {
  if command -v glib-compile-schemas >/dev/null 2>&1; then
    glib-compile-schemas "$SCHEMA_DIR"
  else
    log "Skipping schema cache refresh: glib-compile-schemas not found"
  fi

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  fi

  if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -f "$ICON_THEME_DIR/index.theme" ]; then
    gtk-update-icon-cache -q "$ICON_THEME_DIR" >/dev/null 2>&1 || true
  fi
}

stop_running_app() {
  local pattern="$REPO_ROOT/.venv/bin/$APP_NAME"

  if ! command -v pgrep >/dev/null 2>&1 || ! command -v pkill >/dev/null 2>&1; then
    return
  fi

  if ! pgrep -f -- "$pattern" >/dev/null 2>&1; then
    return
  fi

  log "Stopping running Halo instance"
  pkill -f -- "$pattern" || true

  for _ in 1 2 3 4 5; do
    if ! pgrep -f -- "$pattern" >/dev/null 2>&1; then
      return
    fi
    sleep 0.2
  done
}

install_launcher() {
  mkdir -p "$BIN_DIR"
  cat >"$BIN_DIR/$APP_NAME" <<EOF
#!/usr/bin/env bash
exec "$REPO_ROOT/.venv/bin/$APP_NAME" "\$@"
EOF
  chmod 0755 "$BIN_DIR/$APP_NAME"
}

install_desktop_file() {
  mkdir -p "$DESKTOP_DIR"
  sed "s|^Exec=.*|Exec=\"$BIN_DIR/$APP_NAME\"|" \
    "$REPO_ROOT/data/$APP_ID.desktop" >"$DESKTOP_DIR/$APP_ID.desktop"
  chmod 0644 "$DESKTOP_DIR/$APP_ID.desktop"
}

install_icons() {
  local src size

  for src in "$REPO_ROOT"/data/icons/hicolor/*/apps/"$APP_ID.png"; do
    [ -f "$src" ] || continue
    size="$(basename "$(dirname "$(dirname "$src")")")"
    mkdir -p "$ICON_THEME_DIR/$size/apps"
    install -m 0644 "$src" "$ICON_THEME_DIR/$size/apps/$APP_ID.png"
  done
}

install_schema() {
  mkdir -p "$SCHEMA_DIR"
  install -m 0644 "$REPO_ROOT/data/$APP_ID.gschema.xml" "$SCHEMA_DIR/$APP_ID.gschema.xml"
}

validate_runtime() {
  require_gi_namespace Adw 1 libadwaita
  require_gi_namespace GdkPixbuf 2.0 gdk-pixbuf2
  require_gi_namespace GioUnix 2.0 glib2
  require_gi_namespace Graphene 1.0 graphene
  require_gi_namespace Gst 1.0 gstreamer
  require_gi_namespace Gtk 4.0 gtk4
  require_gi_namespace Notify 0.7 libnotify
  require_gi_namespace Secret 1 libsecret
  require_gst_element appsink gst-plugins-base
  require_gst_element appsrc gst-plugins-base
  require_gst_element audioconvert gst-plugins-base
  require_gst_element audioresample gst-plugins-base
  require_gst_element capsfilter gst-plugins-base
  require_gst_element pulsesrc gst-plugins-good
  require_gst_element queue gst-plugins-base
  require_gst_element videoconvert gst-plugins-base
  require_gst_element volume gst-plugins-base
  require_gst_element gtk4paintablesink gst-plugin-gtk4
  require_gst_element playbin gst-plugins-base
  require_any_gst_element "gst-plugins-good or gst-plugin-pipewire" \
    pulsesink pipewiresink autoaudiosink
}

ensure_venv() {
  if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
    log "Creating virtual environment with system site packages"
    uv venv --system-site-packages "$REPO_ROOT/.venv"
    return
  fi

  if "$REPO_ROOT/.venv/bin/python" -c "import gi" >/dev/null 2>&1; then
    log "Using existing virtual environment"
    return
  fi

  log "Recreating virtual environment with system site packages"
  uv venv --system-site-packages --clear "$REPO_ROOT/.venv"
}

main() {
  if [ "$#" -ne 0 ]; then
    printf 'Usage: %s\n' "$0" >&2
    exit 2
  fi

  require_command uv

  ensure_venv
  log "Checking locked dependency graph"
  uv --project "$REPO_ROOT" lock --check

  stop_running_app

  log "Installing Python dependencies"
  uv --project "$REPO_ROOT" sync --frozen --no-dev

  log "Validating desktop runtime"
  validate_runtime

  log "Installing desktop launcher and assets"
  install_launcher
  install_desktop_file
  install_icons
  install_schema

  log "Refreshing desktop caches"
  refresh_caches

  log "Installed Halo. Run it with: $APP_NAME"
}

main "$@"
