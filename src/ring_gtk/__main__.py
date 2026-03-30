"""Entry point — `uv run ring-gtk` or `python -m ring_gtk`."""

import logging
import sys

from ring_gtk.app import RingApplication

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# Show info/debug from our own code but keep third-party libs quiet.
logging.getLogger("ring_gtk").setLevel(logging.DEBUG)


def main() -> None:
    app = RingApplication()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
