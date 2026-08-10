"""CI import and project data validation checks."""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from importlib import metadata, resources
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PACKAGE_ICON_FILES = (Path("assets") / "icons" / "apps" / "io.github.JamesFromFL.HaloGtk.png",)

_SOURCE_REQUIRED_FILES = (
    Path("data/io.github.JamesFromFL.HaloGtk.desktop"),
    Path("data/io.github.JamesFromFL.HaloGtk.gschema.xml"),
    Path("data/icons/hicolor/32x32/apps/io.github.JamesFromFL.HaloGtk.png"),
    Path("data/icons/hicolor/48x48/apps/io.github.JamesFromFL.HaloGtk.png"),
    Path("data/icons/hicolor/64x64/apps/io.github.JamesFromFL.HaloGtk.png"),
    Path("data/icons/hicolor/128x128/apps/io.github.JamesFromFL.HaloGtk.png"),
    Path("data/icons/hicolor/256x256/apps/io.github.JamesFromFL.HaloGtk.png"),
) + tuple(Path("src/halo_gtk") / path for path in _PACKAGE_ICON_FILES)


def _import_package_modules():
    package = importlib.import_module("halo_gtk")
    modules = sorted(
        module.name
        for module in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}.")
    )
    for module in modules:
        importlib.import_module(module)
    return package


def _validate_source_files() -> None:
    missing = [str(path) for path in _SOURCE_REQUIRED_FILES if not (REPO_ROOT / path).is_file()]
    if missing:
        raise SystemExit("Missing required project data files: " + ", ".join(missing))


def _validate_installed_package(package) -> None:
    package_file = Path(package.__file__).resolve()
    source_root = (REPO_ROOT / "src").resolve()
    if package_file.is_relative_to(source_root):
        raise SystemExit(f"Imported source checkout instead of installed wheel: {package_file}")

    package_root = resources.files(package)
    missing = [
        str(path)
        for path in _PACKAGE_ICON_FILES
        if not package_root.joinpath(*path.parts).is_file()
    ]
    if missing:
        raise SystemExit("Missing required wheel package files: " + ", ".join(missing))

    distribution_files = metadata.files("halo-gtk") or ()
    if not any(tuple(path.parts[-2:]) == ("licenses", "LICENSE") for path in distribution_files):
        raise SystemExit("Installed wheel does not contain its GPL license")

    console_scripts = metadata.entry_points(group="console_scripts")
    if not any(
        entry.name == "halo-gtk" and entry.value == "halo_gtk.__main__:main"
        for entry in console_scripts
    ):
        raise SystemExit("Installed wheel does not expose the halo-gtk console script")

    installed_version = metadata.version("halo-gtk")
    if installed_version != package.APP_VERSION:
        raise SystemExit(
            f"Application version {package.APP_VERSION!r} does not match wheel "
            f"version {installed_version!r}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--installed",
        action="store_true",
        help="validate package data and metadata from an installed wheel",
    )
    args = parser.parse_args()

    package = _import_package_modules()
    if args.installed:
        _validate_installed_package(package)
    else:
        _validate_source_files()


if __name__ == "__main__":
    main()
