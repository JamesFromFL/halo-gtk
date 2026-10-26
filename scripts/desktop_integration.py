"""Bootstrap desktop integration without importing the GTK application."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import re
import shlex
import signal
import tempfile
import time
from pathlib import Path


def _write_atomic(path: Path, contents: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, encoding="utf-8", delete=False
        ) as out:
            temporary = Path(out.name)
            os.fchmod(out.fileno(), mode)
            out.write(contents)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _matches_process(arguments: list[str], executable: str) -> bool:
    if not arguments:
        return False
    if arguments[0] == executable:
        return True
    return (
        len(arguments) > 1
        and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?t?", Path(arguments[0]).name) is not None
        and arguments[1] == executable
    )


def _owned_app_pids(executable: str) -> set[int]:
    result = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            arguments = [
                os.fsdecode(value) for value in (entry / "cmdline").read_bytes().split(b"\0")
            ]
            if _matches_process(arguments, executable):
                result.add(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return result


def stop_app(executable: str) -> None:
    processes = _owned_app_pids(executable)
    for pid in processes:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while processes & _owned_app_pids(executable):
        if time.monotonic() >= deadline:
            raise SystemExit("Halo did not stop; refusing to replace its environment")
        time.sleep(0.1)


def install_launcher(path: Path, executable: str) -> None:
    _write_atomic(path, "#!/usr/bin/env bash\nexec " + shlex.quote(executable) + ' "$@"\n', 0o755)


def install_desktop(path: Path, source: Path, launcher: str) -> None:
    module_path = Path(__file__).resolve().parents[1] / "src/halo_gtk/desktop_entry.py"
    spec = importlib.util.spec_from_file_location("halo_desktop_entry", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Desktop Entry escaping helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = module.quote_exec_argument(launcher)
    contents = (
        "\n".join(
            f"Exec={command}" if line.startswith("Exec=") else line
            for line in source.read_text(encoding="utf-8").splitlines()
        )
        + "\n"
    )
    _write_atomic(path, contents, 0o644)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stop", "launcher", "desktop"))
    parser.add_argument("path", type=Path)
    parser.add_argument("target", nargs="?")
    parser.add_argument("source", type=Path, nargs="?")
    args = parser.parse_args()
    if args.action == "stop":
        stop_app(str(args.path))
    elif args.action == "launcher":
        if args.target is None:
            parser.error("launcher requires an executable")
        install_launcher(args.path, args.target)
    else:
        if args.target is None or args.source is None:
            parser.error("desktop requires a launcher and source file")
        install_desktop(args.path, args.source, args.target)


if __name__ == "__main__":
    main()
