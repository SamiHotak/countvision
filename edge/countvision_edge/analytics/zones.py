"""Polygon zones: occupancy, dwell time and queue length."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass

from ..geometry import point_in_polygon
from ..types import Event, Point, TrackedObject


@dataclass
class _Visit:
    track_id: int
    enter_ts: float
    last_in_ts: float
    class_name: str
    speed_bh_s: float = 0.0


class ZoneMonitor:
    """Tracks who is inside a polygon.

    * Occupancy: objects inside right now. A track that is briefly lost (detector miss,
      short occlusion) still counts for ``exit_grace_s``, so occupancy does not flicker.
      An object that is visible and outside the polygon stops counting at once.
    * Dwell time: reported as a ``zone_visit`` event when the visit ends. Time spent in the
      grace period is not added.
    * Queue length (``kind="queue"``): objects inside that have been there at least
      ``queue_min_dwell_s`` and move slower than ``queue_max_speed`` box heights per second.
    """

    def __init__(
        self,
        name: str,
        polygon: list[Point],
        *,
        camera_id: str,
        kind: str = "area",
        allowed_classes: set[str] | None = None,
        anchor: str = "bottom_center",
        exit_grace_s: float = 1.5,
        min_visit_s: float = 1.0,
        queue_max_speed: float = 0.25,
        queue_min_dwell_s: float = 3.0,
    ) -> None:
        self.name = name
        self.polygon = polygon
        self.camera_id = camera_id
        self.kind = kind
        self.allowed = allowed_classes
        self.anchor = anchor
        self.exit_grace_s = exit_grace_s
        self.min_visit_s = min_visit_s
        self.queue_max_speed = queue_max_speed
        self.queue_min_dwell_s = queue_min_dwell_s
        self._visits: dict[int, _Visit] = {}
        self.occupancy: Counter[str] = Counter()
        self.queue: Counter[str] = Counter()

    def set_geometry(self, polygon: list[Point]) -> None:
        self.polygon = polygon

    def update(
        self, tracks: list[TrackedObject], ts: float
    ) -> tuple[list[Event], Counter[str], Counter[str]]:
        """Process one frame. Returns (finished-visit events, occupancy, queue) per class."""
        events: list[Event] = []
        visible = {t.track_id for t in tracks}
        inside: set[int] = set()
        for track in tracks:
            if self.allowed is not None and track.class_name not in self.allowed:
                continue
            if not point_in_polygon(track.anchor(self.anchor), self.polygon):
                continue
            inside.add(track.track_id)
            visit = self._visits.get(track.track_id)
            if visit is None:
                self._visits[track.track_id] = _Visit(
                    track.track_id, ts, ts, track.class_name, track.speed_bh_s
                )
            else:
                visit.last_in_ts = ts
                visit.class_name = track.class_name
                visit.speed_bh_s = track.speed_bh_s

        occupancy: Counter[str] = Counter()
        queue: Counter[str] = Counter()
        for track_id, visit in list(self._visits.items()):
            if track_id in inside:
                present = True
            elif ts - visit.last_in_ts > self.exit_grace_s:
                events.extend(self._finish(visit))
                del self._visits[track_id]
                continue
            else:
                present = track_id not in visible  # lost (counts) vs. visibly outside (does not)
            if not present:
                continue
            occupancy[visit.class_name] += 1
            if (
                self.kind == "queue"
                and visit.speed_bh_s <= self.queue_max_speed
                and ts - visit.enter_ts >= self.queue_min_dwell_s
            ):
                queue[visit.class_name] += 1
        self.occupancy, self.queue = occupancy, queue
        return events, occupancy, queue

    def flush(self) -> list[Event]:
        """End all open visits (call when the stream ends)."""
        events: list[Event] = []
        for visit in self._visits.values():
            events.extend(self._finish(visit))
        self._visits.clear()
        self.occupancy, self.queue = Counter(), Counter()
        return events

    def _finish(self, visit: _Visit) -> list[Event]:
        dwell = visit.last_in_ts - visit.enter_ts
        if dwell < self.min_visit_s:
            return []
        return [
            Event(
                event_id=uuid.uuid4().hex,
                kind="zone_visit",
                ts=visit.last_in_ts,
                camera_id=self.camera_id,
                name=self.name,
                track_id=visit.track_id,
                class_name=visit.class_name,
                enter_ts=visit.enter_ts,
                dwell_s=dwell,
            )
        ]

    def summary(self) -> dict:
        return {
            "occupancy": int(sum(self.occupancy.values())),
            "queue_length": int(sum(self.queue.values())),
            "by_class": {k: int(v) for k, v in self.occupancy.items()},
        }
