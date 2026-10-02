from __future__ import annotations

import json

import cv2
import numpy as np
import pytest
import yaml

from countvision_edge import cli
from countvision_edge.pipeline import FrameResult
from countvision_edge.testing.synthetic import demo_scene
from countvision_edge.types import Frame
from countvision_edge.viz import ACCENT, Annotator, VideoRecorder, class_color

from .helpers import make_track


def write_demo_config(tmp_path, video) -> str:
    config = {
        "data_dir": str(tmp_path / "data"),
        "heartbeat_interval_s": 3600,
        "detector": {"type": "blobs"},
        "cameras": [{
            "id": "demo",
            "source": {"uri": str(video), "start_time": "2026-01-01T09:00:00"},
            "classes": ["person", "car"],
            "lines": [{"name": "door", "p1": [0.1, 0.5], "p2": [0.9, 0.5]}],
        }],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(path)


def test_demo_command_passes_and_writes_files(tmp_path, capsys):
    code = cli.main(["demo", "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 0 and out.strip().endswith("PASS")
    assert (tmp_path / "out" / "demo_annotated.mp4").stat().st_size > 1000
    assert (tmp_path / "out" / "demo.db").exists()


def test_run_report_and_csv_roundtrip(tmp_path, demo_video, capsys):
    config = write_demo_config(tmp_path, demo_video)
    report_json = tmp_path / "report.json"
    annotated = tmp_path / "annotated.mp4"
    code = cli.main(["run", "--config", config, "--report", str(report_json),
                     "--annotated", str(annotated)])
    out = capsys.readouterr().out
    assert code == 0 and "Line 'door': IN 4, OUT 2" in out
    data = json.loads(report_json.read_text(encoding="utf-8"))
    assert data["cameras"]["demo"]["lines"]["door"]["in"] == 4
    assert annotated.stat().st_size > 1000

    db = tmp_path / "data" / "countvision.db"
    assert cli.main(["report", "--db", str(db), "--csv-dir", str(tmp_path / "csv")]) == 0
    text = capsys.readouterr().out
    assert "Line 'door': IN 4, OUT 2" in text
    assert (tmp_path / "csv" / "line_counts.csv").read_text(encoding="utf-8").startswith("minute_utc,")
    assert cli.main(["report", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cameras"]["demo"]["lines"]["door"]["out"] == 2


def test_report_time_range_can_exclude_everything(tmp_path, demo_video, capsys):
    config = write_demo_config(tmp_path, demo_video)
    cli.main(["run", "--config", config])
    capsys.readouterr()
    db = tmp_path / "data" / "countvision.db"
    assert cli.main(["report", "--db", str(db), "--since", "2027-01-01T00:00:00"]) == 0
    assert "No data" in capsys.readouterr().out


def test_run_with_source_override_and_max_frames(tmp_path, demo_video, capsys):
    config = write_demo_config(tmp_path, "placeholder-that-does-not-exist.avi")
    code = cli.main(["run", "--config", config, "--source", str(demo_video), "--max-frames", "30",
                     "--start-time", "2026-02-02T10:00:00"])
    assert code == 0 and "30 frames processed" in capsys.readouterr().out


def test_errors_are_short_messages_with_exit_code_2(tmp_path, capsys):
    assert cli.main(["report", "--db", str(tmp_path / "missing.db")]) == 2
    assert "Database not found" in capsys.readouterr().err
    assert cli.main(["run", "--config", str(tmp_path / "missing.yaml")]) == 2
    assert "Config file not found" in capsys.readouterr().err
    assert cli.main(["report", "--db", str(tmp_path / "x.db"), "--since", "yesterday"]) == 2


def test_missing_video_file_is_a_clear_error(tmp_path, capsys):
    config = write_demo_config(tmp_path, tmp_path / "nope.avi")
    assert cli.main(["run", "--config", config]) == 2
    assert "Video file not found" in capsys.readouterr().err


def test_snapshot_saves_one_local_frame(tmp_path, demo_video, capsys):
    out = tmp_path / "snap.jpg"
    assert cli.main(["snapshot", "--source", str(demo_video), "--out", str(out)]) == 0
    image = cv2.imread(str(out))
    assert image is not None and image.shape == (360, 640, 3)
    assert "stays on this computer" in capsys.readouterr().out


def test_command_is_required():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


# ----------------------------------------------------------------------------- annotator


def fake_result(tracks) -> FrameResult:
    image = np.full((360, 640, 3), 45, dtype=np.uint8)
    return FrameResult(Frame(image, 0, 0.0, 0.0), len(tracks), tracks, [], 1.0)


def test_annotator_draws_on_a_copy_and_works_without_an_engine():
    result = fake_result([make_track(1, 200, 200), make_track(2, 400, 250, cls="car")])
    original = result.frame.image.copy()
    drawn = Annotator(lambda: None).draw(result, fps=9.5)
    assert drawn.shape == original.shape
    assert (result.frame.image == original).all()  # input frame untouched
    assert (drawn != original).any()


def test_class_colors_are_stable_and_distinct():
    assert class_color("person") == class_color("person")
    assert class_color("person") != class_color("car")
    assert class_color("something-else") == class_color("another-unknown")
    assert len(ACCENT) == 3


def test_video_recorder_writes_a_playable_file(tmp_path):
    path = str(tmp_path / "x.avi")
    recorder = VideoRecorder(path, 10.0)
    scene = demo_scene()
    for i in range(10):
        recorder.write(scene.render(i))
    recorder.close()
    cap = cv2.VideoCapture(path)
    assert cap.isOpened() and int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 10
    cap.release()
