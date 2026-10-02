from __future__ import annotations

import numpy as np
import pytest

from countvision_edge.analytics.heatmap import Heatmap
from countvision_edge.analytics.motion import MotionEstimator, SpeedCalibration

from .helpers import make_track


def test_calibration_gives_metres_per_pixel_and_kmh():
    cal = SpeedCalibration((0, 0), (100, 0), 10.0)  # 100 px = 10 m
    assert cal.meters_per_pixel == pytest.approx(0.1)
    assert cal.kmh(50.0) == pytest.approx(18.0)  # 50 px/s = 5 m/s = 18 km/h


def test_calibration_rejects_bad_input():
    with pytest.raises(ValueError):
        SpeedCalibration((5, 5), (5, 5), 10.0)
    with pytest.raises(ValueError):
        SpeedCalibration((0, 0), (5, 5), 0.0)


def test_motion_estimator_measures_speed_in_pixels_boxheights_and_kmh():
    motion = MotionEstimator(calibration=SpeedCalibration((0, 0), (100, 0), 10.0))
    last = None
    for i in range(20):  # 10 FPS, moving 5 px per frame = 50 px/s
        track = make_track(1, 100 + 5 * i, 300, h=100.0)
        motion.update([track], i / 10)
        last = track
    assert last.speed_px_s == pytest.approx(50.0, rel=0.02)
    assert last.speed_bh_s == pytest.approx(0.5, rel=0.02)
    assert last.speed_kmh == pytest.approx(18.0, rel=0.02)


def test_a_new_track_has_speed_zero_until_it_has_history():
    motion = MotionEstimator()
    track = make_track(1, 100, 100)
    motion.update([track], 0.0)
    assert track.speed_px_s == 0.0 and track.speed_kmh is None


def test_a_standing_object_has_zero_speed():
    motion = MotionEstimator()
    track = None
    for i in range(20):
        track = make_track(1, 200, 200)
        motion.update([track], i / 10)
    assert track.speed_px_s == pytest.approx(0.0, abs=1e-6)


def test_motion_forgets_old_tracks():
    motion = MotionEstimator(memory_s=5.0)
    motion.update([make_track(1, 0, 0)], 0.0)
    motion.update([make_track(2, 0, 0)], 20.0)
    assert 1 not in motion._history


def test_heatmap_accumulates_seconds_at_the_right_cell():
    heat = Heatmap((640, 360), grid_width=64)
    assert heat.grid.shape == (36, 64)
    for _ in range(10):
        heat.add([(320.0, 180.0)], 0.5)
    assert heat.grid[18, 32] == pytest.approx(5.0)
    assert heat.grid.sum() == pytest.approx(5.0)


def test_heatmap_clamps_points_outside_the_frame():
    heat = Heatmap((100, 100), grid_width=10)
    heat.add([(-50.0, 500.0), (1e6, -1e6)], 1.0)
    assert heat.grid.sum() == pytest.approx(2.0)


def test_heatmap_render_overlay_and_save(tmp_path):
    heat = Heatmap((200, 100), grid_width=40)
    heat.add([(100.0, 50.0)] * 5, 1.0)
    image = heat.render()
    assert image.shape == (100, 200, 3)
    base = np.full((100, 200, 3), 30, dtype=np.uint8)
    blended = heat.overlay(base)
    assert blended.shape == base.shape
    assert (blended[50, 100] != base[50, 100]).any()  # hot spot is coloured
    assert (blended[5, 5] == base[5, 5]).all()  # empty area unchanged
    npy, png = heat.save(tmp_path / "h", "cam")
    assert npy.exists() and png.exists()
    assert np.load(npy).sum() == pytest.approx(5.0)


def test_empty_heatmap_renders_without_error():
    heat = Heatmap((100, 100))
    assert heat.render().shape == (100, 100, 3)


def test_heatmap_resize_resets_the_grid_shape():
    heat = Heatmap((640, 360), grid_width=64)
    heat.set_frame_size((320, 320))
    assert heat.grid.shape == (64, 64)
