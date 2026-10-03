"""Live sources: USB webcam, RTSP / IP camera, HTTP MJPEG.

A background thread reads frames as fast as the camera delivers them and keeps only the
newest one. The pipeline therefore never works on old frames, and a slow detector cannot
build up a delay. If the stream stops, the thread reconnects with exponential backoff.
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import sys
import threading
import time
from collections.abc import Callable

import cv2

from ..config import SourceConfig, redact_uri
from ..errors import EndOfStream, SourceError
from ..types import Frame
from .base import FrameSource, SourceStatus

log = logging.getLogger(__name__)

CaptureFactory = Callable[[SourceConfig], object]


class Backoff:
    """Exponential backoff delays: initial, initial*factor, ... up to maximum (with jitter)."""

    def __init__(
        self,
        initial: float,
        factor: float,
        maximum: float,
        jitter: float = 0.0,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.initial, self.factor, self.maximum, self.jitter = initial, factor, maximum, jitter
        self._rng = rng
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self.initial * self.factor ** min(self._attempt, 64), self.maximum)
        self._attempt += 1
        if self.jitter:
            delay *= 1.0 + self.jitter * (2.0 * self._rng() - 1.0)
        return max(0.0, min(delay, self.maximum))

    def reset(self) -> None:
        self._attempt = 0


def quiet_opencv() -> None:
    """Hide OpenCV's own warnings (for example when probing webcams that do not exist)."""
    with contextlib.suppress(AttributeError):  # very old OpenCV builds have no cv2.utils.logging
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)


def fourcc_code(code: str) -> int:
    """OpenCV four-character code, across OpenCV versions."""
    if hasattr(cv2, "VideoWriter_fourcc"):
        return cv2.VideoWriter_fourcc(*code)
    return cv2.VideoWriter.fourcc(*code)


def open_cv_capture(cfg: SourceConfig):
    """Open a cv2.VideoCapture for a webcam or a network stream."""
    if cfg.kind == "webcam":
        backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(cfg.uri), backend)
        if cfg.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, fourcc_code(cfg.fourcc))
        if cfg.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        if cfg.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        if cfg.fps:
            cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        return cap
    if cfg.kind == "rtsp":
        # TCP is slower to start but does not lose packets on bad Wi-Fi.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;{cfg.rtsp_transport}")
    timeout_ms = int(cfg.reconnect.open_timeout_s * 1000)
    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms]
    try:
        return cv2.VideoCapture(cfg.uri, cv2.CAP_FFMPEG, params)
    except (TypeError, cv2.error):  # older OpenCV without the params argument
        return cv2.VideoCapture(cfg.uri, cv2.CAP_FFMPEG)


class LiveSource(FrameSource):
    """Threaded live source with automatic reconnect."""

    is_live = True

    def __init__(
        self,
        cfg: SourceConfig,
        *,
        capture_factory: CaptureFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._factory = capture_factory or open_cv_capture
        self._clock = clock
        self._label = redact_uri(cfg.uri) if cfg.kind != "webcam" else f"webcam {cfg.uri}"
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: tuple[object, float, int] | None = None  # image, ts, seq
        self._seq = 0
        self._returned_seq = 0
        self._t0 = 0.0
        self._last_frame_ts: float | None = None
        self._connected = False
        self._connect_count = 0
        self._error: str | None = None
        self._size: tuple[int, int] | None = None
        self._fps: float | None = None
        self._closed = False

    # -- public API ---------------------------------------------------------------------

    def open(self) -> None:
        if self._thread is not None:
            return
        self._t0 = self._clock()
        self._thread = threading.Thread(target=self._run, name="live-source", daemon=True)
        self._thread.start()

    def read(self, timeout: float = 1.0) -> Frame | None:
        with self._cond:
            ready = self._cond.wait_for(
                lambda: self._closed
                or (self._latest is not None and self._latest[2] > self._returned_seq),
                timeout,
            )
            if self._closed:
                raise EndOfStream("source closed")
            if not ready or self._latest is None:
                return None
            image, ts, seq = self._latest
            self._returned_seq = seq
        return Frame(image=image, index=seq - 1, ts=ts, media_ts=ts - self._t0)  # type: ignore[arg-type]

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None

    def status(self) -> SourceStatus:
        now = self._clock()
        age = None if self._last_frame_ts is None else max(0.0, now - self._last_frame_ts)
        width, height = self._size or (None, None)
        return SourceStatus(
            connected=self._connected,
            reconnects=max(0, self._connect_count - 1),
            last_frame_age_s=age,
            width=width,
            height=height,
            fps=self._fps,
            error=self._error,
        )

    def nominal_fps(self) -> float | None:
        return self._fps

    # -- reader thread ------------------------------------------------------------------

    def _publish(self, image, now: float) -> None:
        with self._cond:
            self._seq += 1
            self._latest = (image, now, self._seq)
            self._last_frame_ts = now
            self._size = (int(image.shape[1]), int(image.shape[0]))
            self._cond.notify_all()

    def _run(self) -> None:
        rc = self._cfg.reconnect
        backoff = Backoff(rc.initial_s, rc.factor, rc.max_s, rc.jitter)
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._factory(self._cfg)
                if not cap.isOpened():
                    raise SourceError("could not open the stream")
                self._connected = True
                self._connect_count += 1
                self._error = None
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                self._fps = fps if 0 < fps < 1000 else None
                if self._connect_count > 1:
                    log.info("Reconnected to %s", self._label)
                else:
                    log.info("Connected to %s", self._label)
                connected_at = self._clock()
                failures = 0
                while not self._stop.is_set():
                    ok, image = cap.read()
                    now = self._clock()
                    if ok and image is not None:
                        failures = 0
                        backoff.reset()
                        self._publish(image, now)
                        continue
                    failures += 1
                    last = self._last_frame_ts if self._last_frame_ts is not None else connected_at
                    if failures >= 3 or (now - max(last, connected_at)) > rc.stall_timeout_s:
                        raise SourceError("stream stopped delivering frames")
                    self._stop.wait(0.05)
            except Exception as exc:  # noqa: BLE001 - any failure leads to a reconnect
                self._error = str(exc) or type(exc).__name__
            finally:
                self._connected = False
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        log.debug("release failed", exc_info=True)
            if self._stop.is_set():
                break
            delay = backoff.next_delay()
            log.warning("Lost %s (%s). Reconnecting in %.1f s.", self._label, self._error, delay)
            self._stop.wait(delay)
