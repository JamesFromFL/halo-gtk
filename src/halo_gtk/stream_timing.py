"""Opt-in numeric timing samples for the local diagnostic export."""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque

SAMPLE_LIMIT = 128


class StreamTiming:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._enabled_at = None
        self._first = deque(maxlen=SAMPLE_LIMIT)
        self._interval = deque(maxlen=SAMPLE_LIMIT)

    @property
    def enabled(self):
        return self._enabled_at is not None

    def set_enabled(self, enabled: bool):
        with self._lock:
            if self.enabled == enabled:
                return
            self._first.clear()
            self._interval.clear()
            self._enabled_at = self._clock() if enabled else None

    def clear(self):
        with self._lock:
            self._first.clear()
            self._interval.clear()
            if self.enabled:
                self._enabled_at = self._clock()

    def video_buffer(self, admitted_at, previous_at, accepted_at):
        """Take durations only for intervals entirely inside the current opt-in."""
        if not self.enabled:
            return
        start = admitted_at if previous_at is None else previous_at
        with self._lock:
            if self._enabled_at is None or start < self._enabled_at:
                return
            duration = accepted_at - start
            if not math.isfinite(duration) or not 0 <= duration <= 3600:
                return
            samples = self._first if previous_at is None else self._interval
            samples.append(round(duration * 1000))

    def summary(self) -> list[str]:
        with self._lock:
            if not self.enabled:
                return []
            rows = ["Stream timing (most recent 128 samples per metric):"]
            for label, samples in (
                ("Admission to first video buffer", self._first),
                ("Interval between video buffers", self._interval),
            ):
                if not samples:
                    rows.append(f"{label}: 0 samples")
                    continue
                ordered = sorted(samples)
                p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
                unit = "sample" if len(ordered) == 1 else "samples"
                rows.append(
                    f"{label}: {len(ordered)} {unit}, "
                    f"median {statistics.median(ordered):g} ms, p95 {p95} ms"
                )
            return rows


timings = StreamTiming()
