from __future__ import annotations

import json
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest

from countvision_edge.config import parse_config
from countvision_edge.demo import demo_config, run_demo
from countvision_edge.detectors import build_detector
from countvision_edge.detectors.blobs import ScriptedDetector
from countvision_edge.inputs import create_source
from countvision_edge.pipeline import CameraPipeline
from countvision_edge.report import build_report
from countvision_edge.storage import SqliteBuffer
from countvision_edge.testing.synthetic import DEMO_EXPECTED, demo_scene
from countvision_edge.types import Frame

START = "2026-01-01T09:00:00"


def epoch(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def run_pipeline(video, tmp_path, *, start=START, detector=None, tweak=None, name="a"):
    cfg = demo_config(video, tmp_path / f"data-{name}", start)
    if tweak:
        tweak(cfg)
    cam = cfg.camera()
    buffer = SqliteBuffer(tmp_path / f"{name}.db")
    detector = detector or build_detector(cfg.detector)
    pipeline = CameraPipeline(cam, detector, buffer, data_dir=cfg.data_dir)
    summary = pipeline.run(create_source(cam.source))
    return pipeline, buffer, summary


def totals(buffer) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = {}
    for r in buffer.query("line_counts", camera_id="demo"):
        if r["class_name"] != "*":
            t = out.setdefault(r["class_name"], [0, 0])
            t[0] += r["in_count"]
            t[1] += r["out_count"]
    return {k: (v[0], v[1]) for k, v in out.items()}


def test_demo_self_test_passes(tmp_path):
    result = run_demo(tmp_path, start_time=START)
    failed = [c for c in result.checks if not c.ok]
    assert result.passed, f"failed checks: {failed}"


def test_exact_counts_with_a_fake_detector_that_returns_the_true_boxes(demo_video, tmp_path):
    """Exact input -> exact output, independent of the colour-blob detector."""
    scene = demo_scene()
    detector = ScriptedDetector([scene.ground_truth(i) for i in range(scene.n_frames)])
    _, buffer, summary = run_pipeline(demo_video, tmp_path, detector=detector)
    assert summary.frames_processed == scene.n_frames
    assert totals(buffer) == {
        "person": (DEMO_EXPECTED["person_in"], DEMO_EXPECTED["person_out"]),
        "car": (DEMO_EXPECTED["car_in"], DEMO_EXPECTED["car_out"]),
    }


def test_counts_from_the_real_video_file_through_the_blob_detector(demo_video, tmp_path):
    _, buffer, _ = run_pipeline(demo_video, tmp_path)
    assert totals(buffer) == {"person": (3, 2), "car": (1, 0)}


def test_events_and_zone_statistics_match_the_scene(demo_video, tmp_path):
    _, buffer, _ = run_pipeline(demo_video, tmp_path)
    events = buffer.query("events")
    crossings = [e for e in events if e["kind"] == "line_cross"]
    visits = [e for e in events if e["kind"] == "zone_visit"]
    assert len(crossings) == 6 and len(visits) == 1
    assert sorted(e["direction"] for e in crossings) == ["in"] * 4 + ["out"] * 2
    assert 9.5 <= visits[0]["dwell_s"] <= 11.5
    zone = next(r for r in buffer.query("zone_stats") if r["class_name"] == "*")
    assert zone["occ_max"] == 1 and zone["queue_max"] == 1 and zone["visits"] == 1
    assert zone["sample_s"] == pytest.approx(18.0, abs=0.2)
    assert 0.5 < zone["occ_avg"] < 0.7  # ~10.4 s of 18 s with one person inside
    (cov,) = buffer.query("coverage")
    assert cov["frames"] == 270 and cov["seconds"] == pytest.approx(18.0, abs=0.2)


def test_timestamps_follow_the_start_time_and_split_across_minutes(demo_video, tmp_path):
    _, buffer, _ = run_pipeline(demo_video, tmp_path, start="2026-01-01T09:00:50")
    windows = {r["window_start"] for r in buffer.query("coverage")}
    base = int(epoch("2026-01-01T09:00:00"))
    assert windows == {base, base + 60}
    assert totals(buffer) == {"person": (3, 2), "car": (1, 0)}  # nothing lost at the boundary
    first, second = (buffer.query("coverage", start=w, end=w + 60)[0] for w in sorted(windows))
    assert first["seconds"] == pytest.approx(10.0, abs=0.2) and second["seconds"] == pytest.approx(8.0, abs=0.2)


def test_frame_skipping_keeps_the_counts(demo_video, tmp_path):
    def tweak(cfg):
        cfg.camera().scheduler.target_fps = 5.0  # a 15 FPS video -> every third frame
        cfg.camera().scheduler.adaptive = False

    _, buffer, summary = run_pipeline(demo_video, tmp_path, tweak=tweak)
    assert summary.frames_processed == 90 and summary.frames_skipped == 180
    assert totals(buffer) == {"person": (3, 2), "car": (1, 0)}


def test_only_the_configured_classes_are_counted(demo_video, tmp_path):
    def tweak(cfg):
        cfg.camera().classes = ["person"]

    _, buffer, _ = run_pipeline(demo_video, tmp_path, tweak=tweak)
    assert totals(buffer) == {"person": (3, 2)}


def test_heartbeat_heatmap_and_state(demo_video, tmp_path):
    pipeline, buffer, summary = run_pipeline(demo_video, tmp_path)
    beat = buffer.latest_heartbeat("demo")
    assert beat is not None and beat["frames_processed"] == 270 and beat["connected"] == 1
    png = tmp_path / "data-a" / "heatmaps" / "demo.png"
    npy = tmp_path / "data-a" / "heatmaps" / "demo.npy"
    assert png.exists() and npy.exists()
    grid = np.load(npy)
    # The grid holds object-seconds: the 8 objects are on screen 4+4+4+4+4+4+4+12 = 40 s in total,
    # minus the ~0.3 s each track needs before it is confirmed.
    assert 36.0 <= float(grid.sum()) <= 40.5
    assert grid.shape == (108, 192)
    state = summary.state
    assert state["lines"]["door"]["in"] == 4 and state["lines"]["door"]["out"] == 2
    assert pipeline.state()["frames_processed"] == 270


def test_running_the_same_video_twice_adds_up(demo_video, tmp_path):
    cfg = demo_config(demo_video, tmp_path / "d", START)
    cam = cfg.camera()
    buffer = SqliteBuffer(tmp_path / "twice.db")
    for _ in range(2):
        CameraPipeline(cam, build_detector(cfg.detector), buffer, data_dir=cfg.data_dir).run(
            create_source(cam.source)
        )
    assert totals(buffer) == {"person": (6, 4), "car": (2, 0)}


def test_nothing_but_numbers_is_stored(demo_video, tmp_path):
    _, buffer, _ = run_pipeline(demo_video, tmp_path)
    for table in ("events", "line_counts", "zone_stats", "coverage", "heartbeats"):
        rows = buffer.query(table)
        for row in rows:
            assert all(isinstance(v, (int, float, str, type(None))) for v in row.values())
            assert not any(isinstance(v, (bytes, bytearray)) for v in row.values())
    stored_images = [p for p in (tmp_path / "data-a").rglob("*") if p.suffix in (".jpg", ".mp4", ".avi")]
    assert stored_images == []  # the heatmap is the only picture, and it is a .png of numbers


def test_max_frames_stops_early(demo_video, tmp_path):
    cfg = demo_config(demo_video, tmp_path / "d", START)
    cam = cfg.camera()
    pipeline = CameraPipeline(cam, build_detector(cfg.detector), SqliteBuffer(tmp_path / "m.db"),
                              data_dir=cfg.data_dir)
    summary = pipeline.run(create_source(cam.source), max_frames=40)
    assert summary.frames_processed == 40


def test_frame_size_change_keeps_counting_with_normalised_geometry(tmp_path):
    cfg = demo_config("unused.avi", tmp_path / "d", START)
    cam = cfg.camera()
    scene = demo_scene()
    pipeline = CameraPipeline(cam, build_detector(cfg.detector), SqliteBuffer(tmp_path / "s.db"),
                              data_dir=cfg.data_dir)
    for i in range(scene.n_frames):
        image = scene.render(i)
        if i >= 100:  # the camera switches to a smaller stream
            image = cv2.resize(image, (320, 180), interpolation=cv2.INTER_NEAREST)
        pipeline.process(Frame(image=image, index=i, ts=epoch(START) + i / 15, media_ts=i / 15))
    pipeline.finish()
    line = pipeline.engine.lines[0]
    assert line.a == pytest.approx((32.0, 90.0)) and line.b == pytest.approx((288.0, 90.0))
    assert pipeline.engine.frame_size == (320, 180)
    assert pipeline.state()["lines"]["door"]["by_class"]["person"] == {"in": 3, "out": 2}


def test_report_matches_the_database(demo_video, tmp_path):
    _, buffer, _ = run_pipeline(demo_video, tmp_path)
    report = build_report(buffer)
    cam = report["cameras"]["demo"]
    assert cam["lines"]["door"]["in"] == 4 and cam["lines"]["door"]["out"] == 2
    assert cam["lines"]["door"]["by_class"]["car"] == {"in": 1, "out": 0}
    zone = cam["zones"]["waiting"]
    assert zone["visits"] == 1 and zone["queue_max"] == 1 and 9.5 <= zone["dwell_avg_s"] <= 11.5
    assert json.dumps(report)  # serialisable


def test_a_file_replay_in_the_past_is_not_purged(demo_video, tmp_path):
    """Retention applies to live runs only: replaying an old video must keep its numbers."""
    _, buffer, _ = run_pipeline(demo_video, tmp_path, start="2020-01-01T09:00:00")
    assert totals(buffer) == {"person": (3, 2), "car": (1, 0)}


def test_pipeline_rejects_a_class_the_model_does_not_know(tmp_path):
    cfg = parse_config(
        {"detector": {"type": "blobs"},
         "cameras": [{"id": "c", "source": {"uri": "v.avi"}, "classes": ["elephant"]}]}, env={})
    from countvision_edge.errors import DetectorError

    with pytest.raises(DetectorError, match="elephant"):
        CameraPipeline(cfg.camera(), build_detector(cfg.detector), SqliteBuffer(tmp_path / "e.db"))
