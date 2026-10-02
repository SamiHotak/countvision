from __future__ import annotations

import pytest

from countvision_edge.scheduler import FrameScheduler


def test_no_target_processes_every_frame():
    s = FrameScheduler(None)
    assert all(s.should_process(i / 30) for i in range(100))
    assert s.processed == 100 and s.skipped == 0 and s.effective_fps is None


def test_half_rate_takes_every_second_frame_of_a_30_fps_stream():
    s = FrameScheduler(15.0, adaptive=False)
    taken = [i for i in range(60) if s.should_process(i / 30)]
    assert taken == list(range(0, 60, 2))
    assert s.skipped == 30


def test_target_above_source_rate_takes_all_frames():
    s = FrameScheduler(30.0, adaptive=False)
    assert all(s.should_process(i / 10) for i in range(20))  # a 10 FPS stream


def test_adaptive_lowers_the_rate_when_processing_is_slow_and_recovers():
    s = FrameScheduler(15.0, min_fps=2.0, adaptive=True)
    for _ in range(50):
        s.record_processing(0.2)  # 0.2 s per frame -> at most ~4.25 FPS with 15% headroom
    assert s.effective_fps == pytest.approx(0.85 / 0.2, rel=0.02)
    for _ in range(100):
        s.record_processing(0.01)  # fast again
    assert s.effective_fps == pytest.approx(15.0)


def test_adaptive_never_goes_below_min_fps():
    s = FrameScheduler(15.0, min_fps=2.0, adaptive=True)
    for _ in range(50):
        s.record_processing(5.0)
    assert s.effective_fps == 2.0


def test_not_adaptive_ignores_load():
    s = FrameScheduler(10.0, adaptive=False)
    for _ in range(50):
        s.record_processing(1.0)
    assert s.effective_fps == 10.0


def test_a_late_burst_does_not_cause_catch_up():
    s = FrameScheduler(10.0, adaptive=False)
    assert s.should_process(0.0)
    assert s.should_process(5.0)  # long gap
    assert not s.should_process(5.01)  # no burst afterwards
