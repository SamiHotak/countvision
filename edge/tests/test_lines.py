from __future__ import annotations

from countvision_edge.analytics.lines import LineCounter

from .helpers import make_track

A, B = (0.0, 100.0), (200.0, 100.0)  # horizontal line, looking from A to B = to the right


def counter(a=A, b=B, **kwargs) -> LineCounter:
    params = {"camera_id": "cam", "in_direction": "to_right", "deadband_px": 4.0, **kwargs}
    return LineCounter("door", a, b, **params)


def walk(lc: LineCounter, track_id: int, x: float, ys: list[float], start_ts: float = 0.0, **kw):
    """Move one track through the y positions, one frame per 0.2 s. Returns all events."""
    events = []
    for i, y in enumerate(ys):
        events += lc.update([make_track(track_id, x, y, **kw)], start_ts + i * 0.2)
    return events


def test_moving_down_counts_in_for_to_right():
    # Standing at A looking at B, "down" on the screen is the right-hand side.
    lc = counter()
    events = walk(lc, 1, 100, [60, 80, 95, 110, 130])
    assert [e.direction for e in events] == ["in"]
    assert lc.summary()["in"] == 1 and lc.summary()["out"] == 0


def test_moving_up_counts_out_for_to_right():
    lc = counter()
    events = walk(lc, 1, 100, [140, 120, 105, 90, 70])
    assert [e.direction for e in events] == ["out"]


def test_to_left_flips_the_meaning():
    lc = counter(in_direction="to_left")
    assert [e.direction for e in walk(lc, 1, 100, [60, 80, 120, 140])] == ["out"]
    assert [e.direction for e in walk(lc, 2, 120, [140, 120, 80, 60])] == ["in"]


def test_jitter_near_the_line_counts_nothing_then_one_real_crossing_counts_once():
    lc = counter()
    jitter = [98, 102, 99, 101, 98, 102, 97, 103]  # all within 4 px of the line
    assert walk(lc, 1, 100, [60, *jitter]) == []  # starts clearly above, then only jitter
    events = walk(lc, 1, 100, [120, 125, 130], start_ts=10.0)
    assert len(events) == 1 and events[0].direction == "in"


def test_crossing_the_infinite_line_outside_the_segment_is_ignored():
    lc = counter(a=(50.0, 100.0), b=(150.0, 100.0))
    assert walk(lc, 1, 300, [60, 80, 120, 140]) == []
    assert lc.summary()["in"] == 0


def test_back_and_forth_counts_one_in_and_one_out():
    lc = counter()
    events = walk(lc, 1, 100, [60, 80, 120, 140, 120, 80, 60])
    assert [e.direction for e in events] == ["in", "out"]


def test_class_filter_ignores_other_classes():
    lc = counter(allowed_classes={"person"})
    assert walk(lc, 1, 100, [60, 80, 120, 140], cls="car") == []
    assert len(walk(lc, 2, 120, [60, 80, 120, 140], cls="person")) == 1


def test_totals_by_class():
    lc = counter()
    walk(lc, 1, 60, [60, 80, 120, 140], cls="person")
    walk(lc, 2, 100, [60, 80, 120, 140], cls="person")
    walk(lc, 3, 140, [140, 120, 80, 60], cls="car")
    summary = lc.summary()
    assert summary["by_class"] == {
        "person": {"in": 2, "out": 0},
        "car": {"in": 0, "out": 1},
    }
    assert summary["in"] == 2 and summary["out"] == 1


def test_track_missing_for_a_while_is_still_counted_when_it_returns():
    lc = counter()
    lc.update([make_track(1, 100, 80)], 0.0)
    assert lc.update([], 1.0) == []  # lost for 2 seconds
    assert lc.update([], 2.0) == []
    events = lc.update([make_track(1, 100, 130)], 2.2)  # same id, other side
    assert [e.direction for e in events] == ["in"]


def test_track_memory_expires():
    lc = counter(memory_s=3.0)
    lc.update([make_track(1, 100, 80)], 0.0)
    lc.update([], 10.0)  # more than memory_s later, the state is dropped
    assert lc.update([make_track(1, 100, 130)], 10.2) == []  # looks like a new track


def test_event_carries_speed_and_ids():
    lc = counter(camera_id="camX")
    events = walk(lc, 7, 100, [60, 80, 120], speed_kmh=12.5)
    assert len(events) == 1
    event = events[0]
    assert (event.camera_id, event.name, event.track_id) == ("camX", "door", 7)
    assert event.speed_kmh == 12.5 and event.kind == "line_cross" and len(event.event_id) == 32


def test_diagonal_line_directions():
    # Line from top-left to bottom-right; looking along it, the right side is the lower-left.
    lc = LineCounter("diag", (0.0, 0.0), (200.0, 200.0), camera_id="c", in_direction="to_right")
    lc.update([make_track(2, 150, 100)], 0.0)  # upper right of the line = left side
    events = lc.update([make_track(2, 100, 150)], 0.2)  # lower left = right side
    assert [e.direction for e in events] == ["in"]
