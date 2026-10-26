"""Normalize one media track's source clock without compressing dropped frames."""

from __future__ import annotations

from fractions import Fraction

SECOND = 1_000_000_000


class FrameClock:
    """Keep source deltas, rebasing missing/backward/reset timestamps safely.

    Each receiver owns one instance. This preserves timing within a track; it
    does not establish a shared audio/video clock or enable sink synchronization.
    """

    def __init__(self) -> None:
        self._source: int | None = None
        self._last = 0
        self._next = 0

    def stamp(self, frame, duration: int) -> tuple[int, bool]:
        source = None
        try:
            if frame.pts is not None and frame.time_base is not None:
                source = int(Fraction(frame.pts) * frame.time_base * SECOND)
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
        discontinuity = False
        if source is None or self._source is None:
            timestamp = self._next
        else:
            delta = source - self._source
            # A restart/backward clock or implausible jump must not create an
            # enormous appsrc backlog. Smaller genuine source gaps are retained.
            discontinuity = delta <= 0 or delta > 10 * SECOND
            timestamp = self._next if discontinuity else self._last + delta
        self._source = source
        self._last = timestamp
        self._next = timestamp + max(1, duration)
        return timestamp, discontinuity
