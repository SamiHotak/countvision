"""Command-line handlers for ``countvision-edge eval ...``, ``bench`` and ``count-helper``."""

from __future__ import annotations

import argparse
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .. import __version__
from ..errors import CountVisionError
from ..logging_setup import setup_logging
from .bench import run_bench, save_json
from .labels import ClipLabels, load_label_dir
from .runner import ClipRunner, EvalParams
from .specs import parse_detector_spec, spec_key, spec_license
from .tune import DEFAULT_GRID, evaluate, grid_search

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017


def _progress(prefix: str):
    def show(done: int, total: int) -> None:
        end = "\n" if done >= total else ""
        print(f"\r{prefix}: {done}/{total}", end=end, flush=True)

    return show


def _video_path(args: argparse.Namespace, labels: ClipLabels) -> Path:
    path = Path(args.videos) / labels.video
    if not path.is_file():
        raise CountVisionError(
            f"Video for clip '{labels.clip}' not found: {path}\n"
            "Run: countvision-edge eval fetch   (or download it by hand, see eval/README.md)"
        )
    return path


def _splits(value: str) -> list[str] | None:
    return None if value == "all" else value.split(",")


def _runners(args: argparse.Namespace, splits: list[str] | None) -> list[ClipRunner]:
    clips = load_label_dir(args.clips, splits)
    if args.clip:
        wanted = set(args.clip)
        clips = [c for c in clips if c.clip in wanted]
    if not clips:
        raise CountVisionError(f"No label files for split(s) {splits or 'all'} in {args.clips}")
    cfg = parse_detector_spec(args.detector, device=args.device, models_dir=args.models_dir)
    return [ClipRunner(c, _video_path(args, c), args.detector, cfg, args.cache) for c in clips]


def _prepare(runners: list[ClipRunner], fps_values: list[float | None]) -> None:
    for runner in runners:
        done = runner.prepare(fps_values, progress=_progress(f"Detecting {runner.labels.clip}"))
        if done:
            ms = runner.cache.detect_ms_mean
            print(f"  {runner.labels.clip}: {done} frames detected ({ms:.0f} ms/frame)")


# ------------------------------------------------------------------------------- eval fetch


def cmd_eval_fetch(args: argparse.Namespace) -> int:
    clips = load_label_dir(args.clips)
    folder = Path(args.videos)
    folder.mkdir(parents=True, exist_ok=True)
    missing = 0
    for c in clips:
        target = folder / c.video
        if target.is_file():
            print(f"[ok]   {c.video}")
            continue
        if not c.download_url:
            missing += 1
            print(f"[todo] {c.video}: download it by hand from {c.source_url or '(no source_url)'} "
                  f"and save it as {target}")
            continue
        print(f"[get]  {c.video} <- {c.download_url}")
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            request = urllib.request.Request(c.download_url, headers={"User-Agent": "countvision-eval"})
            with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:  # noqa: S310
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            tmp.replace(target)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            missing += 1
            print(f"       failed: {exc}")
    print("All videos are here." if not missing else f"{missing} video(s) still missing.")
    return 0 if not missing else 1


# ------------------------------------------------------------------------------- eval run


def cmd_eval_run(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    params = EvalParams.load(args.params) if args.params else EvalParams()
    if args.fps is not None:
        params.fps = None if args.fps <= 0 else args.fps
    runners = _runners(args, _splits(args.split))
    _prepare(runners, [params.fps])
    results, summary = evaluate(runners, params, args.tolerance, keep_events=True)
    name = args.name or f"{args.tag or 'run'}_{spec_key(args.detector)}_{args.split}"
    data = {
        "kind": "eval",
        "name": name,
        "created": _now(),
        "detector": args.detector,
        "license": spec_license(args.detector),
        "tag": args.tag,
        "note": args.note,
        "code_version": __version__,
        "split": args.split,
        "params_file": args.params,
        "params": params.to_dict(),
        "tolerance_s": args.tolerance,
        "summary": summary,
        "clips": [r.to_dict() for r in results],
    }
    out = save_json(data, args.out or Path(args.runs) / f"{name}.json")
    _print_summary(results, summary)
    print(f"\nSaved {out}")
    return 0


def _print_summary(results, summary) -> None:
    from .report import pct

    print(f"\n{'clip':32} {'scene':6} {'counted/true':>13} {'count acc':>10} {'F1':>8} {'occ err':>8}")
    for r in results:
        c, o = r.crossings, r.occupancy
        print(f"{r.clip:32} {r.scene:6} {c['counted']:>6}/{c['true']:<6} {pct(c['count_accuracy']):>10} "
              f"{pct(c['f1']):>8} {pct(o['mean_error']):>8}")
    a = summary["all"]
    print(f"{'ALL':32} {'':6} {a['crossings']['counted']:>6}/{a['crossings']['true']:<6} "
          f"{pct(a['crossings']['count_accuracy']):>10} {pct(a['crossings']['f1']):>8} "
          f"{pct(a['occupancy']['mean_error']):>8}")


# ------------------------------------------------------------------------------- eval tune


def cmd_eval_tune(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    grid = DEFAULT_GRID
    if args.grid:
        grid = yaml.safe_load(Path(args.grid).read_text(encoding="utf-8"))
    base = EvalParams()
    if args.fps is not None:
        base.fps = None if args.fps <= 0 else args.fps
    runners = _runners(args, ["tune"])
    _prepare(runners, [base.fps])
    result = grid_search(runners, grid, base, args.tolerance, progress=_progress("Trying settings"))
    result |= {"kind": "tune", "created": _now(), "detector": args.detector,
               "defaults": EvalParams().to_dict()}
    key = spec_key(args.detector)
    out = save_json(result, Path(args.runs) / f"tune_{key}.json")
    params_out = Path(args.params_out or Path(args.params_dir) / f"{key}.yaml")
    params_out.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# Tuned on: {', '.join(result['clips'])} (split: tune only)\n"
              f"# Detector: {args.detector}\n# Created: {result['created']}\n")
    params_out.write_text(header + yaml.safe_dump({"params": result["best"]}, sort_keys=False),
                          encoding="utf-8")
    from .report import pct

    print("\nTop 5 settings (tune clips):")
    for trial in result["trials"][:5]:
        c = trial["summary"]["crossings"]
        changed = {k: v for k, v in trial["params"].items() if v != getattr(EvalParams(), k)}
        print(f"  count {pct(c['count_accuracy']):>8}  F1 {pct(c['f1']):>8}  {changed or 'defaults'}")
    print(f"\nBest params: {params_out}\nAll trials: {out}")
    print("Next: run the TEST split once with these params:\n"
          f"  countvision-edge eval run --detector {args.detector} --split test --tag after "
          f"--params {params_out}")
    return 0


# ------------------------------------------------------------------------------- eval report


def cmd_eval_report(args: argparse.Namespace) -> int:
    from .cache import probe_video
    from .report import build_auto_block, load_json_dir, write_results

    clips = load_label_dir(args.clips)
    durations = {}
    for c in clips:
        video = Path(args.videos) / c.video
        if video.is_file():
            info = probe_video(video)
            durations[c.clip] = min(c.labeled_until_s or info.duration_s, info.duration_s)
    runs = load_json_dir(args.runs, "eval")
    runs.sort(key=lambda r: (r["split"], r["detector"], {"before": 0, "after": 1}.get(r.get("tag"), 2)))
    tunes = load_json_dir(args.runs, "tune")
    benches = load_json_dir(args.bench, "bench")
    block = build_auto_block(clips, runs, tunes, benches, durations)
    out = write_results(args.out, block)
    print(f"Updated {out} ({len(runs)} accuracy runs, {len(tunes)} tuning runs, {len(benches)} benchmarks)")
    return 0


# ------------------------------------------------------------------------------- bench


def cmd_bench(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    try:
        width, height = (int(v) for v in args.size.lower().split("x"))
    except ValueError as exc:
        raise CountVisionError("--size must look like 1280x720") from exc
    data = run_bench(
        args.model, video=args.video, frames=args.frames, frame_size=(width, height),
        device=args.device, models_dir=args.models_dir, threads=args.threads, label=args.label,
        progress=lambda spec: print(f"Benchmarking {spec} ...", flush=True),
    )
    print(f"\n{'model':40} {'det ms':>8} {'p95':>7} {'det FPS':>8} {'pipe FPS':>9} {'cams@10':>8}")
    for r in data["results"]:
        if r["error"]:
            print(f"{r['spec']:40} ERROR {r['error'][:70]}")
            continue
        print(f"{r['spec']:40} {r['det_mean_ms']:>8.1f} {r['det_p95_ms']:>7.1f} {r['det_fps']:>8.1f} "
              f"{r['pipe_fps']:>9.1f} {r['cameras_at_10fps']:>8}")
    out = save_json(data, args.out or Path(args.bench) / f"{spec_key(data['label'])}.json")
    print(f"\nSaved {out}")
    return 0


# ------------------------------------------------------------------------------- count-helper


def cmd_count_helper(args: argparse.Namespace) -> int:
    from .count_helper import open_session, run_gui

    video = Path(args.video)
    labels_path = Path(args.labels) if args.labels else Path(args.clips) / f"{video.stem}.yaml"
    session = open_session(
        video, labels_path, clip=args.clip, classes=args.classes.split(","), split=args.split,
        scene=args.scene, anchor=args.anchor, labeled_by=args.labeled_by,
        occupancy_every_s=args.occupancy_every,
    )
    print(f"Labels: {labels_path}\nKeys: H in the window shows help. ESC quits (always saved).")
    session = run_gui(session, video)
    print(f"Saved {labels_path}: {session.summary()}")
    return 0


# ------------------------------------------------------------------------------- parser


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Add ``eval``, ``bench`` and ``count-helper`` to the main CLI."""

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--clips", default="eval/clips", help="Folder with label files (*.yaml)")
        p.add_argument("--videos", default="eval/videos", help="Folder with the video files")
        p.add_argument("--log-level", default="WARNING")

    def detector_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--detector", required=True,
                       help="type:model[@imgsz], e.g. onnx:yolox_tiny.onnx or ultralytics:yolo11n.pt")
        p.add_argument("--device", default="auto")
        p.add_argument("--models-dir", default="eval/models", help="Where model files are looked up")
        p.add_argument("--cache", default="eval/cache", help="Detection cache folder")
        p.add_argument("--runs", default="eval/runs", help="Where result JSON files go")
        p.add_argument("--clip", action="append", help="Only this clip (repeatable)")
        p.add_argument("--fps", type=float, help="Frames per second to analyse (default 10 = live; 0 = all)")
        p.add_argument("--tolerance", type=float, default=2.0, help="Seconds for matching events")

    ev = sub.add_parser("eval", help="Accuracy evaluation on hand-labeled clips")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)

    fetch = ev_sub.add_parser("fetch", help="Download the clip videos that have a download_url")
    common(fetch)
    fetch.set_defaults(func=cmd_eval_fetch)

    run = ev_sub.add_parser("run", help="Count the clips and compare with the labels")
    common(run)
    detector_args(run)
    run.add_argument("--split", default="test", help="tune | test | dev | all (comma list allowed)")
    run.add_argument("--params", help="YAML with tuned params (from eval tune)")
    run.add_argument("--tag", choices=["before", "after"], help="before = default params, after = tuned")
    run.add_argument("--name", help="Name of this run (file name)")
    run.add_argument("--note", help="Free text stored with the run (e.g. what changed)")
    run.add_argument("--out", help="Output JSON (default: eval/runs/<name>.json)")
    run.set_defaults(func=cmd_eval_run)

    tune = ev_sub.add_parser("tune", help="Grid search on the TUNE clips only")
    common(tune)
    detector_args(tune)
    tune.add_argument("--grid", help="YAML {param: [values]} (default: 432 combinations incl. the defaults)")
    tune.add_argument("--params-dir", default="eval/params")
    tune.add_argument("--params-out", help="Where to write the best params (YAML)")
    tune.set_defaults(func=cmd_eval_tune)

    rep = ev_sub.add_parser("report", help="Write the tables into eval/results.md")
    common(rep)
    rep.add_argument("--runs", default="eval/runs")
    rep.add_argument("--bench", default="eval/bench")
    rep.add_argument("--out", default="eval/results.md")
    rep.set_defaults(func=cmd_eval_report)

    bench = sub.add_parser("bench", help="Speed benchmark: detector x runtime x input size")
    bench.add_argument("--model", action="append", required=True,
                       help="type:model[@imgsz] (repeatable), e.g. openvino:yolox_tiny.onnx")
    bench.add_argument("--video", help="Real video for realistic frames (default: synthetic)")
    bench.add_argument("--frames", type=int, default=60)
    bench.add_argument("--size", default="1280x720", help="Frame size fed to the detector")
    bench.add_argument("--device", default="auto")
    bench.add_argument("--threads", type=int, default=0, help="CPU threads (0 = runtime default)")
    bench.add_argument("--models-dir", default="eval/models")
    bench.add_argument("--label", default="", help="Name of this machine, e.g. laptop or colab-t4")
    bench.add_argument("--bench", default="eval/bench")
    bench.add_argument("--out", help="Output JSON (default: eval/bench/<label>.json)")
    bench.add_argument("--log-level", default="WARNING")
    bench.set_defaults(func=cmd_bench)

    helper = sub.add_parser("count-helper", help="Label true counts of a video by hand (window)")
    helper.add_argument("video", help="Video file")
    helper.add_argument("--labels", help="Label file (default: eval/clips/<video name>.yaml)")
    helper.add_argument("--clips", default="eval/clips")
    helper.add_argument("--clip", help="Clip id for a new label file")
    helper.add_argument("--classes", default="person", help="Comma list, e.g. person,car,bicycle")
    helper.add_argument("--split", default="dev", choices=["tune", "test", "dev"])
    helper.add_argument("--scene", default="clear", choices=["clear", "hard"])
    helper.add_argument("--anchor", default="bottom_center", choices=["bottom_center", "center"])
    helper.add_argument("--labeled-by", help="Your name (stored in the label file)")
    helper.add_argument("--occupancy-every", type=float, default=10.0,
                        help="G jumps this many seconds (occupancy samples)")
    helper.set_defaults(func=cmd_count_helper)

