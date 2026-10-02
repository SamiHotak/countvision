from __future__ import annotations

import pytest

from countvision_edge.aggregator import MinuteAggregator
from countvision_edge.storage import SqliteBuffer
from countvision_edge.types import Event, FrameSample


@pytest.fixture
def buf(tmp_path):
    buffer = SqliteBuffer(tmp_path / "agg.db")
    yield buffer
    buffer.close()


T0 = 1_800_000_000 - (1_800_000_000 % 60)  # a minute boundary


def cross(ts, direction="in", cls="person", name="door") -> Event:
    return Event(event_id=f"{ts}-{direction}-{cls}", kind="line_cross", ts=ts, camera_id="c",
                 name=name, track_id=1, class_name=cls, direction=direction)


def visit(ts_exit, dwell, cls="person", zone="z") -> Event:
    return Event(event_id=f"v{ts_exit}", kind="zone_visit", ts=ts_exit, camera_id="c", name=zone,
                 track_id=2, class_name=cls, enter_ts=ts_exit - dwell, dwell_s=dwell)


def rows(buf, table):
    return buf.query(table)


def test_line_counts_are_split_by_minute_and_class_and_include_a_total_row(buf):
    agg = MinuteAggregator("c", buf)
    agg.add_event(cross(T0 + 10, "in"))
    agg.add_event(cross(T0 + 20, "in", cls="car"))
    agg.add_event(cross(T0 + 30, "out"))
    agg.add_event(cross(T0 + 70, "in"))  # next minute
    agg.close()
    got = {(r["window_start"], r["class_name"]): (r["in_count"], r["out_count"])
           for r in rows(buf, "line_counts")}
    assert got == {
        (T0, "*"): (2, 1), (T0, "person"): (1, 1), (T0, "car"): (1, 0),
        (T0 + 60, "*"): (1, 0), (T0 + 60, "person"): (1, 0),
    }


def test_zone_occupancy_is_a_time_weighted_average_with_max_and_last(buf):
    agg = MinuteAggregator("c", buf)
    for i in range(60):  # one sample per second
        n = 2 if i < 30 else 0
        agg.add_sample(FrameSample(ts=T0 + i, dt=1.0, zone_occupancy={"z": {"person": n} if n else {}},
                                   zone_queue={"z": {}}))
    agg.close()
    total = next(r for r in rows(buf, "zone_stats") if r["class_name"] == "*")
    assert total["occ_avg"] == pytest.approx(1.0)
    assert (total["occ_max"], total["occ_last"]) == (2, 0)
    assert total["sample_s"] == pytest.approx(60.0)
    person = next(r for r in rows(buf, "zone_stats") if r["class_name"] == "person")
    assert person["occ_avg"] == pytest.approx(1.0)


def test_a_quiet_minute_still_has_zone_and_coverage_rows(buf):
    agg = MinuteAggregator("c", buf)
    for i in range(10):
        agg.add_sample(FrameSample(ts=T0 + i, dt=1.0, zone_occupancy={"z": {}}, zone_queue={"z": {}}))
    agg.close()
    (cov,) = rows(buf, "coverage")
    assert (cov["frames"], cov["seconds"]) == (10, 10.0)
    (zone,) = rows(buf, "zone_stats")
    assert zone["class_name"] == "*" and zone["occ_avg"] == 0 and zone["occ_max"] == 0


def test_dwell_statistics_come_from_zone_visit_events(buf):
    agg = MinuteAggregator("c", buf)
    agg.add_event(visit(T0 + 20, 8.0))
    agg.add_event(visit(T0 + 40, 16.0))
    agg.close()
    total = next(r for r in rows(buf, "zone_stats") if r["class_name"] == "*")
    assert total["visits"] == 2
    assert total["dwell_sum_s"] == pytest.approx(24.0) and total["dwell_max_s"] == pytest.approx(16.0)


def test_a_minute_is_written_only_after_the_late_grace_period(buf):
    agg = MinuteAggregator("c", buf)
    agg.add_sample(FrameSample(ts=T0 + 5, dt=1.0, zone_occupancy={"z": {}}, zone_queue={"z": {}}))
    agg.add_sample(FrameSample(ts=T0 + 62, dt=1.0, zone_occupancy={"z": {}}, zone_queue={"z": {}}))
    assert rows(buf, "coverage") == []  # 62 s is inside the 5 s grace after the minute end
    agg.add_event(visit(T0 + 59, 3.0))  # a late event for the first minute still lands there
    agg.add_sample(FrameSample(ts=T0 + 66, dt=1.0, zone_occupancy={"z": {}}, zone_queue={"z": {}}))
    assert [r["window_start"] for r in rows(buf, "coverage")] == [T0]
    first = next(r for r in rows(buf, "zone_stats") if r["window_start"] == T0 and r["class_name"] == "*")
    assert first["visits"] == 1
    agg.close()


def test_two_runs_in_the_same_minute_add_up(buf):
    for _ in range(2):
        agg = MinuteAggregator("c", buf)
        agg.add_event(cross(T0 + 10))
        agg.add_sample(FrameSample(ts=T0 + 10, dt=5.0, zone_occupancy={"z": {"person": 2}},
                                   zone_queue={"z": {}}))
        agg.close()
    total = next(r for r in rows(buf, "line_counts") if r["class_name"] == "*")
    assert total["in_count"] == 2
    (cov,) = rows(buf, "coverage")
    assert cov["frames"] == 2 and cov["seconds"] == pytest.approx(10.0)
    zone = next(r for r in rows(buf, "zone_stats") if r["class_name"] == "*")
    assert zone["sample_s"] == pytest.approx(10.0) and zone["occ_avg"] == pytest.approx(2.0)
