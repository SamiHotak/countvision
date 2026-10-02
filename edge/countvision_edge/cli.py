"""Command line: run, demo, report, snapshot, probe, export."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import cv2

from . import __version__
from .config import SourceConfig, load_config
from .detectors import build_detector
from .errors import CountVisionError, EndOfStream
from .inputs import create_source
from .inputs.live_source import open_cv_capture
from .logging_setup import setup_logging
from .pipeline import CameraPipeline, FrameResult
from .report import build_report, export_csv, format_report, open_existing
from .storage import SqliteBuffer
from .viz import Annotator, VideoRecorder

log = logging.getLogger("countvision_edge.cli")


def _parse_time(value: str | None) -> float | None:
    """ISO time like 2026-01-31T09:00:00 (UTC) -> epoch seconds."""
    if value is None:
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CountVisionError(f"Bad time '{value}'. Use the form 2026-01-31T09:00:00") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # noqa: UP017
    return parsed.timestamp()


# ------------------------------------------------------------------------------ run


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(args.log_level or cfg.log_level)
    cam = cfg.camera(args.camera)
    if args.source or args.start_time:
        data = cam.source.model_dump()
        if args.source:
            data.update(uri=args.source, kind="auto")
        if args.start_time:
            data["start_time"] = args.start_time
        cam.source = SourceConfig.model_validate(data)

    detector = build_detector(cfg.detector)
    buffer = SqliteBuffer(
        cfg.db_file(),
        retention_days=cfg.storage.retention_days,
        heartbeat_retention_hours=cfg.storage.heartbeat_retention_hours,
    )
    device_id = cfg.resolve_device_id()
    log.info("Device %s, camera %s, database %s", device_id, cam.id, cfg.db_file())
    pipeline = CameraPipeline(
        cam, detector, buffer, data_dir=cfg.data_dir, heartbeat_interval_s=cfg.heartbeat_interval_s
    )
    source = create_source(cam.source)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    annotator = Annotator(lambda: pipeline.engine) if (args.show or args.annotated) else None
    out_fps = cam.effective_target_fps() or source.nominal_fps() or 15.0
    recorder = VideoRecorder(args.annotated, out_fps) if args.annotated else None
    show = {"on": bool(args.show)}
    fps_state = {"last": None, "fps": None}

    def on_result(result: FrameResult) -> None:
        if annotator is None:
            return
        now = time.perf_counter()
        if fps_state["last"] is not None:
            inst = 1.0 / max(now - fps_state["last"], 1e-6)
            fps_state["fps"] = inst if fps_state["fps"] is None else 0.9 * fps_state["fps"] + 0.1 * inst
        fps_state["last"] = now
        image = annotator.draw(result, fps_state["fps"])
        if recorder is not None:
            recorder.write(image)
        if show["on"]:
            try:
                cv2.imshow("CountVision (press q to stop)", image)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    stop.set()
            except cv2.error:
                print("Cannot open a preview window here (no display / headless OpenCV).")
                show["on"] = False

    print(f"CountVision edge {__version__}: running camera '{cam.id}'. Press Ctrl+C to stop.")
    try:
        summary = pipeline.run(source, stop, max_frames=args.max_frames, on_result=on_result)
    finally:
        if recorder is not None:
            recorder.close()
        if args.show:
            cv2.destroyAllWindows()

    print(
        f"\nDone: {summary.frames_processed} frames processed "
        f"({summary.processed_fps:.1f} FPS), {summary.frames_skipped} skipped, "
        f"{summary.frames_dropped} dropped, {summary.events} events."
    )
    report = build_report(buffer, camera_id=cam.id)
    print(format_report(report))
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {args.report}")
    if args.annotated:
        print(f"Annotated video: {args.annotated}")
    print(f"Database: {cfg.db_file()}  (countvision-edge report --db {cfg.db_file()})")
    buffer.close()
    return 0


# ------------------------------------------------------------------------------ demo


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    setup_logging(args.log_level)
    result = run_demo(args.out, annotated=not args.no_video)
    print("CountVision self-test on a synthetic video\n")
    for check in result.checks:
        mark = "OK " if check.ok else "FAIL"
        print(f"  [{mark}] {check.name}: expected {check.expected}, got {check.actual}")
    if result.summary:
        s = result.summary
        print(f"\n  {s.frames_processed} frames, {s.processed_fps:.0f} FPS on this computer")
    print(f"  Video: {result.video_path}")
    if not args.no_video:
        print(f"  Annotated video: {Path(args.out) / 'demo_annotated.mp4'}")
    print(f"  Numbers: {result.db_path}\n")
    print("PASS" if result.passed else "FAIL")
    return 0 if result.passed else 1


# ------------------------------------------------------------------------------ report


def cmd_report(args: argparse.Namespace) -> int:
    buffer = open_existing(args.db)
    start, end = _parse_time(args.since), _parse_time(args.until)
    report = build_report(buffer, args.camera, start, end)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
    if args.csv_dir:
        for path in export_csv(buffer, args.csv_dir, args.camera, start, end):
            print(f"Wrote {path}")
    buffer.close()
    return 0


# ------------------------------------------------------------------------------ snapshot, probe


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Save ONE frame to a local file, to draw lines and zones on. It never leaves this PC."""
    setup_logging(args.log_level)
    source = create_source(SourceConfig(uri=args.source))
    source.open()
    frame = None
    try:
        deadline = time.time() + args.timeout
        skip = 5 if source.is_live else 0  # webcams need a few frames to set exposure
        while time.time() < deadline:
            try:
                got = source.read(timeout=1.0)
            except EndOfStream:
                break
            if got is None:
                continue
            frame = got
            if skip <= 0:
                break
            skip -= 1
    finally:
        source.close()
    if frame is None:
        raise CountVisionError("No frame received. Check the source and try again.")
    if not cv2.imwrite(args.out, frame.image):
        raise CountVisionError(f"Could not write {args.out}")
    width, height = frame.size
    print(f"Saved {args.out} ({width}x{height}). The image stays on this computer.")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Find which webcam indexes work."""
    found = 0
    for index in range(args.max_index + 1):
        cap = open_cv_capture(SourceConfig(uri=str(index), kind="webcam"))
        try:
            if cap.isOpened():
                ok, image = cap.read()
                if ok and image is not None:
                    print(f"Webcam {index}: works ({image.shape[1]}x{image.shape[0]})")
                    found += 1
                    continue
            print(f"Webcam {index}: not available")
        finally:
            cap.release()
    if not found:
        print("No webcam found. Close other apps that use the camera (Teams, Zoom) and try again.")
    return 0 if found else 1


# ------------------------------------------------------------------------------ export


def cmd_export(args: argparse.Namespace) -> int:
    """Export a YOLO .pt model to ONNX or OpenVINO for faster CPU inference."""
    setup_logging(args.log_level)
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise CountVisionError("Needs ultralytics: pip install ultralytics") from exc
    print("Licence note: ultralytics and the official YOLO weights are AGPL-3.0. "
          "The exported model has the same licence.")
    path = YOLO(args.model).export(format=args.format, imgsz=args.imgsz, half=False)
    print(f"Exported: {path}")
    print("Use it with detector.type: onnx or openvino and set detector.model_license.")
    return 0


# ------------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="countvision-edge",
        description="CountVision edge agent: counts people and vehicles from camera streams. "
        "No video leaves this computer.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Count from a camera or video file")
    run.add_argument("--config", required=True, help="YAML config file")
    run.add_argument("--camera", help="Camera id (needed if the config has several)")
    run.add_argument("--source", help="Override the source: 0, a file, rtsp://...")
    run.add_argument("--start-time", help="Files: time of the first frame, e.g. 2026-01-31T09:00:00")
    run.add_argument("--max-frames", type=int, help="Stop after this many processed frames")
    run.add_argument("--show", action="store_true", help="Show a live preview window")
    run.add_argument("--annotated", metavar="FILE", help="Write an annotated video (.mp4)")
    run.add_argument("--report", metavar="FILE", help="Write a JSON report at the end")
    run.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    run.set_defaults(func=cmd_run)

    demo = sub.add_parser("demo", help="Self-test on a synthetic video (no camera, no model)")
    demo.add_argument("--out", default="demo_output", help="Output folder")
    demo.add_argument("--no-video", action="store_true", help="Do not write the annotated video")
    demo.add_argument("--log-level", default="WARNING")
    demo.set_defaults(func=cmd_demo)

    report = sub.add_parser("report", help="Summary and CSV export from a database")
    report.add_argument("--db", required=True, help="Database file")
    report.add_argument("--camera")
    report.add_argument("--since", help="From this UTC time, e.g. 2026-01-31T00:00:00")
    report.add_argument("--until", help="Until this UTC time")
    report.add_argument("--json", action="store_true", help="Print JSON")
    report.add_argument("--csv-dir", help="Write CSV files into this folder")
    report.set_defaults(func=cmd_report)

    snap = sub.add_parser("snapshot", help="Save one frame (for drawing lines and zones)")
    snap.add_argument("--source", required=True)
    snap.add_argument("--out", default="snapshot.jpg")
    snap.add_argument("--timeout", type=float, default=15.0)
    snap.add_argument("--log-level", default="WARNING")
    snap.set_defaults(func=cmd_snapshot)

    probe = sub.add_parser("probe", help="Find working webcam indexes")
    probe.add_argument("--max-index", type=int, default=4)
    probe.set_defaults(func=cmd_probe)

    export = sub.add_parser("export", help="Export a YOLO model to ONNX / OpenVINO")
    export.add_argument("--model", default="yolo11n.pt")
    export.add_argument("--format", choices=["onnx", "openvino"], default="openvino")
    export.add_argument("--imgsz", type=int, default=640)
    export.add_argument("--log-level", default="WARNING")
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CountVisionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
