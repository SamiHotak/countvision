"""YAML configuration with validation, ``${ENV_VAR}`` expansion and credential redaction.

Secrets (for example RTSP passwords) must not be written into the YAML file.
Write ``uri: ${CAM1_RTSP_URL}`` and set the variable in the environment.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError
from .types import Point

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_URI_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")


class _Model(BaseModel):
    """Base model: unknown keys are errors, so typos are caught early."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- source


class ReconnectConfig(_Model):
    """Reconnect behaviour for live sources (webcam, RTSP, HTTP/MJPEG)."""

    initial_s: float = Field(1.0, gt=0)
    factor: float = Field(2.0, ge=1.0)
    max_s: float = Field(15.0, gt=0)  # worst case: back online ~15 s + connect time
    jitter: float = Field(0.1, ge=0, le=1)
    stall_timeout_s: float = Field(10.0, gt=0)  # no frame for this long -> reconnect
    open_timeout_s: float = Field(10.0, gt=0)


class SourceConfig(_Model):
    """Where the frames come from."""

    uri: str  # "0" = first webcam, a file path, rtsp://..., http://.../video
    kind: Literal["auto", "webcam", "file", "rtsp", "http"] = "auto"
    width: int | None = Field(None, gt=0)  # requested from webcams only
    height: int | None = Field(None, gt=0)
    fps: float | None = Field(None, gt=0)
    fourcc: str | None = Field(None, min_length=4, max_length=4)  # e.g. "MJPG" for USB webcams
    rtsp_transport: Literal["tcp", "udp"] = "tcp"
    start_time: datetime | None = None  # files only: wall-clock time of the first frame
    realtime: bool = False  # files only: play at the video's own speed
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)

    @field_validator("uri", mode="before")
    @classmethod
    def _uri_to_str(cls, value: Any) -> str:
        return str(value)

    @model_validator(mode="after")
    def _resolve_kind(self) -> SourceConfig:
        if self.kind == "auto":
            uri = self.uri.strip().lower()
            if uri.isdigit():
                self.kind = "webcam"
            elif uri.startswith(("rtsp://", "rtsps://")):
                self.kind = "rtsp"
            elif uri.startswith(("http://", "https://")):
                self.kind = "http"
            else:
                self.kind = "file"
        return self

    @property
    def is_live(self) -> bool:
        return self.kind != "file"

    def start_epoch(self) -> float | None:
        """``start_time`` as UTC epoch seconds (naive times are taken as UTC)."""
        if self.start_time is None:
            return None
        value = self.start_time
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)  # noqa: UP017 - py310 compatible
        return value.timestamp()


# --------------------------------------------------------------------------- detector


class DetectorConfig(_Model):
    """Which detector and runtime to use. The detector is swappable.

    type:
      ultralytics  YOLO via the ultralytics package. AGPL-3.0 (code AND official weights).
                   Also loads exported models: .onnx, *_openvino_model/, .engine
      rfdetr       RF-DETR via the rfdetr package. Apache-2.0 code and weights (PyTorch).
      onnx         Any exported .onnx model through ONNX Runtime (CPU, or CUDA with onnxruntime-gpu).
                   Also the official YOLOX .onnx files (Apache-2.0 code AND weights), decoder yolox.
      openvino     Any exported .onnx or .xml model through OpenVINO (Intel CPU / iGPU).
      blobs        Colour-blob detector for the synthetic demo and tests. Needs no model.

    For ``onnx`` and ``openvino`` the licence is the licence of the MODEL you export. Set
    ``model_license`` so it is written to the log at start-up.
    """

    type: Literal["ultralytics", "rfdetr", "onnx", "openvino", "blobs"] = "ultralytics"
    model: str = "yolo11n.pt"
    imgsz: int = Field(640, ge=32)
    conf: float = Field(0.2, ge=0, le=1)  # low on purpose: ByteTrack uses low scores too
    iou: float = Field(0.5, gt=0, le=1)  # NMS threshold (YOLO only)
    device: str = "auto"  # auto | cpu | cuda | cuda:0 | GPU (OpenVINO iGPU)
    classes: list[str] | None = None  # optional detector-side filter, by class name
    decoder: Literal["auto", "yolo", "yolo_e2e", "yolox", "detr"] = "auto"  # onnx / openvino only
    normalize: Literal["auto", "none", "imagenet", "raw"] = "auto"  # onnx / openvino only
    names: list[str] | Literal["coco80", "coco91"] | None = None  # class names for exports
    model_license: str | None = None
    rfdetr_variant: Literal["nano", "small", "medium", "base", "large"] = "small"
    threads: int = Field(0, ge=0)  # CPU threads, 0 = runtime default


# --------------------------------------------------------------------------- tracking


class TrackerConfig(_Model):
    """ByteTrack settings plus our own track filtering."""

    track_activation_threshold: float = Field(0.5, ge=0, le=1)
    high_conf_det_threshold: float = Field(0.5, ge=0, le=1)
    lost_track_buffer: int = Field(30, ge=0)  # in 30 FPS frames
    minimum_iou_threshold: float = Field(0.1, ge=0, le=1)
    min_track_frames: int = Field(3, ge=1)  # a track counts only after this many hits
    class_smoothing_window: int = Field(15, ge=1)  # majority vote over the last N frames


# --------------------------------------------------------------------------- analytics


class LineConfig(_Model):
    """A counting line from p1 to p2.

    Stand at p1 and look towards p2 (as seen on the screen). ``to_right`` means: an object
    that moves from your left side to your right side counts as IN, the other way is OUT.
    """

    name: str = Field(min_length=1, max_length=64)
    p1: Point
    p2: Point
    in_direction: Literal["to_right", "to_left"] = "to_right"
    classes: list[str] | None = None  # None = all classes of the camera
    deadband_px: float = Field(4.0, ge=0)  # ignore jitter closer than this to the line

    @model_validator(mode="after")
    def _distinct(self) -> LineConfig:
        if tuple(self.p1) == tuple(self.p2):
            raise ValueError(f"line '{self.name}': p1 and p2 must be different points")
        return self


class ZoneConfig(_Model):
    """A polygon zone: occupancy and dwell time, plus queue length when kind is "queue"."""

    name: str = Field(min_length=1, max_length=64)
    polygon: list[Point] = Field(min_length=3)
    kind: Literal["area", "queue"] = "area"
    classes: list[str] | None = None
    exit_grace_s: float = Field(1.5, ge=0)  # a lost track stays "inside" this long
    min_visit_s: float = Field(1.0, ge=0)  # shorter visits are not reported
    queue_max_speed: float = Field(0.25, ge=0)  # box heights per second; slower = waiting
    queue_min_dwell_s: float = Field(3.0, ge=0)  # must be in the zone this long to be waiting


class HeatmapConfig(_Model):
    enabled: bool = True
    grid_width: int = Field(192, ge=16)
    save_interval_s: float = Field(300.0, gt=0)
    blur_sigma: float = Field(1.5, ge=0)


class SpeedConfig(_Model):
    """Two-point speed calibration: two image points that are ``distance_m`` metres apart.

    Approximation: one constant scale for the whole image. It is only reasonable when the
    camera looks roughly straight at the road or floor. Check it against a known speed.
    """

    p1: Point
    p2: Point
    distance_m: float = Field(gt=0)


class SchedulerConfig(_Model):
    """Frame scheduling. ``target_fps`` None means: every frame for files, 10 FPS for live."""

    target_fps: float | None = Field(None, gt=0)
    min_fps: float = Field(2.0, gt=0)
    adaptive: bool = True  # live only: lower the FPS when processing is too slow


class CameraConfig(_Model):
    id: str = Field("cam1", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str | None = None
    source: SourceConfig
    classes: list[str] = Field(default_factory=lambda: ["person"], min_length=1)
    coordinates: Literal["normalized", "pixel"] = "normalized"  # for lines, zones, speed
    anchor: Literal["bottom_center", "center"] = "bottom_center"
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    lines: list[LineConfig] = Field(default_factory=list)
    zones: list[ZoneConfig] = Field(default_factory=list)
    heatmap: HeatmapConfig = Field(default_factory=HeatmapConfig)
    speed: SpeedConfig | None = None
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)

    @model_validator(mode="after")
    def _check(self) -> CameraConfig:
        for label, items in (("line", self.lines), ("zone", self.zones)):
            names = [item.name for item in items]
            if len(set(names)) != len(names):
                raise ValueError(f"camera '{self.id}': {label} names must be unique")
        if self.coordinates == "normalized":
            points: list[Point] = []
            for line in self.lines:
                points += [line.p1, line.p2]
            for zone in self.zones:
                points += list(zone.polygon)
            if self.speed:
                points += [self.speed.p1, self.speed.p2]
            for x, y in points:
                if not (-0.01 <= x <= 1.01 and -0.01 <= y <= 1.01):
                    raise ValueError(
                        f"camera '{self.id}': point ({x}, {y}) is outside 0..1. "
                        "Use coordinates: pixel if you want pixel values."
                    )
        return self

    def effective_target_fps(self) -> float | None:
        """Target FPS after applying the live default. None = process every frame."""
        if self.scheduler.target_fps is not None:
            return self.scheduler.target_fps
        return 10.0 if self.source.is_live else None


# --------------------------------------------------------------------------- top level


class PrivacyConfig(_Model):
    """Privacy by design. Only numbers are ever stored; these settings control the picture.

    preview:      picture in the local web app. "blur" (default) pixelates every detected
                  person/vehicle and lowers the resolution to 640 px, "off" shows no picture
                  at all, "full" shows the camera as it is (only for setup, with permission).
    preview_max_width: the preview is never wider than this.
    snapshots:    allow "countvision-edge snapshot" (one frame saved locally, for drawing
                  lines). Set false on a site where nobody may store any picture.
    """

    preview: Literal["blur", "off", "full"] = "blur"
    preview_max_width: int = Field(960, ge=160, le=3840)
    snapshots: bool = True


class AlertsConfig(_Model):
    """When to raise an alert. Alerts are written to the log, to status.json and as events
    (kind ``camera_offline`` / ``camera_online``) that the cloud will send on (phase 4)."""

    camera_offline_after_s: float = Field(300.0, gt=0)  # target: alert if offline > 5 min


class CloudConfig(_Model):
    """Upload numbers to the CountVision cloud (only after "countvision-edge pair").

    enabled:            set false to keep everything local even when paired.
    url:                override the cloud address saved at pairing (env CV_CLOUD_URL), e.g.
                        http://host.docker.internal:3000 when the agent runs in Docker.
    upload_interval_s:  how often to upload (also the "I am alive" signal; the cloud shows a
                        device offline after 90 s without contact).
    batch_rows:         max rows per table and upload. A backlog after an outage is sent in
                        several uploads one after the other.
    """

    enabled: bool = True
    url: str | None = None
    upload_interval_s: float = Field(15.0, ge=2, le=60)
    batch_rows: int = Field(1000, ge=10, le=5000)
    timeout_s: float = Field(20.0, gt=0)


class StorageConfig(_Model):
    db_path: str | None = None  # default: <data_dir>/countvision.db
    retention_days: int = Field(30, ge=1)  # numbers older than this are deleted
    heartbeat_retention_hours: int = Field(48, ge=1)


class EdgeConfig(_Model):
    device_id: str | None = None  # default: random id stored in <data_dir>/device_id
    data_dir: str = "data"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    heartbeat_interval_s: float = Field(10.0, gt=0)
    status_interval_s: float = Field(5.0, gt=0)  # how often <data_dir>/status.json is written
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    cameras: list[CameraConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_cameras(self) -> EdgeConfig:
        ids = [camera.id for camera in self.cameras]
        if len(set(ids)) != len(ids):
            raise ValueError("camera ids must be unique")
        return self

    def db_file(self) -> Path:
        return Path(self.storage.db_path) if self.storage.db_path else Path(self.data_dir) / (
            "countvision.db"
        )

    def resolve_device_id(self) -> str:
        """The configured id, or a random id that is created once and kept in data_dir."""
        return self.device_id or device_id_for(self.data_dir)

    def camera(self, camera_id: str | None = None) -> CameraConfig:
        """Return one camera. With no id, the config must contain exactly one camera."""
        if camera_id is None:
            if len(self.cameras) != 1:
                ids = ", ".join(c.id for c in self.cameras)
                raise ConfigError(
                    f"The config has several cameras ({ids}). Choose one with --camera. "
                    "(countvision-edge run without --show runs all cameras together.)"
                )
            return self.cameras[0]
        for camera in self.cameras:
            if camera.id == camera_id:
                return camera
        raise ConfigError(f"No camera with id '{camera_id}' in the config.")


# --------------------------------------------------------------------------- helpers


def device_id_for(data_dir: str | Path) -> str:
    """The random device id kept in <data_dir>/device_id (created on first use)."""
    path = Path(data_dir) / "device_id"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = f"edge-{uuid.uuid4().hex[:12]}"
    path.write_text(value + "\n", encoding="utf-8")
    return value


def redact_uri(uri: str) -> str:
    """Hide ``user:password@`` in a URL so it can be logged."""
    return _URI_CREDENTIALS.sub(r"\1***@", uri)


def expand_env(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Replace ``${VAR}`` and ``${VAR:-default}`` in all strings of a nested structure."""
    environ = os.environ if env is None else env
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in environ:
                return environ[name]
            if default is not None:
                return default
            raise ConfigError(f"Environment variable {name} is used in the config but not set.")

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [expand_env(item, environ) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item, environ) for key, item in value.items()}
    return value


def parse_config(data: Mapping[str, Any], env: Mapping[str, str] | None = None) -> EdgeConfig:
    """Validate a config dict. Environment overrides: CV_DATA_DIR, CV_DB_PATH, CV_LOG_LEVEL,
    CV_DEVICE_ID, CV_PREVIEW (blur | off | full), CV_CLOUD_URL."""
    environ = os.environ if env is None else env
    expanded = expand_env(dict(data), environ)
    overrides = {
        "CV_DATA_DIR": ("data_dir",),
        "CV_LOG_LEVEL": ("log_level",),
        "CV_DEVICE_ID": ("device_id",),
        "CV_DB_PATH": ("storage", "db_path"),
        "CV_PREVIEW": ("privacy", "preview"),
        "CV_CLOUD_URL": ("cloud", "url"),
    }
    for variable, path in overrides.items():
        if environ.get(variable):
            target = expanded
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = environ[variable]
    try:
        return EdgeConfig.model_validate(expanded)
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        raise ConfigError(f"Invalid config:\n{exc}") from exc


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> EdgeConfig:
    """Read and validate a YAML config file."""
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Config file not found: {file}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Config file must contain a YAML mapping at the top level.")
    return parse_config(data, env)
