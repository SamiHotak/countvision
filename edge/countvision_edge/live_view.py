"""Live camera picture for CountVision Local (``live:`` in the config, ``countvision-edge run``).

A small HTTP server on 127.0.0.1 serves every running camera's picture with the analysis drawn
on it: boxes with class colours, track ids, counting lines with IN/OUT, zones with occupancy.

    GET /health                                      -> {"ok": true, "cameras": [...]}
    GET /cameras/<id>/frame.jpg?mode=&geometry=&width=
    GET /cameras/<id>/stream.mjpg?mode=&geometry=&width=&fps=

    mode      blur (default) = people and vehicles pixelated | full = the picture as it is
    geometry  1 (default) = draw lines and zones | 0 = boxes only (the camera editor draws the
              lines itself)
    width     max picture width (never wider than live.max_width)
    fps       max pictures per second of a stream (never more than live.max_fps)

Every request needs ``Authorization: Bearer <live.key>``. Only the CountVision API on this
computer knows the key; it decides who may watch (members of the business, the owner's privacy
setting) and passes the picture on.

Privacy by design:
* A picture is made only when somebody asks for one: nothing is encoded while nobody watches.
* JPEGs live in memory only (the newest one per camera and view); nothing is written to disk.
* "blur" pixelates every detected object of ALL classes (also people at a car-park camera), and
  objects that are not tracks yet. When the camera is paused (outside counting hours) there is no
  fresh analysis, so a fresh detection is made for the picture (at most 2 per second).
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np

from .config import LiveViewConfig
from .privacy import limit_width, pixelate_boxes
from .viz import Annotator, _label

if TYPE_CHECKING:  # pragma: no cover
    from .supervisor import CameraWorker

log = logging.getLogger(__name__)

BOUNDARY = "countvisionframe"
BLUR_MAX_WIDTH = 960  # a pixelated picture never needs to be large
STALE_S = 15.0  # a live picture older than this is "no picture" (camera offline)
PAUSED_DETECT_INTERVAL_S = 0.5
JPEG_QUALITY = 78
_CAMERA_PATH = re.compile(r"^/cameras/([A-Za-z0-9_.\-]{1,64})/(frame\.jpg|stream\.mjpg)$")


class NoPicture(Exception):
    """The camera has no picture right now (starting, offline, stopped)."""


@dataclass
class _Cached:
    frame_key: tuple[int, int]
    jpeg: bytes
    seq: int
    made_at: float


class CameraRenderer:
    """Makes the annotated JPEGs of one camera. Several viewers of the same view share one
    encoded picture per analysed frame."""

    def __init__(self, worker_getter: Callable[[], CameraWorker | None], detector, cfg: LiveViewConfig,
                 clock: Callable[[], float] = time.time) -> None:
        self._worker_getter = worker_getter
        self.detector = detector
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, bool, int], _Cached] = {}
        self._annotators: dict[bool, Annotator] = {}
        self._seq = 0

    def _annotator(self, geometry: bool, worker: CameraWorker) -> Annotator:
        annotator = self._annotators.get(geometry)
        if annotator is None:
            def engine(_w=worker):
                pipeline = _w.pipeline
                return pipeline.engine if pipeline is not None else None

            annotator = self._annotators[geometry] = Annotator(engine, geometry=geometry)
        return annotator

    def render(self, mode: str, geometry: bool, width: int) -> tuple[int, bytes]:
        """(sequence number, JPEG) of the newest picture. The same sequence = nothing new."""
        worker = self._worker_getter()
        if worker is None:
            raise NoPicture("this camera is not running")
        result = worker.last_result
        frame = worker.last_frame
        pipeline = worker.pipeline
        paused = bool(pipeline is not None and pipeline.paused)
        if frame is None:
            raise NoPicture("the camera has no picture yet (still starting)")
        if worker.cam.source.is_live and frame.ts < self.clock() - STALE_S:
            raise NoPicture("the camera sends no picture right now (offline, reconnecting)")
        analysed = (result is not None and not paused
                    and frame.ts - result.frame.ts <= 2.0)  # analysis keeps up with the camera
        shown = result.frame if analysed else frame
        frame_key = (id(shown), shown.index)
        view = (mode, geometry, width)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(view)
            if cached is not None and cached.frame_key == frame_key:
                return cached.seq, cached.jpeg
            if (cached is not None and not analysed and mode == "blur"
                    and now - cached.made_at < PAUSED_DETECT_INTERVAL_S):
                return cached.seq, cached.jpeg  # paused + blur: max 2 fresh detections a second
            jpeg = self._encode(worker, shown, result if analysed else None, mode, geometry, width,
                                paused)
            self._seq += 1
            self._cache[view] = _Cached(frame_key, jpeg, self._seq, now)
            return self._seq, jpeg

    def _encode(self, worker: CameraWorker, frame, result, mode: str, geometry: bool, width: int,
                paused: bool) -> bytes:
        annotator = self._annotator(geometry, worker)
        if result is not None:
            image = frame.image.copy()
            if mode == "blur":
                boxes = [t.xyxy for t in result.tracks]
                boxes += [tuple(b) for b in np.asarray(result.boxes).reshape(-1, 4)]
                pixelate_boxes(image, boxes)
            image = annotator.draw(_with_image(result, image), worker.health.fps)
        else:
            image = frame.image.copy()
            if mode == "blur":  # no fresh analysis (paused): detect everything for the picture
                boxes = [tuple(b) for b in self.detector.detect(image).xyxy.reshape(-1, 4)]
                pixelate_boxes(image, boxes)
            engine = annotator._engine_getter()
            if engine is not None and geometry:
                annotator._draw_zones(image, engine)
                annotator._draw_lines(image, engine)
            if paused:
                _label(image, "Paused: outside counting hours", (8, 20), (240, 240, 240))
        limit = min(width, self.cfg.max_width, BLUR_MAX_WIDTH if mode == "blur" else 10_000)
        image = limit_width(image, limit)
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            raise NoPicture("could not encode the picture")
        return encoded.tobytes()


def _with_image(result, image):
    """The same FrameResult with another picture (the original frame is never changed)."""
    import dataclasses

    return dataclasses.replace(result, frame=dataclasses.replace(result.frame, image=image))


class LiveViewServer:
    """The HTTP server. ``workers`` returns the supervisor's current list of camera workers."""

    def __init__(self, cfg: LiveViewConfig, workers: Callable[[], list[CameraWorker]], detector,
                 clock: Callable[[], float] = time.time) -> None:
        if not cfg.key or len(cfg.key) < 16:
            raise ValueError("live.key is missing or shorter than 16 characters")
        self.cfg = cfg
        self._workers = workers
        self.detector = detector
        self.clock = clock
        self.stop_event = threading.Event()
        self._renderers: dict[str, CameraRenderer] = {}
        self._lock = threading.Lock()
        self.viewers = 0  # open streams (for status.json and tests)
        self.frames_sent = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- cameras --------------------------------------------------------------------------

    def worker(self, camera_id: str) -> CameraWorker | None:
        return next((w for w in self._workers() if w.cam.id == camera_id), None)

    def renderer(self, camera_id: str) -> CameraRenderer:
        with self._lock:
            renderer = self._renderers.get(camera_id)
            if renderer is None:
                renderer = self._renderers[camera_id] = CameraRenderer(
                    lambda cid=camera_id: self.worker(cid), self.detector, self.cfg, self.clock)
            return renderer

    def viewer_joined(self, delta: int) -> None:
        with self._lock:
            self.viewers += delta

    def forget_removed(self) -> None:
        """Drop the renderers (and their pictures) of cameras that no longer run."""
        running = {w.cam.id for w in self._workers()}
        with self._lock:
            for camera_id in [c for c in self._renderers if c not in running]:
                del self._renderers[camera_id]

    # -- lifecycle ------------------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self.cfg.port

    def start(self) -> None:
        server = self

        class Handler(_Handler):
            live = server

        self._httpd = _Server((self.cfg.host, self.cfg.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="live-view", daemon=True)
        self._thread.start()
        log.info("Live picture on http://%s:%d (key protected; pictures only while someone watches)",
                 self.cfg.host, self.port)

    def stop(self) -> None:
        self.stop_event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        return {"state": "ok" if self._httpd else "stopped", "port": self.port, "viewers": self.viewers,
                "max_mode": self.cfg.max_mode}

    # -- requests -------------------------------------------------------------------------

    def authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), (self.cfg.key or "").encode())

    def view_args(self, query: dict[str, list[str]]) -> tuple[str, bool, int, float]:
        mode = (query.get("mode") or ["blur"])[0]
        if mode not in ("blur", "full"):
            raise ValueError("mode must be blur or full")
        if mode == "full" and self.cfg.max_mode != "full":
            raise PermissionError("the full picture is switched off on this device (live.max_mode: blur)")
        geometry = (query.get("geometry") or ["1"])[0] not in ("0", "false", "no")
        try:
            width = int((query.get("width") or [self.cfg.max_width])[0])
            fps = float((query.get("fps") or [self.cfg.max_fps])[0])
        except ValueError as exc:
            raise ValueError("width and fps must be numbers") from exc
        width = max(160, min(width, self.cfg.max_width))
        fps = max(0.2, min(fps, self.cfg.max_fps))
        return mode, geometry, width, fps


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    live: LiveViewServer
    protocol_version = "HTTP/1.1"
    server_version = "CountVisionLive"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        log.debug("live view: " + format, *args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        live = self.live
        if not live.authorized(self.headers.get("Authorization")):
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "missing or wrong live view key")
            return
        url = urlsplit(self.path)
        if url.path == "/health":
            self._json(200, {"ok": True, "cameras": [w.cam.id for w in live._workers()],
                             **live.status()})
            return
        match = _CAMERA_PATH.match(url.path)
        if not match:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "unknown address")
            return
        camera_id, kind = match.groups()
        try:
            mode, geometry, width, fps = live.view_args(parse_qs(url.query))
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, "full_not_allowed", str(exc))
            return
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
            return
        if live.worker(camera_id) is None:
            self._error(HTTPStatus.NOT_FOUND, "camera_not_running",
                        f"camera {camera_id} is not running on this device")
            return
        renderer = live.renderer(camera_id)
        if kind == "frame.jpg":
            self._frame(renderer, mode, geometry, width)
        else:
            self._stream(renderer, camera_id, mode, geometry, width, fps)

    def _frame(self, renderer: CameraRenderer, mode: str, geometry: bool, width: int) -> None:
        try:
            _, jpeg = renderer.render(mode, geometry, width)
        except NoPicture as exc:
            self._error(HTTPStatus.CONFLICT, "no_picture", str(exc))
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpeg)
        self.live.frames_sent += 1

    def _stream(self, renderer: CameraRenderer, camera_id: str, mode: str, geometry: bool, width: int,
                fps: float) -> None:
        """multipart/x-mixed-replace: one JPEG part per new picture until the viewer leaves."""
        live = self.live
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        interval = 1.0 / fps
        last_seq = -1
        live.viewer_joined(+1)
        log.info("Live picture of camera %s: viewer connected (%s)", camera_id, mode)
        try:
            while not live.stop_event.is_set():
                started = time.monotonic()
                if live.worker(camera_id) is None:
                    break  # the camera was removed
                try:
                    seq, jpeg = renderer.render(mode, geometry, width)
                except NoPicture:
                    seq, jpeg = last_seq, b""
                if seq != last_seq and jpeg:
                    self.wfile.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
                        .encode() + jpeg + b"\r\n")
                    self.wfile.flush()
                    last_seq = seq
                    live.frames_sent += 1
                live.stop_event.wait(max(0.01, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # the viewer closed the page: nothing more is encoded for them
        finally:
            live.viewer_joined(-1)
            log.info("Live picture of camera %s: viewer left", camera_id)
