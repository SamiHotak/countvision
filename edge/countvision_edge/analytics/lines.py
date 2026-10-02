"""Line crossing with direction."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..geometry import segments_intersect, signed_distance
from ..types import Event, Point, TrackedObject


@dataclass
class _LineState:
    side: int  # +1 right of the line, -1 left, 0 unknown
    pos: Point  # last position that was clearly on a side
    last_seen: float


class LineCounter:
    """Counts objects that cross the segment a->b, per direction and per class.

    Method: for every track the side of the line is remembered, but only when the object is
    at least ``deadband_px`` away from the line, so jitter around the line cannot count twice.
    When the remembered side flips, the path from the last clear position to the new one must
    cross the segment itself (not only the infinite line). Then one IN or OUT is counted.

    Direction: standing at ``a`` looking at ``b`` (as seen on the screen), moving from the left
    to the right side is "to_right".
    """

    def __init__(
        self,
        name: str,
        a: Point,
        b: Point,
        *,
        camera_id: str,
        in_direction: str = "to_right",
        allowed_classes: set[str] | None = None,
        deadband_px: float = 4.0,
        anchor: str = "bottom_center",
        memory_s: float = 10.0,
    ) -> None:
        self.name = name
        self.a, self.b = a, b
        self.camera_id = camera_id
        self.in_direction = in_direction
        self.allowed = allowed_classes
        self.deadband_px = deadband_px
        self.anchor = anchor
        self.memory_s = memory_s
        self._states: dict[int, _LineState] = {}
        self.totals: dict[str, dict[str, int]] = {}

    def set_geometry(self, a: Point, b: Point) -> None:
        """Move the line (new frame size). Track memory is cleared, counters are kept."""
        self.a, self.b = a, b
        self._states.clear()

    def update(self, tracks: list[TrackedObject], ts: float) -> list[Event]:
        events: list[Event] = []
        for track in tracks:
            if self.allowed is not None and track.class_name not in self.allowed:
                continue
            p = track.anchor(self.anchor)
            distance = signed_distance(p, self.a, self.b)
            state = self._states.get(track.track_id)
            if state is None:
                side = 0 if abs(distance) < self.deadband_px else (1 if distance > 0 else -1)
                self._states[track.track_id] = _LineState(side, p, ts)
                continue
            state.last_seen = ts
            if abs(distance) < self.deadband_px:
                continue  # too close to the line to know the side
            new_side = 1 if distance > 0 else -1
            if state.side == 0:
                state.side, state.pos = new_side, p
            elif new_side != state.side:
                if segments_intersect(state.pos, p, self.a, self.b):
                    direction = self._direction(new_side)
                    self._count(track.class_name, direction)
                    events.append(
                        Event(
                            event_id=uuid.uuid4().hex,
                            kind="line_cross",
                            ts=ts,
                            camera_id=self.camera_id,
                            name=self.name,
                            track_id=track.track_id,
                            class_name=track.class_name,
                            direction=direction,
                            speed_kmh=track.speed_kmh,
                        )
                    )
                state.side, state.pos = new_side, p
            else:
                state.pos = p
        stale = [tid for tid, s in self._states.items() if ts - s.last_seen > self.memory_s]
        for tid in stale:
            del self._states[tid]
        return events

    def _direction(self, new_side: int) -> str:
        """new_side +1 means the object moved from left to right ("to_right")."""
        moved_right = new_side > 0
        counts_in = moved_right if self.in_direction == "to_right" else not moved_right
        return "in" if counts_in else "out"

    def _count(self, class_name: str, direction: str) -> None:
        bucket = self.totals.setdefault(class_name, {"in": 0, "out": 0})
        bucket[direction] += 1

    def summary(self) -> dict:
        """Totals since start: {"in": n, "out": n, "by_class": {...}}."""
        total_in = sum(v["in"] for v in self.totals.values())
        total_out = sum(v["out"] for v in self.totals.values())
        return {
            "in": total_in,
            "out": total_out,
            "by_class": {k: dict(v) for k, v in self.totals.items()},
        }
