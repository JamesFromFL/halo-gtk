"""Entry point — `uv run halo-gtk` or `python -m halo_gtk`."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from halo_gtk import storage_paths  # noqa: E402
from halo_gtk.app import RingApplication  # noqa: E402
from halo_gtk.log_redaction import RedactingFormatter  # noqa: E402

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUP_COUNT = 5


class _DropExactMessage(logging.Filter):
    """Drop known noisy third-party messages that Halo handles elsewhere."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != self._message


def _log_dir() -> Path:
    return storage_paths.state_dir()


def _setup_logging() -> None:
    """Log warnings to stderr and verbose app diagnostics to a rotating file."""
    formatter = RedactingFormatter(_LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "halo-gtk.log",
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not initialise file logging: %s", exc)

    # Show info/debug from our own code but keep third-party libs quiet.
    logging.getLogger("halo_gtk").setLevel(logging.DEBUG)
    logging.getLogger("firebase_messaging").setLevel(logging.CRITICAL)

    # ring-doorbell currently logs newer stickup camera models as ERROR even
    # when the devices still work through Halo's generic camera paths.
    logging.getLogger("ring_doorbell.stickup_cam").addFilter(
        _DropExactMessage("Unknown kind: stickup_cam_mini_v3")
    )


def main() -> None:
    _setup_logging()
    start_background = "--background" in sys.argv
    argv = [arg for arg in sys.argv if arg != "--background"]
    # Set prgname and application name before the GApplication is created so
    # the desktop environment and taskbar read the correct values immediately.
    GLib.set_prgname("halo-gtk")
    GLib.set_application_name("Halo")
    app = RingApplication(start_background=start_background)
    sys.exit(app.run(argv))


if __name__ == "__main__":
    main()
