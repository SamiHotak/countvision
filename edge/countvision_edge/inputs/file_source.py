"""Video file source. Deterministic: every frame is returned, in order."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from pathlib import Path

import cv2

from ..errors import EndOfStream, SourceError
from ..types import Frame
from .base import FrameSource, SourceStatus

log = logging.getLogger(__name__)


class FileSource(FrameSource):
    """Reads a video file.

    Frame timestamps are ``start_time + index / fps``, so results do not depend on how fast
    the file is processed. With ``realtime=True`` the source sleeps to play at normal speed.
    """

    is_live = False

    def __init__(
        self,
        path: str | Path,
        *,
        start_time: float | None = None,
        realtime: bool = False,
        capture_factory: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._path = Path(path)
        self._start_time = start_time
        self._realtime = realtime
        self._factory = capture_factory or (lambda p: cv2.VideoCapture(p))
        self._clock = clock
        self._sleep = sleep
        self._cap = None
        self._fps = 25.0
        self._index = 0
        self._wall_start = 0.0
        self._size: tuple[int, int] | None = None

    def open(self) -> None:
        if not self._path.is_file():
            raise SourceError(f"Video file not found: {self._path}")
        cap = self._factory(str(self._path))
        if not cap.isOpened():
            raise SourceError(f"Could not open video file (unsupported format?): {self._path}")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(fps) or fps <= 0:
            log.warning("File reports no frame rate, assuming 25 FPS: %s", self._path)
            fps = 25.0
        self._cap, self._fps = cap, fps
        self._wall_start = self._clock()
        if self._start_time is None:
            self._start_time = self._wall_start
        log.info("Opened file %s (%.2f FPS)", self._path.name, fps)

    def read(self, timeout: float = 1.0) -> Frame | None:
        if self._cap is None:
            raise EndOfStream("source is closed")
        ok, image = self._cap.read()
        if not ok or image is None:
            raise EndOfStream("end of file")
        media_ts = self._index / self._fps
        if self._realtime:
            ahead = media_ts - (self._clock() - self._wall_start)
            if ahead > 0:
                self._sleep(ahead)
        frame = Frame(
            image=image,
            index=self._index,
            ts=(self._start_time or 0.0) + media_ts,
            media_ts=media_ts,
        )
        self._index += 1
        self._size = (int(image.shape[1]), int(image.shape[0]))
        return frame

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def status(self) -> SourceStatus:
        width, height = self._size or (None, None)
        return SourceStatus(
            connected=self._cap is not None, width=width, height=height, fps=self._fps
        )

    def nominal_fps(self) -> float | None:
        return self._fps

    def total_frames(self) -> int | None:
        """Number of frames in the file as reported by the container (may be approximate)."""
        if self._cap is None:
            return None
        count = float(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        return int(count) if math.isfinite(count) and count > 0 else None
