from __future__ import annotations

import pytest

from countvision_edge.geometry import (
    point_in_polygon,
    segments_intersect,
    signed_distance,
    to_pixels,
)


def test_signed_distance_right_side_is_positive():
    # Looking from (0,0) towards (10,0): the right-hand side on screen is DOWN (y grows down).
    assert signed_distance((5, 3), (0, 0), (10, 0)) == pytest.approx(3.0)
    assert signed_distance((5, -3), (0, 0), (10, 0)) == pytest.approx(-3.0)


def test_signed_distance_is_in_pixels_for_diagonal_line():
    # Line along the diagonal; the point (10, 0) is 10/sqrt(2) away, on the left side.
    assert signed_distance((10, 0), (0, 0), (10, 10)) == pytest.approx(-(10 / 2**0.5))


def test_signed_distance_degenerate_line_is_zero():
    assert signed_distance((1, 1), (2, 2), (2, 2)) == 0.0


@pytest.mark.parametrize(
    ("p1", "p2", "q1", "q2", "expected"),
    [
        ((0, 0), (10, 10), (0, 10), (10, 0), True),  # X shape
        ((0, 0), (10, 0), (0, 5), (10, 5), False),  # parallel
        ((0, 0), (4, 0), (5, -5), (5, 5), False),  # would cross, but segment ends first
        ((0, 0), (5, 0), (5, -5), (5, 5), True),  # touches at an end point
        ((0, 0), (10, 0), (3, 0), (20, 0), True),  # collinear and overlapping
        ((0, 0), (2, 0), (3, 0), (20, 0), False),  # collinear, apart
    ],
)
def test_segments_intersect(p1, p2, q1, q2, expected):
    assert segments_intersect(p1, p2, q1, q2) is expected


def test_point_in_polygon_convex_and_concave():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert point_in_polygon((5, 5), square)
    assert not point_in_polygon((15, 5), square)
    # "U" shape: the notch in the middle is outside.
    u_shape = [(0, 0), (9, 0), (9, 9), (6, 9), (6, 3), (3, 3), (3, 9), (0, 9)]
    assert point_in_polygon((1, 5), u_shape)
    assert not point_in_polygon((4.5, 6), u_shape)


def test_to_pixels_modes():
    assert to_pixels((0.5, 0.25), (640, 360), "normalized") == (320.0, 90.0)
    assert to_pixels((100, 50), (640, 360), "pixel") == (100.0, 50.0)
