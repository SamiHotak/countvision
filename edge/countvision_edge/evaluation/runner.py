"""Run the real counting pipeline on labeled clips and score it.

The same ``CameraPipeline`` that runs live is used here: same tracker, same line and zone
logic. Only the detector is replaced by a replay of cached detections, and frames are
picked the way the live scheduler picks them (default 10 FPS), so the numbers describe the
live product, not an offline best case.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import yaml

from ..config import CameraConfig, DetectorConfig, SourceConfig, TrackerConfig
from ..detectors import build_detector
from ..pipeline import CameraPipeline
from ..scheduler import FrameScheduler
from ..storage import SqliteBuffer
from ..types import Frame
from .cache import CONF_FLOOR, DetectionCache, ReplayDetector, probe_video
from .labels import ClipLabels
from .metrics import (
    Crossing,
    CrossingMetrics,
    KeyRow,
    OccupancyMetrics,
    compare_crossings,
    value_at,
)
from .specs import spec_key

log = logging.getLogger(__name__)

START_TS = 1_700_000_000.0  # fixed fake wall-clock start, so runs are reproducible


@dataclass
class EvalParams:
    """Everything that tuning may change. Defaults = the shipped defaults ("before")."""

    conf: float = 0.2
    track_activation_threshold: float = 0.5
    high_conf_det_threshold: float = 0.5
    lost_track_buffer: int = 30
    minimum_iou_threshold: float = 0.1
    min_track_frames: int = 3
    class_smoothing_window: int = 15
    deadband_px: float = 4.0
    fps: float | None = 10.0  # None = every frame of the file

    @classmethod
    def from_dict(cls, data: dict | None) -> EvalParams:
        data = dict(data or {})
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")
        return cls(**data)

    @classmethod
    def load(cls, path: str | Path) -> EvalParams:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(data.get("params", data))

    def to_dict(self) -> dict:
        return asdict(self)

    def tracker(self) -> TrackerConfig:
        return TrackerConfig(
            track_activation_threshold=self.track_activation_threshold,
            high_conf_det_threshold=self.high_conf_det_threshold,
            lost_track_buffer=self.lost_track_buffer,
            minimum_iou_threshold=self.minimum_iou_threshold,
            min_track_frames=self.min_track_frames,
            class_smoothing_window=self.class_smoothing_window,
        )


@dataclass
class ClipResult:
    clip: str
    split: str
    scene: str
    detector: str
    params: dict
    frames_processed: int
    duration_s: float
    crossings: dict
    occupancy: dict
    counted_events: list[dict] = field(default_factory=list)
    detect_ms_mean: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def scheduled_indices(n_frames: int, video_fps: float, target_fps: float | None) -> list[int]:
    """Frame indices the live scheduler would process (adaptive off = a fast enough device)."""
    scheduler = FrameScheduler(target_fps, adaptive=False)
    return [i for i in range(n_frames) if scheduler.should_process(i / video_fps)]


def camera_for_clip(labels: ClipLabels, params: EvalParams, video: Path) -> CameraConfig:
    lines = [line.model_copy(update={"deadband_px": params.deadband_px}) for line in labels.lines]
    return CameraConfig(
        id=labels.clip,
        source=SourceConfig(uri=str(video), kind="file"),
        classes=labels.classes,
        coordinates="normalized",
        anchor=labels.anchor,
        tracker=params.tracker(),
        lines=lines,
        zones=labels.zones,
        heatmap={"enabled": False},
        scheduler={"target_fps": params.fps, "adaptive": False},
    )


class ClipRunner:
    """Prepares the detection cache of one clip and replays it with any parameters."""

    def __init__(
        self,
        labels: ClipLabels,
        video: str | Path,
        detector_spec: str,
        detector_cfg: DetectorConfig,
        cache_dir: str | Path,
    ) -> None:
        self.labels = labels
        self.video = Path(video)
        self.spec = detector_spec
        self.detector_cfg = detector_cfg
        self.info = probe_video(self.video)
        cache_path = Path(cache_dir) / spec_key(detector_spec) / f"{labels.clip}.npz"
        self.cache = DetectionCache.open(cache_path, self.info) or DetectionCache(
            path=cache_path, info=self.info, names={}
        )

    def prepare(
        self,
        fps_values: list[float | None],
        detector_factory: Callable[[], object] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Make sure every frame needed for these FPS values is cached. Returns frames detected."""
        needed: set[int] = set()
        for fps in fps_values:
            needed.update(scheduled_indices(self.info.n_frames, self.info.fps, fps))
        missing = self.cache.missing(needed)
        if not missing:
            return 0
        factory = detector_factory or (
            lambda: build_detector(self.detector_cfg.model_copy(update={"conf": CONF_FLOOR}))
        )
        detector = factory()
        try:
            detector.warmup()
            done = self.cache.fill(self.video, missing, detector, progress)
        finally:
            detector.close()
            self.cache.save()  # also after Ctrl+C: the next run continues where this one stopped
        return done

    def run(self, params: EvalParams, tolerance_s: float = 2.0, keep_events: bool = False) -> ClipResult:
        """Replay the cached detections through the real pipeline and score the result."""
        labels = self.labels
        cam = camera_for_clip(labels, params, self.video)
        indices = scheduled_indices(self.info.n_frames, self.info.fps, params.fps)
        missing = self.cache.missing(indices)
        if missing:
            raise RuntimeError(f"{labels.clip}: {len(missing)} frames are not cached; call prepare()")
        detector = ReplayDetector(self.cache, params.conf)
        dummy = np.broadcast_to(np.zeros((1, 1, 1), np.uint8), (self.info.height, self.info.width, 3))
        # (zone, class or None for all classes) -> [(media time, occupancy), ...]
        zone_series: dict[tuple[str, str | None], list[tuple[float, int]]] = {}
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            buffer = SqliteBuffer(":memory:")
            pipeline = CameraPipeline(cam, detector, buffer, data_dir=tmp, heartbeat_interval_s=1e9)
            pipeline.set_stream_rate(params.fps or self.info.fps)
            for index in indices:
                media_ts = index / self.info.fps
                detector.index = index
                result = pipeline.process(Frame(dummy, index, START_TS + media_ts, media_ts))
                events.extend(result.events)
                for monitor in pipeline.engine.zones if pipeline.engine else []:
                    summary = monitor.summary()
                    zone_series.setdefault((monitor.name, None), []).append(
                        (media_ts, int(summary["occupancy"]))
                    )
                    for cls in labels.classes:
                        zone_series.setdefault((monitor.name, cls), []).append(
                            (media_ts, int(summary["by_class"].get(cls, 0)))
                        )
            pipeline.finish()
            buffer.close()

        counted = [
            Crossing(e.ts - START_TS, e.name, e.direction or "", e.class_name)
            for e in events
            if e.kind == "line_cross" and labels.in_labeled_range(e.ts - START_TS)
        ]
        truth = [
            Crossing(c.t, c.line, c.dir, c.cls)
            for c in labels.crossings
            if labels.in_labeled_range(c.t)
        ]
        crossing_metrics = compare_crossings(truth, counted, tolerance_s)
        occupancy = OccupancyMetrics()
        for sample in labels.occupancy:
            occupancy.add(sample.n, value_at(zone_series.get((sample.zone, sample.cls), []), sample.t))
        duration = labels.labeled_until_s or self.info.duration_s
        return ClipResult(
            clip=labels.clip,
            split=labels.split,
            scene=labels.scene,
            detector=self.spec,
            params=params.to_dict(),
            frames_processed=len(indices),
            duration_s=round(min(duration, self.info.duration_s), 2),
            crossings=crossing_metrics.to_dict(),
            occupancy=occupancy.to_dict(),
            counted_events=[asdict(c) for c in counted] if keep_events else [],
            detect_ms_mean=self.cache.detect_ms_mean,
        )


def aggregate(results: list[ClipResult]) -> dict:
    """Micro-averaged totals over clips (every event weighs the same)."""
    crossings = CrossingMetrics()
    occupancy = OccupancyMetrics()
    for r in results:
        crossings.rows.extend(KeyRow(**row) for row in r.crossings["rows"])
        occupancy.merge(OccupancyMetrics.from_dict(r.occupancy))
    return {
        "clips": len(results),
        "frames": sum(r.frames_processed for r in results),
        "crossings": crossings.to_dict() | {"rows": []},
        "occupancy": occupancy.to_dict(),
    }


def score(summary: dict) -> tuple[float, float, float]:
    """Tuning objective: count accuracy first, then event F1, then low occupancy error."""
    c = summary["crossings"]
    occ = summary["occupancy"]["mean_error"]
    return (
        c["count_accuracy"] if c["count_accuracy"] is not None else 0.0,
        c["f1"] if c["f1"] is not None else 0.0,
        -(occ if occ is not None else 0.0),
    )
