"""Tracking: ByteTrack plus class smoothing and a minimum track length.

ByteTrack comes from the ``trackers`` package (Apache-2.0, from Roboflow). The older
``supervision.ByteTrack`` is deprecated and is removed in supervision 0.31.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

from .config import TrackerConfig
from .types import Detections, TrackedObject

log = logging.getLogger(__name__)

_STATE_TTL_S = 30.0


@dataclass
class _TrackState:
    first_ts: float
    last_ts: float
    hits: int = 0
    votes: deque = field(default_factory=deque)  # (class_id, confidence)


class TrackManager:
    """Wraps ByteTrack. Returns only confirmed tracks with a smoothed class."""

    def __init__(self, cfg: TrackerConfig, names: dict[int, str], frame_rate: float) -> None:
        self.cfg = cfg
        self.names = names
        self._frame_rate = frame_rate
        self._states: dict[int, _TrackState] = {}
        self._tracker = self._new_tracker()

    def _new_tracker(self) -> ByteTrackTracker:
        return ByteTrackTracker(
            lost_track_buffer=self.cfg.lost_track_buffer,
            frame_rate=self._frame_rate,
            track_activation_threshold=self.cfg.track_activation_threshold,
            minimum_consecutive_frames=1,  # we apply our own min_track_frames
            minimum_iou_threshold=self.cfg.minimum_iou_threshold,
            high_conf_det_threshold=self.cfg.high_conf_det_threshold,
        )

    def reset(self) -> None:
        self._states.clear()
        self._tracker = self._new_tracker()

    def update(self, detections: Detections, media_ts: float, ts: float) -> list[TrackedObject]:
        """Feed one frame of detections. ``media_ts`` is stream time, ``ts`` absolute time."""
        sv_det = sv.Detections(
            xyxy=detections.xyxy.astype(np.float32),
            confidence=detections.confidence.astype(np.float32),
            class_id=detections.class_id.astype(int),
        )
        out = self._tracker.update(sv_det, timestamp=media_ts)
        tracked: list[TrackedObject] = []
        if out.tracker_id is not None and len(out) > 0:
            conf = out.confidence if out.confidence is not None else np.ones(len(out))
            class_ids = out.class_id if out.class_id is not None else np.zeros(len(out), dtype=int)
            for i in range(len(out)):
                track_id = int(out.tracker_id[i])
                if track_id < 0:
                    continue
                state = self._states.get(track_id)
                if state is None:
                    state = _TrackState(first_ts=ts, last_ts=ts)
                    state.votes = deque(maxlen=self.cfg.class_smoothing_window)
                    self._states[track_id] = state
                state.hits += 1
                state.last_ts = ts
                state.votes.append((int(class_ids[i]), float(conf[i])))
                if state.hits < self.cfg.min_track_frames:
                    continue
                class_id = self._vote(state)
                tracked.append(
                    TrackedObject(
                        track_id=track_id,
                        xyxy=tuple(float(v) for v in out.xyxy[i]),  # type: ignore[arg-type]
                        class_id=class_id,
                        class_name=self.names.get(class_id, str(class_id)),
                        confidence=float(conf[i]),
                        hits=state.hits,
                        age_s=ts - state.first_ts,
                    )
                )
        self._prune(ts)
        tracked.sort(key=lambda t: t.track_id)
        return tracked

    @staticmethod
    def _vote(state: _TrackState) -> int:
        """Class with the highest summed confidence over the window (latest wins ties)."""
        scores: dict[int, float] = {}
        for class_id, confidence in state.votes:
            scores[class_id] = scores.get(class_id, 0.0) + confidence
        best = max(scores.values())
        for class_id, _ in reversed(state.votes):
            if scores[class_id] >= best - 1e-9:
                return class_id
        return state.votes[-1][0]

    def _prune(self, ts: float) -> None:
        stale = [tid for tid, s in self._states.items() if ts - s.last_ts > _STATE_TTL_S]
        for tid in stale:
            del self._states[tid]
