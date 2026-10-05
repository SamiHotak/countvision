"""Frame scheduling: target FPS per camera and adaptive frame skipping under load."""

from __future__ import annotations


class FrameScheduler:
    """Decides which frames are processed.

    * ``target_fps=None``: process every frame (files, offline analysis).
    * ``target_fps=N``: process about N frames per second of stream time. Extra frames are
      skipped. With ``adaptive=True`` the rate is lowered when the detector is too slow, so
      the machine keeps about 15% headroom (for other cameras, the heartbeat, the web app).
      It recovers automatically when the load drops.

    All decisions use stream time (``media_ts``), so the result is the same for a file
    processed fast or slow.
    """

    HEADROOM = 0.85  # use at most 85% of the available time per frame

    def __init__(
        self,
        target_fps: float | None,
        *,
        min_fps: float = 2.0,
        adaptive: bool = True,
    ) -> None:
        self.target_fps = target_fps
        self.min_fps = min_fps
        self.adaptive = adaptive and target_fps is not None
        self._next_ts: float | None = None
        self._ema_s: float | None = None
        self.processed = 0
        self.skipped = 0

    @property
    def effective_fps(self) -> float | None:
        """The rate the scheduler currently aims for (None = every frame)."""
        if self.target_fps is None:
            return None
        fps = self.target_fps
        if self.adaptive and self._ema_s:
            fps = min(fps, max(self.min_fps, self.HEADROOM / self._ema_s))
        return min(fps, self.target_fps)

    def should_process(self, media_ts: float) -> bool:
        """True if the frame at ``media_ts`` should be processed. Call once per frame."""
        fps = self.effective_fps
        if fps is None:
            self.processed += 1
            return True
        interval = 1.0 / fps
        # 10% tolerance so a 30 FPS file with target 15 takes exactly every second frame.
        if self._next_ts is None or media_ts >= self._next_ts - 0.1 * interval:
            if self._next_ts is None or media_ts - self._next_ts >= interval:
                # First frame, or far behind (gap, slow device): restart from this frame.
                # Never "catch up" with a burst.
                self._next_ts = media_ts + interval
            else:
                # Keep the rhythm. Anchoring to each frame's own time would round every
                # interval UP to a whole number of source frames: a 12 FPS camera with target
                # 10 would then get only 6 FPS analysed (fixed in phase 2 A).
                self._next_ts += interval
            self.processed += 1
            return True
        self.skipped += 1
        return False

    def record_processing(self, seconds: float) -> None:
        """Report how long processing a frame took (detector + tracking + analytics)."""
        if seconds <= 0:
            return
        self._ema_s = seconds if self._ema_s is None else 0.8 * self._ema_s + 0.2 * seconds
