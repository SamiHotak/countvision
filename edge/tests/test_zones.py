from __future__ import annotations

import pytest

from countvision_edge.analytics.zones import ZoneMonitor

from .helpers import make_track

SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
INSIDE, OUTSIDE = (50, 50), (300, 50)


def zone(**kwargs) -> ZoneMonitor:
    return ZoneMonitor("z", kwargs.pop("polygon", SQUARE), camera_id="cam", **kwargs)


def test_occupancy_counts_objects_inside_per_class():
    z = zone()
    _, occupancy, _ = z.update(
        [make_track(1, *INSIDE), make_track(2, 60, 60, cls="car"), make_track(3, *OUTSIDE)], 0.0
    )
    assert dict(occupancy) == {"person": 1, "car": 1}
    assert z.summary()["occupancy"] == 2


def test_dwell_time_is_reported_when_the_visit_ends():
    z = zone(exit_grace_s=1.0)
    events = []
    for i in range(26):  # inside from 0.0 to 5.0 s, five frames per second
        events += z.update([make_track(1, *INSIDE)], i * 0.2)[0]
    assert events == []  # still inside, nothing finished yet
    ended = []
    for i in range(26, 40):  # visible, outside
        ended += z.update([make_track(1, *OUTSIDE)], i * 0.2)[0]
    assert len(ended) == 1
    visit = ended[0]
    assert visit.kind == "zone_visit" and visit.name == "z" and visit.track_id == 1
    assert visit.dwell_s == pytest.approx(5.0)
    assert visit.enter_ts == pytest.approx(0.0)


def test_visibly_outside_stops_counting_immediately():
    z = zone(exit_grace_s=5.0)
    z.update([make_track(1, *INSIDE)], 0.0)
    _, occupancy, _ = z.update([make_track(1, *OUTSIDE)], 0.2)
    assert sum(occupancy.values()) == 0


def test_lost_track_still_counts_during_grace_and_visit_continues():
    z = zone(exit_grace_s=1.5)
    z.update([make_track(1, *INSIDE)], 0.0)
    _, occupancy, _ = z.update([], 1.0)  # not visible for 1 s: detector missed it
    assert sum(occupancy.values()) == 1
    z.update([make_track(1, *INSIDE)], 1.2)  # back: same visit
    events, _, _ = z.update([], 10.0)  # long gone
    assert len(events) == 1
    assert events[0].dwell_s == pytest.approx(1.2)  # 0.0 .. 1.2, the gap is not extra


def test_lost_longer_than_grace_ends_the_visit_and_stops_counting():
    z = zone(exit_grace_s=1.5)
    z.update([make_track(1, *INSIDE)], 0.0)
    z.update([make_track(1, *INSIDE)], 2.0)
    events, occupancy, _ = z.update([], 4.0)
    assert sum(occupancy.values()) == 0
    assert len(events) == 1 and events[0].dwell_s == pytest.approx(2.0)


def test_short_visits_are_not_reported():
    z = zone(min_visit_s=1.0, exit_grace_s=0.5)
    z.update([make_track(1, *INSIDE)], 0.0)
    z.update([make_track(1, *INSIDE)], 0.4)
    events, _, _ = z.update([], 5.0)
    assert events == []


def test_queue_counts_only_waiting_objects():
    z = zone(kind="queue", queue_max_speed=0.25, queue_min_dwell_s=3.0)
    waiting = make_track(1, 20, 50, speed_bh=0.05)
    walking = make_track(2, 60, 50, speed_bh=1.0)
    newcomer = make_track(3, 80, 50, speed_bh=0.0)
    z.update([waiting, walking], 0.0)
    _, _, queue = z.update([waiting, walking], 4.0)
    assert sum(queue.values()) == 1  # only the waiting one has been there 3 s and is slow
    _, _, queue = z.update([waiting, walking, newcomer], 4.2)
    assert sum(queue.values()) == 1  # the newcomer has not been in the zone long enough


def test_area_zone_has_no_queue():
    z = zone(kind="area")
    waiting = make_track(1, 20, 50, speed_bh=0.0)
    z.update([waiting], 0.0)
    _, _, queue = z.update([waiting], 10.0)
    assert sum(queue.values()) == 0


def test_flush_ends_open_visits():
    z = zone()
    z.update([make_track(1, *INSIDE)], 0.0)
    z.update([make_track(1, *INSIDE)], 3.0)
    events = z.flush()
    assert len(events) == 1 and events[0].dwell_s == pytest.approx(3.0)
    assert z.flush() == []


def test_class_filter():
    z = zone(allowed_classes={"person"})
    _, occupancy, _ = z.update([make_track(1, *INSIDE, cls="car")], 0.0)
    assert sum(occupancy.values()) == 0
