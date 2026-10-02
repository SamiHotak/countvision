"""Plain data types shared by all modules.

All coordinates are pixels of the frame that was analysed (x to the right,
y down). Times are seconds. ``ts`` is absolute (UTC epoch seconds),
``media_ts`` is seconds since the stream started.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

Point = tuple[float, float]


@dataclass(frozen=True)
class Frame:
    """One video frame with timing information."""

    image: np.ndarray  # BGR, uint8, H x W x 3
    index: int  # sequence number in the source (gaps mean dropped frames)
    ts: float  # absolute capture time, UTC epoch seconds
    media_ts: float  # seconds since the stream started

    @property
    def size(self) -> tuple[int, int]:
        """(width, height)."""
        return int(self.image.shape[1]), int(self.image.shape[0])


@dataclass
class Detections:
    """Detector output for one frame. Arrays have equal length N."""

    xyxy: np.ndarray  # (N, 4) float32
    confidence: np.ndarray  # (N,) float32
    class_id: np.ndarray  # (N,) int32

    @classmethod
    def empty(cls) -> Detections:
        return cls(
            np.zeros((0, 4), dtype=np.float32),
            np.zeros((0,), dtype=np.float32),
            np.zeros((0,), dtype=np.int32),
        )

    @classmethod
    def from_arrays(cls, xyxy, confidence, class_id) -> Detections:
        """Build from array-likes, fixing dtypes and shapes."""
        xyxy_a = np.asarray(xyxy, dtype=np.float32).reshape(-1, 4)
        conf_a = np.asarray(confidence, dtype=np.float32).reshape(-1)
        cls_a = np.asarray(class_id, dtype=np.int32).reshape(-1)
        if not (len(xyxy_a) == len(conf_a) == len(cls_a)):
            raise ValueError("xyxy, confidence and class_id must have the same length")
        return cls(xyxy_a, conf_a, cls_a)

    def __len__(self) -> int:
        return int(self.xyxy.shape[0])

    def select(self, mask: np.ndarray) -> Detections:
        """Return the rows where ``mask`` is true (or at the given indices)."""
        return Detections(self.xyxy[mask], self.confidence[mask], self.class_id[mask])


@dataclass
class TrackedObject:
    """A confirmed track in the current frame."""

    track_id: int
    xyxy: tuple[float, float, float, float]
    class_id: int
    class_name: str
    confidence: float
    hits: int  # number of frames this track was matched
    age_s: float  # seconds since the track was first seen
    speed_px_s: float = 0.0  # centre speed in pixels per second
    speed_bh_s: float = 0.0  # centre speed in box heights per second (scale free)
    speed_kmh: float | None = None  # only when speed calibration is configured

    @property
    def height(self) -> float:
        return max(1.0, self.xyxy[3] - self.xyxy[1])

    @property
    def center(self) -> Point:
        return ((self.xyxy[0] + self.xyxy[2]) / 2.0, (self.xyxy[1] + self.xyxy[3]) / 2.0)

    @property
    def bottom_center(self) -> Point:
        return ((self.xyxy[0] + self.xyxy[2]) / 2.0, self.xyxy[3])

    def anchor(self, mode: str) -> Point:
        """Point used for counting: the feet ("bottom_center") or the box centre."""
        return self.bottom_center if mode == "bottom_center" else self.center


@dataclass(frozen=True)
class Event:
    """A discrete thing that happened. Contains no image data and no coordinates."""

    event_id: str
    kind: str  # "line_cross" or "zone_visit"
    ts: float  # when it happened (UTC epoch seconds)
    camera_id: str
    name: str  # line or zone name
    track_id: int
    class_name: str
    direction: str | None = None  # line_cross: "in" or "out"
    enter_ts: float | None = None  # zone_visit
    dwell_s: float | None = None  # zone_visit
    speed_kmh: float | None = None  # line_cross, only with speed calibration


@dataclass
class FrameSample:
    """Per-frame zone measurements, used to build 1-minute aggregates."""

    ts: float
    dt: float  # seconds this sample represents (capped)
    zone_occupancy: dict[str, dict[str, int]] = field(default_factory=dict)  # zone -> class -> n
    zone_queue: dict[str, dict[str, int]] = field(default_factory=dict)  # zone -> class -> n
