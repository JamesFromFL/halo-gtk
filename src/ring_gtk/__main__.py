"""Entry point — `uv run ring-gtk` or `python -m ring_gtk`."""

import sys

from ring_gtk.app import RingApplication


def main() -> None:
    app = RingApplication()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
