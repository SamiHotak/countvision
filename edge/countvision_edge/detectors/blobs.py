"""Model-free detectors for the synthetic demo and for tests."""

from __future__ import annotations

import cv2
import numpy as np

from ..types import Detections
from .base import Detector, DetectorInfo

DEMO_NAMES = {0: "person", 2: "car"}


class BlobDetector(Detector):
    """Finds saturated colour rectangles: red = person (class 0), blue = car (class 2).

    This is for the synthetic demo video and the tests only. It lets the whole pipeline run
    from a real video file with exact, repeatable results and without any model download.
    """

    def __init__(self, min_area: int = 60, confidence: float = 0.9) -> None:
        self.min_area = min_area
        self.confidence = confidence
        self.names = dict(DEMO_NAMES)
        self.info = DetectorInfo(
            name="blobs", runtime="opencv", license="MIT-like (this repo's own test helper)"
        )

    def detect(self, image: np.ndarray) -> Detections:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        colored = ((hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 120)).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(colored, connectivity=8)
        boxes, classes = [], []
        for i in range(1, count):
            x, y, w, h, area = (int(v) for v in stats[i])
            if area < self.min_area:
                continue
            region = hsv[y : y + h, x : x + w, 0][labels[y : y + h, x : x + w] == i]
            hue = float(np.median(region))
            if hue <= 12 or hue >= 168:
                class_id = 0
            elif 100 <= hue <= 130:
                class_id = 2
            else:
                continue
            boxes.append((x, y, x + w, y + h))
            classes.append(class_id)
        if not boxes:
            return Detections.empty()
        return Detections.from_arrays(boxes, [self.confidence] * len(boxes), classes)


class ScriptedDetector(Detector):
    """Returns pre-defined boxes, one list per call. Used to test counting with exact input.

    ``frames`` is a list: entry i = list of (x1, y1, x2, y2, class_id) for the i-th call.
    It assumes every frame is processed (target_fps = None).
    """

    def __init__(self, frames: list[list[tuple[float, float, float, float, int]]]) -> None:
        self._frames = frames
        self._call = 0
        self.names = dict(DEMO_NAMES)
        self.info = DetectorInfo(name="scripted", runtime="none", license="n/a (test helper)")

    def detect(self, image: np.ndarray) -> Detections:
        boxes = self._frames[self._call] if self._call < len(self._frames) else []
        self._call += 1
        if not boxes:
            return Detections.empty()
        arr = np.asarray(boxes, dtype=np.float32)
        return Detections.from_arrays(arr[:, :4], np.full(len(arr), 0.9), arr[:, 4])
