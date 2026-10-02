"""Shared test helpers."""

from __future__ import annotations

from countvision_edge.types import TrackedObject

CLASS_IDS = {"person": 0, "car": 2}


def make_track(
    track_id: int,
    x: float,
    y: float,
    *,
    cls: str = "person",
    w: float = 30.0,
    h: float = 70.0,
    speed_bh: float = 0.0,
    speed_kmh: float | None = None,
) -> TrackedObject:
    """A confirmed track whose feet (bottom centre) are at (x, y)."""
    return TrackedObject(
        track_id=track_id,
        xyxy=(x - w / 2, y - h, x + w / 2, y),
        class_id=CLASS_IDS.get(cls, 9),
        class_name=cls,
        confidence=0.9,
        hits=5,
        age_s=1.0,
        speed_bh_s=speed_bh,
        speed_kmh=speed_kmh,
    )
