#!/usr/bin/env bash
# Run display and playback tests without using the desktop user's session or data.
set -eu
python="$(realpath "${1:-.venv/bin/python}" -s)"
temp="$(mktemp -d)"
display_pid=""
cleanup() {
  if [[ -n "$display_pid" ]]; then
    kill "$display_pid" 2>/dev/null || true
    wait "$display_pid" 2>/dev/null || true
  fi
  rm -rf -- "$temp"
}
trap cleanup EXIT
unset DISPLAY WAYLAND_DISPLAY
export HOME="$temp/home" XDG_CONFIG_HOME="$temp/config" XDG_STATE_HOME="$temp/state"
export XDG_DATA_HOME="$temp/data" XDG_CACHE_HOME="$temp/cache" XDG_RUNTIME_DIR="$temp/run"
mkdir -m 700 "$HOME" "$XDG_RUNTIME_DIR"
export GSETTINGS_BACKEND=memory DBUS_SESSION_BUS_ADDRESS="unix:path=$temp/no-session-bus"
export GDK_BACKEND=broadway GSK_RENDERER=cairo BROADWAY_DISPLAY=:17
export HALO_NATIVE_LAYOUT_TESTS=1 HALO_NATIVE_MEDIA_TESTS=1
gtk4-broadwayd --address=127.0.0.1 --port=0 "$BROADWAY_DISPLAY" &
display_pid=$!
for ((attempt=0; attempt<100; attempt++)); do
  [[ -S "$XDG_RUNTIME_DIR/broadway18.socket" ]] && break
  kill -0 "$display_pid" || exit 1
  sleep .05
done
"$python" -m pytest -q -p no:cacheprovider -k native
