"""1-minute aggregation of events and zone samples."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field

from .storage import CoverageRow, LineCountRow, SqliteBuffer, ZoneStatRow
from .types import Event, FrameSample

log = logging.getLogger(__name__)

ALL = "*"  # class_name used for "all classes together"


@dataclass
class _ZoneAcc:
    occ_time: float = 0.0
    occ_max: int = 0
    occ_last: int = 0
    queue_time: float = 0.0
    queue_max: int = 0
    visits: int = 0
    dwell_sum: float = 0.0
    dwell_max: float = 0.0


@dataclass
class _Window:
    start: int
    frames: int = 0
    seconds: float = 0.0
    lines: dict[tuple[str, str], list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    zones: dict[tuple[str, str], _ZoneAcc] = field(default_factory=lambda: defaultdict(_ZoneAcc))
    zone_seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))


class MinuteAggregator:
    """Collects events and per-frame zone samples and writes one set of rows per minute.

    A minute is written when the stream time is past its end by ``LATE_GRACE_S`` (late events
    like a zone visit that ends just after the boundary still land in the right minute), or at
    close(). Rows exist for every minute in which frames were processed, so a quiet minute
    (zero visitors) can be told apart from a minute when the camera was offline.
    """

    WINDOW_S = 60
    LATE_GRACE_S = 5.0

    def __init__(self, camera_id: str, buffer: SqliteBuffer) -> None:
        self.camera_id = camera_id
        self.buffer = buffer
        self._windows: dict[int, _Window] = {}
        self._latest_ts = 0.0

    def _window(self, ts: float) -> _Window:
        start = int(math.floor(ts / self.WINDOW_S) * self.WINDOW_S)
        window = self._windows.get(start)
        if window is None:
            window = self._windows[start] = _Window(start=start)
        return window

    def add_sample(self, sample: FrameSample) -> None:
        window = self._window(sample.ts)
        window.frames += 1
        window.seconds += sample.dt
        for zone, occupancy in sample.zone_occupancy.items():
            queue = sample.zone_queue.get(zone, {})
            window.zone_seconds[zone] += sample.dt
            keys = {c for (z, c) in window.zones if z == zone} | set(occupancy) | set(queue) | {ALL}
            for cls in keys:
                n = sum(occupancy.values()) if cls == ALL else occupancy.get(cls, 0)
                q = sum(queue.values()) if cls == ALL else queue.get(cls, 0)
                acc = window.zones[(zone, cls)]
                acc.occ_time += n * sample.dt
                acc.occ_max = max(acc.occ_max, n)
                acc.occ_last = n
                acc.queue_time += q * sample.dt
                acc.queue_max = max(acc.queue_max, q)
        self._advance(sample.ts)

    def add_event(self, event: Event) -> None:
        window = self._window(event.ts)
        if event.kind == "line_cross" and event.direction in ("in", "out"):
            idx = 0 if event.direction == "in" else 1
            window.lines[(event.name, event.class_name)][idx] += 1
            window.lines[(event.name, ALL)][idx] += 1
        elif event.kind == "zone_visit" and event.dwell_s is not None:
            for cls in (event.class_name, ALL):
                acc = window.zones[(event.name, cls)]
                acc.visits += 1
                acc.dwell_sum += event.dwell_s
                acc.dwell_max = max(acc.dwell_max, event.dwell_s)
        self._advance(event.ts)

    def _advance(self, ts: float) -> None:
        self._latest_ts = max(self._latest_ts, ts)
        ready = [
            start
            for start in self._windows
            if start + self.WINDOW_S + self.LATE_GRACE_S <= self._latest_ts
        ]
        for start in sorted(ready):
            self._write(self._windows.pop(start))

    def close(self) -> None:
        """Write all open minutes."""
        for start in sorted(self._windows):
            self._write(self._windows[start])
        self._windows.clear()

    def _write(self, w: _Window) -> None:
        cam = self.camera_id
        if w.frames:
            self.buffer.add_coverage(CoverageRow(cam, w.start, w.frames, round(w.seconds, 3)))
        self.buffer.add_line_counts(
            LineCountRow(cam, w.start, line, cls, counts[0], counts[1])
            for (line, cls), counts in sorted(w.lines.items())
        )
        zone_rows = []
        for (zone, cls), acc in sorted(w.zones.items()):
            seconds = w.zone_seconds.get(zone, 0.0)
            zone_rows.append(
                ZoneStatRow(
                    camera_id=cam,
                    window_start=w.start,
                    zone=zone,
                    class_name=cls,
                    sample_s=round(seconds, 3),
                    occ_avg=acc.occ_time / seconds if seconds > 0 else 0.0,
                    occ_max=acc.occ_max,
                    occ_last=acc.occ_last,
                    queue_avg=acc.queue_time / seconds if seconds > 0 else 0.0,
                    queue_max=acc.queue_max,
                    visits=acc.visits,
                    dwell_sum_s=acc.dwell_sum,
                    dwell_max_s=acc.dwell_max,
                )
            )
        self.buffer.add_zone_stats(zone_rows)
