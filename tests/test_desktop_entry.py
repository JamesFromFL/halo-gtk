"""Literal argument handling for generated desktop files and process targeting."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from gi.repository import GLib

from halo_gtk.desktop_entry import quote_exec_argument


@pytest.mark.parametrize("path", ["/plain/halo", '/home/a $() `x` &|%"\\/halo'])
def test_desktop_exec_survives_both_desktop_and_shell_parsing(path):
    key_file = GLib.KeyFile()
    data = "[Desktop Entry]\nType=Application\nName=Halo\nExec=" + quote_exec_argument(path) + "\n"
    key_file.load_from_data(data, len(data), GLib.KeyFileFlags.NONE)
    # %% is the Desktop Entry literal-percent field code.
    command = key_file.get_string("Desktop Entry", "Exec").replace("%%", "%")
    valid, arguments = GLib.shell_parse_argv(command)
    assert valid
    assert arguments == [path]


@pytest.mark.parametrize("value", ["", "/path\nExec=bad", "/path\0bad"])
def test_desktop_exec_rejects_control_characters(value):
    with pytest.raises(ValueError):
        quote_exec_argument(value)


def test_process_target_requires_exact_owned_entrypoint_argument():
    path = Path(__file__).resolve().parents[1] / "scripts/desktop_integration.py"
    spec = importlib.util.spec_from_file_location("desktop_integration_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = "/checkout [special]+/.venv/bin/halo-gtk"
    assert module._matches_process(["/usr/bin/python3", target], target)
    assert module._matches_process([target], target)
    assert not module._matches_process(["/usr/bin/editor", target], target)
    assert not module._matches_process(["/usr/bin/python3", target + "-other"], target)
    assert not module._matches_process(["/usr/bin/python3", "/different", target], target)


def test_stop_targets_only_matching_app_process_in_isolated_checkout(tmp_path):
    target = tmp_path / 'checkout [special]+ $()"' / ".venv/bin/halo-gtk"
    target.parent.mkdir(parents=True)
    target.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    unrelated = tmp_path / "other.py"
    unrelated.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    app = subprocess.Popen([sys.executable, target])
    other = subprocess.Popen([sys.executable, unrelated, target])
    helper = Path(__file__).resolve().parents[1] / "scripts/desktop_integration.py"
    try:
        subprocess.run([sys.executable, helper, "stop", target], check=True, timeout=10)
        assert app.wait(timeout=2) == -15
        assert other.poll() is None
    finally:
        for process in (app, other):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
