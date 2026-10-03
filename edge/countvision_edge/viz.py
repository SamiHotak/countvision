"""Annotated preview: boxes, track ids, trails, lines with IN arrows, zones, counters.

Colours are defined once here so the same class always has the same colour. The local web app
(session B) reuses CLASS_COLORS and ACCENT. This module draws on a COPY of the frame; nothing
is stored unless you ask for ``--annotated`` or ``--show``.
"""

from __future__ import annotations

import math
from collections import deque

import cv2
import numpy as np

from .analytics import AnalyticsEngine
from .pipeline import FrameResult

# BGR. One accent colour (teal) for lines and zones; one colour per class.
ACCENT = (177, 195, 25)
TEXT = (240, 240, 240)
CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "person": (255, 190, 70),
    "car": (70, 160, 255),
    "bus": (110, 110, 255),
    "truck": (130, 90, 230),
    "bicycle": (120, 225, 140),
    "motorcycle": (200, 130, 255),
}
DEFAULT_CLASS_COLOR = (200, 200, 200)


def class_color(name: str) -> tuple[int, int, int]:
    return CLASS_COLORS.get(name, DEFAULT_CLASS_COLOR)


def _label(image, text, origin, color, scale=0.5):
    """Text with a dark background for readability."""
    (w, h), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x, y = int(origin[0]), int(origin[1])
    cv2.rectangle(image, (x, y - h - base - 2), (x + w + 4, y + 2), (20, 20, 20), -1)
    cv2.putText(image, text, (x + 2, y - base // 2), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1,
                cv2.LINE_AA)


class VideoRecorder:
    """Writes annotated frames to a video file. The file is created on the first frame."""

    def __init__(self, path: str, fps: float) -> None:
        self.path = path
        self.fps = fps
        self._writer: cv2.VideoWriter | None = None

    def write(self, image: np.ndarray) -> None:
        if self._writer is None:
            from .inputs.live_source import fourcc_code

            code = "MJPG" if self.path.lower().endswith(".avi") else "mp4v"
            height, width = image.shape[:2]
            self._writer = cv2.VideoWriter(self.path, fourcc_code(code), self.fps, (width, height))
            if not self._writer.isOpened():
                self._writer = None
                raise OSError(f"Could not create video file: {self.path}")
        self._writer.write(image)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None


class Annotator:
    """Draws the current analysis on a frame."""

    def __init__(self, engine_getter, trail_length: int = 40, *, geometry: bool = True) -> None:
        self._engine_getter = engine_getter  # () -> AnalyticsEngine | None
        self._trails: dict[int, deque] = {}
        self._trail_length = trail_length
        self.geometry = geometry  # False: boxes only (the web page draws lines and zones itself)

    def draw(self, result: FrameResult, fps: float | None = None) -> np.ndarray:
        image = result.frame.image.copy()
        engine: AnalyticsEngine | None = self._engine_getter()
        if engine is not None and self.geometry:
            self._draw_zones(image, engine)
            self._draw_lines(image, engine)
        self._draw_tracks(image, result, engine.cam.anchor if engine is not None else "bottom_center")
        if fps is not None:
            _label(image, f"{fps:.1f} FPS", (8, 20), TEXT)
        return image

    def _draw_tracks(self, image, result: FrameResult, anchor: str = "bottom_center") -> None:
        live = set()
        for track in result.tracks:
            live.add(track.track_id)
            color = class_color(track.class_name)
            x1, y1, x2, y2 = (int(v) for v in track.xyxy)
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
            text = f"#{track.track_id} {track.class_name}"
            if track.speed_kmh is not None and track.speed_kmh > 1:
                text += f" {track.speed_kmh:.0f}km/h"
            _label(image, text, (x1, max(14, y1)), color, 0.45)
            trail = self._trails.setdefault(track.track_id, deque(maxlen=self._trail_length))
            point = track.anchor(anchor)  # the point that is counted
            trail.append((int(point[0]), int(point[1])))
            if len(trail) > 1:
                cv2.polylines(image, [np.array(trail, dtype=np.int32)], False, color, 1, cv2.LINE_AA)
        for track_id in [t for t in self._trails if t not in live and len(self._trails[t]) == 0]:
            del self._trails[track_id]
        if len(self._trails) > 500:  # keep memory bounded
            for track_id in list(self._trails)[:250]:
                del self._trails[track_id]

    def _draw_lines(self, image, engine: AnalyticsEngine) -> None:
        for counter in engine.lines:
            a = (int(counter.a[0]), int(counter.a[1]))
            b = (int(counter.b[0]), int(counter.b[1]))
            cv2.line(image, a, b, ACCENT, 2, cv2.LINE_AA)
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / length, dx / length  # screen-right normal
            if counter.in_direction == "to_left":
                nx, ny = -nx, -ny
            mid = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
            tip = (int(mid[0] + nx * 34), int(mid[1] + ny * 34))
            cv2.arrowedLine(image, mid, tip, ACCENT, 2, cv2.LINE_AA, tipLength=0.35)
            summary = counter.summary()
            _label(image, f"{counter.name}  IN {summary['in']}  OUT {summary['out']}",
                   (min(a[0], b[0]), min(a[1], b[1]) - 6), ACCENT)

    def _draw_zones(self, image, engine: AnalyticsEngine) -> None:
        overlay = image.copy()
        for monitor in engine.zones:
            points = np.array([(int(x), int(y)) for x, y in monitor.polygon], dtype=np.int32)
            cv2.fillPoly(overlay, [points], ACCENT)
        cv2.addWeighted(overlay, 0.15, image, 0.85, 0, image)
        for monitor in engine.zones:
            points = np.array([(int(x), int(y)) for x, y in monitor.polygon], dtype=np.int32)
            cv2.polylines(image, [points], True, ACCENT, 2, cv2.LINE_AA)
            summary = monitor.summary()
            text = f"{monitor.name}: {summary['occupancy']} inside"
            if monitor.kind == "queue":
                text += f", queue {summary['queue_length']}"
            _label(image, text, (int(points[0][0]), int(points[0][1]) - 6), ACCENT)
