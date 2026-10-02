from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from countvision_edge.config import SourceConfig
from countvision_edge.errors import EndOfStream, SourceError
from countvision_edge.inputs import Backoff, FileSource, LiveSource, create_source
from countvision_edge.testing.synthetic import demo_scene


def test_file_source_gives_every_frame_with_exact_timestamps(demo_video):
    scene = demo_scene()
    source = FileSource(demo_video, start_time=1_000_000.0)
    source.open()
    frames = []
    with pytest.raises(EndOfStream):
        while True:
            frames.append(source.read())
    source.close()
    assert len(frames) == scene.n_frames
    assert frames[0].index == 0 and frames[0].ts == pytest.approx(1_000_000.0)
    assert frames[15].media_ts == pytest.approx(1.0)
    assert frames[15].ts == pytest.approx(1_000_001.0)
    assert frames[0].size == (640, 360)


def test_file_source_missing_file():
    with pytest.raises(SourceError, match="not found"):
        FileSource("does-not-exist.mp4").open()


def test_file_source_realtime_sleeps_to_play_at_video_speed(demo_video):
    now = {"t": 100.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    source = FileSource(demo_video, realtime=True, clock=lambda: now["t"], sleep=sleep)
    source.open()
    for _ in range(31):  # frames 0..30 = 2.0 s of video
        source.read()
    source.close()
    assert sum(slept) == pytest.approx(2.0, abs=0.01)


def test_backoff_doubles_up_to_the_maximum_and_resets():
    b = Backoff(1.0, 2.0, 10.0, jitter=0.0)
    assert [b.next_delay() for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    b.reset()
    assert b.next_delay() == 1.0


def test_backoff_jitter_stays_in_range_and_below_maximum():
    b = Backoff(4.0, 2.0, 10.0, jitter=0.5, rng=lambda: 1.0)
    assert [b.next_delay() for _ in range(3)] == [6.0, 10.0, 10.0]


def test_create_source_picks_the_right_class():
    assert isinstance(create_source(SourceConfig(uri="video.mp4")), FileSource)
    assert isinstance(create_source(SourceConfig(uri="0")), LiveSource)
    assert isinstance(create_source(SourceConfig(uri="rtsp://cam/stream")), LiveSource)


# ---------------------------------------------------------------- live source with fake captures


class FakeCapture:
    """Mimics cv2.VideoCapture: delivers ``n_frames`` frames, then fails (or never opens)."""

    def __init__(self, n_frames: int, opens: bool = True, delay: float = 0.0, then_fail=True):
        self._left = n_frames
        self._opens = opens
        self._delay = delay
        self._then_fail = then_fail
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 - OpenCV naming
        return self._opens

    def read(self):
        if self._delay:
            time.sleep(self._delay)
        if self._left > 0:
            self._left -= 1
            return True, np.full((36, 64, 3), 7, dtype=np.uint8)
        if self._then_fail:
            return False, None
        time.sleep(0.01)
        return True, np.full((36, 64, 3), 7, dtype=np.uint8)

    def get(self, prop):
        return 0.0

    def set(self, prop, value):
        return True

    def release(self):
        self.released = True


def live_config() -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "uri": "rtsp://user:secret@cam/stream",
            "reconnect": {"initial_s": 0.01, "factor": 2, "max_s": 0.05, "jitter": 0,
                          "stall_timeout_s": 0.5, "open_timeout_s": 1},
        }
    )


def factory_from(captures: list[FakeCapture]):
    calls = {"n": 0}

    def factory(cfg):
        calls["n"] += 1
        return captures[min(calls["n"] - 1, len(captures) - 1)]

    return factory, calls


def read_frames(source: LiveSource, n: int, timeout: float = 3.0) -> list:
    got, end = [], time.time() + timeout
    while len(got) < n and time.time() < end:
        frame = source.read(timeout=0.2)
        if frame is not None:
            got.append(frame)
    return got


def test_live_source_reconnects_after_the_stream_stops():
    first = FakeCapture(5)  # delivers 5 frames, then fails
    second = FakeCapture(5, then_fail=False)  # the camera is back and stays up
    factory, calls = factory_from([first, second])
    source = LiveSource(live_config(), capture_factory=factory)
    source.open()
    try:
        # Read slowly enough that frames from both connections are seen.
        indexes = []
        end = time.time() + 3.0
        while time.time() < end and calls["n"] < 2:
            time.sleep(0.02)
        frame = None
        while frame is None and time.time() < end:
            frame = source.read(timeout=0.2)
        assert frame is not None
        status = source.status()
        assert calls["n"] == 2 and first.released
        assert status.reconnects == 1 and status.connected
        indexes.append(frame.index)
    finally:
        source.close()


def test_live_source_retries_when_the_camera_cannot_be_opened():
    captures = [FakeCapture(0, opens=False), FakeCapture(0, opens=False), FakeCapture(3, then_fail=False)]
    factory, calls = factory_from(captures)
    source = LiveSource(live_config(), capture_factory=factory)
    source.open()
    try:
        frames = read_frames(source, 1)
        assert len(frames) == 1
        assert calls["n"] == 3
        assert source.status().connected
    finally:
        source.close()


def test_live_source_returns_only_the_newest_frame():
    cap = FakeCapture(10_000, delay=0.001, then_fail=False)
    factory, _ = factory_from([cap])
    source = LiveSource(live_config(), capture_factory=factory)
    source.open()
    try:
        time.sleep(0.3)  # the reader produced many frames while nobody asked
        frame = source.read(timeout=1.0)
        assert frame is not None and frame.index > 20  # old frames were dropped
        again = source.read(timeout=1.0)
        assert again is not None and again.index > frame.index
    finally:
        source.close()


def test_live_source_read_times_out_when_there_is_no_new_frame():
    factory, _ = factory_from([FakeCapture(0, opens=False)])
    source = LiveSource(live_config(), capture_factory=factory)
    source.open()
    try:
        assert source.read(timeout=0.1) is None
        assert not source.status().connected
    finally:
        source.close()


def test_closing_a_live_source_ends_readers_and_threads():
    factory, _ = factory_from([FakeCapture(10_000, delay=0.001, then_fail=False)])
    source = LiveSource(live_config(), capture_factory=factory)
    source.open()
    source.read(timeout=1.0)
    source.close()
    with pytest.raises(EndOfStream):
        source.read(timeout=0.1)
    assert not any(t.name == "live-source" and t.is_alive() for t in threading.enumerate())


def test_credentials_are_not_in_the_status_error_label():
    factory, _ = factory_from([FakeCapture(0, opens=False)])
    source = LiveSource(live_config(), capture_factory=factory)
    assert "secret" not in source._label
