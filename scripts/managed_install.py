"""Opt-in staged Halo releases; no account access, app stopping or Git mutation.

Only this module's managed paths and two explicitly owned desktop files are
published. The checkout installer remains a separate, unchanged default.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

APP_ID = "io.github.JamesFromFL.HaloGtk"
UV_VERSION = "0.11.24"
FORMAT = 1
RELEASE_ID = re.compile(r"[0-9a-f]{12}-[0-9a-f]{12}-[0-9a-f]{12}\Z")
MAX_FILE = 256 * 1024

# Capability discovery does not construct sources, sinks or a Ring client.
HEALTH_CODE = r"""
import importlib, json, pkgutil, sys
import gi
for name, version in {"Adw":"1", "GdkPixbuf":"2.0", "GioUnix":"2.0",
                      "Graphene":"1.0", "Gst":"1.0", "Gtk":"4.0",
                      "Notify":"0.7", "Secret":"1"}.items():
    gi.require_version(name, version)
    getattr(__import__("gi.repository", fromlist=[name]), name)
from gi.repository import Adw, Gio, Gst, Gtk
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5), "libadwaita < 1.5"
Gst.init(None)
for name in ("appsrc", "appsink", "audioconvert", "audioresample", "capsfilter",
             "pulsesrc", "queue", "videoconvert", "volume", "gtk4paintablesink", "playbin"):
    assert Gst.ElementFactory.find(name), "Missing GStreamer element: " + name
assert any(Gst.ElementFactory.find(n) for n in ("pulsesink", "pipewiresink", "autoaudiosink"))
import halo_gtk
for module in pkgutil.walk_packages(halo_gtk.__path__, "halo_gtk."):
    importlib.import_module(module.name)
assert Gio.SettingsSchemaSource.get_default().lookup("io.github.JamesFromFL.HaloGtk", True)
print(json.dumps({"python": sys.version, "python_executable": sys.executable,
                  "gtk": [Gtk.get_major_version(), Gtk.get_minor_version()],
                  "adw": [Adw.get_major_version(), Adw.get_minor_version()],
                  "gstreamer": Gst.version_string()}))
"""


class InstallError(RuntimeError):
    """A handled failure with the current release preserved or recoverable."""


class Busy(InstallError):
    """Activation or removal must wait for a running app/installer."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_directory(path: Path, *, owned: bool = True) -> None:
    """Reject symlinks anywhere, and foreign ownership at installer boundaries."""
    if not path.is_absolute() or any(ord(c) < 32 for c in str(path)):
        raise InstallError(f"Invalid absolute path: {path}")
    for parent in reversed((path, *path.parents)):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise InstallError(f"Directory is a symlink or non-directory: {parent}")
            if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
                raise InstallError(f"Unsafe writable ancestor: {parent}")
        else:
            parent.mkdir(mode=0o700)
    if owned and path.stat().st_uid != os.getuid():
        raise InstallError(f"Directory is owned by another user: {path}")


def read_regular(path: Path, *, limit: int = MAX_FILE) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            raise InstallError(f"Not an owned bounded regular file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise InstallError(f"File grew beyond its limit: {path}")
        return data
    finally:
        os.close(fd)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    if len(data) > MAX_FILE:
        raise InstallError("Installer metadata exceeds the safe file limit")
    safe_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_regular(path)
    fd, temporary = tempfile.mkstemp(prefix=".halo-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_write(path: Path, value) -> None:
    atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def json_read(path: Path):
    try:
        return json.loads(read_regular(path))
    except (ValueError, OSError) as exc:
        raise InstallError(f"Cannot read installation metadata: {path}: {exc}") from exc


def file_snapshot(path: Path):
    if path.parent.exists() or path.parent.is_symlink():
        safe_directory(path.parent)
    if not path.exists() and not path.is_symlink():
        return None
    return {
        "data": base64.b64encode(read_regular(path)).decode(),
        "mode": stat.S_IMODE(path.lstat().st_mode),
    }


def validate_snapshot(value) -> None:
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"data", "mode"}
        or type(value["mode"]) is not int
        or not 0 <= value["mode"] <= 0o777
        or not isinstance(value["data"], str)
    ):
        raise InstallError("Invalid saved file metadata")
    try:
        data = base64.b64decode(value["data"], validate=True)
    except ValueError as exc:
        raise InstallError("Invalid saved file data") from exc
    if len(data) > MAX_FILE:
        raise InstallError("Oversized saved file")


def restore_file(path: Path, value) -> None:
    validate_snapshot(value)
    safe_directory(path.parent)
    if value is None:
        if path.exists() or path.is_symlink():
            read_regular(path)
            path.unlink()
            sync_directory(path.parent)
    else:
        atomic_write(path, base64.b64decode(value["data"]), value["mode"])


def checkout_running(launcher: Path) -> bool:
    """Recognize only literal launcher/executable argv, never signal any process."""
    targets = {str(launcher)}
    snapshot = file_snapshot(launcher)
    if snapshot:
        contents = base64.b64decode(snapshot["data"]).decode(errors="replace")
        try:
            words = shlex.split(contents)
        except ValueError:
            words = []
        # shlex retains the shebang as two tokens unless comments=True.
        if words[:2] == ["#!/usr/bin/env", "bash"]:
            words = words[2:]
        if (
            len(words) == 3
            and words[0] == "exec"
            and words[2] == "$@"
            and Path(words[1]).is_absolute()
        ):
            targets.add(words[1])
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            arguments = [
                os.fsdecode(value) for value in (entry / "cmdline").read_bytes().split(b"\0")
            ]
            if arguments and arguments[0] in targets:
                return True
            if (
                len(arguments) > 1
                and arguments[1] in targets
                and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?t?", Path(arguments[0]).name)
            ):
                return True
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return False


def command(
    arguments, *, cwd: Path, log: Path, env=None, timeout: float = 120, output: Path | None = None
) -> str:
    """File-backed output avoids inherited-pipe deadlocks; bound the whole group."""
    safe_directory(log.parent)
    destination = output or log
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        error_stream = log.open("xb") if output else stream
        try:
            process = subprocess.Popen(
                [str(a) for a in arguments],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=error_stream,
                start_new_session=True,
            )
            try:
                status = process.wait(timeout=timeout)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                raise
            # A command is not allowed to leave background build/health jobs.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        finally:
            if output:
                error_stream.close()
    with log.open("rb") as result:
        result.seek(max(0, log.stat().st_size - 8192))
        tail = result.read().decode(errors="replace")
    if status:
        raise InstallError(f"Command failed ({status}); see {log}\n{tail}")
    return tail


def quote_exec(value: str) -> str:
    # Same freedesktop two-layer escaping as halo_gtk.desktop_entry, without GI imports.
    if not value or any(ord(c) < 32 for c in value):
        raise InstallError("Invalid desktop argument")
    value = value.replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value.replace("\\", "\\\\") + '"'


def default_root() -> Path:
    home = Path.home()
    value = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    return (value if value.is_absolute() else home / ".local/share") / "halo-gtk/managed"


class Manager:
    def __init__(self, root: Path, *, home: Path | None = None):
        self.root = root
        self.home = home or Path.home()
        safe_directory(root)
        if stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise InstallError("Managed directory must be private (mode 0700)")
        safe_directory(root / "releases")
        # Root is always <data-home>/halo-gtk/managed, also in isolated tests.
        if root.name != "managed" or root.parent.name != "halo-gtk":
            raise InstallError("Managed root must end in halo-gtk/managed")
        self.assets = {
            "launcher": self.home / ".local/bin/halo-gtk",
            "desktop": root.parent.parent / "applications" / f"{APP_ID}.desktop",
        }
        self.journal = root / "transaction.json"
        self.installation = root / "installation.json"
        config = Path(os.environ.get("XDG_CONFIG_HOME") or self.home / ".config")
        if home is not None or not config.is_absolute():
            config = self.home / ".config"
        self.autostart = config / "autostart" / f"{APP_ID}.desktop"

    @contextlib.contextmanager
    def lock(self, *, shared: bool = False):
        fd = os.open(self.root / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise InstallError("Unsafe installer lock")
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy("Another managed installation operation is in progress") from exc
            yield
        finally:
            os.close(fd)

    def release(self, name: str) -> Path:
        if not isinstance(name, str) or not RELEASE_ID.fullmatch(name):
            raise InstallError("Invalid release identifier")
        path = self.root / "releases" / name
        safe_directory(path)
        return path

    def pointer(self, name: str) -> str | None:
        path = self.root / name
        if not path.is_symlink():
            if path.exists():
                raise InstallError(f"Invalid {name} pointer")
            return None
        target = os.readlink(path)
        if not target.startswith("releases/") or not RELEASE_ID.fullmatch(target[9:]):
            raise InstallError(f"Unsafe {name} pointer")
        return target[9:]

    def set_pointer(self, name: str, value: str | None) -> None:
        self.pointer(name)
        path = self.root / name
        if value is None:
            path.unlink(missing_ok=True)
            sync_directory(self.root)
            return
        self.manifest(value)
        temporary = self.root / (".pointer-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to("releases/" + value)
            os.replace(temporary, path)
            sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def manifest(self, name: str) -> dict:
        path = self.release(name)
        value = json_read(path / "release.json")
        if (
            not isinstance(value, dict)
            or value.get("format") != FORMAT
            or value.get("release") != name
            or value.get("data_format") != 1
            or value.get("validated") is not True
        ):
            raise InstallError(f"Unsupported or unvalidated release: {name}")
        if digest(read_regular(path / "source/uv.lock", limit=16 * 1024 * 1024)) != value.get(
            "lock"
        ):
            raise InstallError(f"Release lock changed: {name}")
        return value

    @contextlib.contextmanager
    def lease(self, name: str, *, shared: bool = False):
        path = self.release(name) / "use.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise InstallError("Unsafe release lease")
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy("Halo is running from this release; close it and retry") from exc
            yield fd
        finally:
            os.close(fd)

    def checkpoint(self, name: str) -> None:
        """Transaction boundary for deterministic process-interruption tests."""

    def _run(self, arguments, *, release: Path, label: str, env=None, timeout=120, output=None):
        return command(
            arguments,
            cwd=release,
            log=release / "logs" / f"{label}-{uuid.uuid4().hex}.log",
            env=env,
            timeout=timeout,
            output=output,
        )

    def _health(self, release: Path) -> dict:
        safe_directory(release / "checks")
        with tempfile.TemporaryDirectory(prefix="health-", dir=release / "checks") as temporary:
            env = os.environ.copy()
            for variable in ("PYTHONPATH", "PYTHONHOME", "GI_TYPELIB_PATH", "LD_PRELOAD"):
                env.pop(variable, None)
            env.update(
                HOME=temporary,
                GSETTINGS_BACKEND="memory",
                GSETTINGS_SCHEMA_DIR=str(release / "assets/schemas"),
                DBUS_SESSION_BUS_ADDRESS="unix:path=" + temporary + "/no-session-bus",
                PYTHONDONTWRITEBYTECODE="1",
            )
            for kind in ("CONFIG", "CACHE", "DATA", "STATE", "RUNTIME"):
                directory = Path(temporary) / kind.lower()
                directory.mkdir(mode=0o700)
                env[f"XDG_{kind}_HOME" if kind != "RUNTIME" else "XDG_RUNTIME_DIR"] = str(directory)
            python = release / ".venv/bin/python"
            result = self._run(
                [python, "-I", "-c", HEALTH_CODE],
                release=release,
                label="capabilities",
                env=env,
                timeout=45,
            )
            self._run(
                [python, "-I", "-m", "halo_gtk", "--help"],
                release=release,
                label="entrypoint",
                env=env,
                timeout=20,
            )
            try:
                return json.loads(result.splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise InstallError("Invalid runtime health report") from exc

    def stage(
        self, repository: Path, ref: str, *, uv: str = "uv", python: str = sys.executable
    ) -> str:
        self.recover()
        temporary = self.root / ("stage-" + uuid.uuid4().hex)
        safe_directory(temporary)
        completed = False
        try:
            revision = self._run(
                [
                    "git",
                    "-C",
                    repository,
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    ref + "^{commit}",
                ],
                release=temporary,
                label="revision",
            ).strip()
            if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
                raise InstallError("Source must resolve to a local committed Git revision")
            archive = temporary / "source.tar"
            self._run(
                ["git", "-C", repository, "archive", "--format=tar", revision],
                release=temporary,
                label="archive",
                output=archive,
            )
            source = temporary / "source"
            source.mkdir()
            with tarfile.open(archive) as bundle:
                total = 0
                for member in bundle:
                    parts = Path(member.name).parts
                    total += member.size
                    if (
                        not parts
                        or member.name.startswith("/")
                        or ".." in parts
                        or not (member.isfile() or member.isdir())
                        or total > 256 * 1024 * 1024
                    ):
                        raise InstallError("Unsafe or oversized Git archive")
                    destination = source / member.name
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.extractfile(member) as incoming, destination.open("xb") as out:
                            shutil.copyfileobj(incoming, out)
                        destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            import tomllib

            project = tomllib.loads(read_regular(source / "pyproject.toml").decode())
            if project.get("project", {}).get("name") != "halo-gtk":
                raise InstallError("Source is not a Halo GTK project")
            lock = digest(read_regular(source / "uv.lock", limit=16 * 1024 * 1024))
            name = f"{revision[:12]}-{lock[:12]}-{uuid.uuid4().hex[:12]}"
            release = self.release(name)
            json_write(release / "candidate.json", {"format": FORMAT, "release": name})
            os.rename(source, release / "source")
            # No environment exists until source is at its final absolute path.
            self._build(release, uv=uv, python=python)
            health = self._health(release)
            json_write(
                release / "release.json",
                {
                    "format": FORMAT,
                    "data_format": 1,
                    "validated": True,
                    "release": name,
                    "revision": revision,
                    "lock": lock,
                    "uv": UV_VERSION,
                    "health": health,
                    "created": time.time(),
                },
            )
            self.manifest(name)
            self.checkpoint("staged")
            completed = True
            return name
        finally:
            if completed:
                shutil.rmtree(temporary)

    def _build(self, release: Path, *, uv: str, python: str):
        safe_directory(self.root / "build-cache")
        env = os.environ.copy()
        env.update(
            UV_PROJECT_ENVIRONMENT=str(release / ".venv"),
            UV_CACHE_DIR=str(self.root / "build-cache"),
            UV_PYTHON_DOWNLOADS="never",
        )
        env.pop("VIRTUAL_ENV", None)
        version = self._run([uv, "--version"], release=release, label="uv-version", env=env).split()
        if version[:2] != ["uv", UV_VERSION]:
            raise InstallError(f"Managed installation requires uv {UV_VERSION}")
        source = release / "source"
        self._run(
            [uv, "--project", source, "lock", "--check", "--offline"],
            release=release,
            label="lock",
            env=env,
        )
        self._run(
            [uv, "venv", "--system-site-packages", "--python", python, release / ".venv"],
            release=release,
            label="venv",
            env=env,
        )
        self._run(
            [
                uv,
                "--project",
                source,
                "sync",
                "--frozen",
                "--no-dev",
                "--no-group",
                "audit",
                "--no-editable",
            ],
            release=release,
            label="sync",
            env=env,
            timeout=600,
        )
        schemas = release / "assets/schemas"
        schemas.mkdir(parents=True)
        shutil.copyfile(
            source / "data" / f"{APP_ID}.gschema.xml", schemas / f"{APP_ID}.gschema.xml"
        )
        shutil.copyfile(
            source / "data/icons/hicolor/256x256/apps" / f"{APP_ID}.png",
            release / "assets/icon.png",
        )
        self._run(["glib-compile-schemas", "--strict", schemas], release=release, label="schemas")

    def installed(self):
        if not self.installation.exists():
            return None
        value = json_read(self.installation)
        if (
            not isinstance(value, dict)
            or value.get("format") != FORMAT
            or set(value.get("originals", {})) != set(self.assets)
            or set(value.get("published", {})) != set(self.assets)
        ):
            raise InstallError("Unsupported installation manifest")
        return value

    def desired_assets(self) -> dict:
        # Keep versioned bootstrap files: a launcher never sees a half-updated manager.
        contents = Path(__file__).read_bytes()
        bootstrap = self.root / ("manager-" + digest(contents)[:16] + ".py")
        if bootstrap.exists():
            if read_regular(bootstrap) != contents:
                raise InstallError("Managed bootstrap changed")
        else:
            atomic_write(bootstrap, contents)
        launcher = (
            "#!/usr/bin/env bash\nexec "
            + shlex.quote(str(Path(sys.executable).resolve()))
            + " -I "
            + shlex.quote(str(bootstrap))
            + " --root "
            + shlex.quote(str(self.root))
            + ' launch -- "$@"\n'
        )
        icon = str(self.root / "current/assets/icon.png").replace("\\", "\\\\")
        desktop = (
            "[Desktop Entry]\nName=Halo\nGenericName=Home Security\n"
            "Comment=Native GTK4 client for Ring home security\n"
            f"Exec={quote_exec(str(self.assets['launcher']))}\nIcon={icon}\n"
            "Terminal=false\nType=Application\nCategories=AudioVideo;Video;\n"
            "StartupNotify=true\nStartupWMClass=halo-gtk\nX-GNOME-UsesNotifications=true\n"
        )
        return {
            "launcher": {"data": base64.b64encode(launcher.encode()).decode(), "mode": 0o755},
            "desktop": {"data": base64.b64encode(desktop.encode()).decode(), "mode": 0o644},
        }

    def recover(self) -> bool:
        if not self.journal.exists():
            return False
        transaction = json_read(self.journal)
        if (
            not isinstance(transaction, dict)
            or transaction.get("format") != FORMAT
            or not {"old", "new", "previous", "installation_before"}.issubset(transaction)
            or transaction.get("phase") not in {"prepared", "published", "activated", "committed"}
            or not isinstance(transaction.get("before"), dict)
            or not isinstance(transaction.get("after"), dict)
            or set(transaction["before"]) != set(self.assets)
            or set(transaction["after"]) != set(self.assets)
            or transaction.get("operation", "activate") not in {"activate", "uninstall"}
        ):
            raise InstallError("Invalid recovery journal; no files changed")
        for key in ("old", "new", "previous"):
            name = transaction[key]
            if name is not None and (not isinstance(name, str) or not RELEASE_ID.fullmatch(name)):
                raise InstallError("Invalid recovery release identifier")
        for value in (
            *transaction["before"].values(),
            *transaction["after"].values(),
            transaction["installation_before"],
        ):
            validate_snapshot(value)
        if transaction.get("operation") == "uninstall":
            names = transaction.get("releases")
            if (
                not isinstance(names, list)
                or any(
                    not isinstance(name, str) or not RELEASE_ID.fullmatch(name) for name in names
                )
                or len(names) != len(set(names))
            ):
                raise InstallError("Invalid recovery cleanup identifiers")
        if "autostart_before" in transaction:
            validate_snapshot(transaction["autostart_before"])
            if transaction.get("autostart_after", "missing") is not None:
                raise InstallError("Invalid autostart recovery metadata")
        if transaction["phase"] != "committed":
            if self.pointer("current") not in (transaction["old"], transaction["new"]):
                raise InstallError("Current release changed outside the interrupted transaction")
            for key, path in self.assets.items():
                current = file_snapshot(path)
                if current not in (transaction["before"][key], transaction["after"][key]):
                    raise InstallError(f"Desktop file changed outside the transaction: {path}")
            if "autostart_before" in transaction and file_snapshot(self.autostart) not in (
                transaction["autostart_before"],
                None,
            ):
                raise InstallError("Autostart entry changed outside the transaction")
            for name in (transaction["old"], transaction["previous"]):
                if name is not None:
                    self.manifest(name)
            self.set_pointer("current", transaction["old"])
            self.set_pointer("previous", transaction["previous"])
            for key, path in self.assets.items():
                restore_file(path, transaction["before"][key])
            restore_file(self.installation, transaction["installation_before"])
            if "autostart_before" in transaction:
                restore_file(self.autostart, transaction["autostart_before"])
        elif transaction.get("operation") == "uninstall":
            self._remove_releases(transaction["releases"])
        self.journal.unlink()
        sync_directory(self.root)
        return True

    def _remove_releases(self, names):
        if not isinstance(names, list) or len(set(names)) != len(names):
            raise InstallError("Invalid release cleanup journal")
        with contextlib.ExitStack() as stack:
            paths = []
            for name in names:
                if not isinstance(name, str) or not RELEASE_ID.fullmatch(name):
                    raise InstallError("Invalid release cleanup identifier")
                path = self.root / "releases" / name
                if not path.exists() and not path.is_symlink():
                    continue
                safe_directory(path)
                self._candidate(name)
                stack.enter_context(self.lease(name))
                paths.append(path)
            for path in paths:
                shutil.rmtree(path)
                self.checkpoint("removed-" + path.name)
            sync_directory(self.root / "releases")

    def _candidate(self, name: str) -> None:
        marker = json_read(self.release(name) / "candidate.json")
        if marker != {"format": FORMAT, "release": name}:
            raise InstallError("Release was not created by this installer")

    def activate(self, name: str, *, replace_checkout: bool = False) -> None:
        self.recover()
        self.manifest(name)
        current = self.pointer("current")
        if current == name:
            return
        with contextlib.ExitStack() as stack:
            if current:
                stack.enter_context(self.lease(current))
            stack.enter_context(self.lease(name))
            self._health(self.release(name))
            installed = self.installed()
            before = {key: file_snapshot(path) for key, path in self.assets.items()}
            if installed:
                if before != installed["published"]:
                    raise InstallError(
                        "Managed desktop files were changed; refusing to overwrite them"
                    )
            elif any(before.values()) and not replace_checkout:
                raise InstallError(
                    "Existing desktop integration: use --replace-checkout explicitly"
                )
            elif installed is None and checkout_running(self.assets["launcher"]):
                raise Busy("The checkout app is running; close it before migration")
            after = self.desired_assets()
            transaction = {
                "format": FORMAT,
                "phase": "prepared",
                "old": current,
                "new": name,
                "previous": self.pointer("previous"),
                "before": before,
                "after": after,
                "installation_before": file_snapshot(self.installation),
            }
            json_write(self.journal, transaction)
            try:
                self.checkpoint("prepared")
                for key, path in self.assets.items():
                    restore_file(path, after[key])
                    self.checkpoint("asset-" + key)
                transaction["phase"] = "published"
                json_write(self.journal, transaction)
                self.checkpoint("published")
                self.set_pointer("previous", current)
                self.checkpoint("previous")
                self.set_pointer("current", name)
                self.checkpoint("current")
                json_write(
                    self.installation,
                    {
                        "format": FORMAT,
                        "originals": installed["originals"] if installed else before,
                        "published": after,
                        "autostart_original": installed.get("autostart_original")
                        if installed
                        else file_snapshot(self.autostart),
                    },
                )
                transaction["phase"] = "activated"
                json_write(self.journal, transaction)
                self.checkpoint("activated")
                self._health(self.release(name))
                transaction["phase"] = "committed"
                json_write(self.journal, transaction)
                self.checkpoint("committed")
            except Exception:
                self.recover()
                raise
            self.recover()

    def rollback(self):
        self.recover()
        previous = self.pointer("previous")
        if previous is None:
            raise InstallError("No previous managed release is available")
        self.activate(previous)

    def status(self) -> dict:
        self.recover()
        current, previous = self.pointer("current"), self.pointer("previous")
        releases = []
        for path in sorted((self.root / "releases").iterdir()):
            if not RELEASE_ID.fullmatch(path.name):
                raise InstallError("Unexpected file in managed releases")
            try:
                metadata = self.manifest(path.name)
                releases.append(
                    {"release": path.name, "revision": metadata["revision"], "validated": True}
                )
            except (InstallError, OSError):
                releases.append({"release": path.name, "validated": False})
        return {
            "current": current,
            "previous": previous,
            "releases": releases,
            "runtime_note": "Recorded validation; shared OS dependencies can change.",
        }

    def uninstall(self):
        self.recover()
        installed = self.installed()
        if installed is None:
            return
        releases = list((self.root / "releases").iterdir())
        with contextlib.ExitStack() as stack:
            for path in releases:
                self._candidate(path.name)
                stack.enter_context(self.lease(path.name))
            before = {key: file_snapshot(path) for key, path in self.assets.items()}
            if before != installed["published"]:
                raise InstallError("Managed desktop files changed; refusing to remove them")
            transaction = {
                "format": FORMAT,
                "operation": "uninstall",
                "releases": [path.name for path in releases],
                "phase": "prepared",
                "old": self.pointer("current"),
                "new": None,
                "previous": self.pointer("previous"),
                "before": before,
                "after": installed["originals"],
                "installation_before": file_snapshot(self.installation),
            }
            # Only remove Halo's exact generated entry when it did not predate
            # managed installation and no checkout launcher is being restored.
            if (
                installed.get("autostart_original") is None
                and installed["originals"]["launcher"] is None
            ):
                autostart = file_snapshot(self.autostart)
                if autostart and self._owns_autostart(autostart):
                    transaction.update(autostart_before=autostart, autostart_after=None)
            json_write(self.journal, transaction)
            try:
                for key, path in self.assets.items():
                    restore_file(path, installed["originals"][key])
                    self.checkpoint("uninstall-" + key)
                if "autostart_before" in transaction:
                    restore_file(self.autostart, None)
                    self.checkpoint("uninstall-autostart")
                self.set_pointer("current", None)
                self.set_pointer("previous", None)
                restore_file(self.installation, None)
                transaction["phase"] = "committed"
                json_write(self.journal, transaction)
            except Exception:
                self.recover()
                raise
        # Drop the preflight leases before recovery reacquires them for cleanup.
        # The journal remains until deletion succeeds; interrupted cleanup retries.
        self.recover()

    def _owns_autostart(self, snapshot) -> bool:
        command = quote_exec(str(self.assets["launcher"]))
        contents = base64.b64decode(snapshot["data"]).decode(errors="replace")
        template = (
            "[Desktop Entry]\nType=Application\nName=Halo\nComment=Start Halo\nExec={}\n"
            f"Icon={APP_ID}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
        )
        return contents in (template.format(command), template.format(command + " --background"))

    def launch(self, arguments: list[str]) -> None:
        # The installer lock closes the pointer-read/lease-acquire race.
        with self.lock(shared=True):
            if self.journal.exists():
                raise InstallError("Interrupted installation; run managed status to recover first")
            name = self.pointer("current")
            if name is None:
                raise InstallError("No managed release is active")
            self.manifest(name)
            release = self.release(name)
            lease = self.lease(name, shared=True)
            fd = lease.__enter__()
        try:
            os.set_inheritable(fd, True)
            env = os.environ.copy()
            env.update(
                GSETTINGS_SCHEMA_DIR=str(release / "assets/schemas"), PYTHONDONTWRITEBYTECODE="1"
            )
            python = str(release / ".venv/bin/python")
            os.execve(python, [python, "-I", "-m", "halo_gtk", *arguments], env)
        finally:
            lease.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root(), help="Managed data root")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "stage", "update"):
        sub = commands.add_parser(action)
        sub.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
        sub.add_argument(
            "--ref", default="HEAD", help="Local committed Git revision; never fetched"
        )
        sub.add_argument("--uv", default="uv")
        sub.add_argument("--python", default=sys.executable)
        sub.add_argument("--replace-checkout", action="store_true")
    sub = commands.add_parser("activate")
    sub.add_argument("release")
    sub.add_argument("--replace-checkout", action="store_true")
    for action in ("status", "rollback", "uninstall"):
        commands.add_parser(action)
    sub = commands.add_parser("launch")
    sub.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        manager = Manager(args.root)
        if args.action == "launch":
            manager.launch(args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments)
            return
        with manager.lock():
            if args.action in {"install", "stage", "update"}:
                if args.action == "update" and manager.pointer("current") is None:
                    raise InstallError("No managed installation exists; use install first")
                name = manager.stage(args.repository, args.ref, uv=args.uv, python=args.python)
                print("Validated staged release:", name, flush=True)
                if args.action != "stage":
                    manager.activate(name, replace_checkout=args.replace_checkout)
                    print("Activated:", name)
            elif args.action == "activate":
                manager.activate(args.release, replace_checkout=args.replace_checkout)
                print("Activated:", args.release)
            elif args.action == "status":
                print(json.dumps(manager.status(), indent=2))
            elif args.action == "rollback":
                manager.rollback()
                print("Activated:", manager.pointer("current"))
            else:
                manager.uninstall()
                print("Managed releases removed; settings, media and credentials preserved.")
    except (InstallError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Managed installation: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
