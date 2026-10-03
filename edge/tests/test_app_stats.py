"""Numbers for the web app (stats module) and live geometry changes in the pipeline."""

from __future__ import annotations

import time
from datetime import datetime

import numpy as np

from countvision_edge.app.stats import day_bounds, hourly_csv, line_timeline, line_totals, zone_totals
from countvision_edge.config import CameraConfig
from countvision_edge.detectors.blobs import BlobDetector
from countvision_edge.pipeline import CameraPipeline
from countvision_edge.report import build_report, format_report
from countvision_edge.storage import SqliteBuffer, ZoneStatRow
from countvision_edge.types import Event, Frame

from .helpers import make_track


def _cross(ts: float, name: str = "door", direction: str = "in", cls: str = "person", n: int = 0) -> Event:
    return Event(f"{name}-{ts}-{direction}-{n}", "line_cross", ts, "cam", name, n, cls, direction=direction)


def _visit(ts: float, dwell: float) -> Event:
    return Event(f"v-{ts}", "zone_visit", ts, "cam", "till", 1, "person", enter_ts=ts - dwell, dwell_s=dwell)


def test_day_bounds_is_the_local_day():
    noon = datetime(2026, 10, 3, 12, 30).timestamp()
    start, end = day_bounds(noon)
    assert datetime.fromtimestamp(start) == datetime(2026, 10, 3)
    assert datetime.fromtimestamp(end) == datetime(2026, 10, 4)


def test_totals_and_timeline(tmp_path):
    buffer = SqliteBuffer(tmp_path / "t.db")
    start = datetime(2026, 10, 3).timestamp()
    buffer.add_events([
        _cross(start + 9 * 3600 + 5, n=1),
        _cross(start + 9 * 3600 + 50, direction="out", n=2),
        _cross(start + 14 * 3600, cls="car", n=3),
        _cross(start + 14 * 3600 + 1, name="side", n=4),
        _cross(start - 10, n=5),  # yesterday: not counted
        _visit(start + 10 * 3600, 30.0),
        _visit(start + 11 * 3600, 90.0),
    ])
    buffer.add_zone_stats([ZoneStatRow("cam", int(start + 10 * 3600), "till", "*", 60, 1.5, 4, 2, 0, 0, 1, 30, 30)])
    end = start + 86400

    lines = line_totals(buffer, "cam", start, end)
    assert lines["door"]["in"] == 2 and lines["door"]["out"] == 1
    assert lines["door"]["by_class"] == {"person": {"in": 1, "out": 1}, "car": {"in": 1, "out": 0}}
    assert lines["side"]["in"] == 1

    zones = zone_totals(buffer, "cam", start, end)
    assert zones["till"] == {"visits": 2, "dwell_avg_s": 60.0, "dwell_max_s": 90.0, "occupancy_max": 4}

    hours = line_timeline(buffer, "cam", start, end, 3600, line="door")
    assert len(hours) == 24
    assert (hours[9]["in"], hours[9]["out"], hours[14]["in"]) == (1, 1, 1)
    all_lines = line_timeline(buffer, "cam", start, end, 3600)
    assert all_lines[14]["in"] == 2

    csv_text = hourly_csv(buffer, "cam", start, end)
    rows = csv_text.strip().splitlines()
    assert rows[0].startswith("date,hour,camera,type,name")
    assert "2026-10-03,09:00,cam,line,door,1,1,,," in rows
    assert "2026-10-03,10:00,cam,zone,till,,,1,30.0,4" in rows
    german = hourly_csv(buffer, "cam", start, end, delimiter=";").strip().splitlines()
    assert german[0].startswith("date;hour;camera")
    assert "2026-10-03;10:00;cam;zone;till;;;1;30,0;4" in german
    buffer.close()


def test_report_prints_zero_for_quiet_lines(tmp_path):
    buffer = SqliteBuffer(tmp_path / "t.db")
    report = build_report(buffer, "cam", line_names=["door"])
    assert "Line 'door': IN 0, OUT 0" in format_report(report)
    buffer.close()


def _camera(lines) -> CameraConfig:
    return CameraConfig.model_validate({
        "id": "cam", "source": {"uri": "x.mp4"}, "classes": ["person"], "coordinates": "pixel",
        "lines": lines, "heatmap": {"enabled": False},
    })


def test_pipeline_reconfigure_keeps_totals_by_name(tmp_path):
    buffer = SqliteBuffer(tmp_path / "p.db")
    cam = _camera([{"name": "door", "p1": [0, 100], "p2": [400, 100]}])
    pipeline = CameraPipeline(cam, BlobDetector(), buffer, data_dir=tmp_path)
    image = np.zeros((200, 400, 3), dtype=np.uint8)
    now = time.time()
    pipeline.process(Frame(image, 0, now, 0.0))
    engine = pipeline.engine
    engine.lines[0].update([make_track(1, 50, 80)], now)
    engine.lines[0].update([make_track(1, 50, 120)], now + 0.1)
    assert engine.lines[0].summary()["in"] + engine.lines[0].summary()["out"] == 1

    new_cam = _camera([
        {"name": "door", "p1": [0, 150], "p2": [400, 150]},
        {"name": "new", "p1": [200, 0], "p2": [200, 200]},
    ])
    pipeline.reconfigure(new_cam)
    pipeline.process(Frame(image, 1, now + 0.2, 0.2))
    assert pipeline.engine is not engine
    assert pipeline.cam is new_cam
    names = {c.name: c for c in pipeline.engine.lines}
    assert set(names) == {"door", "new"}
    assert names["door"].a == (0.0, 150.0)
    assert sum(names["door"].summary()[k] for k in ("in", "out")) == 1  # totals kept
    assert names["new"].summary()["in"] == 0
    pipeline.finish()
    buffer.close()
