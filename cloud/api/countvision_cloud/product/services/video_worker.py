"""The video worker: probe an uploaded video, then count it with the edge counting engine.

Runs in the "video-worker" container (Celery queue "video", one job at a time) or in a thread of
the API process (CV_VIDEO_RUNNER=thread, development). Needs the ``countvision_edge`` package
(installed in the cloud image) and, for H.264 videos that play in the browser, ``ffmpeg``.

Detector: CV_VIDEO_DETECTOR (onnx = YOLOX-Tiny, Apache-2.0, downloaded once and checked), the same
default as the edge agent. On a GPU server set CV_VIDEO_DEVICE=cuda with onnxruntime-gpu.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from ...db import db_session, utcnow
from ...settings import get_settings
from .. import live
from ..models import VideoJob
from . import video_jobs

log = logging.getLogger(__name__)

THUMB_MAX_W = 1280
OUT_MAX_W = 1280
PROGRESS_EVERY_S = 2.0


class Cancelled(Exception):
    pass


def _publish(job: VideoJob) -> None:
    live.publish(
        job.org_id,
        {
            "type": "video_job",
            "job_id": str(job.id),
            "status": job.status,
            "progress": round(job.progress, 4),
        },
    )


def _fail(job_id: uuid.UUID, message: str) -> None:
    with db_session() as db:
        job = db.get(VideoJob, job_id)
        if job is None:
            return
        job.status, job.error, job.finished_at, job.updated_at = "failed", message[:500], utcnow(), utcnow()
        _publish(job)


# --------------------------------------------------------------------------- probe


def probe(job_id: uuid.UUID | str) -> None:
    """Read the video's size, frame rate and length, save a first frame to draw on."""
    import cv2

    job_id = uuid.UUID(str(job_id))
    with db_session() as db:
        job = db.get(VideoJob, job_id)
        if job is None or job.status != "probing":
            return
        path = video_jobs.input_path(job)
        org_id = job.org_id
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return _fail(job_id, "This file cannot be read as a video (unknown format or broken file).")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if fps <= 0 or fps > 240:
            fps = 25.0
        # a frame 1 s in (the very first frame is often black)
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(int(fps), max(0, frames - 1)))
        ok, image = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, image = cap.read()
        if not ok or image is None:
            return _fail(job_id, "No picture could be read from this video.")
    finally:
        cap.release()
    duration = frames / fps if frames > 0 else None
    limit = get_settings().video_max_minutes
    if duration is not None and duration > limit * 60:
        with db_session() as db:
            job = db.get(VideoJob, job_id)
            video_jobs.delete_input(job)
        return _fail(job_id, f"The video is {duration / 60:.0f} minutes long; the limit is {limit} minutes.")
    h, w = image.shape[:2]
    if w > THUMB_MAX_W:
        image = cv2.resize(image, (THUMB_MAX_W, int(h * THUMB_MAX_W / w)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(video_jobs.job_dir(job_id) / "thumbnail.jpg"), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    with db_session() as db:
        job = db.get(VideoJob, job_id)
        job.fps, job.frame_count, job.width, job.height = round(fps, 3), frames or None, width, height
        job.duration_s = round(duration, 2) if duration else None
        job.status, job.updated_at = "ready", utcnow()
        _publish(job)
    log.info("Video job %s (org %s): %dx%d, %.1f FPS, %s s", job_id, org_id, width, height, fps, duration)


# --------------------------------------------------------------------------- annotated video


class _Writer:
    """Annotated video as H.264 MP4 through ffmpeg (plays in every browser). Without ffmpeg: MP4V
    with OpenCV (download only, browsers may not play it)."""

    def __init__(self, path: Path, fps: float) -> None:
        self.path, self.fps = path, fps
        self._proc: subprocess.Popen | None = None
        self._cv = None
        self.size: tuple[int, int] | None = None
        self.codec = "none"

    def write(self, image) -> None:
        import cv2

        h, w = image.shape[:2]
        if w > OUT_MAX_W:
            image = cv2.resize(
                image, (OUT_MAX_W, int(h * OUT_MAX_W / w) // 2 * 2), interpolation=cv2.INTER_AREA
            )
        h, w = image.shape[:2]
        if w % 2 or h % 2:
            image = image[: h - h % 2, : w - w % 2]
        if self.size is None:
            self.size = (image.shape[1], image.shape[0])
            self._open()
        if image.shape[1] != self.size[0] or image.shape[0] != self.size[1]:
            image = cv2.resize(image, self.size)
        if self._proc is not None:
            self._proc.stdin.write(image.tobytes())
        elif self._cv is not None:
            self._cv.write(image)

    def _open(self) -> None:
        import cv2

        ffmpeg = shutil.which("ffmpeg")
        w, h = self.size
        if ffmpeg:
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-s",
                f"{w}x{h}",
                "-r",
                f"{self.fps:.3f}",
                "-i",
                "-",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "26",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(self.path),
            ]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            self.codec = "h264"
        else:
            self._cv = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
            self.codec = "mp4v"

    def close(self) -> None:
        if self._proc is not None:
            self._proc.stdin.close()
            err = self._proc.stderr.read().decode(errors="replace")
            if self._proc.wait() != 0:
                log.warning("ffmpeg failed: %s", err[-500:])
                self.path.unlink(missing_ok=True)
                self.codec = "none"
        if self._cv is not None:
            self._cv.release()

    def abort(self) -> None:
        if self._proc is not None:
            self._proc.kill()
        if self._cv is not None:
            self._cv.release()
        self.path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- run


def edge_config(job: VideoJob, work: Path) -> dict[str, Any]:
    settings = get_settings()
    cfg = job.config or {}
    if settings.video_detector == "blobs":
        detector: dict[str, Any] = {"type": "blobs"}
    else:
        detector = {
            "type": settings.video_detector,
            "model": settings.video_model,
            "model_license": "Apache-2.0 (Megvii YOLOX)",
            "conf": 0.2,
            "device": settings.video_device,
        }
    camera: dict[str, Any] = {
        "id": "video",
        "source": {"uri": str(video_jobs.input_path(job)), "start_time": video_jobs.BASE_TIME.isoformat()},
        "classes": cfg.get("classes") or ["person"],
        "coordinates": "normalized",
        "anchor": cfg.get("anchor", "bottom_center"),
        "lines": [
            {
                "name": ln["name"],
                "p1": ln["p1"],
                "p2": ln["p2"],
                "in_direction": ln.get("in_direction", "to_right"),
            }
            for ln in cfg.get("lines", [])
        ],
        "zones": [
            {"name": z["name"], "polygon": z["polygon"], "kind": z.get("kind", "area")}
            for z in cfg.get("zones", [])
        ],
    }
    if cfg.get("sample_fps"):
        camera["scheduler"] = {"target_fps": float(cfg["sample_fps"]), "adaptive": False}
    return {
        "data_dir": str(work),
        "detector": detector,
        "heartbeat_interval_s": 3600,
        "privacy": {"preview": "off", "snapshots": False},
        "cameras": [camera],
    }


def run(job_id: uuid.UUID | str) -> None:
    """Count one video. Safe to call twice (a second worker does nothing)."""
    from countvision_edge.config import parse_config
    from countvision_edge.detectors import build_detector
    from countvision_edge.inputs import create_source
    from countvision_edge.pipeline import CameraPipeline
    from countvision_edge.privacy import pixelate_boxes
    from countvision_edge.storage import SqliteBuffer
    from countvision_edge.viz import Annotator

    job_id = uuid.UUID(str(job_id))
    with db_session() as db:
        job = db.get(VideoJob, job_id)
        if job is None or job.status not in ("queued", "running"):
            return
        job.status, job.started_at, job.updated_at, job.progress = "running", utcnow(), utcnow(), 0.0
        _publish(job)
        db.flush()
        db.expunge(job)
    folder = video_jobs.job_dir(job)
    work = folder / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    out_video = folder / "annotated.mp4"
    out_video.unlink(missing_ok=True)
    blur = (job.config or {}).get("blur", True)
    want_video = (job.config or {}).get("annotated", True)
    started = time.perf_counter()
    writer: _Writer | None = None
    try:
        cfg = parse_config(edge_config(job, work), env={})
        cam = cfg.camera()
        buffer = SqliteBuffer(work / "counts.db")
        detector = build_detector(cfg.detector)
        pipeline = CameraPipeline(cam, detector, buffer, data_dir=work, heartbeat_interval_s=3600)
        source = create_source(cam.source)
        out_fps = cam.effective_target_fps() or job.fps or 25.0
        writer = _Writer(out_video, out_fps) if want_video else None
        annotator = Annotator(lambda: pipeline.engine) if want_video else None
        stop = threading.Event()
        state = {"next": time.monotonic() + PROGRESS_EVERY_S, "error": None}
        total = job.frame_count or 0

        def on_result(result) -> None:
            if annotator is not None:
                if blur:
                    pixelate_boxes(
                        result.frame.image, [t.xyxy for t in result.tracks if t.class_name == "person"]
                    )
                    pixelate_boxes(result.frame.image, [tuple(b) for b in result.boxes])
                writer.write(annotator.draw(result))
            now = time.monotonic()
            if now >= state["next"]:
                state["next"] = now + PROGRESS_EVERY_S
                with db_session() as db2:
                    j = db2.get(VideoJob, job_id)
                    if j.cancel_requested:
                        stop.set()
                        return
                    j.progress = min(0.99, pipeline.frames_read / total) if total else 0.0
                    j.updated_at = utcnow()
                    _publish(j)

        summary = pipeline.run(source, stop, on_result=on_result)
        if stop.is_set():
            raise Cancelled()
        if writer is not None:
            writer.close()
        result = summarize(
            buffer, job, summary, time.perf_counter() - started, writer.codec if writer else "none"
        )
        buffer.close()
        (folder / "counts.csv").write_text(counts_csv(buffer_path=work / "counts.db"), encoding="utf-8")
        _write_pdf(job, result)
    except Cancelled:
        if writer is not None:
            writer.abort()
        shutil.rmtree(work, ignore_errors=True)
        with db_session() as db:
            j = db.get(VideoJob, job_id)
            j.status, j.error, j.finished_at, j.updated_at = (
                "cancelled",
                "Stopped by a user.",
                utcnow(),
                utcnow(),
            )
            _publish(j)
        return
    except Exception as exc:  # report every problem on the page, keep the input for a retry
        log.exception("Video job %s failed", job_id)
        if writer is not None:
            writer.abort()
        _fail(job_id, f"The analysis failed: {type(exc).__name__}: {exc}")
        return
    shutil.rmtree(work, ignore_errors=True)
    with db_session() as db:
        j = db.get(VideoJob, job_id)
        video_jobs.delete_input(j)  # privacy: the uploaded video is not needed any more
        j.result, j.status, j.progress = result, "done", 1.0
        j.finished_at = j.updated_at = utcnow()
        _publish(j)
    log.info("Video job %s done: %s", job_id, json.dumps(result.get("totals")))


def _sec(ts: float) -> float:
    return ts - video_jobs.BASE_TIME.timestamp()


def summarize(buffer, job: VideoJob, summary, wall_s: float, codec: str) -> dict[str, Any]:
    """Numbers of the whole video: lines (per class), zones, visits, a per-minute timeline."""
    lines: dict[str, dict[str, Any]] = {}
    timeline: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for row in buffer.query("line_counts", camera_id="video"):
        ln = lines.setdefault(row["line"], {"name": row["line"], "in": 0, "out": 0, "classes": {}})
        if row["class_name"] == "*":
            ln["in"] += row["in_count"]
            ln["out"] += row["out_count"]
            minute = int(_sec(row["window_start"]) // 60)
            timeline[minute][0] += row["in_count"]
            timeline[minute][1] += row["out_count"]
        else:
            c = ln["classes"].setdefault(row["class_name"], {"in": 0, "out": 0})
            c["in"] += row["in_count"]
            c["out"] += row["out_count"]
    for ln in (job.config or {}).get("lines", []):  # lines nobody crossed
        lines.setdefault(ln["name"], {"name": ln["name"], "in": 0, "out": 0, "classes": {}})
    zones: dict[str, dict[str, Any]] = {}
    for row in buffer.query("zone_stats", camera_id="video"):
        if row["class_name"] != "*":
            continue
        z = zones.setdefault(
            row["zone"],
            {
                "name": row["zone"],
                "occ_w": 0.0,
                "q_w": 0.0,
                "s": 0.0,
                "occ_max": 0,
                "queue_max": 0,
                "visits": 0,
                "dwell_sum": 0.0,
            },
        )
        z["occ_w"] += row["occ_avg"] * row["sample_s"]
        z["q_w"] += row["queue_avg"] * row["sample_s"]
        z["s"] += row["sample_s"]
        z["occ_max"] = max(z["occ_max"], row["occ_max"])
        z["queue_max"] = max(z["queue_max"], row["queue_max"])
        z["visits"] += row["visits"]
        z["dwell_sum"] += row["dwell_sum_s"]
    kinds = {z["name"]: z.get("kind", "area") for z in (job.config or {}).get("zones", [])}
    zone_out = []
    for name, z in zones.items():
        zone_out.append(
            {
                "name": name,
                "kind": kinds.get(name, "area"),
                "occ_avg": round(z["occ_w"] / z["s"], 2) if z["s"] else None,
                "occ_max": z["occ_max"],
                "queue_avg": round(z["q_w"] / z["s"], 2) if z["s"] else None,
                "queue_max": z["queue_max"],
                "visits": z["visits"],
                "dwell_avg_s": round(z["dwell_sum"] / z["visits"], 1) if z["visits"] else None,
            }
        )
    dwell = [
        e["dwell_s"]
        for e in buffer.query("events", camera_id="video")
        if e["kind"] == "zone_visit" and e["dwell_s"] is not None
    ]
    edges = [30, 60, 120, 300, 600, 1200]
    labels = ["< 30 s", "30-60 s", "1-2 min", "2-5 min", "5-10 min", "10-20 min", "> 20 min"]
    buckets = [0] * len(labels)
    for d in dwell:
        buckets[next((i for i, e in enumerate(edges) if d < e), len(edges))] += 1
    n_min = int((job.duration_s or 0) // 60) + 1 if job.duration_s else (max(timeline) + 1 if timeline else 0)
    seconds = job.duration_s or 0
    return {
        "lines": sorted(lines.values(), key=lambda x: x["name"]),
        "zones": sorted(zone_out, key=lambda x: x["name"]),
        "totals": {"in": sum(x["in"] for x in lines.values()), "out": sum(x["out"] for x in lines.values())},
        "timeline": [
            {"minute": m, "in": timeline.get(m, [0, 0])[0], "out": timeline.get(m, [0, 0])[1]}
            for m in range(max(n_min, (max(timeline) + 1) if timeline else 0))
        ],
        "dwell": {
            "count": len(dwell),
            "avg_s": round(sum(dwell) / len(dwell), 1) if dwell else None,
            "buckets": [{"label": lbl, "count": c} for lbl, c in zip(labels, buckets, strict=True)],
        },
        "frames_read": summary.frames_read,
        "frames_processed": summary.frames_processed,
        "processing_s": round(wall_s, 1),
        "speed_x": round(seconds / wall_s, 2) if wall_s and seconds else None,
        "processed_fps": round(summary.frames_processed / wall_s, 1) if wall_s else None,
        "detector": get_settings().video_detector,
        "video_codec": codec,
        "classes": (job.config or {}).get("classes", ["person"]),
    }


def counts_csv(buffer_path: Path) -> str:
    """Per video minute, line and class: IN/OUT (time = position in the video)."""
    from countvision_edge.storage import SqliteBuffer

    buffer = SqliteBuffer(buffer_path)
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["video_time", "line", "class", "in", "out"])
    for row in sorted(
        buffer.query("line_counts", camera_id="video"),
        key=lambda r: (r["window_start"], r["line"], r["class_name"]),
    ):
        s = int(_sec(row["window_start"]))
        w.writerow(
            [
                f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}",
                row["line"],
                "all" if row["class_name"] == "*" else row["class_name"],
                row["in_count"],
                row["out_count"],
            ]
        )
    buffer.close()
    return out.getvalue()


def _write_pdf(job: VideoJob, result: dict[str, Any]) -> None:
    from ...saas.models import Organization
    from ..reports import pdf

    with db_session() as db:
        org = db.get(Organization, job.org_id)
        name = org.name if org else ""
    data = pdf.build_video_pdf(job, result, name)
    (video_jobs.job_dir(job) / "report.pdf").write_bytes(data)
