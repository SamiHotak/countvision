"""Evaluation: label files, metrics, detection cache, runner, tuning guard, report, bench."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from countvision_edge.cli import main
from countvision_edge.errors import ConfigError
from countvision_edge.evaluation.bench import run_bench
from countvision_edge.evaluation.cache import DetectionCache, probe_video
from countvision_edge.evaluation.labels import (
    ClipLabels,
    labels_to_yaml,
    load_label_dir,
    load_labels,
    save_labels,
)
from countvision_edge.evaluation.metrics import (
    Crossing,
    OccupancyMetrics,
    compare_crossings,
    count_accuracy,
    match_times,
    value_at,
)
from countvision_edge.evaluation.report import build_auto_block, write_results
from countvision_edge.evaluation.runner import ClipRunner, EvalParams, scheduled_indices
from countvision_edge.evaluation.specs import known_license, parse_detector_spec, spec_key
from countvision_edge.evaluation.tune import SplitError, evaluate, expand_grid, grid_search

# The synthetic demo scene (15 FPS, 640x360) with hand-written truth: see testing/synthetic.py.
DEMO_LABELS = {
    "clip": "demo",
    "video": "demo.avi",
    "split": "tune",
    "classes": ["person", "car"],
    "lines": [{"name": "door", "p1": [0.1, 0.5], "p2": [0.9, 0.5]}],
    "zones": [{"name": "waiting", "polygon": [[0.05, 0.62], [0.45, 0.62], [0.45, 0.98], [0.05, 0.98]]}],
    # feet cross y=180: A (330, 1..5 s, 60->300) at t = 1 + (180-60)/240*4 = 3.0 ...
    "crossings": [
        {"t": 3.0 - 35 / 60, "line": "door", "dir": "in", "cls": "person"},
        {"t": 5.0 - 35 / 60, "line": "door", "dir": "in", "cls": "person"},
        {"t": 7.0 - 35 / 60, "line": "door", "dir": "in", "cls": "person"},
        {"t": 10.0 + 35 / 60, "line": "door", "dir": "out", "cls": "person"},
        {"t": 12.0 + 35 / 60, "line": "door", "dir": "out", "cls": "person"},
        {"t": 14.0 - 25 / 60, "line": "door", "dir": "in", "cls": "car"},
    ],
    "occupancy": [
        {"t": 1.0, "zone": "waiting", "n": 0},
        {"t": 7.0, "zone": "waiting", "n": 1},
        {"t": 9.0, "zone": "waiting", "n": 1},
        {"t": 16.0, "zone": "waiting", "n": 0},
    ],
}


@pytest.fixture
def eval_dirs(tmp_path: Path, demo_video: str) -> dict[str, Path]:
    clips, videos = tmp_path / "clips", tmp_path / "videos"
    clips.mkdir()
    videos.mkdir()
    shutil.copy(demo_video, videos / "demo.avi")
    save_labels(ClipLabels.model_validate(DEMO_LABELS), clips / "demo.yaml")
    return {"root": tmp_path, "clips": clips, "videos": videos, "cache": tmp_path / "cache",
            "runs": tmp_path / "runs"}


def blob_runner(dirs, labels: ClipLabels | None = None) -> ClipRunner:
    labels = labels or load_labels(dirs["clips"] / "demo.yaml")
    spec = "blobs:demo"
    return ClipRunner(labels, dirs["videos"] / labels.video, spec, parse_detector_spec(spec), dirs["cache"])


# ------------------------------------------------------------------ labels


def test_labels_roundtrip_and_sorting(tmp_path):
    data = dict(DEMO_LABELS, crossings=list(reversed(DEMO_LABELS["crossings"])))
    labels = ClipLabels.model_validate(data)
    assert [c.t for c in labels.crossings] == sorted(c.t for c in labels.crossings)
    path = save_labels(labels, tmp_path / "x.yaml")
    again = load_labels(path)  # times are written with 2 decimals
    assert [round(c.t, 2) for c in labels.crossings] == [c.t for c in again.crossings]
    assert again.model_dump(exclude={"crossings"}) == labels.model_dump(exclude={"crossings"})
    text = labels_to_yaml(labels)
    assert "- {t: 2.42, line: door, dir: in, cls: person}" in text


@pytest.mark.parametrize(("change", "message"), [
    ({"crossings": [{"t": 1, "line": "nope", "dir": "in"}]}, "unknown line"),
    ({"occupancy": [{"t": 1, "zone": "nope", "n": 1}]}, "unknown zone"),
    ({"crossings": [{"t": 1, "line": "door", "dir": "in", "cls": "dog"}]}, "class 'dog'"),
    ({"lines": [{"name": "door", "p1": [10, 10], "p2": [200, 10]}]}, "outside 0..1"),
    ({"split": "train"}, "split"),
])
def test_bad_labels_are_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        ClipLabels.model_validate(DEMO_LABELS | change)


def test_label_dir_filters_splits_and_finds_duplicates(tmp_path):
    save_labels(ClipLabels.model_validate(DEMO_LABELS), tmp_path / "a.yaml")
    save_labels(ClipLabels.model_validate(DEMO_LABELS | {"clip": "b", "split": "test"}), tmp_path / "b.yaml")
    assert [c.clip for c in load_label_dir(tmp_path, ["test"])] == ["b"]
    save_labels(ClipLabels.model_validate(DEMO_LABELS), tmp_path / "c.yaml")
    with pytest.raises(ConfigError, match="Duplicate clip ids"):
        load_label_dir(tmp_path)


def test_repo_label_files_are_valid():
    folder = Path(__file__).resolve().parents[2] / "eval" / "clips"
    clips = load_label_dir(folder)
    assert len(clips) >= 6
    assert {c.split for c in clips} == {"tune", "test"}
    for clip in clips:
        assert clip.license and clip.credit and clip.download_url, clip.clip


# ------------------------------------------------------------------ metrics


def test_match_times_is_one_to_one():
    assert match_times([1.0, 2.0, 3.0], [1.1, 1.2, 2.9], 0.5) == 2
    assert match_times([], [1.0], 1.0) == 0
    assert match_times([1.0, 1.1], [1.05], 1.0) == 1


def test_compare_crossings_counts_and_cancelling_errors():
    truth = [Crossing(1, "d", "in", "person"), Crossing(10, "d", "in", "person")]
    # one missed (t=10) and one false count (t=30): count is right, events are not
    counted = [Crossing(1.5, "d", "in", "person"), Crossing(30, "d", "in", "person")]
    m = compare_crossings(truth, counted, tolerance_s=2).to_dict()
    assert m["count_accuracy"] == 1.0
    assert m["matched"] == 1 and m["f1"] == pytest.approx(0.5)
    wrong_dir = compare_crossings(truth, [Crossing(1, "d", "out", "person")]).to_dict()
    assert wrong_dir["abs_error"] == 3 and wrong_dir["count_accuracy"] == 0.0


def test_occupancy_metrics_and_step_series():
    series = [(0.0, 0), (1.0, 2), (2.0, 1)]
    assert value_at(series, 0.5) == 0 and value_at(series, 1.0) == 2 and value_at(series, 9) == 1
    assert value_at([], 3) == 0
    occ = OccupancyMetrics()
    for true_n, counted_n in [(2, 2), (4, 3), (0, 1)]:
        occ.add(true_n, counted_n)
    d = occ.to_dict()
    assert d["mae"] == pytest.approx(2 / 3) and d["exact_rate"] == pytest.approx(1 / 3)
    assert d["mean_error"] == 0.0  # 6 true, 6 counted on average
    assert OccupancyMetrics.from_dict(d).to_dict() == d
    assert count_accuracy(5, 0) is None and count_accuracy(15, 10) == 0.0


# ------------------------------------------------------------------ specs


def test_detector_specs_and_licences():
    cfg = parse_detector_spec("onnx:yolox_tiny.onnx@416")
    assert cfg.type == "onnx" and cfg.imgsz == 416 and cfg.model_license.startswith("Apache-2.0")
    assert parse_detector_spec("rfdetr:nano").rfdetr_variant == "nano"
    assert known_license("yolo11n.pt", "ultralytics").startswith("AGPL")
    assert known_license("yolo11n.onnx", "onnx").startswith("AGPL")
    assert known_license("mystery.onnx", "onnx").startswith("UNKNOWN")
    assert spec_key("ultralytics:yolo11n.pt@640") == "ultralytics_yolo11n.pt_640"
    for bad in ("yolo", "foo:x.onnx", "rfdetr:huge"):
        with pytest.raises(ConfigError):
            parse_detector_spec(bad)


# ------------------------------------------------------------------ cache + runner


def test_scheduled_indices_follow_the_live_scheduler():
    assert scheduled_indices(30, 15.0, None) == list(range(30))
    assert len(scheduled_indices(150, 15.0, 10.0)) == pytest.approx(100, abs=2)
    assert len(scheduled_indices(120, 12.0, 10.0)) == pytest.approx(100, abs=2)


def test_runner_gives_exact_counts_on_the_synthetic_scene(eval_dirs):
    runner = blob_runner(eval_dirs)
    detected = runner.prepare([None, 10.0])
    assert detected == runner.info.n_frames  # every frame, because fps=None was asked for
    for fps in (None, 10.0):
        result = runner.run(EvalParams(fps=fps), keep_events=True)
        c = result.crossings
        assert (c["true"], c["counted"], c["matched"]) == (6, 6, 6), fps
        assert result.occupancy["exact_rate"] == 1.0
    assert all(abs(e["t"] - t["t"]) < 0.5 for e, t in zip(
        sorted(result.counted_events, key=lambda e: e["t"]), DEMO_LABELS["crossings"], strict=True))


def test_cache_is_reused_and_survives_a_reload(eval_dirs):
    runner = blob_runner(eval_dirs)
    runner.prepare([10.0])
    again = blob_runner(eval_dirs)
    assert again.prepare([10.0]) == 0  # nothing to detect again
    assert again.cache.detector_name == "blobs"
    # a different video file (other size) invalidates the cache
    info = probe_video(eval_dirs["videos"] / "demo.avi")
    info.file_size += 1
    assert DetectionCache.open(runner.cache.path, info) is None


def test_unprepared_frames_are_an_error(eval_dirs):
    with pytest.raises(RuntimeError, match="not cached"):
        blob_runner(eval_dirs).run(EvalParams())


def test_conf_threshold_is_applied_on_replay(eval_dirs):
    runner = blob_runner(eval_dirs)
    runner.prepare([10.0])
    result = runner.run(EvalParams(conf=0.95))  # blobs have confidence 0.9 -> nothing found
    assert result.crossings["counted"] == 0


def test_labeled_until_limits_the_scored_range(eval_dirs):
    labels = ClipLabels.model_validate(DEMO_LABELS | {"labeled_until_s": 8.0})
    runner = blob_runner(eval_dirs, labels)
    runner.prepare([10.0])
    assert runner.run(EvalParams()).crossings["true"] == 3


# ------------------------------------------------------------------ tuning


def test_grid_search_refuses_test_clips(eval_dirs):
    labels = ClipLabels.model_validate(DEMO_LABELS | {"split": "test"})
    with pytest.raises(SplitError, match="split: tune"):
        grid_search([blob_runner(eval_dirs, labels)], {"conf": [0.2]})
    with pytest.raises(SplitError):
        grid_search([], {"conf": [0.2]})


def test_grid_search_ranks_and_prefers_defaults_on_ties(eval_dirs):
    runner = blob_runner(eval_dirs)
    runner.prepare([10.0])
    result = grid_search([runner], {"conf": [0.2, 0.95], "min_track_frames": [3, 2]})
    assert len(result["trials"]) == 4
    assert result["best"]["conf"] == 0.2 and result["best"]["min_track_frames"] == 3  # = defaults
    assert result["trials"][-1]["params"]["conf"] == 0.95
    with pytest.raises(ValueError, match="Unknown grid"):
        expand_grid({"speed": [1]}, EvalParams())


def test_evaluate_summaries_by_scene_and_split(eval_dirs):
    runner = blob_runner(eval_dirs)
    runner.prepare([10.0])
    results, summary = evaluate([runner], EvalParams())
    assert summary["all"]["crossings"]["count_accuracy"] == 1.0
    assert set(summary["by_scene"]) == {"clear"} and set(summary["by_split"]) == {"tune"}
    assert results[0].to_dict()["clip"] == "demo"


def test_params_files(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("params: {conf: 0.3, min_track_frames: 5}\n", encoding="utf-8")
    params = EvalParams.load(path)
    assert params.conf == 0.3 and params.tracker().min_track_frames == 5
    with pytest.raises(ValueError, match="Unknown parameter"):
        EvalParams.from_dict({"nope": 1})


# ------------------------------------------------------------------ CLI + report


def test_cli_tune_run_report(eval_dirs, capsys):
    d = {k: str(v) for k, v in eval_dirs.items()}
    common = ["--clips", d["clips"], "--videos", d["videos"], "--cache", d["cache"], "--runs", d["runs"]]
    grid = eval_dirs["root"] / "grid.yaml"
    grid.write_text("conf: [0.2, 0.3]\n", encoding="utf-8")
    assert main(["eval", "tune", "--detector", "blobs:demo", *common, "--grid", str(grid),
                 "--params-dir", str(eval_dirs["root"] / "params")]) == 0
    params = eval_dirs["root"] / "params" / "blobs_demo.yaml"
    assert params.is_file()
    for tag, extra in (("before", []), ("after", ["--params", str(params)])):
        assert main(["eval", "run", "--detector", "blobs:demo", *common, "--split", "tune",
                     "--tag", tag, *extra]) == 0
    out = capsys.readouterr().out
    assert "100.0 %" in out
    run = json.loads((eval_dirs["runs"] / "after_blobs_demo_tune.json").read_text())
    assert run["summary"]["all"]["crossings"]["count_accuracy"] == 1.0 and run["code_version"]

    bench = run_bench(["blobs:demo"], video=eval_dirs["videos"] / "demo.avi", frames=10,
                      frame_size=(640, 360), label="test")
    (eval_dirs["root"] / "bench").mkdir()
    (eval_dirs["root"] / "bench" / "test.json").write_text(json.dumps(bench), encoding="utf-8")
    results = eval_dirs["root"] / "results.md"
    results.write_text("# Results\n\nMy conclusion stays.\n", encoding="utf-8")
    assert main(["eval", "report", "--clips", d["clips"], "--videos", d["videos"], "--runs", d["runs"],
                 "--bench", str(eval_dirs["root"] / "bench"), "--out", str(results)]) == 0
    text = results.read_text(encoding="utf-8")
    assert "My conclusion stays." in text
    assert "100.0 % → **100.0 %**" in text  # before -> after row
    assert "`blobs:demo`" in text and "Pipeline FPS" in text
    # running the report again replaces the block instead of adding a second one
    main(["eval", "report", "--clips", d["clips"], "--videos", d["videos"], "--runs", d["runs"],
          "--bench", str(eval_dirs["root"] / "bench"), "--out", str(results)])
    assert results.read_text(encoding="utf-8").count("AUTO:START") == 1


def test_missing_video_gives_a_clear_error(eval_dirs, capsys):
    (eval_dirs["videos"] / "demo.avi").unlink()
    code = main(["eval", "run", "--detector", "blobs:demo", "--clips", str(eval_dirs["clips"]),
                 "--videos", str(eval_dirs["videos"]), "--split", "tune"])
    assert code == 2 and "eval fetch" in capsys.readouterr().err


def test_bench_reports_errors_per_model(tmp_path):
    data = run_bench(["blobs:demo", "onnx:does_not_exist.onnx"], video=None, frames=5,
                     frame_size=(320, 240))
    ok, broken = data["results"]
    assert ok["error"] is None and ok["det_fps"] > 0 and ok["cameras_at_10fps"] >= 0
    assert broken["error"] and broken["det_fps"] is None
    assert data["machine"]["cpu"]
    block = build_auto_block([], [], [], [data])
    assert "error:" in block
    write_results(tmp_path / "r.md", block)
    assert (tmp_path / "r.md").read_text(encoding="utf-8").startswith("# CountVision evaluation results")


def test_bench_frames_from_video_are_resized(demo_video):
    from countvision_edge.evaluation.bench import load_frames

    frames = load_frames(demo_video, 40, (320, 180))  # objects appear after 1 s
    assert len(frames) == 40 and frames[0].shape == (180, 320, 3)
    assert not np.array_equal(frames[0], frames[-1])
