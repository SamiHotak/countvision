"""Runs the counting pipeline in a background thread for the local web app.

The runner owns one camera at a time. It can switch the source (camera from the config,
another webcam, an uploaded video file), apply new lines and zones while counting, and keeps
the newest preview image as JPEG - only while somebody is watching, and only in memory.

Video files are counted under the camera id ``<camera id>-video`` so a test video never mixes
with the real camera's numbers. Each file run gets its own time range in the database.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import cv2

from ..config import CameraConfig, EdgeConfig, SourceConfig, redact_uri
from ..detectors.base import Detector
from ..errors import ConfigError
from ..geometry import to_pixels
from ..inputs import create_source
from ..inputs.base import FrameSource
from ..pipeline import CameraPipeline, FrameResult
from ..storage import SqliteBuffer
from ..viz import Annotator
from .config_store import save_geometry
from .stats import day_bounds

log = logging.getLogger(__name__)

PreviewMode = Literal["full", "blur", "off"]
VIDEO_SUFFIX = "-video"
PREVIEW_MAX_WIDTH = 960
PREVIEW_MAX_FPS = 15.0
VIEWER_TIMEOUT_S = 3.0


@dataclass
class RunInfo:
    """One run of the pipeline on one source."""

    kind: Literal["live", "file"]
    label: str  # shown in the page, never contains a password
    camera_id: str
    data_start: float  # time of the first frame (files: chosen so runs never overlap)
    started_at: float = dataclasses.field(default_factory=time.time)
    state: Literal["starting", "running", "finished", "stopped", "error"] = "starting"
    error: str | None = None
    ended_at: float | None = None
    upload: Path | None = None  # deleted when the run ends (we do not keep videos)


@dataclass(frozen=True)
class Scope:
    """Which numbers the page shows: today for a live camera, the whole run for a video."""

    kind: Literal["today", "video"]
    camera_id: str
    start: float
    end: float
    bucket_s: int

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _to_normalized(point, size: tuple[int, int] | None, mode: str) -> list[float]:
    x, y = float(point[0]), float(point[1])
    if mode == "normalized":
        return [round(x, 4), round(y, 4)]
    if size is None:
        raise ConfigError("Wait until the camera shows a picture, then try again.")
    return [round(x / size[0], 4), round(y / size[1], 4)]


def blur_boxes(image, result: FrameResult) -> None:
    """Blur every tracked box in place (privacy option for the preview)."""
    height, width = image.shape[:2]
    for track in result.tracks:
        x1, y1, x2, y2 = (int(v) for v in track.xyxy)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        roi = image[y1:y2, x1:x2]
        small = cv2.resize(roi, (max(1, (x2 - x1) // 12), max(1, (y2 - y1) // 12)))
        image[y1:y2, x1:x2] = cv2.resize(small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST)


class LiveRunner:
    """Background counting for the local app. All public methods are thread-safe."""

    def __init__(
        self,
        cfg: EdgeConfig,
        cam: CameraConfig,
        detector: Detector,
        buffer: SqliteBuffer,
        *,
        config_path: str | Path | None = None,
        preview: PreviewMode = "full",
    ) -> None:
        self.cfg = cfg
        self.base_cam = cam
        self.detector = detector
        self.buffer = buffer
        self.config_path = Path(config_path) if config_path else None
        self.preview: PreviewMode = preview
        self._lock = threading.RLock()
        self._pipeline: CameraPipeline | None = None
        self._source: FrameSource | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._annotator: Annotator | None = None
        self.run: RunInfo | None = None
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._frame_size: tuple[int, int] | None = None
        self._last_ts: float | None = None
        self._fps: float | None = None
        self._last_result_at: float | None = None
        self._viewer_until = 0.0
        self._last_encode = 0.0
        self.config_version = 0

    # -- start / stop -------------------------------------------------------------------

    def start(self, source: SourceConfig | None = None, *, upload: Path | None = None,
              label: str | None = None) -> RunInfo:
        """Stop the current run and start counting from ``source`` (default: the config's)."""
        self.stop()
        source = source or self.base_cam.source
        is_file = source.kind == "file"
        camera_id = self.base_cam.id + VIDEO_SUFFIX if is_file else self.base_cam.id
        data_start = time.time()
        if is_file:
            if source.start_time is None:
                latest = self.buffer.latest_ts(camera_id)
                if latest is not None:  # never overlap an earlier analysis of a video
                    data_start = max(data_start, math.ceil(latest / 60) * 60 + 60)
                source = source.model_copy(
                    update={"start_time": datetime.fromtimestamp(data_start, tz=timezone.utc)}  # noqa: UP017
                )
            else:
                data_start = source.start_epoch() or data_start
        cam = self.base_cam.model_copy(update={"id": camera_id, "source": source})
        if label is None:
            label = Path(source.uri).name if is_file else (
                f"Webcam {source.uri}" if source.kind == "webcam" else redact_uri(source.uri)
            )
        info = RunInfo("file" if is_file else "live", label, camera_id, data_start, upload=upload)

        pipeline = CameraPipeline(
            cam, self.detector, self.buffer,
            data_dir=self.cfg.data_dir, heartbeat_interval_s=self.cfg.heartbeat_interval_s,
        )
        frame_source = create_source(source)
        stop = threading.Event()
        with self._lock:
            self._pipeline, self._source, self._stop, self.run = pipeline, frame_source, stop, info
            self._annotator = Annotator(lambda: pipeline.engine, geometry=False)
            self._jpeg, self._frame_size, self._last_ts = None, None, None
            self._fps, self._last_result_at = None, None
            self._thread = threading.Thread(
                target=self._work, args=(pipeline, frame_source, stop, info),
                name=f"countvision-{camera_id}", daemon=True,
            )
            self._thread.start()
        log.info("Started %s run: %s (camera id %s)", info.kind, info.label, camera_id)
        return info

    def stop(self, timeout: float = 15.0) -> None:
        """Stop the current run and wait until its numbers are written."""
        with self._lock:
            thread, stop = self._thread, self._stop
        if thread is None:
            return
        stop.set()
        thread.join(timeout)
        if thread.is_alive():
            log.warning("The counting thread did not stop within %.0f s", timeout)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until the current run ends by itself (video files). True if it ended."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _work(self, pipeline: CameraPipeline, source: FrameSource, stop: threading.Event,
              info: RunInfo) -> None:
        info.state = "running"
        try:
            pipeline.run(source, stop, on_result=lambda r: self._on_result(r, pipeline))
            info.state = "stopped" if stop.is_set() else "finished"
        except Exception as exc:  # noqa: BLE001 - shown in the page instead of killing the app
            log.exception("Counting stopped with an error")
            info.state, info.error = "error", str(exc) or exc.__class__.__name__
        finally:
            info.ended_at = time.time()
            if info.upload is not None:
                with contextlib.suppress(OSError):
                    info.upload.unlink(missing_ok=True)
                    log.info("Deleted the uploaded video %s (videos are not kept)", info.upload.name)

    # -- preview ------------------------------------------------------------------------

    def _on_result(self, result: FrameResult, pipeline: CameraPipeline) -> None:
        now = time.monotonic()
        with self._lock:
            if pipeline is not self._pipeline:
                return
            if self._last_result_at is not None:
                inst = 1.0 / max(now - self._last_result_at, 1e-6)
                self._fps = inst if self._fps is None else 0.9 * self._fps + 0.1 * inst
            self._last_result_at = now
            self._frame_size = result.frame.size
            self._last_ts = result.frame.ts
            annotator = self._annotator
            watching = now < self._viewer_until
            due = now - self._last_encode >= 1.0 / PREVIEW_MAX_FPS
        if self.preview == "off" or annotator is None or not watching or not due:
            return
        if self.preview == "blur":
            image = result.frame.image.copy()
            blur_boxes(image, result)
            result = dataclasses.replace(result, frame=dataclasses.replace(result.frame, image=image))
        image = annotator.draw(result)
        height, width = image.shape[:2]
        if width > PREVIEW_MAX_WIDTH:
            scale = PREVIEW_MAX_WIDTH / width
            image = cv2.resize(image, (PREVIEW_MAX_WIDTH, int(round(height * scale))),
                               interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        if not ok:
            return
        with self._lock:
            self._jpeg = encoded.tobytes()
            self._jpeg_seq += 1
            self._last_encode = now

    def touch_viewer(self) -> None:
        """A browser is watching the preview: keep making JPEGs for a few seconds."""
        with self._lock:
            self._viewer_until = time.monotonic() + VIEWER_TIMEOUT_S

    def latest_jpeg(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._jpeg_seq, self._jpeg

    # -- geometry -----------------------------------------------------------------------

    def geometry(self) -> dict:
        """Lines and zones in normalized coordinates (0..1), plus the frame size."""
        with self._lock:
            cam, size = self.base_cam, self._frame_size
        mode = cam.coordinates
        known = mode == "normalized" or size is not None
        lines, zones = [], []
        if known:
            lines = [
                {"name": line.name, "p1": _to_normalized(line.p1, size, mode),
                 "p2": _to_normalized(line.p2, size, mode), "in_direction": line.in_direction}
                for line in cam.lines
            ]
            zones = [
                {"name": zone.name, "kind": zone.kind,
                 "polygon": [_to_normalized(p, size, mode) for p in zone.polygon]}
                for zone in cam.zones
            ]
        return {
            "version": self.config_version,
            "ready": known,
            "frame": {"width": size[0], "height": size[1]} if size else None,
            "anchor": cam.anchor,
            "lines": lines,
            "zones": zones,
        }

    def apply_geometry(self, lines: list[dict], zones: list[dict]) -> dict:
        """Validate new lines and zones, use them at once, and save them to the config file.

        Settings the page does not edit (classes, deadband, queue speed ...) are kept for
        lines and zones that keep their name. Raises ConfigError with a readable message.
        """
        with self._lock:
            cam, size = self.base_cam, self._frame_size
        old_lines = {line.name: line for line in cam.lines}
        old_zones = {zone.name: zone for zone in cam.zones}
        new_lines = []
        for item in lines:
            base = old_lines[item["name"]].model_dump() if item.get("name") in old_lines else {}
            base.update(name=item.get("name"), p1=item.get("p1"), p2=item.get("p2"),
                        in_direction=item.get("in_direction", "to_right"))
            new_lines.append(base)
        new_zones = []
        for item in zones:
            base = old_zones[item["name"]].model_dump() if item.get("name") in old_zones else {}
            base.update(name=item.get("name"), polygon=item.get("polygon"),
                        kind=item.get("kind", "area"))
            new_zones.append(base)

        data = cam.model_dump()
        data.update(coordinates="normalized", lines=new_lines, zones=new_zones)
        if cam.coordinates == "pixel" and cam.speed is not None:
            data["speed"]["p1"] = _to_normalized(cam.speed.p1, size, "pixel")
            data["speed"]["p2"] = _to_normalized(cam.speed.p2, size, "pixel")
        try:
            new_cam = CameraConfig.model_validate(data)
        except ValueError as exc:
            raise ConfigError(_readable(exc)) from exc
        for zone in new_cam.zones:
            pixels = [to_pixels(p, (1000, 1000), "normalized") for p in zone.polygon]
            if _polygon_area(pixels) < 1.0:
                raise ConfigError(f"Zone '{zone.name}' has no area. Spread its corners apart.")

        saved_to = None
        if self.config_path is not None:
            saved_to = str(save_geometry(self.config_path, new_cam))
        with self._lock:
            self.base_cam = new_cam
            self.config_version += 1
            pipeline = self._pipeline
        if pipeline is not None:
            pipeline.reconfigure(
                new_cam.model_copy(update={"id": pipeline.cam.id, "source": pipeline.cam.source})
            )
        result = self.geometry()
        result["saved_to"] = saved_to
        return result

    # -- status -------------------------------------------------------------------------

    def scope(self) -> Scope:
        """The time range the page shows."""
        with self._lock:
            info, last_ts = self.run, self._last_ts
        if info is not None and info.kind == "file":
            start = math.floor(info.data_start / 60) * 60
            end = max(start + 60, (last_ts or info.data_start) + 1)
            return Scope("video", info.camera_id, start, end, 60)
        start, end = day_bounds()
        return Scope("today", self.base_cam.id, start, end, 3600)

    def status(self) -> dict:
        """Everything the page needs to show the state of counting. JSON-serialisable."""
        with self._lock:
            info, pipeline, source = self.run, self._pipeline, self._source
            fps, size, last_at = self._fps, self._frame_size, self._last_result_at
            cam = self.base_cam
        live = pipeline.state() if pipeline is not None else {"lines": {}, "zones": {}}
        source_status = source.status() if source is not None else None
        progress = None
        if info is not None and info.kind == "file" and source is not None and pipeline is not None:
            total = getattr(source, "total_frames", lambda: None)()
            if total:
                progress = min(1.0, pipeline.frames_read / total)
            if info.state == "finished":
                progress = 1.0
        idle_s = time.monotonic() - last_at if last_at is not None else None
        return {
            "camera": {"id": cam.id, "name": cam.name or cam.id, "classes": cam.classes},
            "run": None if info is None else {
                "kind": info.kind, "label": info.label, "camera_id": info.camera_id,
                "state": info.state, "error": info.error, "started_at": info.started_at,
                "ended_at": info.ended_at, "progress": progress,
            },
            "source": None if source_status is None else {
                "connected": source_status.connected,
                "reconnects": source_status.reconnects,
                "error": source_status.error,
            },
            "fps": round(fps, 1) if fps is not None and idle_s is not None and idle_s < 3 else None,
            "frame": {"width": size[0], "height": size[1]} if size else None,
            "preview": self.preview,
            "detector": {
                "name": self.detector.info.name,
                "license": self.detector.info.license,
            },
            "config_version": self.config_version,
            "live": {"lines": live.get("lines", {}), "zones": live.get("zones", {})},
        }


def _polygon_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True):
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _readable(exc: ValueError) -> str:
    """Short message from a pydantic error (first problem only)."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            message = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
            return f"{message} ({where})" if where else message
        except (IndexError, TypeError):
            pass
    return str(exc)
