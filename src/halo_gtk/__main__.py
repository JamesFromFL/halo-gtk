"""Entry point — `uv run halo-gtk` or `python -m halo_gtk`."""

import logging
import sys

from halo_gtk.app import RingApplication

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# Show info/debug from our own code but keep third-party libs quiet.
logging.getLogger("halo_gtk").setLevel(logging.DEBUG)


def main() -> None:
    app = RingApplication()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
