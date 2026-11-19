#!/usr/bin/env bash
# Launch one opt-in benchmark configuration on a private, Unix-only Broadway display.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
python=$(realpath -s "${HALO_BENCHMARK_PYTHON:-$repo/.venv/bin/python}")
temp=$(mktemp -d)
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
export HOME="$temp/home" XDG_CONFIG_HOME="$temp/config" XDG_CACHE_HOME="$temp/cache"
export XDG_STATE_HOME="$temp/state" XDG_DATA_HOME="$temp/data" XDG_RUNTIME_DIR="$temp/run"
mkdir -m 700 "$HOME" "$XDG_RUNTIME_DIR"
export PYTHONPATH="$repo/src" PYTHONDONTWRITEBYTECODE=1 GSETTINGS_BACKEND=memory
export DBUS_SESSION_BUS_ADDRESS="unix:path=$temp/no-session-bus" GIO_USE_VFS=local GTK_USE_PORTAL=0
export GDK_BACKEND=broadway GSK_RENDERER=cairo BROADWAY_DISPLAY=:18 NO_AT_BRIDGE=1
gtk4-broadwayd --unixsocket="$temp/display" "$BROADWAY_DISPLAY" >"$temp/display.log" 2>&1 &
display_pid=$!
for ((i=0; i<100; i++)); do
    [[ -S "$XDG_RUNTIME_DIR/broadway19.socket" ]] && break
    kill -0 "$display_pid" 2>/dev/null || { cat "$temp/display.log" >&2; exit 1; }
    sleep 0.05
done
"$python" "$repo/scripts/benchmark_live_renderer.py" "$@"
