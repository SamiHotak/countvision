"""Analytics engine: runs motion, lines, zones and the heatmap for one camera."""

from __future__ import annotations

import logging

from ..config import CameraConfig
from ..geometry import to_pixels
from ..types import Event, FrameSample, TrackedObject
from .heatmap import Heatmap
from .lines import LineCounter
from .motion import MotionEstimator, SpeedCalibration
from .zones import ZoneMonitor

log = logging.getLogger(__name__)


class AnalyticsEngine:
    """All per-camera analytics. Geometry is configured in the camera's coordinate mode and
    converted to pixels for the real frame size, so it survives a resolution change."""

    def __init__(self, cam: CameraConfig, frame_size: tuple[int, int]) -> None:
        self.cam = cam
        self.frame_size = frame_size
        base = set(cam.classes)
        self.lines: list[LineCounter] = []
        for line in cam.lines:
            self.lines.append(
                LineCounter(
                    line.name,
                    self._px(line.p1),
                    self._px(line.p2),
                    camera_id=cam.id,
                    in_direction=line.in_direction,
                    allowed_classes=set(line.classes) & base if line.classes else base,
                    deadband_px=line.deadband_px,
                    anchor=cam.anchor,
                )
            )
        self.zones: list[ZoneMonitor] = []
        for zone in cam.zones:
            self.zones.append(
                ZoneMonitor(
                    zone.name,
                    [self._px(p) for p in zone.polygon],
                    camera_id=cam.id,
                    kind=zone.kind,
                    allowed_classes=set(zone.classes) & base if zone.classes else base,
                    anchor=cam.anchor,
                    exit_grace_s=zone.exit_grace_s,
                    min_visit_s=zone.min_visit_s,
                    queue_max_speed=zone.queue_max_speed,
                    queue_min_dwell_s=zone.queue_min_dwell_s,
                )
            )
        calibration = None
        if cam.speed is not None:
            calibration = SpeedCalibration(
                self._px(cam.speed.p1), self._px(cam.speed.p2), cam.speed.distance_m
            )
        self.motion = MotionEstimator(calibration=calibration)
        self.heatmap: Heatmap | None = (
            Heatmap(frame_size, cam.heatmap.grid_width, cam.heatmap.blur_sigma)
            if cam.heatmap.enabled
            else None
        )

    def _px(self, point) -> tuple[float, float]:
        return to_pixels(point, self.frame_size, self.cam.coordinates)

    def set_frame_size(self, frame_size: tuple[int, int]) -> None:
        """Re-resolve the geometry after the stream changed resolution. Counters are kept."""
        if frame_size == self.frame_size:
            return
        log.info("Frame size changed %s -> %s, rebuilding geometry", self.frame_size, frame_size)
        self.frame_size = frame_size
        for counter, line in zip(self.lines, self.cam.lines, strict=True):
            counter.set_geometry(self._px(line.p1), self._px(line.p2))
        for monitor, zone in zip(self.zones, self.cam.zones, strict=True):
            monitor.set_geometry([self._px(p) for p in zone.polygon])
        if self.cam.speed is not None:
            self.motion.calibration = SpeedCalibration(
                self._px(self.cam.speed.p1), self._px(self.cam.speed.p2), self.cam.speed.distance_m
            )
        if self.heatmap is not None:
            self.heatmap.set_frame_size(frame_size)

    def update(
        self, tracks: list[TrackedObject], ts: float, dt: float
    ) -> tuple[list[Event], FrameSample]:
        """Process one frame of confirmed tracks."""
        self.motion.update(tracks, ts)
        events: list[Event] = []
        for counter in self.lines:
            events.extend(counter.update(tracks, ts))
        sample = FrameSample(ts=ts, dt=dt)
        for monitor in self.zones:
            zone_events, occupancy, queue = monitor.update(tracks, ts)
            events.extend(zone_events)
            sample.zone_occupancy[monitor.name] = dict(occupancy)
            sample.zone_queue[monitor.name] = dict(queue)
        if self.heatmap is not None and tracks:
            self.heatmap.add([t.anchor(self.cam.anchor) for t in tracks], dt)
        return events, sample

    def finish(self) -> list[Event]:
        """End of stream: close open zone visits."""
        events: list[Event] = []
        for monitor in self.zones:
            events.extend(monitor.flush())
        return events

    def snapshot(self) -> dict:
        """Current numbers, JSON-serialisable (used by the local app in session B)."""
        return {
            "lines": {c.name: c.summary() for c in self.lines},
            "zones": {m.name: m.summary() for m in self.zones},
        }
