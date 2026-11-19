"""Managed release transactions use temporary Git repositories and fake runtimes."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/managed_install.py"
spec = importlib.util.spec_from_file_location("managed_install", SCRIPT)
managed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(managed)


def git(repository, *arguments):
    return subprocess.check_output(["git", "-C", str(repository), *arguments], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "source checkout"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "synthetic@example.invalid")
    git(path, "config", "user.name", "Synthetic fixture")
    (path / "pyproject.toml").write_text('[project]\nname = "halo-gtk"\n')
    (path / "uv.lock").write_text("version = 1\n")
    (path / "payload").write_text("committed content")
    git(path, "add", ".")
    git(path, "commit", "-qm", "Synthetic release one")
    return path


@pytest.fixture
def manager(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    result = managed.Manager(home / ".local/share/halo-gtk/managed", home=home)
    monkeypatch.setattr(managed.Manager, "_build", fake_build)
    monkeypatch.setattr(managed.Manager, "_health", fake_health)
    return result


def fake_build(self, release, *, uv, python):
    assert release.parent == self.root / "releases"
    assert managed.RELEASE_ID.fullmatch(release.name)
    executable = release / ".venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['HALO_TEST_RESULT']).write_text(json.dumps({'args':sys.argv[1:],"
        "'schema':os.environ['GSETTINGS_SCHEMA_DIR']}))\n"
        "if '--hold' in sys.argv:\n"
        "    while not Path(os.environ['HALO_TEST_RELEASE']).exists(): time.sleep(.01)\n"
    )
    executable.chmod(0o755)
    (release / "assets/schemas").mkdir(parents=True)
    (release / "assets/icon.png").write_bytes(b"synthetic icon")


def fake_health(self, release):
    assert (release / ".venv/bin/python").exists()
    return {"python": "synthetic", "gtk": [4, 22], "adw": [1, 9]}


def stage(manager, repository):
    with manager.lock():
        return manager.stage(repository, "HEAD")


def activate(manager, name, **kwargs):
    with manager.lock():
        manager.activate(name, **kwargs)


def two_releases(manager, repository):
    old = stage(manager, repository)
    activate(manager, old)
    (repository / "payload").write_text("second committed version")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Synthetic release two")
    return old, stage(manager, repository)


def snapshots(manager):
    return {key: managed.file_snapshot(path) for key, path in manager.assets.items()}


def test_stage_uses_only_committed_ref_and_keeps_final_environment_path(manager, repository):
    committed = git(repository, "rev-parse", "HEAD")
    (repository / "payload").write_text("dirty edit preserved")
    (repository / "untracked").write_text("private data never staged")
    name = stage(manager, repository)
    release = manager.release(name)
    assert manager.manifest(name)["revision"] == committed
    assert (release / "source/payload").read_text() == "committed content"
    assert not (release / "source/untracked").exists()
    assert (repository / "payload").read_text() == "dirty edit preserved"
    assert manager.pointer("current") is None
    assert not any(path.exists() for path in manager.assets.values())
    assert manager.status()["releases"][0]["validated"] is True


@pytest.mark.parametrize("ref", ["does-not-exist", "--output=/tmp/unexpected", "HEAD:payload"])
def test_stage_requires_local_commit(manager, repository, ref):
    with manager.lock(), pytest.raises(managed.InstallError):
        manager.stage(repository, ref)
    assert manager.pointer("current") is None


def test_source_symlink_is_rejected_without_following_target(manager, repository, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (repository / "link").symlink_to(outside)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Unsafe source fixture")
    with pytest.raises(managed.InstallError, match="Unsafe"):
        stage(manager, repository)
    assert outside.read_text() == "keep"


@pytest.mark.parametrize("failure", ["build", "health"])
def test_stage_failure_preserves_active_release_and_keeps_candidate_diagnostics(
    manager, repository, monkeypatch, failure
):
    old = stage(manager, repository)
    activate(manager, old)
    before = snapshots(manager)

    def fail(*args, **kwargs):
        raise managed.InstallError("synthetic qualification failure")

    git(repository, "commit", "--allow-empty", "-qm", "Candidate with a new revision")
    monkeypatch.setattr(manager, "_build" if failure == "build" else "_health", fail)
    with pytest.raises(managed.InstallError):
        stage(manager, repository)
    assert manager.pointer("current") == old
    assert snapshots(manager) == before
    assert any(not item["validated"] for item in manager.status()["releases"])


def test_activate_rollback_twice_preserves_shared_user_data(manager, repository):
    settings = manager.home / ".config/halo-gtk/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"existing":true}')
    media = manager.root.parent / "recording.mp4"
    media.write_bytes(b"user media")
    old, new = two_releases(manager, repository)
    activate(manager, new)
    assert manager.pointer("current") == new
    assert manager.pointer("previous") == old
    with manager.lock():
        manager.rollback()
        assert manager.pointer("current") == old
        manager.rollback()
        assert manager.pointer("current") == new
    assert settings.read_bytes() == b'{"existing":true}'
    assert media.read_bytes() == b"user media"
    assert not manager.journal.exists()


def test_explicit_checkout_migration_restores_exact_original_files(manager, repository):
    for key, path in manager.assets.items():
        path.parent.mkdir(parents=True)
        path.write_bytes(("original " + key + " $%\\\n").encode())
        path.chmod(0o700 if key == "launcher" else 0o600)
    originals = snapshots(manager)
    name = stage(manager, repository)
    with pytest.raises(managed.InstallError, match="replace-checkout"):
        activate(manager, name)
    assert snapshots(manager) == originals
    activate(manager, name, replace_checkout=True)
    assert snapshots(manager) != originals
    with manager.lock():
        manager.uninstall()
    assert snapshots(manager) == originals
    assert not list((manager.root / "releases").iterdir())
    assert manager.pointer("current") is None


@pytest.mark.parametrize(
    "point",
    [
        "prepared",
        "asset-launcher",
        "asset-desktop",
        "published",
        "previous",
        "current",
        "activated",
    ],
)
def test_handled_transaction_failure_restores_old_release(manager, repository, monkeypatch, point):
    old, new = two_releases(manager, repository)
    before = snapshots(manager)

    def fail(name):
        if name == point:
            raise OSError("synthetic full disk")

    monkeypatch.setattr(manager, "checkpoint", fail)
    with pytest.raises(OSError, match="full disk"):
        activate(manager, new)
    assert manager.pointer("current") == old
    assert manager.pointer("previous") is None
    assert snapshots(manager) == before
    assert not manager.journal.exists()


def test_post_activation_health_failure_rolls_back(manager, repository, monkeypatch):
    old, new = two_releases(manager, repository)
    before = snapshots(manager)

    def health(release):
        if manager.pointer("current") == new:
            raise managed.InstallError("shared library unavailable")
        return fake_health(manager, release)

    monkeypatch.setattr(manager, "_health", health)
    with pytest.raises(managed.InstallError, match="shared library"):
        activate(manager, new)
    assert manager.pointer("current") == old
    assert snapshots(manager) == before


@pytest.mark.parametrize(
    "point",
    [
        "prepared",
        "asset-launcher",
        "asset-desktop",
        "published",
        "previous",
        "current",
        "activated",
        "committed",
    ],
)
def test_actual_process_exit_recovery_is_idempotent(manager, repository, point):
    old, new = two_releases(manager, repository)
    before = snapshots(manager)
    code = """
import importlib.util, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('managed', sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
manager=m.Manager(Path(sys.argv[2]), home=Path(sys.argv[3]))
manager._health=lambda *_: {'synthetic':True}
manager.checkpoint=lambda name: os._exit(77) if name==sys.argv[5] else None
with manager.lock(): manager.activate(sys.argv[4])
"""
    result = subprocess.run(
        [sys.executable, "-c", code, SCRIPT, manager.root, manager.home, new, point],
        timeout=10,
        check=False,
    )
    assert result.returncode == 77
    assert manager.journal.exists()
    with manager.lock():
        assert manager.recover()
        assert not manager.recover()
    assert manager.pointer("current") == (new if point == "committed" else old)
    assert snapshots(manager) == before


def test_one_shot_fsync_failure_is_recoverable(manager, repository, monkeypatch):
    old, new = two_releases(manager, repository)
    original = managed.os.fsync
    failed = False

    def fail_once(fd):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("synthetic fsync failure")
        return original(fd)

    monkeypatch.setattr(managed.os, "fsync", fail_once)
    with pytest.raises(OSError, match="fsync"):
        activate(manager, new)
    with manager.lock():
        manager.recover()
    assert manager.pointer("current") == old


@pytest.mark.parametrize("operation", ["activate", "uninstall", "rollback"])
def test_running_release_lease_defers_without_signalling(manager, repository, operation):
    old, new = two_releases(manager, repository)
    activate(manager, new)
    with manager.lease(new, shared=True), manager.lock(), pytest.raises(managed.Busy):
        if operation == "activate":
            manager.activate(old)
        else:
            getattr(manager, operation)()
    assert manager.pointer("current") == new


def test_lock_serializes_installation_and_launcher(manager):
    with manager.lock():
        with pytest.raises(managed.Busy), manager.lock():
            pass
        with pytest.raises(managed.Busy), manager.lock(shared=True):
            pass


@pytest.mark.parametrize("asset", ["launcher", "desktop"])
def test_changed_desktop_file_is_never_overwritten_or_removed(manager, repository, asset):
    old, new = two_releases(manager, repository)
    manager.assets[asset].write_text("external modification")
    with pytest.raises(managed.InstallError, match="changed"):
        activate(manager, new)
    with manager.lock(), pytest.raises(managed.InstallError, match="changed"):
        manager.uninstall()
    assert manager.assets[asset].read_text() == "external modification"
    assert manager.pointer("current") == old


def test_uninstall_cleanup_interruption_retries_only_owned_releases(
    manager, repository, monkeypatch
):
    old, new = two_releases(manager, repository)
    activate(manager, new)

    class Interrupted(BaseException):
        pass

    def fail(name):
        if name.startswith("removed-"):
            raise Interrupted()

    monkeypatch.setattr(manager, "checkpoint", fail)
    with manager.lock(), pytest.raises(Interrupted):
        manager.uninstall()
    assert manager.journal.exists()
    assert manager.pointer("current") is None
    monkeypatch.setattr(manager, "checkpoint", lambda *_: None)
    with manager.lock():
        manager.recover()
        manager.uninstall()
    assert not list((manager.root / "releases").iterdir())
    assert not any(path.exists() for path in manager.assets.values())


def test_uninstall_asset_failure_restores_active_release(manager, repository, monkeypatch):
    old = stage(manager, repository)
    activate(manager, old)
    before = snapshots(manager)

    def fail(point):
        if point == "uninstall-launcher":
            raise OSError("synthetic publication failure")

    monkeypatch.setattr(manager, "checkpoint", fail)
    with manager.lock(), pytest.raises(OSError):
        manager.uninstall()
    assert manager.pointer("current") == old
    assert snapshots(manager) == before


@pytest.mark.parametrize("target", ["root", "release", "launcher", "desktop", "lock"])
def test_symlink_boundaries_are_rejected(manager, repository, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    preserved = outside / "preserved"
    preserved.write_text("untouched")
    if target == "root":
        path = tmp_path / "link"
        path.symlink_to(outside, target_is_directory=True)
        with pytest.raises(managed.InstallError):
            managed.Manager(path / "halo-gtk/managed", home=manager.home)
    elif target == "release":
        name = "a" * 12 + "-" + "b" * 12 + "-" + "c" * 12
        (manager.root / "releases" / name).symlink_to(outside)
        with pytest.raises(managed.InstallError):
            manager.manifest(name)
    elif target == "lock":
        (manager.root / "install.lock").symlink_to(preserved)
        with pytest.raises(OSError), manager.lock():
            pass
    else:
        name = stage(manager, repository)
        path = manager.assets[target]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(preserved)
        with pytest.raises(OSError):
            activate(manager, name, replace_checkout=True)
    assert preserved.read_text() == "untouched"


@pytest.mark.parametrize("target", ["/etc", "releases/../outside", "releases/invalid"])
def test_unsafe_release_pointer_is_rejected(manager, target):
    (manager.root / "current").symlink_to(target)
    with pytest.raises(managed.InstallError, match="Unsafe"):
        manager.status()


def test_mutated_lock_and_data_format_block_activation(manager, repository):
    name = stage(manager, repository)
    lock = manager.release(name) / "source/uv.lock"
    lock.write_text("different dependency graph")
    with pytest.raises(managed.InstallError, match="lock changed"):
        activate(manager, name)
    other = stage(manager, repository)
    manifest_path = manager.release(other) / "release.json"
    manifest = managed.json_read(manifest_path)
    manifest["data_format"] = 2
    managed.json_write(manifest_path, manifest)
    with pytest.raises(managed.InstallError, match="Unsupported"):
        activate(manager, other)


@pytest.mark.parametrize("xdg", ["", "relative/path"])
def test_empty_relative_xdg_uses_home(tmp_path, monkeypatch, xdg):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", xdg)
    assert managed.default_root() == tmp_path / ".local/share/halo-gtk/managed"


def test_special_path_launcher_arguments_and_concrete_release_lease(
    tmp_path, repository, monkeypatch
):
    home = tmp_path / "home $() &|%\"'`\\"
    home.mkdir()
    manager = managed.Manager(home / ".local/share/halo-gtk/managed", home=home)
    monkeypatch.setattr(managed.Manager, "_build", fake_build)
    monkeypatch.setattr(managed.Manager, "_health", fake_health)
    old, new = two_releases(manager, repository)
    output = tmp_path / "result.json"
    release_signal = tmp_path / "allow-exit"
    env = {**os.environ, "HALO_TEST_RESULT": str(output), "HALO_TEST_RELEASE": str(release_signal)}
    arguments = ["--hold", "two words", "literal $%\\\"'`"]
    process = subprocess.Popen([manager.assets["launcher"], *arguments], env=env)
    try:
        deadline = time.monotonic() + 5
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert output.exists()
        result = json.loads(output.read_text())
        assert result["args"] == ["-I", "-m", "halo_gtk", *arguments]
        assert result["schema"] == str(manager.release(old) / "assets/schemas")
        with pytest.raises(managed.Busy):
            activate(manager, new)
        release_signal.touch()
        assert process.wait(timeout=5) == 0
        activate(manager, new)
        assert manager.pointer("current") == new
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    from halo_gtk.desktop_entry import quote_exec_argument

    assert (
        "Exec=" + quote_exec_argument(str(manager.assets["launcher"]))
        in manager.assets["desktop"].read_text()
    )


def test_build_commands_pin_tools_and_frozen_lock(manager, tmp_path, monkeypatch):
    release = manager.release("a" * 12 + "-" + "b" * 12 + "-" + "c" * 12)
    source = release / "source/data"
    source.mkdir(parents=True)
    (source / f"{managed.APP_ID}.gschema.xml").write_text("schema")
    icon = source / "icons/hicolor/256x256/apps" / f"{managed.APP_ID}.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"icon")
    calls = []

    def run(arguments, **kwargs):
        calls.append(([str(arg) for arg in arguments], kwargs))
        return "uv " + managed.UV_VERSION

    monkeypatch.setattr(manager, "_run", run)
    # Fixture replaces _build on the class; get the original from an isolated module.
    fresh_spec = importlib.util.spec_from_file_location("build_contract", SCRIPT)
    fresh = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    fresh.Manager._build(manager, release, uv="pinned-uv", python="/system/python")
    assert calls[0][0] == ["pinned-uv", "--version"]
    assert calls[1][0][-3:] == ["lock", "--check", "--offline"]
    assert "--system-site-packages" in calls[2][0]
    assert calls[3][0][-6:] == [
        "sync",
        "--frozen",
        "--no-dev",
        "--no-group",
        "audit",
        "--no-editable",
    ]
    assert calls[3][1]["env"]["UV_PROJECT_ENVIRONMENT"] == str(release / ".venv")
    assert calls[3][1]["env"]["UV_PYTHON_DOWNLOADS"] == "never"
    assert calls[-1][0][0:2] == ["glib-compile-schemas", "--strict"]


def test_subprocess_timeout_kills_own_group_preserves_unrelated_process(tmp_path):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child_file = tmp_path / "child.pid"
    code = """
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path(sys.argv[1]).write_text(str(child.pid))
print('bounded command started', flush=True)
time.sleep(30)
"""
    try:
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            managed.command(
                [sys.executable, "-c", code, child_file],
                cwd=tmp_path,
                log=tmp_path / "command.log",
                timeout=0.2,
            )
        assert time.monotonic() - start < 5
        assert unrelated.poll() is None
        assert b"bounded command started" in (tmp_path / "command.log").read_bytes()
        child = Path("/proc") / child_file.read_text() / "stat"
        deadline = time.monotonic() + 2
        while (
            child.exists() and child.read_text().split()[2] != "Z" and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not child.exists() or child.read_text().split()[2] == "Z"
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_command_nonzero_retains_actionable_log(tmp_path):
    with pytest.raises(managed.InstallError, match="explanation"):
        managed.command(
            [sys.executable, "-c", "print('explanation'); raise SystemExit(3)"],
            cwd=tmp_path,
            log=tmp_path / "failed.log",
        )


def test_health_is_isolated_and_does_not_start_account_or_media(manager, repository, monkeypatch):
    name = stage(manager, repository)
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return '{"python":"synthetic"}'

    monkeypatch.setattr(manager, "_run", run)
    fresh_spec = importlib.util.spec_from_file_location("health_contract", SCRIPT)
    fresh = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    fresh.Manager._health(manager, manager.release(name))
    assert len(calls) == 2
    for _arguments, kwargs in calls:
        assert kwargs["env"]["HOME"] != str(manager.home)
        assert kwargs["env"]["GSETTINGS_BACKEND"] == "memory"
        assert "/no-session-bus" in kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"]
        assert kwargs["timeout"] <= 45
    assert calls[1][0][-3:] == ["-m", "halo_gtk", "--help"]
    assert "ElementFactory.find" in fresh.HEALTH_CODE
    assert "ElementFactory.make" not in fresh.HEALTH_CODE


@pytest.mark.parametrize(
    "missing", ["old", "new", "previous", "installation_before", "before", "after"]
)
def test_truncated_journal_fails_gracefully_without_changing_files(manager, repository, missing):
    old, new = two_releases(manager, repository)
    before = snapshots(manager)
    transaction = {
        "format": 1,
        "phase": "prepared",
        "old": old,
        "new": new,
        "previous": None,
        "before": before,
        "after": before,
        "installation_before": managed.file_snapshot(manager.installation),
    }
    del transaction[missing]
    managed.json_write(manager.journal, transaction)
    with manager.lock(), pytest.raises(managed.InstallError, match="Invalid recovery"):
        manager.recover()
    assert manager.pointer("current") == old
    assert snapshots(manager) == before


def test_known_checkout_process_defers_explicit_migration(manager, repository, tmp_path):
    executable = tmp_path / "checkout/.venv/bin/halo-gtk"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
    executable.chmod(0o755)
    launcher = manager.assets["launcher"]
    launcher.parent.mkdir(parents=True)
    import shlex

    launcher.write_text("#!/usr/bin/env bash\nexec " + shlex.quote(str(executable)) + ' "$@"\n')
    launcher.chmod(0o755)
    name = stage(manager, repository)
    process = subprocess.Popen([launcher])
    try:
        deadline = time.monotonic() + 3
        while not managed.checkout_running(launcher) and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(managed.Busy, match="checkout app is running"):
            activate(manager, name, replace_checkout=True)
        assert process.poll() is None
        assert manager.pointer("current") is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("background", [False, True])
def test_uninstall_removes_only_exact_new_managed_autostart(manager, repository, background):
    name = stage(manager, repository)
    activate(manager, name)
    command = managed.quote_exec(str(manager.assets["launcher"]))
    if background:
        command += " --background"
    contents = (
        "[Desktop Entry]\nType=Application\nName=Halo\nComment=Start Halo\n"
        f"Exec={command}\nIcon={managed.APP_ID}\nTerminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    manager.autostart.parent.mkdir(parents=True)
    manager.autostart.write_text(contents)
    with manager.lock():
        manager.uninstall()
    assert not manager.autostart.exists()


def test_uninstall_preserves_custom_autostart(manager, repository):
    name = stage(manager, repository)
    activate(manager, name)
    manager.autostart.parent.mkdir(parents=True)
    manager.autostart.write_text("custom user autostart")
    with manager.lock():
        manager.uninstall()
    assert manager.autostart.read_text() == "custom user autostart"


def test_uninstall_refuses_unowned_release_before_removing_launcher(manager, repository):
    name = stage(manager, repository)
    activate(manager, name)
    before = snapshots(manager)
    unknown = manager.release("a" * 12 + "-" + "b" * 12 + "-" + "c" * 12)
    (unknown / "unrelated").write_text("preserve")
    with manager.lock(), pytest.raises(managed.InstallError):
        manager.uninstall()
    assert snapshots(manager) == before
    assert manager.pointer("current") == name
    assert (unknown / "unrelated").read_text() == "preserve"


def test_modified_file_during_crash_recovery_is_preserved(manager, repository, monkeypatch):
    old, new = two_releases(manager, repository)

    class Interrupted(BaseException):
        pass

    def fail(name):
        if name == "current":
            raise Interrupted()

    monkeypatch.setattr(manager, "checkpoint", fail)
    with pytest.raises(Interrupted):
        activate(manager, new)
    manager.assets["desktop"].write_text("outside edit")
    with manager.lock(), pytest.raises(managed.InstallError, match="outside"):
        manager.recover()
    assert manager.assets["desktop"].read_text() == "outside edit"
    assert manager.journal.exists()


def test_wrappers_managed_branch_precedes_checkout_mutations():
    scripts = SCRIPT.parent
    for name, command in (("install", "install"), ("update", "update"), ("uninstall", "uninstall")):
        text = (scripts / f"{name}.sh").read_text()
        assert '"${1:-}" == "--managed"' in text
        assert f'managed_install.py" {command}' in text
        if name == "update":
            assert text.index('managed_install.py" update') < text.index("pull --ff-only")


def test_uninstall_does_not_follow_replaced_asset_parent(manager, repository, tmp_path):
    name = stage(manager, repository)
    activate(manager, name)
    launcher = manager.assets["launcher"]
    original_parent = launcher.parent
    moved = tmp_path / "user-directory"
    original_parent.rename(moved)
    original_parent.symlink_to(moved, target_is_directory=True)
    before = (moved / "halo-gtk").read_bytes()
    with manager.lock(), pytest.raises(managed.InstallError, match="symlink"):
        manager.uninstall()
    assert (moved / "halo-gtk").read_bytes() == before
    assert manager.pointer("current") == name


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
@pytest.mark.parametrize("profile", ["active", "changed-xdg", "interrupted"])
def test_plain_wrappers_preserve_managed_assets(manager, repository, action, profile, tmp_path):
    name = stage(manager, repository)
    activate(manager, name)
    data = manager.root.parent.parent
    if profile == "changed-xdg":
        data = tmp_path / "new data"
    elif profile == "interrupted":
        manager.installation.rename(manager.journal)
        manager.assets["launcher"].unlink()
    before = snapshots(manager)
    current = manager.pointer("current")
    result = subprocess.run(
        [SCRIPT.parent / f"{action}.sh"],
        env={**os.environ, "HOME": str(manager.home), "XDG_DATA_HOME": str(data)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "managed installer owns" in result.stderr
    assert str(manager.root) in result.stderr
    assert snapshots(manager) == before and manager.pointer("current") == current


def test_repeated_stage_and_prune_preserve_active_previous_and_leased(manager, repository):
    first, second = two_releases(manager, repository)
    activate(manager, second)
    assert stage(manager, repository) == second
    git(repository, "commit", "--allow-empty", "-qm", "Unactivated candidate")
    extra = stage(manager, repository)
    with manager.lease(extra, shared=True):
        assert manager.prune() == []
    assert manager.prune() == [extra]
    assert manager.pointer("current") == second and manager.pointer("previous") == first


def test_active_launcher_available_while_building(manager, repository, monkeypatch, tmp_path):
    first = stage(manager, repository)
    activate(manager, first)
    git(repository, "commit", "--allow-empty", "-qm", "Stage a new revision")
    result = tmp_path / "launch.json"

    def build_during_launch(release, *, uv, python):
        subprocess.run(
            [manager.assets["launcher"]],
            check=True,
            timeout=5,
            env={**os.environ, "HALO_TEST_RESULT": str(result)},
        )
        assert json.loads(result.read_text())["schema"] == str(
            manager.release(first) / "assets/schemas"
        )
        fake_build(manager, release, uv=uv, python=python)

    monkeypatch.setattr(manager, "_build", build_during_launch)
    monkeypatch.setattr(managed, "Manager", lambda root: manager)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "stage", "--repository", str(repository)])
    managed.main()
