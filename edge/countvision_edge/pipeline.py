"""Camera pipeline: frames in, numbers out.

frame -> scheduler -> detector -> class filter -> ByteTrack -> analytics -> aggregator -> SQLite

No frame leaves this process. The only things that are stored are events and aggregates.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .aggregator import MinuteAggregator
from .analytics import AnalyticsEngine
from .config import CameraConfig
from .detectors.base import Detector
from .errors import EndOfStream
from .heartbeat import HeartbeatMonitor
from .inputs.base import FrameSource, SourceStatus
from .scheduler import FrameScheduler
from .storage import SqliteBuffer
from .tracking import TrackManager
from .types import Detections, Event, Frame, TrackedObject

log = logging.getLogger(__name__)

MAX_SAMPLE_DT_S = 2.0  # a gap longer than this (camera offline) is not counted as measured time
PURGE_INTERVAL_S = 3600.0


@dataclass
class FrameResult:
    """What happened in one processed frame."""

    frame: Frame
    detections: int
    tracks: list[TrackedObject]
    events: list[Event]
    proc_ms: float
    # Boxes the detector found in this frame (after the class filter), including objects that
    # are not yet confirmed tracks. Used only to pixelate people in the preview.
    boxes: np.ndarray = field(default_factory=lambda: np.zeros((0, 4), dtype=np.float32))


@dataclass
class RunSummary:
    frames_read: int = 0
    frames_processed: int = 0
    frames_skipped: int = 0
    frames_dropped: int = 0
    events: int = 0
    wall_s: float = 0.0
    state: dict = field(default_factory=dict)

    @property
    def processed_fps(self) -> float:
        return self.frames_processed / self.wall_s if self.wall_s > 0 else 0.0


class CameraPipeline:
    """Runs the full chain for one camera."""

    def __init__(
        self,
        cam: CameraConfig,
        detector: Detector,
        buffer: SqliteBuffer,
        *,
        data_dir: str | Path = "data",
        heartbeat_interval_s: float = 10.0,
    ) -> None:
        self.cam = cam
        self.detector = detector
        self.buffer = buffer
        self.data_dir = Path(data_dir)
        self._class_ids = detector.class_ids(cam.classes)
        self.scheduler = FrameScheduler(
            cam.effective_target_fps(), min_fps=cam.scheduler.min_fps, adaptive=cam.scheduler.adaptive
        )
        self.tracker = self._make_tracker(cam.effective_target_fps() or 15.0)
        self.aggregator = MinuteAggregator(cam.id, buffer)
        self.engine: AnalyticsEngine | None = None
        self._status_fn: Callable[[], SourceStatus] | None = None
        self.heartbeat = HeartbeatMonitor(
            cam.id, heartbeat_interval_s, buffer, status_fn=lambda: self._status()
        )
        self._last_ts: float | None = None
        self._last_index: int | None = None
        self._nominal_dt = 0.1
        self._next_heatmap_save: float | None = None
        self._last_purge = 0.0
        self._is_live = cam.source.is_live
        self._finished = False
        self._pending_cam: CameraConfig | None = None
        self._cam_lock = threading.Lock()
        self.frames_read = 0
        self.events_total = 0
        self.paused = False  # outside the camera's schedule (opening hours)
        self.last_frame: Frame | None = None  # newest frame read (memory only; for snapshots)
        self._schedule_check: tuple[int, bool] = (-1, True)  # (second, active) cache

    # -- setup --------------------------------------------------------------------------

    def _make_tracker(self, frame_rate: float) -> TrackManager:
        return TrackManager(self.cam.tracker, self.detector.names, frame_rate)

    def _status(self) -> SourceStatus:
        if self._status_fn is not None:
            return self._status_fn()
        return SourceStatus(connected=True)

    def _configure_for_source(self, source: FrameSource) -> None:
        self._status_fn = source.status
        self.set_stream_rate(self.scheduler.target_fps or source.nominal_fps() or 15.0)

    def set_stream_rate(self, fps: float) -> None:
        """Tell the tracker how many frames per second it will get. ``run()`` does this from
        the source; callers that feed frames to ``process()`` themselves (evaluation) call it
        before the first frame."""
        self.tracker = self._make_tracker(fps)
        self._nominal_dt = 1.0 / fps

    # -- one frame ----------------------------------------------------------------------

    def reconfigure(self, cam: CameraConfig) -> None:
        """Use new lines and zones from the next frame on. Thread-safe.

        Only geometry is meant to change (lines, zones, anchor, speed). Line totals are kept
        for lines with the same name; open zone visits are closed and written first.
        """
        with self._cam_lock:
            self._pending_cam = cam

    def _apply_pending(self, frame_size: tuple[int, int]) -> None:
        with self._cam_lock:
            cam, self._pending_cam = self._pending_cam, None
        if cam is None:
            return
        old = self.engine
        if cam.classes != self.cam.classes:
            self._class_ids = self.detector.class_ids(cam.classes)
        self.cam = cam
        self._schedule_check = (-1, True)  # the schedule may have changed
        if old is None:
            return
        closed = old.finish()
        for event in closed:
            self.aggregator.add_event(event)
        self.buffer.add_events(closed)
        self.events_total += len(closed)
        engine = AnalyticsEngine(cam, frame_size)
        totals = {counter.name: counter.totals for counter in old.lines}
        for counter in engine.lines:
            if counter.name in totals:
                counter.totals = totals[counter.name]
        if old.heatmap is not None and engine.heatmap is not None and old.frame_size == frame_size:
            engine.heatmap = old.heatmap
        self.engine = engine
        log.info(
            "Camera %s: new geometry (%d lines, %d zones)", cam.id, len(cam.lines), len(cam.zones)
        )

    def is_active(self, ts: float) -> bool:
        """Inside the schedule? (checked at most once per second)"""
        schedule = self.cam.schedule
        if schedule is None:
            return True
        second = int(ts)
        if self._schedule_check[0] != second:
            self._schedule_check = (second, schedule.is_active(ts))
        return self._schedule_check[1]

    def process(self, frame: Frame) -> FrameResult:
        """Run detection, tracking and analytics on one frame (no scheduling)."""
        started = time.perf_counter()
        if self._pending_cam is not None:
            self._apply_pending(frame.size)
        if self.engine is None:
            self.engine = AnalyticsEngine(self.cam, frame.size)
            log.info("Camera %s: analysing %dx%d frames", self.cam.id, *frame.size)
        else:
            self.engine.set_frame_size(frame.size)

        detections = self._filter(self.detector.detect(frame.image))
        tracks = self.tracker.update(detections, frame.media_ts, frame.ts)
        if self._last_ts is None:
            dt = self._nominal_dt
        else:
            dt = min(max(frame.ts - self._last_ts, 0.0), MAX_SAMPLE_DT_S)
        self._last_ts = frame.ts

        events, sample = self.engine.update(tracks, frame.ts, dt)
        self.aggregator.add_sample(sample)
        for event in events:
            self.aggregator.add_event(event)
        self.buffer.add_events(events)
        self.events_total += len(events)
        self._maybe_save_heatmap(frame.ts)

        elapsed = time.perf_counter() - started
        self.scheduler.record_processing(elapsed)
        self.heartbeat.frame_processed(elapsed)
        return FrameResult(frame, len(detections), tracks, events, elapsed * 1000.0, detections.xyxy)

    def _filter(self, detections: Detections) -> Detections:
        if self._class_ids is None or len(detections) == 0:
            return detections
        return detections.select(np.isin(detections.class_id, list(self._class_ids)))

    # -- stream loop --------------------------------------------------------------------

    def run(
        self,
        source: FrameSource,
        stop: threading.Event | None = None,
        *,
        max_frames: int | None = None,
        on_result: Callable[[FrameResult], None] | None = None,
    ) -> RunSummary:
        """Process a source until it ends, ``stop`` is set, or ``max_frames`` were processed."""
        stop = stop or threading.Event()
        started = time.perf_counter()
        source.open()
        try:
            self._configure_for_source(source)
            self.detector.warmup()
            self._maybe_purge()
            while not stop.is_set():
                try:
                    frame = source.read(timeout=1.0)
                except EndOfStream:
                    break
                if frame is None:  # live source: no new frame yet (camera down or slow)
                    self.heartbeat.maybe_emit()
                    continue
                self.frames_read += 1
                self.last_frame = frame
                if self._last_index is not None:
                    gap = frame.index - self._last_index - 1
                    if gap > 0 and self._is_live:
                        self.heartbeat.frames_dropped(gap)
                self._last_index = frame.index
                if self._pending_cam is not None:  # also while paused (a new schedule)
                    self._apply_pending(frame.size)
                active = self.is_active(frame.ts)
                if active != (not self.paused):
                    self.paused = not active
                    log.info("Camera %s: %s (schedule)", self.cam.id,
                             "paused" if self.paused else "counting again")
                if self.paused:
                    self.heartbeat.frame_skipped()
                    self.heartbeat.maybe_emit()
                    continue
                if not self.scheduler.should_process(frame.media_ts):
                    self.heartbeat.frame_skipped()
                    self.heartbeat.maybe_emit()
                    continue
                result = self.process(frame)
                if on_result is not None:
                    on_result(result)
                self.heartbeat.maybe_emit()
                self._maybe_purge()
                if max_frames is not None and self.heartbeat.processed >= max_frames:
                    break
        finally:
            self.finish()
            source.close()
        return RunSummary(
            frames_read=self.frames_read,
            frames_processed=self.heartbeat.processed,
            frames_skipped=self.heartbeat.skipped,
            frames_dropped=self.heartbeat.dropped,
            events=self.events_total,
            wall_s=time.perf_counter() - started,
            state=self.state(),
        )

    # -- end ----------------------------------------------------------------------------

    def finish(self) -> None:
        """Close open zone visits, write the last minute, save the heatmap. Safe to repeat."""
        if self._finished:
            return
        self._finished = True
        if self.engine is not None:
            events = self.engine.finish()
            for event in events:
                self.aggregator.add_event(event)
            self.buffer.add_events(events)
            self.events_total += len(events)
            self._save_heatmap()
        self.aggregator.close()
        self.heartbeat.maybe_emit(force=True)

    # -- state --------------------------------------------------------------------------

    def state(self) -> dict:
        """Current numbers, JSON-serialisable."""
        analytics = self.engine.snapshot() if self.engine else {"lines": {}, "zones": {}}
        return {
            "camera_id": self.cam.id,
            "frames_processed": self.heartbeat.processed,
            "frames_skipped": self.heartbeat.skipped,
            "frames_dropped": self.heartbeat.dropped,
            "effective_fps": self.scheduler.effective_fps,
            **analytics,
        }

    # -- housekeeping -------------------------------------------------------------------

    def _maybe_save_heatmap(self, ts: float) -> None:
        if self.engine is None or self.engine.heatmap is None:
            return
        if self._next_heatmap_save is None:
            self._next_heatmap_save = ts + self.cam.heatmap.save_interval_s
        elif ts >= self._next_heatmap_save:
            self._save_heatmap()
            self._next_heatmap_save = ts + self.cam.heatmap.save_interval_s

    def _save_heatmap(self) -> None:
        if self.engine is None or self.engine.heatmap is None:
            return
        try:
            self.engine.heatmap.save(self.data_dir / "heatmaps", self.cam.id)
        except OSError:
            log.warning("Could not save the heatmap", exc_info=True)

    def _maybe_purge(self) -> None:
        """Delete data past the retention time. Only for live runs: replaying an old video
        file must not delete the numbers it just wrote."""
        if not self._is_live:
            return
        now = time.monotonic()
        if self._last_purge == 0.0 or now - self._last_purge >= PURGE_INTERVAL_S:
            self._last_purge = now
            self.buffer.purge()
