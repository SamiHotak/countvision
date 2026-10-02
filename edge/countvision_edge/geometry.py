"""Small geometry helpers. Pure Python, no OpenCV, easy to test."""

from __future__ import annotations

import math
from collections.abc import Sequence

from .types import Point

EPS = 1e-9


def to_pixels(point: Point, size: tuple[int, int], mode: str) -> Point:
    """Convert a configured point to pixels. ``size`` is (width, height)."""
    if mode == "normalized":
        return (point[0] * size[0], point[1] * size[1])
    return (float(point[0]), float(point[1]))


def cross(o: Point, a: Point, b: Point) -> float:
    """z component of (a - o) x (b - o)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def signed_distance(p: Point, a: Point, b: Point) -> float:
    """Distance of p from the infinite line a->b in pixels.

    Positive when p is on the RIGHT-hand side when you stand at a and look towards b, as seen
    on the screen (image y axis points down). Negative on the left.
    """
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length < EPS:
        return 0.0
    return cross(a, b, p) / length


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    """q is collinear with p-r; is it inside the bounding box?"""
    return (
        min(p[0], r[0]) - EPS <= q[0] <= max(p[0], r[0]) + EPS
        and min(p[1], r[1]) - EPS <= q[1] <= max(p[1], r[1]) + EPS
    )


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """True if segment p1-p2 touches or crosses segment q1-q2."""
    d1 = cross(q1, q2, p1)
    d2 = cross(q1, q2, p2)
    d3 = cross(p1, p2, q1)
    d4 = cross(p1, p2, q2)
    if ((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS)) and (
        (d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS)
    ):
        return True
    if abs(d1) <= EPS and _on_segment(q1, p1, q2):
        return True
    if abs(d2) <= EPS and _on_segment(q1, p2, q2):
        return True
    if abs(d3) <= EPS and _on_segment(p1, q1, p2):
        return True
    return abs(d4) <= EPS and _on_segment(p1, q2, p2)


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray casting test. Points exactly on an edge may count as inside or outside."""
    x, y = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside
