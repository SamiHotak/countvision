"""Detection cache: run the slow detector ONCE per clip, then replay it many times.

Tuning tries hundreds of tracker/threshold settings. The detector output does not depend on
those settings (except the confidence threshold, which is applied on replay), so detections
are stored with a low confidence floor in one ``.npz`` file per clip and detector:

    <cache_dir>/<detector key>/<clip>.npz

A replay needs no video decoding at all, which makes a full tuning grid take minutes on a
laptop CPU instead of hours. The cache is only numbers (boxes, scores, classes).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..detectors.base import Detector, DetectorInfo
from ..errors import SourceError
from ..types import Detections

log = logging.getLogger(__name__)

CACHE_VERSION = 1
CONF_FLOOR = 0.05  # detections below this are never stored


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    n_frames: int
    file_size: int

    @property
    def duration_s(self) -> float:
        return self.n_frames / self.fps if self.fps > 0 else 0.0


def probe_video(path: str | Path) -> VideoInfo:
    """Size, frame rate and frame count. The frame count is checked by decoding when the
    container does not report it."""
    file = Path(path)
    if not file.is_file():
        raise SourceError(f"Video file not found: {file}")
    cap = cv2.VideoCapture(str(file))
    if not cap.isOpened():
        raise SourceError(f"Could not open video: {file}")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if count <= 0:
            count = 0
            while cap.grab():
                count += 1
    finally:
        cap.release()
    return VideoInfo(width, height, fps, count, file.stat().st_size)


@dataclass
class DetectionCache:
    """Detections of one clip for one detector, keyed by frame index."""

    path: Path
    info: VideoInfo
    names: dict[int, str]
    detector_name: str = ""
    runtime: str = ""
    license: str = ""
    frames: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = field(default_factory=dict)
    detect_ms: list[float] = field(default_factory=list)
    dirty: bool = False

    # -- persistence --------------------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, info: VideoInfo) -> DetectionCache | None:
        """Load a cache file. Returns None if it is missing or belongs to another video file."""
        file = Path(path)
        if not file.is_file():
            return None
        try:
            with np.load(file, allow_pickle=False) as data:
                meta = json.loads(str(data["meta"]))
                if meta.get("version") != CACHE_VERSION or meta.get("file_size") != info.file_size:
                    log.info("Cache %s is outdated, it will be rebuilt", file)
                    return None
                cache = cls(
                    path=file,
                    info=info,
                    names={int(k): v for k, v in meta["names"].items()},
                    detector_name=meta.get("detector", ""),
                    runtime=meta.get("runtime", ""),
                    license=meta.get("license", ""),
                    detect_ms=list(meta.get("detect_ms", [])),
                )
                index, counts = data["index"], data["counts"]
                xyxy, conf, cls_id = data["xyxy"], data["conf"], data["cls"]
        except (OSError, KeyError, ValueError) as exc:
            log.warning("Ignoring broken cache %s: %s", file, exc)
            return None
        start = 0
        for i, n in zip(index.tolist(), counts.tolist(), strict=True):
            end = start + n
            cache.frames[i] = (xyxy[start:end], conf[start:end], cls_id[start:end])
            start = end
        return cache

    def save(self) -> None:
        if not self.dirty:
            return
        order = sorted(self.frames)
        parts = [self.frames[i] for i in order]
        meta = {
            "version": CACHE_VERSION,
            "file_size": self.info.file_size,
            "names": {str(k): v for k, v in self.names.items()},
            "detector": self.detector_name,
            "runtime": self.runtime,
            "license": self.license,
            "conf_floor": CONF_FLOOR,
            "detect_ms": self.detect_ms[-2000:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.stem + ".tmp.npz")
        np.savez_compressed(
            tmp,
            meta=np.array(json.dumps(meta)),
            index=np.asarray(order, dtype=np.int32),
            counts=np.asarray([len(p[1]) for p in parts], dtype=np.int32),
            xyxy=np.concatenate([p[0] for p in parts]) if parts else np.zeros((0, 4), np.float32),
            conf=np.concatenate([p[1] for p in parts]) if parts else np.zeros((0,), np.float32),
            cls=np.concatenate([p[2] for p in parts]) if parts else np.zeros((0,), np.int32),
        )
        tmp.replace(self.path)
        self.dirty = False

    # -- filling ------------------------------------------------------------------------

    def missing(self, indices: Iterable[int]) -> list[int]:
        return [i for i in indices if i not in self.frames]

    def fill(
        self,
        video: str | Path,
        indices: Iterable[int],
        detector: Detector,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Run ``detector`` on the given frames of ``video`` and store the results."""
        wanted = sorted(set(self.missing(indices)))
        if not wanted:
            return 0
        self.names = dict(detector.names)
        self.detector_name, self.runtime = detector.info.name, detector.info.runtime
        self.license = detector.info.license
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise SourceError(f"Could not open video: {video}")
        done, position, todo = 0, 0, iter(wanted)
        target = next(todo, None)
        ended = False
        try:
            while target is not None:
                if not cap.grab():
                    log.warning("Video ended at frame %d (expected %d frames)", position, self.info.n_frames)
                    ended = True
                    break
                if position == target:
                    ok, image = cap.retrieve()
                    if not ok:
                        break
                    started = time.perf_counter()
                    det = detector.detect(image)
                    self.detect_ms.append((time.perf_counter() - started) * 1000.0)
                    keep = det.confidence >= CONF_FLOOR
                    self.frames[target] = (
                        det.xyxy[keep].astype(np.float32),
                        det.confidence[keep].astype(np.float32),
                        det.class_id[keep].astype(np.int32),
                    )
                    done += 1
                    target = next(todo, None)
                    if progress:
                        progress(done, len(wanted))
                    self.dirty = True
                position += 1
        finally:
            cap.release()
        if ended:
            # Frames past the real end of the file: store as empty so we never retry forever.
            for index in [target, *todo]:
                if index is not None and index not in self.frames:
                    self.frames[index] = (np.zeros((0, 4), np.float32), np.zeros(0, np.float32),
                                          np.zeros(0, np.int32))
        return done

    def get(self, index: int, conf: float) -> Detections:
        xyxy, scores, cls_id = self.frames[index]
        keep = scores >= conf
        return Detections(xyxy[keep], scores[keep], cls_id[keep])

    @property
    def detect_ms_mean(self) -> float | None:
        return float(np.mean(self.detect_ms)) if self.detect_ms else None


class ReplayDetector(Detector):
    """A Detector that returns cached detections for the frame index set in ``index``."""

    def __init__(self, cache: DetectionCache, conf: float) -> None:
        if conf < CONF_FLOOR:
            raise ValueError(f"conf {conf} is below the cache floor {CONF_FLOOR}")
        self.cache = cache
        self.conf = conf
        self.names = dict(cache.names)
        self.index = 0
        self.info = DetectorInfo(
            name=f"replay:{cache.detector_name}", runtime="cache", license=cache.license
        )

    def detect(self, image: np.ndarray) -> Detections:  # noqa: ARG002 - image not needed
        return self.cache.get(self.index, self.conf)
