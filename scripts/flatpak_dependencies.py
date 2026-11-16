"""Generate the GNOME 50 x86_64 dependency module from the existing uv lock.

No network or dependency resolution occurs here. Regeneration requires the
repository's pinned uv and the packaging module from the development runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT / "packaging/flatpak"


def dependency_module(lock: dict, requirements: str):
    platforms = [f"manylinux_2_{minor}_x86_64" for minor in range(42, 4, -1)]
    platforms += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]
    tags = list(cpython_tags((3, 13), ["cp313"], platforms))
    tags += list(compatible_tags((3, 13), "cp313", platforms))
    priorities = {tag: index for index, tag in reversed(list(enumerate(tags)))}
    environment = default_environment()
    environment.update(
        python_version="3.13",
        python_full_version="3.13.16",
        sys_platform="linux",
        os_name="posix",
        platform_machine="x86_64",
        platform_system="Linux",
        implementation_name="cpython",
        platform_python_implementation="CPython",
    )
    sources, pinned, names = [], [], set()
    for line in requirements.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        name = canonicalize_name(requirement.name)
        if name in names or requirement.url or requirement.extras:
            raise ValueError("Unexpected duplicate or non-registry requirement")
        names.add(name)
        matches = [
            p
            for p in lock["package"]
            if canonicalize_name(p["name"]) == name and p.get("version") in requirement.specifier
        ]
        if len(matches) != 1 or str(requirement.specifier) != "==" + matches[0]["version"]:
            raise ValueError(f"Requirement is not locked exactly: {name}")
        package = matches[0]
        wheels = []
        for artifact in package.get("wheels", []):
            filename = unquote(urlsplit(artifact["url"]).path.rsplit("/", 1)[-1])
            wheel_name, version, _build, wheel_tags = parse_wheel_filename(filename)
            compatible = wheel_tags & priorities.keys()
            if str(version) != package["version"] or canonicalize_name(wheel_name) != name:
                raise ValueError("Wheel identity does not match the lock")
            if compatible:
                wheels.append((min(priorities[tag] for tag in compatible), artifact))
        if wheels:
            artifact = min(wheels, key=lambda item: item[0])[1]
        elif name == "http-ece" and package.get("sdist"):
            artifact = package["sdist"]
        else:
            raise ValueError(f"No qualified Python 3.13 x86_64 wheel for {name}")
        url = urlsplit(artifact["url"])
        sha = artifact["hash"].removeprefix("sha256:")
        if (
            url.scheme != "https"
            or url.hostname != "files.pythonhosted.org"
            or url.query
            or url.fragment
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise ValueError("Unexpected artifact URL or digest")
        sources.append({"type": "file", "url": artifact["url"], "sha256": sha})
        pinned.append(f"{name}=={package['version']}")
    if not pinned:
        raise ValueError("Empty runtime dependency set")
    sources.append(
        {
            "type": "inline",
            "dest-filename": "requirements.txt",
            "contents": "\n".join(pinned) + "\n",
        }
    )
    return {
        "name": "python-dependencies",
        "buildsystem": "simple",
        "build-commands": [
            'python3 -c "import platform,sys; assert sys.version_info[:2] == (3,13) '
            "and platform.machine() == 'x86_64', 'This package targets Python 3.13 x86_64'\"",
            "python3 -m pip install --ignore-installed --no-index --no-deps --no-build-isolation "
            "--prefix=/app --find-links=. -r requirements.txt",
        ],
        "sources": sources,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    version = subprocess.check_output([args.uv, "--version"], text=True).strip()
    if version.split()[:2] != ["uv", "0.11.24"]:
        parser.error("uv 0.11.24 is required")
    exported = subprocess.check_output(
        [
            args.uv,
            "export",
            "--frozen",
            "--no-dev",
            "--group",
            "packaging",
            "--no-emit-project",
            "--no-hashes",
        ],
        cwd=ROOT,
        text=True,
    )
    raw_lock = (ROOT / "uv.lock").read_bytes()
    module = dependency_module(tomllib.loads(raw_lock.decode()), exported)
    outputs = {
        "python-dependencies.json": module,
        "dependency-lock.json": {
            "uv_lock_sha256": hashlib.sha256(raw_lock).hexdigest(),
            "python": "3.13",
            "architecture": "x86_64",
            "runtime": "GNOME 50",
        },
    }
    for name, value in outputs.items():
        content = json.dumps(value, indent=2) + "\n"
        path = DESTINATION / name
        if args.check:
            if not path.is_file() or path.read_text() != content:
                parser.error(
                    f"Regenerate {path.relative_to(ROOT)} after reviewing runtime compatibility"
                )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


if __name__ == "__main__":
    main()
