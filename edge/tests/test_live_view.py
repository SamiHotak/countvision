"""Live camera picture (live:, CountVision Local): key, views, privacy, only while watched,
paused cameras, removed cameras, and the stop file of ``run``."""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

import cv2
import numpy as np
import pytest

from countvision_edge.config import parse_config
from countvision_edge.detectors import build_detector
from countvision_edge.errors import ConfigError
from countvision_edge.inputs.base import FrameSource, SourceStatus
from countvision_edge.live_view import BOUNDARY
from countvision_edge.storage import SqliteBuffer
from countvision_edge.supervisor import Supervisor
from countvision_edge.types import Frame

from .test_cloud_sync import wait_for

KEY = "test-live-key-0123456789"


class LoopingCamera(FrameSource):
    """A live camera that plays the demo scene in a loop (real coloured objects to detect)."""

    is_live = True

    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = frames
        self.index = 0
        self.closed = False
        self.t0 = time.time()

    def open(self) -> None:
        pass

    def read(self, timeout: float = 1.0) -> Frame | None:
        if self.closed:
            from countvision_edge.errors import EndOfStream

            raise EndOfStream("closed")
        time.sleep(0.03)
        image = self.frames[self.index % len(self.frames)]
        self.index += 1
        now = time.time()
        return Frame(image, self.index, now, now - self.t0)

    def close(self) -> None:
        self.closed = True

    def status(self) -> SourceStatus:
        return SourceStatus(connected=True, last_frame_age_s=0.0)


@pytest.fixture(scope="module")
def scene_frames(demo_video) -> list[np.ndarray]:
    cap = cv2.VideoCapture(demo_video)
    frames = []
    while True:
        ok, image = cap.read()
        if not ok:
            break
        frames.append(image)
    cap.release()
    # the part of the scene where objects are visible (the blob detector finds them)
    detector = build_detector(parse_config({"cameras": [], "cloud": {"manage_cameras": True}}, env={}).detector
                              .model_copy(update={"type": "blobs"}))
    busy = [f for f in frames if len(detector.detect(f)) > 0]
    assert busy, "the demo scene has objects"
    return busy


def live_config(tmp_path, **live):
    data = {"data_dir": str(tmp_path / "edge"), "detector": {"type": "blobs"}, "status_interval_s": 0.2,
            "cloud": {"manage_cameras": True}, "cameras": [],
            "live": {"enabled": True, "port": 0, "key": KEY, "max_mode": "full", **live}}
    return parse_config(data, env={})


def get(port: int, path: str, key: str | None = KEY, timeout: float = 5):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


def status_of(port: int, path: str, key: str | None = KEY) -> int:
    try:
        with get(port, path, key) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def running(tmp_path, scene_frames):
    """A supervisor with one managed camera playing the demo scene and the live server on."""
    def make(**live):
        cfg = live_config(tmp_path, **live)
        sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"),
                         source_factory=lambda cam: LoopingCamera(scene_frames))
        stop = threading.Event()
        runner = threading.Thread(target=sup.run_forever, args=(stop,), kwargs={"upload": False})
        runner.start()
        holder.append((stop, runner))
        assert wait_for(lambda: sup.live_view is not None)
        sup.sync_cameras([{"id": "cam-1", "name": "Door", "source": {"kind": "rtsp", "uri": "rtsp://x/1"}}])
        assert wait_for(lambda: sup.workers and sup.workers[0].last_result is not None, timeout=10)
        return sup

    holder: list = []
    yield make
    for stop, runner in holder:
        stop.set()
        runner.join(timeout=20)


def test_live_needs_a_key(tmp_path):
    with pytest.raises(ConfigError, match="live.key"):
        parse_config({"cameras": [], "cloud": {"manage_cameras": True}, "live": {"enabled": True}}, env={})
    with pytest.raises(ConfigError, match="live.key"):
        parse_config({"cameras": [], "cloud": {"manage_cameras": True},
                      "live": {"enabled": True, "key": "short"}}, env={})
    cfg = parse_config({"cameras": [], "cloud": {"manage_cameras": True}, "live": {"enabled": True}},
                       env={"CV_LIVE_KEY": KEY})
    assert cfg.live.key == KEY and cfg.live.host == "127.0.0.1" and cfg.live.max_mode == "blur"
    assert parse_config({"cameras": [], "cloud": {"manage_cameras": True}}, env={}).live.enabled is False


def test_picture_only_with_the_key_and_only_while_watched(running):
    sup = running()
    port = sup.live_view.port
    assert status_of(port, "/health", key=None) == 401
    assert status_of(port, "/health", key="wrong-key-wrong-key") == 401
    assert status_of(port, "/cameras/cam-1/frame.jpg", key=None) == 401
    with get(port, "/health") as r:
        assert r.status == 200 and b"cam-1" in r.read()
    time.sleep(1.0)  # the camera runs, nobody watches: no picture is made
    assert sup.live_view.frames_sent == 0
    assert all(not r._cache for r in sup.live_view._renderers.values())

    with get(port, "/cameras/cam-1/frame.jpg?mode=full&geometry=1") as r:
        assert r.headers["Content-Type"] == "image/jpeg" and r.headers["Cache-Control"] == "no-store"
        image = cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and image.shape[1] == sup.workers[0].frame_size[0]
    assert status_of(port, "/cameras/nope/frame.jpg") == 404
    assert status_of(port, "/cameras/cam-1/frame.jpg?mode=weird") == 400
    assert status_of(port, "/cameras/cam-1/other.png") == 404


def test_full_refused_when_the_device_allows_blur_only(running):
    sup = running(max_mode="blur")
    port = sup.live_view.port
    assert status_of(port, "/cameras/cam-1/frame.jpg?mode=full") == 403
    assert status_of(port, "/cameras/cam-1/frame.jpg?mode=blur") == 200
    assert status_of(port, "/cameras/cam-1/frame.jpg") == 200  # blur is the default


def test_blur_pixelates_every_detected_object(running):
    sup = running()
    port = sup.live_view.port
    detector = build_detector(sup.cfg.detector)
    worker = sup.workers[0]
    for _ in range(20):  # a picture where the blob detector finds objects in the SAME frame
        with get(port, "/cameras/cam-1/frame.jpg?mode=full&geometry=0") as r:
            full = cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_COLOR)
        boxes = detector.detect(full).xyxy
        if len(boxes):
            break
        time.sleep(0.1)
    assert len(boxes), "an object is visible"
    # the blurred view of the newest analysed frame: no sharp object edges are left
    result = worker.last_result
    from countvision_edge.live_view import CameraRenderer

    renderer = CameraRenderer(lambda: worker, sup.detector, sup.cfg.live)
    _, jpeg = renderer.render("blur", False, 1280)
    blurred = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert blurred.shape[1] <= 960
    for x1, y1, x2, y2 in np.asarray(result.boxes).reshape(-1, 4).astype(int):
        scale = blurred.shape[1] / result.frame.image.shape[1]
        region = blurred[int(y1 * scale) + 2:int(y2 * scale) - 2, int(x1 * scale) + 2:int(x2 * scale) - 2]
        if region.size == 0:
            continue
        # pixelated = big flat blocks: few distinct colours compared with the size of the box
        distinct = len(np.unique(region.reshape(-1, 3) // 16, axis=0))
        assert distinct < region.shape[0] * region.shape[1] / 4, (distinct, region.shape)


def test_stream_sends_pictures_and_stops_when_the_viewer_leaves(running):
    sup = running()
    port = sup.live_view.port
    with get(port, "/cameras/cam-1/stream.mjpg?mode=blur&fps=10&width=320") as r:
        assert r.headers["Content-Type"] == f"multipart/x-mixed-replace; boundary={BOUNDARY}"
        data = b""
        started = time.time()
        while data.count(b"Content-Type: image/jpeg") < 4 and time.time() - started < 5:
            data += r.read1(65536)
        assert data.count(b"Content-Type: image/jpeg") >= 4
        part = data.split(b"\r\n\r\n", 1)[1]
        image = cv2.imdecode(np.frombuffer(part[: part.index(b"\r\n--")], np.uint8), cv2.IMREAD_COLOR)
        assert image is not None and image.shape[1] <= 320
        assert sup.live_view.viewers == 1
        assert sup.live_view.status()["viewers"] == 1
    assert wait_for(lambda: sup.live_view.viewers == 0, timeout=5)
    sent = sup.live_view.frames_sent
    time.sleep(0.8)
    assert sup.live_view.frames_sent == sent  # nobody watches: nothing more is made


def test_stream_ends_when_the_camera_is_removed(running):
    sup = running()
    port = sup.live_view.port
    with get(port, "/cameras/cam-1/stream.mjpg?fps=5") as r:
        r.read1(65536)
        sup.sync_cameras([])
        data = b""
        started = time.time()
        while time.time() - started < 5:
            chunk = r.read1(65536)
            if not chunk:
                break
            data += chunk
        assert time.time() - started < 5  # the stream ended
    assert status_of(port, "/cameras/cam-1/frame.jpg") == 404
    assert "cam-1" not in sup.live_view._renderers


def test_paused_camera_still_shows_a_pixelated_picture(running):
    sup = running()
    worker = sup.workers[0]
    calls = []
    real = sup.detector.inner.detect

    def counting_detect(image):
        calls.append(1)
        return real(image)

    sup.detector.inner.detect = counting_detect
    worker.pipeline.paused = True  # what the schedule does outside the counting hours
    worker.last_result = None
    port = sup.live_view.port
    with get(port, "/cameras/cam-1/frame.jpg?mode=blur") as r:
        assert r.status == 200
    assert calls, "a fresh detection was made for the paused picture"
    with get(port, "/cameras/cam-1/frame.jpg?mode=full") as r:
        assert r.status == 200


def test_stale_picture_is_no_picture(running):
    sup = running()
    worker = sup.workers[0]
    live = sup.live_view
    renderer = live.renderer("cam-1")
    renderer.clock = lambda: time.time() + 60  # the newest frame is a minute old
    assert status_of(live.port, "/cameras/cam-1/frame.jpg") == 409
    assert worker.last_frame is not None


def test_port_in_use_keeps_counting(tmp_path, scene_frames):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        cfg = live_config(tmp_path, port=port)
        sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"))
        sup.start_live_view()
        assert sup.live_view is None  # logged, no crash
        assert sup.status()["live_view"] == {"state": "disabled"}
    finally:
        blocker.close()


def test_stop_file_stops_run_cleanly(tmp_path):
    cfg = live_config(tmp_path)
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"))
    stop_file = tmp_path / "stop"
    runner = threading.Thread(target=sup.run_forever, kwargs={"upload": False, "stop_file": stop_file})
    runner.start()
    assert wait_for(lambda: sup.live_view is not None)
    time.sleep(0.3)
    assert runner.is_alive()
    stop_file.write_text("stop")
    runner.join(timeout=10)
    assert not runner.is_alive()
    assert sup.status_path.exists()  # the last status was written
