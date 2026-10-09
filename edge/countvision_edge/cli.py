"""Command line: run, demo, report, snapshot, probe, export, app, eval, bench, count-helper."""

from __future__ import annotations

import argparse
import json
import logging
import os
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
from .inputs.live_source import open_cv_capture, quiet_opencv
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


def _override_source(cam, args: argparse.Namespace):
    if not (args.source or args.start_time):
        return cam
    data = cam.source.model_dump()
    if args.source:
        data.update(uri=args.source, kind="auto")
    if args.start_time:
        data["start_time"] = args.start_time
    return cam.model_copy(update={"source": SourceConfig.model_validate(data)})


def cmd_run(args: argparse.Namespace) -> int:
    """Count. Without --show/--annotated all cameras of the config run together (supervisor)."""
    cfg = load_config(args.config)
    setup_logging(args.log_level or cfg.log_level)
    if not (args.show or args.annotated):
        return _run_supervised(cfg, args)
    cam = _override_source(cfg.camera(args.camera), args)

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
    report = build_report(buffer, camera_id=cam.id, line_names=[line.name for line in cam.lines])
    print(format_report(report))
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {args.report}")
    if args.annotated:
        print(f"Annotated video: {args.annotated}")
    print(f"Database: {cfg.db_file()}  (countvision-edge report --db {cfg.db_file()})")
    buffer.close()
    return 0


def _run_supervised(cfg, args: argparse.Namespace) -> int:
    """All cameras (or --camera) with restarts, status.json and offline alerts."""
    from .supervisor import Supervisor

    cameras = [cfg.camera(args.camera)] if args.camera else list(cfg.cameras)
    if args.source or args.start_time:
        if len(cameras) != 1:
            raise CountVisionError("--source / --start-time need one camera: add --camera <id>.")
        cameras = [_override_source(cameras[0], args)]
    detector = build_detector(cfg.detector)
    buffer = SqliteBuffer(
        cfg.db_file(),
        retention_days=cfg.storage.retention_days,
        heartbeat_retention_hours=cfg.storage.heartbeat_retention_hours,
    )
    supervisor = Supervisor(cfg, detector, buffer, cameras, max_frames=args.max_frames)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    ids = ", ".join(c.id for c in cameras)
    print(f"CountVision edge {__version__}: running {len(cameras)} camera(s): {ids}. "
          "Press Ctrl+C to stop.")
    print(f"Status: {supervisor.status_path}  (countvision-edge health --data-dir {cfg.data_dir})")
    try:
        supervisor.run_forever(stop)
    finally:
        detector.close()

    failed = []
    for worker in supervisor.workers:
        frames = sum(s.frames_processed for s in worker.summaries)
        wall = sum(s.wall_s for s in worker.summaries)
        fps = frames / wall if wall > 0 else 0.0
        h = worker.health
        print(f"\nCamera '{h.camera_id}': {h.state}, {frames} frames processed ({fps:.1f} FPS), "
              f"{h.events} events, {h.restarts} restarts")
        if h.state == "failed":
            failed.append(f"camera {h.camera_id}: {h.last_error}")
            continue
        report = build_report(buffer, camera_id=worker.cam.id,
                              line_names=[line.name for line in worker.cam.lines])
        print(format_report(report))
        if args.report and len(cameras) == 1:
            Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\nReport written to {args.report}")
    if args.report and len(cameras) > 1:
        report = build_report(buffer)
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {args.report}")
    print(f"Database: {cfg.db_file()}  (countvision-edge report --db {cfg.db_file()})")
    buffer.close()
    for message in failed:
        print(f"Error: {message}", file=sys.stderr)
    return 2 if failed else 0


def cmd_health(args: argparse.Namespace) -> int:
    """Is the counting service running? Reads <data_dir>/status.json."""
    from .supervisor import STATUS_FILE, check_health

    if args.config:
        data_dir = Path(load_config(args.config).data_dir)
    else:
        data_dir = Path(args.data_dir or os.environ.get("CV_DATA_DIR") or "data")
    report = check_health(data_dir / STATUS_FILE, max_age_s=args.max_age)
    print("\n".join(report.lines))
    return report.exit_code


# ----------------------------------------------------------------------------- cloud


def _data_dir(args: argparse.Namespace) -> tuple[Path, str]:
    """(data dir, edge device id) from --config, --data-dir, CV_DATA_DIR or ./data."""
    from .config import device_id_for

    if getattr(args, "config", None):
        cfg = load_config(args.config)
        return Path(cfg.data_dir), cfg.resolve_device_id()
    data_dir = Path(args.data_dir or os.environ.get("CV_DATA_DIR") or "data")
    return data_dir, os.environ.get("CV_DEVICE_ID") or device_id_for(data_dir)


def cmd_pair(args: argparse.Namespace) -> int:
    """Connect this device to an organization in the cloud with a one-time code."""
    from .cloud import Credentials, pair

    data_dir, edge_id = _data_dir(args)
    old = Credentials.load(data_dir)
    if old is not None and not args.force:
        print(f"This device is already connected to '{old.organization}' (site '{old.site}', "
              f"{old.url}).\nTo connect it again, add --force.", file=sys.stderr)
        return 1
    creds = pair(args.url, args.code, data_dir, edge_id)
    print(f"Connected. Organization: {creds.organization}, site: {creds.site}, device: {creds.device_name}")
    print(f"Saved in {Credentials.path(data_dir)} (keep this file private: it contains the device token).")
    print("Restart the counting service so it starts uploading (Docker: docker compose restart countvision).")
    return 0


def cmd_unpair(args: argparse.Namespace) -> int:
    from .cloud import Credentials

    data_dir, _ = _data_dir(args)
    path = Credentials.path(data_dir)
    if not path.exists():
        print("This device is not connected to a cloud.")
        return 0
    path.unlink()
    print(f"Removed {path}. The device counts locally only. (Also remove it in the web app: Devices.)")
    return 0


def cmd_cloud_status(args: argparse.Namespace) -> int:
    """Check the connection: who am I in the cloud, how many rows wait for upload."""
    from .cloud import CloudError, Credentials, normalize_url, request_json

    data_dir, _ = _data_dir(args)
    creds = Credentials.load(data_dir)
    if creds is None:
        print("Not connected to a cloud. Connect with: countvision-edge pair --url <address> --code <code>")
        return 1
    url = normalize_url(args.url or os.environ.get("CV_CLOUD_URL") or creds.url)
    print(f"Cloud: {url}\nDevice: {creds.device_name} (site {creds.site}, organization {creds.organization})")
    db = Path(args.db) if args.db else data_dir / "countvision.db"
    if db.exists():
        buffer = SqliteBuffer(db)
        print(f"Rows waiting for upload: {buffer.count_unsent()}")
        buffer.close()
    try:
        me = request_json("GET", f"{url}/api/device/me", token=creds.token, timeout=15)
    except CloudError as exc:
        print(f"Connection check FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"Connection OK: the cloud knows this device as '{me['name']}' at site '{me['site']}'.")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    """Send everything that is waiting in the buffer now (normally "run" does this by itself)."""
    from .cloud import CloudError, CloudUploader, Credentials

    cfg = load_config(args.config)
    creds = Credentials.load(cfg.data_dir)
    if creds is None:
        raise CountVisionError("Not connected to a cloud. First: countvision-edge pair --url ... --code ...")
    buffer = SqliteBuffer(cfg.db_file())
    before = buffer.count_unsent()
    up = CloudUploader(buffer, creds, url=cfg.cloud.url, batch_rows=cfg.cloud.batch_rows,
                       timeout_s=cfg.cloud.timeout_s)
    try:
        while up.upload_once():
            pass
    except CloudError as exc:
        print(f"Upload failed: {exc}. {buffer.count_unsent()} rows are still waiting.", file=sys.stderr)
        buffer.close()
        return 2
    left = buffer.count_unsent()
    buffer.close()
    print(f"Uploaded {before - left} rows to {up.url}. Waiting: {left}.")
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
    quiet_opencv()
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


# ------------------------------------------------------------------------------ app


def cmd_app(args: argparse.Namespace) -> int:
    """Local web app: live preview, draw lines and zones, live counters, chart, CSV."""
    from .app.main import AppOptions, run_app

    return run_app(
        AppOptions(
            config=args.config,
            camera=args.camera,
            source=args.source,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            preview=args.preview,
            demo=args.demo,
            log_level=args.log_level,
        )
    )


# ------------------------------------------------------------------------------ models


def cmd_models(args: argparse.Namespace) -> int:
    """List or download the known (permissively licensed) detector models."""
    from .models import MODELS, download

    if args.action == "list":
        for m in MODELS.values():
            print(f"{m.name:12} {m.size_mb:5.1f} MB  {m.license:28} {m.note}")
        return 0
    names = args.names or ["yolox_tiny"]
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        raise CountVisionError(f"Unknown model(s): {', '.join(unknown)}. See: countvision-edge models list")
    for name in names:
        info = MODELS[name]
        path = download(info, Path(args.dir) / info.file)
        print(f"{info.file}: OK ({path}, {info.license})")
    return 0


# ------------------------------------------------------------------------------ pilot docs


def cmd_pilot_docs(args: argparse.Namespace) -> int:
    """Fill the pilot templates (sign, privacy notice, agreement, AVV, DPIA, checklist)."""
    from .pilot.docs import DOCUMENTS, PilotInfo, load_info, write_pack

    if not args.info and not args.blank:
        raise CountVisionError("Give --info pilot.yaml (see docs/pilot/pilot.example.yaml) or --blank.")
    info = load_info(args.info) if args.info else PilotInfo()
    files = write_pack(info, args.out)
    print(f"Wrote {len(files)} files to {args.out}:")
    for doc in DOCUMENTS:
        print(f"  {doc.file:26} {doc.title} - {doc.audience}")
    print("Open index.html in the browser and print each document (Ctrl+P, 'Save as PDF').")
    print("TEMPLATES, not legal advice: have them checked before real use.")
    return 0


# ------------------------------------------------------------------------------ export


def cmd_export(args: argparse.Namespace) -> int:
    """Export a YOLO .pt model to ONNX or OpenVINO for faster CPU inference."""
    setup_logging(args.log_level)
    from . import PRIVACY_ENV_SET_BY_US

    if "YOLO_OFFLINE" in PRIVACY_ENV_SET_BY_US:
        # Export may need to pip-install a helper (onnxslim); Ultralytics only does that online.
        os.environ["YOLO_OFFLINE"] = "0"
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

    run = sub.add_parser("run", help="Count: all cameras of the config (or one with --camera)")
    run.add_argument("--config", required=True, help="YAML config file")
    run.add_argument("--camera", help="Only this camera (default: all cameras of the config)")
    run.add_argument("--source", help="Override the source: 0, a file, rtsp://...")
    run.add_argument("--start-time", help="Files: time of the first frame, e.g. 2026-01-31T09:00:00")
    run.add_argument("--max-frames", type=int, help="Stop after this many processed frames (per camera)")
    run.add_argument("--show", action="store_true", help="Show a live preview window (one camera)")
    run.add_argument("--annotated", metavar="FILE", help="Write an annotated video (.mp4, one camera)")
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

    app = sub.add_parser("app", help="Open the local web app (preview, draw lines, live counts)")
    app.add_argument("--config", help="YAML config file (lines you draw are saved into it)")
    app.add_argument("--demo", action="store_true", help="Demo with a synthetic video (no camera)")
    app.add_argument("--camera", help="Camera id (needed if the config has several)")
    app.add_argument("--source", help="Override the source: 0, a video file, rtsp://...")
    app.add_argument("--host", default="127.0.0.1",
                     help="127.0.0.1 = only this PC (default). 0.0.0.0 = also phones in your Wi-Fi")
    app.add_argument("--port", type=int, default=8000)
    app.add_argument("--preview", choices=["full", "blur", "off"], default=None,
                     help="Preview picture: blur (people pixelated, default from privacy.preview), "
                          "off, or full (setup only)")
    app.add_argument("--no-browser", action="store_true", help="Do not open the browser")
    app.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    app.set_defaults(func=cmd_app)

    pair_p = sub.add_parser("pair", help="Connect this device to the CountVision cloud (one-time code)")
    pair_p.add_argument("--url", required=True, help="Cloud address, e.g. https://app.countvision.de")
    pair_p.add_argument("--code", required=True, help="Pairing code from the web app (Devices -> Add device)")
    pair_p.add_argument("--config", help="Config file (for data_dir and device id)")
    pair_p.add_argument("--data-dir", help="Data folder (default: CV_DATA_DIR or ./data)")
    pair_p.add_argument("--force", action="store_true", help="Replace an existing connection")
    pair_p.set_defaults(func=cmd_pair)

    unpair = sub.add_parser("unpair", help="Forget the cloud connection (count locally only)")
    unpair.add_argument("--config")
    unpair.add_argument("--data-dir")
    unpair.set_defaults(func=cmd_unpair)

    cstat = sub.add_parser("cloud-status", help="Check the cloud connection and the upload backlog")
    cstat.add_argument("--config")
    cstat.add_argument("--data-dir")
    cstat.add_argument("--db", help="Database (default: <data_dir>/countvision.db)")
    cstat.add_argument("--url", help="Override the cloud address (default: the one saved at pairing)")
    cstat.set_defaults(func=cmd_cloud_status)

    upload = sub.add_parser("upload", help="Send the waiting numbers to the cloud now")
    upload.add_argument("--config", required=True)
    upload.set_defaults(func=cmd_upload)

    health = sub.add_parser("health", help="Is the counting service running? (exit 0 = healthy)")
    health.add_argument("--config", help="Config file (to find data_dir)")
    health.add_argument("--data-dir", help="Folder with status.json (default: $CV_DATA_DIR or data)")
    health.add_argument("--max-age", type=float, default=60.0, help="Seconds before the status is stale")
    health.set_defaults(func=cmd_health)

    models = sub.add_parser("models", help="List or download detector models (Apache-2.0 YOLOX)")
    models.add_argument("action", choices=["list", "download"])
    models.add_argument("names", nargs="*", help="Model names (default: yolox_tiny)")
    models.add_argument("--dir", default="models", help="Folder for the model files")
    models.add_argument("--log-level", default="WARNING")
    models.set_defaults(func=cmd_models)

    pilot = sub.add_parser("pilot-docs", help="Make the pilot papers: sign, privacy notice, agreement, AVV")
    pilot.add_argument("--info", help="YAML with the business details (docs/pilot/pilot.example.yaml)")
    pilot.add_argument("--blank", action="store_true", help="Empty lines to fill in by hand")
    pilot.add_argument("--out", default="pilot_docs", help="Output folder")
    pilot.set_defaults(func=cmd_pilot_docs)

    export = sub.add_parser("export", help="Export a YOLO model to ONNX / OpenVINO")
    export.add_argument("--model", default="yolo11n.pt")
    export.add_argument("--format", choices=["onnx", "openvino"], default="openvino")
    export.add_argument("--imgsz", type=int, default=640)
    export.add_argument("--log-level", default="WARNING")
    export.set_defaults(func=cmd_export)

    from .evaluation.commands import add_parsers

    add_parsers(sub)
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
