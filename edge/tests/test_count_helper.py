"""count-helper: the labeling logic (no window needed)."""

from __future__ import annotations

import numpy as np
import pytest

from countvision_edge.evaluation.count_helper import LabelSession, open_session, render
from countvision_edge.evaluation.labels import load_labels


@pytest.fixture
def session(tmp_path, demo_video) -> LabelSession:
    return open_session(demo_video, tmp_path / "clip.yaml", classes=["person", "car"], split="tune",
                        labeled_by="tester", occupancy_every_s=5)


def test_new_session_creates_a_label_file(session):
    assert session.path.is_file()
    labels = load_labels(session.path)
    assert labels.clip == "demo" and labels.split == "tune" and labels.labeled_by == "tester"
    assert session.fps == 15 and session.n_frames == 270


def test_draw_line_mark_in_out_and_reload(session, tmp_path, demo_video):
    session.handle_key("i")
    assert "line first" in session.message  # nothing to mark yet
    session.handle_key("t")
    session.click((0.1, 0.5))
    session.click((0.9, 0.5))
    assert [line.name for line in session.labels.lines] == ["line1"] and session.tool == "off"
    session.seek(45)  # 3.0 s
    session.handle_key("i")
    session.seek(160)
    session.handle_key("c")  # class -> car
    session.handle_key("o")
    reloaded = open_session(demo_video, tmp_path / "clip.yaml")
    assert [(c.t, c.dir, c.cls) for c in reloaded.labels.crossings] == [
        (3.0, "in", "person"), (pytest.approx(10.67, abs=0.01), "out", "car")]
    assert "IN 1 OUT 1" in reloaded.summary()


def test_delete_nearest_and_undo(session):
    session.add_line((0, 0.5), (1, 0.5))
    session.seek(30)
    session.mark("in")
    session.seek(60)
    session.mark("out")
    session.seek(62)
    assert session.delete_nearest()
    assert [c.dir for c in session.labels.crossings] == ["in"]
    session.seek(200)
    assert not session.delete_nearest()  # nothing within 1 s
    assert session.undo()
    assert [c.dir for c in session.labels.crossings] == ["in", "out"]
    assert [c.dir for c in load_labels(session.path).crossings] == ["in", "out"]  # undo is saved


def test_zone_occupancy_keys(session):
    session.handle_key("3")
    assert "zone first" in session.message
    session.handle_key("t")
    session.handle_key("t")  # tool: zone
    for p in [(0.1, 0.6), (0.5, 0.6), (0.5, 0.95)]:
        session.click(p)
    session.close_zone()
    assert [z.name for z in session.labels.zones] == ["zone1"]
    session.handle_key("2")
    session.handle_key("+")
    session.handle_key("g")  # next occupancy stop: 5 s
    assert session.t == 5.0
    session.handle_key("0")
    occ = [(o.t, o.n) for o in session.labels.occupancy]
    assert occ == [(0.0, 3), (5.0, 0)]  # "2" then "+" replaced the sample at 0 s


def test_flip_navigation_and_render(session):
    session.add_line((0, 0.5), (1, 0.5))
    session.add_zone([(0, 0.6), (0.5, 0.6), (0.5, 1)])
    assert session.active_target() == ("zone", "zone1")
    session.handle_key("n")
    assert session.active_target() == ("line", "line1")
    session.handle_key("f")
    assert session.labels.lines[0].in_direction == "to_left"
    session.seek(30)
    session.mark("in")
    session.seek(0)
    assert session.jump_mark(True) and session.frame == 30 - 15  # 1 s before the mark
    session.handle_key("right")
    assert session.frame == 16
    session.handle_key("l")
    assert session.frame == 31
    image = np.zeros((360, 640, 3), np.uint8)
    out = render(session, image, show_help=True, playing=False, speed=1.0)
    assert out.shape == image.shape and out.any()
    assert not image.any()  # the original frame is not changed


def test_zone_needs_three_points(session):
    session.tool = "zone"
    session.click((0.1, 0.1))
    session.click((0.2, 0.1))
    session.close_zone()
    assert session.labels.zones == [] and "3 points" in session.message


def test_gui_loop_with_a_fake_window(session, demo_video, monkeypatch):
    """Drive run_gui with scripted keys (no screen needed)."""
    import cv2

    from countvision_edge.evaluation import count_helper

    keys = iter([ord("t"), -1, ord(" "), -1, -1, ord(" "), 2555904, ord("i"), ord("]"), ord("h"), 27])
    clicks = {}

    def named_window(name, flags=0):
        clicks["name"] = name

    def set_mouse(name, callback):
        clicks["cb"] = callback

    def wait_key(delay):
        key = next(keys)
        if key == -1 and "cb" in clicks and not clicks.get("done"):
            clicks["cb"](cv2.EVENT_LBUTTONDOWN, 64, 180, 0, None)
            clicks["cb"](cv2.EVENT_LBUTTONDOWN, 576, 180, 0, None)
            clicks["done"] = True
        return key

    monkeypatch.setattr(count_helper.cv2, "namedWindow", named_window)
    monkeypatch.setattr(count_helper.cv2, "setMouseCallback", set_mouse)
    monkeypatch.setattr(count_helper.cv2, "imshow", lambda *a: None)
    monkeypatch.setattr(count_helper.cv2, "waitKeyEx", wait_key)
    monkeypatch.setattr(count_helper.cv2, "getWindowProperty", lambda *a: 1.0)
    monkeypatch.setattr(count_helper.cv2, "destroyAllWindows", lambda: None)
    result = count_helper.run_gui(session, demo_video)
    assert [line.name for line in result.labels.lines] == ["line1"]
    assert result.labels.lines[0].p1 == pytest.approx((0.1, 0.5))
    assert len(result.labels.crossings) == 1 and result.labels.crossings[0].dir == "in"
    assert result.frame >= 3  # played two frames, then one step to the right
    assert len(load_labels(session.path).crossings) == 1
