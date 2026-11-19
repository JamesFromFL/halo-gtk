#!/usr/bin/env bash
set -euo pipefail

: "${FLATPAK_USER_DIR:?Select the disposable Flatpak installation before running this smoke check}"
app_id=io.github.JamesFromFL.HaloGtk
commit=$(flatpak info --user --show-commit "$app_id")
[[ "$commit" =~ ^[0-9a-f]{64}$ ]]
# Run the installed app on Platform. The app sees temporary settings and an
# in-sandbox display. Neither the account service nor audio devices are needed.
flatpak run --user --unshare=network --unshare=ipc --nosocket=pulseaudio \
    --nosocket=x11 --nosocket=wayland --nosocket=fallback-x11 --nodevice=all \
    --nofilesystem=host:reset --no-session-bus --no-a11y-bus --no-documents-portal \
    --env=GDK_BACKEND=broadway --env=BROADWAY_DISPLAY=:153 --env=GSK_RENDERER=cairo \
    --env=GSETTINGS_BACKEND=memory --env=GTK_A11Y=none --env=HALO_SMOKE_COMMIT="$commit" \
    --command=sh "$app_id" -eu <<'SMOKE'
state=$(mktemp -d)
export XDG_CONFIG_HOME="$state/config" XDG_DATA_HOME="$state/data"
export XDG_CACHE_HOME="$state/cache" XDG_STATE_HOME="$state/state"
export XDG_RUNTIME_DIR="$state/runtime"
mkdir -m 700 "$XDG_RUNTIME_DIR"
gtk4-broadwayd --address=127.0.0.1 --port=0 :153 >"$state/display.log" 2>&1 &
display_pid=$!
trap 'kill "$display_pid" 2>/dev/null || true' EXIT
for i in $(seq 1 100); do
    [ ! -S "$XDG_RUNTIME_DIR/broadway154.socket" ] || break
    sleep 0.05
done
if [ ! -S "$XDG_RUNTIME_DIR/broadway154.socket" ]; then
    cat "$state/display.log" >&2
    ls -la "$XDG_RUNTIME_DIR" >&2
    exit 1
fi
halo-gtk --help

timeout 30 python3 - <<'PY'
import importlib
import importlib.metadata
import os
import pkgutil
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst, Gtk

import halo_gtk
from halo_gtk.app import RingApplication
from halo_gtk.diagnostics import _diagnostics_text

modules = list(pkgutil.walk_packages(halo_gtk.__path__, halo_gtk.__name__ + "."))
for module in modules:
    importlib.import_module(module.name)
assert Path(halo_gtk.__file__).is_relative_to("/app")
assert importlib.metadata.version("halo-gtk")
diagnostics = _diagnostics_text([]).splitlines()
assert "Install profile: Flatpak" in diagnostics
assert f"Flatpak deployment commit: {os.environ['HALO_SMOKE_COMMIT']}" in diagnostics
assert Gio.SettingsSchemaSource.get_default().lookup("io.github.JamesFromFL.HaloGtk", True)
Gtk.init()
Gst.init(None)
for factory in ("playbin", "appsink", "appsrc", "gtk4paintablesink"):
    assert Gst.ElementFactory.find(factory), factory

app = RingApplication()
pipeline = Gst.parse_launch(
    "videotestsrc num-buffers=4 ! video/x-raw,format=RGBA,width=160,height=90 ! "
    "gtk4paintablesink name=preview sync=false"
)
paintable = pipeline.get_by_name("preview").get_property("paintable")
result = []

def inspect():
    window = app.get_main_window()
    if not window or not window.get_mapped():
        return GLib.SOURCE_CONTINUE
    if pipeline.current_state == Gst.State.NULL:
        pipeline.set_state(Gst.State.PLAYING)
    error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
    if error:
        result.append(str(error.parse_error()[0]))
    elif paintable.get_intrinsic_width() == 160 and paintable.get_intrinsic_height() == 90:
        result.append("rendered")
    else:
        return GLib.SOURCE_CONTINUE
    pipeline.set_state(Gst.State.NULL)
    app.quit()
    return GLib.SOURCE_REMOVE

GLib.timeout_add(50, inspect)
assert app.run(["halo-gtk"]) == 0 and result == ["rendered"], result
print(f"Mapped the installed window, rendered a synthetic frame, and imported {len(modules)} modules")
PY
SMOKE
