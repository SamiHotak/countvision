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
    first_xyxy: tuple[float, float, float, float] | None = None
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
        # Boxes of the previous frame that ByteTrack did not give an id yet (a new track
        # gets its id one frame later). Used to find where a new track really started.
        self._unassigned: np.ndarray = np.zeros((0, 4), dtype=np.float32)

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
        self._unassigned = np.zeros((0, 4), dtype=np.float32)

    def update(self, detections: Detections, media_ts: float, ts: float) -> list[TrackedObject]:
        """Feed one frame of detections. ``media_ts`` is stream time, ``ts`` absolute time."""
        sv_det = sv.Detections(
            xyxy=detections.xyxy.astype(np.float32),
            confidence=detections.confidence.astype(np.float32),
            class_id=detections.class_id.astype(int),
        )
        out = self._tracker.update(sv_det, timestamp=media_ts)
        tracked: list[TrackedObject] = []
        previous_unassigned, unassigned = self._unassigned, []
        if out.tracker_id is not None and len(out) > 0:
            conf = out.confidence if out.confidence is not None else np.ones(len(out))
            class_ids = out.class_id if out.class_id is not None else np.zeros(len(out), dtype=int)
            for i in range(len(out)):
                track_id = int(out.tracker_id[i])
                if track_id < 0:
                    unassigned.append(out.xyxy[i])
                    continue
                state = self._states.get(track_id)
                if state is None:
                    first = _best_overlap(out.xyxy[i], previous_unassigned)
                    state = _TrackState(
                        first_ts=ts, last_ts=ts,
                        first_xyxy=tuple(float(v) for v in first),  # type: ignore[arg-type]
                    )
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
                        first_xyxy=state.first_xyxy,
                    )
                )
        self._unassigned = (
            np.asarray(unassigned, dtype=np.float32).reshape(-1, 4) if unassigned
            else np.zeros((0, 4), dtype=np.float32)
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


def _best_overlap(box: np.ndarray, candidates: np.ndarray, min_iou: float = 0.2) -> np.ndarray:
    """The candidate box with the highest IoU to ``box`` (at least ``min_iou``), else ``box``."""
    if len(candidates) == 0:
        return box
    x1 = np.maximum(box[0], candidates[:, 0])
    y1 = np.maximum(box[1], candidates[:, 1])
    x2 = np.minimum(box[2], candidates[:, 2])
    y2 = np.minimum(box[3], candidates[:, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area = (box[2] - box[0]) * (box[3] - box[1])
    areas = (candidates[:, 2] - candidates[:, 0]) * (candidates[:, 3] - candidates[:, 1])
    iou = inter / np.maximum(area + areas - inter, 1e-9)
    best = int(np.argmax(iou))
    return candidates[best] if iou[best] >= min_iou else box
