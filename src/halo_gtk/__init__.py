"""halo-gtk — Native GTK4 + libadwaita desktop client for Ring home security."""

from importlib.metadata import PackageNotFoundError, version

APP_ID = "io.github.JamesFromFL.HaloGtk"

try:
    APP_VERSION = version("halo-gtk")
except PackageNotFoundError:
    APP_VERSION = "0.0.0+uninstalled"
