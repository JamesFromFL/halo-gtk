"""Entry point — `uv run halo-gtk` or `python -m halo_gtk`."""

import logging
import sys

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from halo_gtk.app import RingApplication  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# Show info/debug from our own code but keep third-party libs quiet.
logging.getLogger("halo_gtk").setLevel(logging.DEBUG)


def main() -> None:
    # Set prgname and application name before the GApplication is created so
    # the desktop environment and taskbar read the correct values immediately.
    GLib.set_prgname("halo-gtk")
    GLib.set_application_name("Halo")
    app = RingApplication()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
