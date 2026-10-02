"""Speed estimation from track motion, with optional two-point calibration."""

from __future__ import annotations

import math
from collections import deque

from ..types import Point, TrackedObject


class SpeedCalibration:
    """Two image points that are ``distance_m`` metres apart give one metres-per-pixel scale.

    This is a flat, one-scale approximation. It ignores perspective, so it is only roughly
    right when the camera looks straight at the road or floor. Validate it with a known speed.
    """

    def __init__(self, p1: Point, p2: Point, distance_m: float) -> None:
        pixels = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if pixels <= 0:
            raise ValueError("speed calibration points must be different")
        if distance_m <= 0:
            raise ValueError("speed calibration distance must be positive")
        self.meters_per_pixel = distance_m / pixels

    def kmh(self, pixels_per_second: float) -> float:
        return pixels_per_second * self.meters_per_pixel * 3.6


class MotionEstimator:
    """Fills ``speed_px_s``, ``speed_bh_s`` (box heights per second) and ``speed_kmh``.

    Speed is the displacement of the box centre over the last ``window_s`` seconds. A track
    needs at least ``min_span_s`` of history, before that its speed is reported as 0.
    """

    def __init__(
        self,
        window_s: float = 1.0,
        min_span_s: float = 0.25,
        calibration: SpeedCalibration | None = None,
        memory_s: float = 10.0,
    ) -> None:
        self.window_s = window_s
        self.min_span_s = min_span_s
        self.calibration = calibration
        self.memory_s = memory_s
        self._history: dict[int, deque[tuple[float, float, float]]] = {}

    def update(self, tracks: list[TrackedObject], ts: float) -> None:
        for track in tracks:
            cx, cy = track.center
            history = self._history.setdefault(track.track_id, deque())
            history.append((ts, cx, cy))
            while len(history) > 2 and ts - history[0][0] > self.window_s:
                history.popleft()
            t0, x0, y0 = history[0]
            span = ts - t0
            speed = math.hypot(cx - x0, cy - y0) / span if span >= self.min_span_s else 0.0
            track.speed_px_s = speed
            track.speed_bh_s = speed / track.height
            if self.calibration is not None:
                track.speed_kmh = self.calibration.kmh(speed)
        stale = [
            tid for tid, h in self._history.items() if h and ts - h[-1][0] > self.memory_s
        ]
        for tid in stale:
            del self._history[tid]
