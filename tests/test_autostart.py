"""Tests for user autostart integration."""


def test_set_enabled_writes_user_autostart_file(tmp_path, monkeypatch):
    from halo_gtk import autostart

    autostart_file = tmp_path / "autostart" / "io.github.JamesFromFL.HaloGtk.desktop"
    monkeypatch.setattr(autostart, "AUTOSTART_DIR", autostart_file.parent)
    monkeypatch.setattr(autostart, "AUTOSTART_FILE", autostart_file)
    monkeypatch.setattr(autostart.shutil, "which", lambda _name: "/home/test/.local/bin/halo-gtk")

    autostart.set_enabled(True)

    contents = autostart_file.read_text()
    assert 'Exec="/home/test/.local/bin/halo-gtk"\n' in contents
    assert autostart.is_enabled() is True


def test_set_enabled_background_writes_background_command(tmp_path, monkeypatch):
    from halo_gtk import autostart

    autostart_file = tmp_path / "autostart" / "io.github.JamesFromFL.HaloGtk.desktop"
    monkeypatch.setattr(autostart, "AUTOSTART_DIR", autostart_file.parent)
    monkeypatch.setattr(autostart, "AUTOSTART_FILE", autostart_file)
    monkeypatch.setattr(autostart.shutil, "which", lambda _name: "/home/test/.local/bin/halo-gtk")

    autostart.set_enabled(True, background=True)

    contents = autostart_file.read_text()
    assert 'Exec="/home/test/.local/bin/halo-gtk" --background' in contents


def test_set_enabled_false_removes_user_autostart_file(tmp_path, monkeypatch):
    from halo_gtk import autostart

    autostart_file = tmp_path / "autostart" / "io.github.JamesFromFL.HaloGtk.desktop"
    autostart_file.parent.mkdir()
    autostart_file.write_text("[Desktop Entry]\n")
    monkeypatch.setattr(autostart, "AUTOSTART_DIR", autostart_file.parent)
    monkeypatch.setattr(autostart, "AUTOSTART_FILE", autostart_file)

    autostart.set_enabled(False)

    assert autostart.is_enabled() is False


def test_autostart_preserves_special_characters_in_executable(tmp_path, monkeypatch):
    from gi.repository import GLib

    from halo_gtk import autostart

    executable = '/home/a $() `x` &|%"\\/halo-gtk'
    autostart_file = tmp_path / "halo.desktop"
    monkeypatch.setattr(autostart, "AUTOSTART_DIR", tmp_path)
    monkeypatch.setattr(autostart, "AUTOSTART_FILE", autostart_file)
    monkeypatch.setattr(autostart.shutil, "which", lambda _name: executable)

    autostart.set_enabled(True, background=True)

    desktop = GLib.KeyFile()
    desktop.load_from_file(str(autostart_file), GLib.KeyFileFlags.NONE)
    command = desktop.get_string("Desktop Entry", "Exec").replace("%%", "%")
    valid, arguments = GLib.shell_parse_argv(command)
    assert valid
    assert arguments == [executable, "--background"]
