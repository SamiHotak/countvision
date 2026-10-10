"""Supervisor: runs several cameras at once and keeps them running.

    countvision-edge run --config site.yaml      # all cameras in the config

* One worker thread per camera. Each has its own source, tracker and analytics, all write
  into the same SQLite buffer.
* One detector is shared by all cameras (one model in memory). Calls are serialised with a
  lock; every camera's scheduler lowers its own frame rate when the machine is too slow.
* A camera that crashes (bug, driver error) is restarted with a growing delay (1 s ... 60 s).
  A camera that loses its stream reconnects by itself (see inputs/live_source.py).
* Every few seconds ``<data_dir>/status.json`` is written: state, FPS and connection of every
  camera. ``countvision-edge health`` reads it (Docker uses that as the health check).
* A camera that is offline longer than ``alerts.camera_offline_after_s`` (default 5 min)
  raises an alert: a WARNING in the log, ``alert`` in status.json and a ``camera_offline``
  event in the database (``camera_online`` with the outage length when it is back).

* When paired with the cloud: the uploader sends the numbers, and ConfigSync (cloud_sync.py)
  receives lines/zones/classes/schedules drawn in the web app and applies them to the running
  camera within seconds. Snapshot requests get ONE pixelated, scaled-down JPEG.

No video ever leaves a worker. status.json contains only numbers and redacted camera URLs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import __version__
from .cloud_sync import ConfigSync, apply_doc, load_overlay, report_doc, save_overlay
from .config import CameraConfig, EdgeConfig, redact_uri
from .detectors.base import Detector
from .errors import CountVisionError, SourceError
from .inputs import create_source
from .inputs.base import FrameSource
from .inputs.live_source import Backoff
from .pipeline import CameraPipeline, FrameResult, RunSummary
from .privacy import limit_width, pixelate_boxes
from .storage import SqliteBuffer
from .types import Detections, Event

log = logging.getLogger(__name__)

STATUS_FILE = "status.json"
SNAPSHOT_MAX_WIDTH = 960


class SharedDetector(Detector):
    """Lets several camera threads use one detector safely (one call at a time)."""

    def __init__(self, inner: Detector) -> None:
        self.inner = inner
        self.names = inner.names
        self.info = inner.info
        self._lock = threading.Lock()
        self._warm = False

    def detect(self, image: np.ndarray) -> Detections:
        with self._lock:
            return self.inner.detect(image)

    def warmup(self) -> None:
        with self._lock:
            if not self._warm:
                self.inner.warmup()
                self._warm = True

    def close(self) -> None:
        self.inner.close()


@dataclass
class CameraHealth:
    """What status.json says about one camera."""

    camera_id: str
    name: str | None
    source: str  # redacted URI
    kind: str
    # starting | connecting | running | offline | restarting | finished | failed | stopped
    state: str = "starting"
    connected: bool | None = None
    frame_age_s: float | None = None
    reconnects: int = 0
    restarts: int = 0
    fps: float | None = None
    frames_processed: int = 0
    events: int = 0
    offline_since: float | None = None
    alert: str | None = None
    last_error: str | None = None
    paused: bool = False  # outside the schedule (opening hours)
    config_version: int = 0  # cloud config version in use (0 = the YAML config)
    config_error: str | None = None


class CameraWorker(threading.Thread):
    """Runs one camera until ``stop`` is set (live) or the file ends."""

    def __init__(
        self,
        cam: CameraConfig,
        cfg: EdgeConfig,
        detector: Detector,
        buffer: SqliteBuffer,
        stop: threading.Event,
        *,
        max_frames: int | None = None,
        on_result: Callable[[str, FrameResult], None] | None = None,
        source_factory: Callable[[CameraConfig], FrameSource] | None = None,
    ) -> None:
        super().__init__(name=f"camera-{cam.id}", daemon=True)
        self.cam = cam
        self.cfg = cfg
        self.detector = detector
        self.buffer = buffer
        self.stop_event = stop
        self.max_frames = max_frames
        self.on_result = on_result
        self.source_factory = source_factory or (lambda c: create_source(c.source))
        self.health = CameraHealth(
            camera_id=cam.id, name=cam.name, source=_label(cam), kind=cam.source.kind
        )
        self.source: FrameSource | None = None
        self.pipeline: CameraPipeline | None = None
        self.summaries: list[RunSummary] = []
        self._restart = Backoff(1.0, 2.0, 60.0, jitter=0.1)
        self._lock = threading.Lock()
        self._rate_mark: tuple[float, int] | None = None  # (monotonic time, frames) for the FPS
        self.connect_deadline = 0.0  # a new live stream gets this long before it counts as offline
        self.last_result: FrameResult | None = None  # newest analysed frame (memory only)
        self._frame_size: tuple[int, int] | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                source = self.source_factory(self.cam)
                pipeline = CameraPipeline(
                    self.cam, self.detector, self.buffer,
                    data_dir=self.cfg.data_dir, heartbeat_interval_s=self.cfg.heartbeat_interval_s,
                )
                with self._lock:
                    self.source, self.pipeline = source, pipeline
                    self.connect_deadline = time.monotonic() + (
                        self.cam.source.reconnect.open_timeout_s + self.cam.source.reconnect.stall_timeout_s
                    )
                self.health.state = "connecting" if source.is_live else "running"
                def callback(result: FrameResult, _cam=self.cam.id) -> None:
                    self.last_result = result  # replaced every frame, never written anywhere
                    if self.on_result is not None:
                        self.on_result(_cam, result)
                summary = pipeline.run(source, self.stop_event, max_frames=self.max_frames,
                                       on_result=callback)
                self.summaries.append(summary)
                self.health.events += summary.events
                if not source.is_live or self.max_frames is not None:
                    self.health.state = "finished"
                    return
                if self.stop_event.is_set():
                    break
                raise SourceError("live source ended unexpectedly")
            except (SourceError, CountVisionError) as exc:
                self.health.last_error = str(exc)
                if not self.cam.source.is_live:
                    # A missing or broken video file does not fix itself.
                    self.health.state = "failed"
                    log.error("Camera %s: %s", self.cam.id, exc)
                    return
                self._schedule_restart(exc, started)
            except Exception as exc:  # noqa: BLE001 - a bug must not stop the other cameras
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Camera %s crashed", self.cam.id)
                self._schedule_restart(exc, started)
        self.health.state = "stopped"

    def _schedule_restart(self, exc: Exception, started: float) -> None:
        if time.monotonic() - started > 300:
            self._restart.reset()  # it ran fine for a while: start again with a short delay
        delay = self._restart.next_delay()
        self.health.restarts += 1
        self.health.state = "restarting"
        log.warning("Camera %s: restarting in %.0f s (%s)", self.cam.id, delay, exc)
        self.stop_event.wait(delay)

    @property
    def frame_size(self) -> tuple[int, int] | None:
        """(width, height) of the camera picture, once a frame was read."""
        frame = self.last_frame
        if frame is not None:
            self._frame_size = frame.size
        return self._frame_size

    @property
    def last_frame(self):
        pipeline = self.pipeline
        return pipeline.last_frame if pipeline is not None else None

    def reconfigure(self, cam: CameraConfig) -> None:
        """New lines/zones/classes/schedule: used by the running pipeline from the next frame and
        by every restart after that."""
        with self._lock:
            self.cam = cam
            pipeline = self.pipeline
        if pipeline is not None:
            pipeline.reconfigure(cam)

    def refresh(self) -> CameraHealth:
        """Update connection numbers from the source and pipeline (called by the supervisor)."""
        with self._lock:
            source, pipeline = self.source, self.pipeline
        h = self.health
        if source is not None and h.state in ("connecting", "running", "offline"):
            status = source.status()
            if h.state == "connecting" and (status.connected or time.monotonic() > self.connect_deadline):
                h.state = "running"
            h.connected = status.connected
            h.reconnects = status.reconnects
            h.frame_age_s = None if status.last_frame_age_s is None else round(status.last_frame_age_s, 1)
            if status.error and not status.connected:
                h.last_error = status.error
        if pipeline is not None:
            frames = pipeline.heartbeat.processed
            now = time.monotonic()
            if self._rate_mark is not None and now - self._rate_mark[0] >= 1.0:
                done = frames - self._rate_mark[1]
                h.fps = round(max(done, 0) / (now - self._rate_mark[0]), 1)
                self._rate_mark = (now, frames)
            elif self._rate_mark is None:
                self._rate_mark = (now, frames)
            h.frames_processed = frames
            h.paused = pipeline.paused
        return h


class Supervisor:
    """Starts one worker per camera, watches them, writes status.json and offline alerts."""

    def __init__(
        self,
        cfg: EdgeConfig,
        detector: Detector,
        buffer: SqliteBuffer,
        cameras: list[CameraConfig] | None = None,
        *,
        max_frames: int | None = None,
        on_result: Callable[[str, FrameResult], None] | None = None,
        source_factory: Callable[[CameraConfig], FrameSource] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.detector = SharedDetector(detector)
        self.buffer = buffer
        self.stop_event = threading.Event()
        self.clock = clock
        self.device_id = cfg.resolve_device_id()
        self.status_path = Path(cfg.data_dir) / STATUS_FILE
        self._overlay = load_overlay(cfg.data_dir)
        self._config_lock = threading.Lock()
        self.workers = []
        for cam in (cameras if cameras is not None else cfg.cameras):
            worker = CameraWorker(self._with_overlay(cam), cfg, self.detector, buffer, self.stop_event,
                                  max_frames=max_frames, on_result=on_result, source_factory=source_factory)
            saved = self._overlay.get(cam.id)
            if saved and worker.cam is not cam:
                worker.health.config_version = int(saved.get("version", 0))
            self.workers.append(worker)
        self.started_at: float | None = None
        self.uploader = None  # cloud.CloudUploader when paired (see start_uploader)
        self.config_sync: ConfigSync | None = None  # when paired (see start_config_sync)
        self._last_status: dict | None = None

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        self.started_at = self.clock()
        self.detector.warmup()
        for worker in self.workers:
            worker.start()
        log.info("Supervisor: %d camera(s) started, detector %s (%s)", len(self.workers),
                 self.detector.info.name, self.detector.info.license)

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        for worker in self.workers:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            worker.join(remaining)

    @property
    def alive(self) -> bool:
        return any(w.is_alive() for w in self.workers)

    def start_uploader(self, uploader=None) -> None:
        """Start uploading to the cloud (when this device is paired). ``uploader`` for tests."""
        from .cloud import make_uploader

        self.uploader = uploader or make_uploader(self.cfg, self.buffer, self.cloud_status)
        if self.uploader is not None:
            self.uploader.start()

    def start_config_sync(self, sync: ConfigSync | None = None) -> None:
        """Receive configs and snapshot requests from the cloud (when paired). ``sync`` for tests."""
        if sync is None:
            from .cloud import Credentials

            creds = Credentials.load(self.cfg.data_dir) if self.cfg.cloud.enabled else None
            if creds is None or not self.cfg.cloud.config_sync:
                return
            sync = ConfigSync(creds, apply_config=self.apply_camera_config, take_snapshot=self.snapshot,
                              url=self.cfg.cloud.url, timeout_s=self.cfg.cloud.timeout_s)
        self.config_sync = sync
        sync.applied.update({w.cam.id: w.health.config_version for w in self.workers
                             if w.health.config_version})
        sync.start()

    def cloud_status(self) -> dict:
        """Status for the cloud: like status.json, plus every camera's lines/zones/classes/schedule
        (normalized) and the cloud config version in use."""
        status = dict(self._last_status or self.status())
        by_id = {w.cam.id: w for w in self.workers}
        cameras = []
        for cam in status.pop("cameras", []):
            worker = by_id.get(cam["camera_id"])
            conf = worker.cam if worker else None
            item = {
                "id": cam["camera_id"], "name": cam.get("name") or cam["camera_id"],
                "state": "paused" if cam.get("paused") and cam["state"] == "running" else cam["state"],
                "connected": cam.get("connected"), "fps": cam.get("fps"), "reconnects": cam.get("reconnects"),
                "alert": cam.get("alert"),
                "lines": [line.name for line in conf.lines] if conf else [],
                "zones": [zone.name for zone in conf.zones] if conf else [],
            }
            if worker is not None and conf is not None:
                item.update(
                    config=report_doc(conf, worker.frame_size),
                    config_version=worker.health.config_version,
                    config_error=worker.health.config_error,
                    snapshots_allowed=bool(self.cfg.privacy.snapshots),
                    frame=list(worker.frame_size) if worker.frame_size else None,
                )
            cameras.append(item)
        status.pop("pid", None)
        status["cameras"] = cameras
        return status

    # -- config from the cloud ----------------------------------------------------------

    def _with_overlay(self, cam: CameraConfig) -> CameraConfig:
        saved = self._overlay.get(cam.id)
        if not saved or not saved.get("config"):
            return cam
        try:
            new = apply_doc(cam, saved["config"], saved.get("timezone"))
            self.detector.class_ids(new.classes)
        except Exception as exc:  # noqa: BLE001 - a bad saved file must not stop the camera
            log.warning("Camera %s: saved cloud config not used (%s); using the YAML config", cam.id, exc)
            return cam
        log.info("Camera %s: using cloud config v%s (%s)", cam.id, saved.get("version"),
                 Path(self.cfg.data_dir) / "cloud_config.json")
        return new

    def apply_camera_config(self, camera_id: str, version: int, doc: dict,
                            timezone: str | None) -> str | None:
        """Apply a config from the cloud to a running camera. Returns an error text or None."""
        worker = next((w for w in self.workers if w.cam.id == camera_id), None)
        if worker is None:
            return f"camera {camera_id} is not configured on this device"
        with self._config_lock:
            try:
                new = apply_doc(worker.cam, doc, timezone, worker.frame_size)
                self.detector.class_ids(new.classes)  # the model must know every class
            except Exception as exc:  # noqa: BLE001 - reported to the web app as text
                error = _short_error(exc)
                worker.health.config_error = error
                self._report_now()
                return error
            worker.reconfigure(new)
            worker.health.config_version, worker.health.config_error = version, None
            self._overlay[camera_id] = {"version": version, "config": doc, "timezone": timezone}
            try:
                save_overlay(self.cfg.data_dir, self._overlay)
            except OSError as exc:
                log.warning("Could not save the cloud config (%s); it is used until the next restart", exc)
        self._report_now()
        return None

    def _report_now(self) -> None:
        self._last_status = None  # the next upload reports the new version at once
        if self.uploader is not None and hasattr(self.uploader, "wake"):
            self.uploader.wake()

    def snapshot(self, camera_id: str) -> bytes:
        """ONE privacy-safe JPEG of the newest frame: every detected person/vehicle pixelated,
        max 960 px wide. Memory only. Raises when not allowed or no picture yet."""
        if not self.cfg.privacy.snapshots:
            raise PermissionError("snapshots are switched off on this device (privacy.snapshots: false)")
        worker = next((w for w in self.workers if w.cam.id == camera_id), None)
        if worker is None:
            raise LookupError(f"camera {camera_id} is not configured on this device")
        frame = worker.last_frame
        if frame is None or worker.health.state not in ("running", "connecting", "finished"):
            raise RuntimeError("the camera has no picture right now (offline or still starting)")
        if worker.cam.source.is_live and frame.ts < self.clock() - 30:
            raise RuntimeError("the newest picture is older than 30 s (camera stalled)")
        image = frame.image.copy()
        # A fresh detection on exactly this frame, ALL classes (not only the counted ones): a
        # snapshot for a car park still pixelates the people walking through it.
        boxes = [tuple(b) for b in self.detector.detect(image).xyxy.reshape(-1, 4)]
        result = worker.last_result
        if result is not None:
            boxes += [t.xyxy for t in result.tracks]
        pixelate_boxes(image, boxes)
        image = limit_width(image, SNAPSHOT_MAX_WIDTH)
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise RuntimeError("could not encode the picture")
        return encoded.tobytes()

    def run_forever(self, stop: threading.Event | None = None, *, upload: bool = True) -> None:
        """Start, then check every second until everything ended or ``stop`` is set."""
        self.start()
        if upload:
            self.start_uploader()
            self.start_config_sync()
        next_status = 0.0
        try:
            while self.alive:
                if stop is not None and stop.is_set():
                    break
                self.check_alerts()
                if time.monotonic() >= next_status:
                    self.write_status()
                    next_status = time.monotonic() + self.cfg.status_interval_s
                time.sleep(0.5)
        finally:
            self.stop()
            self.join(timeout=15)
            self.check_alerts()
            self.write_status()
            if self.config_sync is not None:
                self.config_sync.stop()
            if self.uploader is not None:
                self.uploader.stop()
                self.uploader.join(timeout=5)
                if self.uploader.state.state == "ok":
                    self.uploader.flush(timeout_s=10)  # send the last minutes before exiting
                self.write_status()

    # -- alerts -------------------------------------------------------------------------

    def check_alerts(self) -> list[Event]:
        """Mark cameras offline/online and write alert events. Returns the new events."""
        now = self.clock()
        events: list[Event] = []
        limit = self.cfg.alerts.camera_offline_after_s
        for worker in self.workers:
            h = worker.refresh()
            if not worker.cam.source.is_live or h.state not in ("running", "offline"):
                continue  # files, and cameras still connecting for the first time
            offline = h.connected is False or (
                h.frame_age_s is not None and h.frame_age_s > worker.cam.source.reconnect.stall_timeout_s
            )
            if offline:
                if h.offline_since is None:
                    h.offline_since = now
                    h.state = "offline"
                    log.warning("Camera %s is offline (%s). Reconnecting ...", h.camera_id,
                                h.last_error or "no frames")
                elif h.alert is None and now - h.offline_since >= limit:
                    h.alert = f"offline for more than {_duration(now - h.offline_since)}"
                    log.warning("ALERT: camera %s %s", h.camera_id, h.alert)
                    events.append(self._event("camera_offline", h.camera_id, now))
            elif h.offline_since is not None:
                outage = now - h.offline_since
                log.info("Camera %s is back online after %.0f s", h.camera_id, outage)
                if h.alert is not None:
                    events.append(self._event("camera_online", h.camera_id, now, dwell_s=outage))
                h.offline_since, h.alert, h.state = None, None, "running"
        if events:
            self.buffer.add_events(events)
        return events

    @staticmethod
    def _event(kind: str, camera_id: str, ts: float, dwell_s: float | None = None) -> Event:
        return Event(event_id=uuid.uuid4().hex, kind=kind, ts=ts, camera_id=camera_id,
                     name=camera_id, track_id=-1, class_name="", dwell_s=dwell_s)

    # -- status -------------------------------------------------------------------------

    def status(self) -> dict:
        info = self.detector.info
        return {
            "version": __version__,
            "device_id": self.device_id,
            "pid": os.getpid(),
            "updated": self.clock(),
            "started": self.started_at,
            "detector": {"name": info.name, "runtime": info.runtime, "device": info.device,
                         "license": info.license},
            "privacy": {"preview": self.cfg.privacy.preview, "stores_images": False},
            "cameras": [asdict(w.refresh()) for w in self.workers],
            "upload": asdict(self.uploader.state) if self.uploader is not None else {"state": "disabled"},
            "config_sync": ({"state": self.config_sync.state, "last_error": self.config_sync.last_error}
                            if self.config_sync is not None else {"state": "disabled"}),
        }

    def write_status(self) -> Path:
        """Write status.json atomically (a reader never sees half a file)."""
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_name(f".{STATUS_FILE}.{os.getpid()}.tmp")
        status = self.status()
        self._last_status = status
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(tmp, self.status_path)
        return self.status_path


def _short_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return (" ".join(line.strip() for line in text[:4]) or type(exc).__name__)[:290]


def _duration(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min"


def _label(cam: CameraConfig) -> str:
    if cam.source.kind == "webcam":
        return f"webcam {cam.source.uri}"
    if cam.source.kind == "file":
        return Path(cam.source.uri).name
    return redact_uri(cam.source.uri)


# ------------------------------------------------------------------------------- health


@dataclass
class HealthReport:
    ok: bool
    exit_code: int
    lines: list[str] = field(default_factory=list)


def check_health(status_path: str | Path, max_age_s: float = 60.0, now: float | None = None) -> HealthReport:
    """Read status.json. Exit codes: 0 healthy, 1 not running / stale, 2 a camera has an
    alert or failed."""
    path = Path(status_path)
    now = time.time() if now is None else now
    if not path.is_file():
        return HealthReport(False, 1, [f"No status file at {path}. Is countvision-edge run running?"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return HealthReport(False, 1, [f"Cannot read {path}: {exc}"])
    age = now - float(data.get("updated", 0))
    lines = [f"CountVision {data.get('version', '?')} on {data.get('device_id', '?')}, "
             f"status {age:.0f} s old, detector {data.get('detector', {}).get('name', '?')}"]
    code = 0
    if age > max_age_s:
        lines.append(f"STALE: status not updated for {age:.0f} s (limit {max_age_s:.0f} s)")
        code = 1
    upload = data.get("upload") or {}
    if upload.get("state") and upload["state"] != "disabled":
        line = f"  cloud: {upload['state']}, {upload.get('pending', 0)} rows waiting"
        if upload.get("last_error") and upload["state"] != "ok":
            line += f" ({upload['last_error']})"
        lines.append(line)
    sync = data.get("config_sync") or {}
    if sync.get("state") and sync["state"] != "disabled":
        line = f"  cloud config: {sync['state']}"
        if sync.get("last_error") and sync["state"] != "ok":
            line += f" ({sync['last_error']})"
        lines.append(line)
    for cam in data.get("cameras", []):
        fps = "-" if cam.get("fps") is None else f"{cam['fps']:.1f}"
        line = (f"  {cam['camera_id']}: {cam['state']}, {fps} FPS, "
                f"{cam.get('frames_processed', 0)} frames, reconnects {cam.get('reconnects', 0)}, "
                f"restarts {cam.get('restarts', 0)}")
        if cam.get("paused"):
            line += "  (paused: outside the schedule)"
        if cam.get("config_version"):
            line += f"  cloud config v{cam['config_version']}"
        if cam.get("config_error"):
            line += f"  CONFIG NOT APPLIED: {cam['config_error']}"
        if cam.get("alert"):
            line += f"  ALERT: {cam['alert']}"
        if cam.get("last_error") and cam["state"] != "running":
            line += f"  ({cam['last_error']})"
        lines.append(line)
        if code == 0 and (cam.get("alert") or cam["state"] == "failed"):
            code = 2
    return HealthReport(code == 0, code, lines)
