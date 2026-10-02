from __future__ import annotations

import pytest

from countvision_edge.storage import (
    CoverageRow,
    LineCountRow,
    SqliteBuffer,
    ZoneStatRow,
)
from countvision_edge.types import Event


@pytest.fixture
def buf(tmp_path):
    buffer = SqliteBuffer(tmp_path / "sub" / "t.db")
    yield buffer
    buffer.close()


def event(event_id="e1", ts=1000.0, **kw) -> Event:
    base = {"kind": "line_cross", "camera_id": "c", "name": "door", "track_id": 1,
            "class_name": "person", "direction": "in"}
    return Event(event_id=event_id, ts=ts, **{**base, **kw})


def zone_row(**kw) -> ZoneStatRow:
    base = {"camera_id": "c", "window_start": 600, "zone": "z", "class_name": "*",
            "sample_s": 30.0, "occ_avg": 2.0, "occ_max": 3, "occ_last": 1, "queue_avg": 1.0,
            "queue_max": 2, "visits": 1, "dwell_sum_s": 10.0, "dwell_max_s": 10.0}
    return ZoneStatRow(**{**base, **kw})


def test_creates_parent_folder_and_uses_wal(tmp_path):
    buffer = SqliteBuffer(tmp_path / "a" / "b" / "x.db")
    mode = buffer._db.execute("PRAGMA journal_mode").fetchone()[0]
    buffer.close()
    assert mode.lower() == "wal"


def test_events_are_idempotent_by_event_id(buf):
    buf.add_events([event("a"), event("b")])
    buf.add_events([event("a")])  # replay
    assert len(buf.query("events")) == 2


def test_events_have_no_position_or_image_columns(buf):
    columns = set(buf.query("events")[0] if buf.query("events") else [])
    buf.add_events([event()])
    columns = set(buf.query("events")[0])
    assert not {"x", "y", "xyxy", "image", "frame"} & columns


def test_line_counts_are_added_when_the_minute_exists_and_marked_unsent(buf):
    buf.add_line_counts([LineCountRow("c", 600, "door", "person", 2, 1)])
    row = buf.fetch_unsent("line_counts")[0]
    buf.mark_sent("line_counts", [row["id"]])
    assert buf.fetch_unsent("line_counts") == []
    buf.add_line_counts([LineCountRow("c", 600, "door", "person", 3, 4)])  # restart in same minute
    rows = buf.query("line_counts")
    assert len(rows) == 1 and (rows[0]["in_count"], rows[0]["out_count"]) == (5, 5)
    assert len(buf.fetch_unsent("line_counts")) == 1  # changed rows must be uploaded again


def test_zone_stats_merge_is_weighted_by_measured_seconds(buf):
    buf.add_zone_stats([zone_row(sample_s=30, occ_avg=2.0, occ_max=3, queue_avg=1.0)])
    buf.add_zone_stats(
        [zone_row(sample_s=10, occ_avg=6.0, occ_max=7, occ_last=0, queue_avg=3.0, queue_max=4,
                  visits=2, dwell_sum_s=5.0, dwell_max_s=4.0)]
    )
    (row,) = buf.query("zone_stats")
    assert row["sample_s"] == 40
    assert row["occ_avg"] == pytest.approx((2 * 30 + 6 * 10) / 40)
    assert row["queue_avg"] == pytest.approx((1 * 30 + 3 * 10) / 40)
    assert (row["occ_max"], row["occ_last"], row["queue_max"]) == (7, 0, 4)
    assert (row["visits"], row["dwell_sum_s"], row["dwell_max_s"]) == (3, 15.0, 10.0)


def test_coverage_is_added(buf):
    buf.add_coverage(CoverageRow("c", 600, 100, 10.0))
    buf.add_coverage(CoverageRow("c", 600, 50, 5.0))
    (row,) = buf.query("coverage")
    assert (row["frames"], row["seconds"]) == (150, 15.0)


def test_fetch_unsent_and_mark_sent_for_events(buf):
    buf.add_events([event(f"e{i}", ts=1000 + i) for i in range(5)])
    first = buf.fetch_unsent("events", limit=3)
    assert [r["event_id"] for r in first] == ["e0", "e1", "e2"]
    buf.mark_sent("events", [r["id"] for r in first])
    assert [r["event_id"] for r in buf.fetch_unsent("events")] == ["e3", "e4"]


def test_query_filters_by_camera_and_time_range(buf):
    buf.add_events([event("a", ts=100.0), event("b", ts=200.0), event("c", ts=300.0, camera_id="other")])
    assert [r["event_id"] for r in buf.query("events", camera_id="c")] == ["a", "b"]
    assert [r["event_id"] for r in buf.query("events", start=150.0, end=250.0)] == ["b"]


def test_purge_deletes_old_rows_only(tmp_path):
    buffer = SqliteBuffer(tmp_path / "p.db", retention_days=30, heartbeat_retention_hours=48)
    now = 100 * 86400.0
    old, recent = now - 31 * 86400, now - 1 * 86400
    buffer.add_events([event("old", ts=old), event("new", ts=recent)])
    buffer.add_line_counts([LineCountRow("c", int(old), "door", "person", 1, 0),
                            LineCountRow("c", int(recent), "door", "person", 1, 0)])
    buffer.add_heartbeat({"ts": now - 49 * 3600, "camera_id": "c"})
    buffer.add_heartbeat({"ts": now - 1 * 3600, "camera_id": "c"})
    deleted = buffer.purge(now)
    assert deleted["events"] == 1 and deleted["line_counts"] == 1 and deleted["heartbeats"] == 1
    assert [r["event_id"] for r in buffer.query("events")] == ["new"]
    buffer.close()


def test_a_second_connection_can_read_while_the_writer_is_open(tmp_path):
    writer = SqliteBuffer(tmp_path / "w.db")
    writer.add_events([event()])
    reader = SqliteBuffer(tmp_path / "w.db")
    assert len(reader.query("events")) == 1
    writer.add_events([event("e2")])
    assert len(reader.query("events")) == 2
    reader.close()
    writer.close()


def test_heartbeat_roundtrip(buf):
    buf.add_heartbeat({"ts": 5.0, "camera_id": "c", "fps": 9.5, "connected": 1})
    beat = buf.latest_heartbeat("c")
    assert beat["fps"] == 9.5 and beat["connected"] == 1
    assert buf.latest_heartbeat("nope") is None


def test_unknown_table_is_rejected(buf):
    with pytest.raises(ValueError):
        buf.query("sqlite_master")
