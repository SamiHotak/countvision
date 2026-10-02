from __future__ import annotations

from countvision_edge.config import TrackerConfig
from countvision_edge.tracking import TrackManager
from countvision_edge.types import Detections

NAMES = {0: "person", 2: "car"}


def det(boxes, confs, classes) -> Detections:
    return Detections.from_arrays(boxes, confs, classes)


def manager(**kwargs) -> TrackManager:
    cfg = TrackerConfig(**{"min_track_frames": 3, **kwargs})
    return TrackManager(cfg, NAMES, frame_rate=15.0)


def box(x: float, y: float = 100.0):
    return [x, y, x + 30, y + 70]


def test_a_moving_object_keeps_one_id_and_is_confirmed_after_min_frames():
    tm = manager()
    seen = []
    for i in range(12):
        tracks = tm.update(det([box(10 + 5 * i)], [0.9], [0]), i / 15, i / 15)
        seen.append([t.track_id for t in tracks])
    first = next(i for i, ids in enumerate(seen) if ids)
    assert first >= 2  # not confirmed in the first frames
    ids = {t for frame in seen for t in frame}
    assert len(ids) == 1
    assert all(seen[i] for i in range(first, 12))  # present in every frame after confirmation


def test_two_objects_get_two_ids_and_keep_their_classes():
    tm = manager()
    last = []
    for i in range(10):
        dets = det([box(10 + 4 * i), box(300 - 4 * i, 200)], [0.9, 0.9], [0, 2])
        last = tm.update(dets, i / 15, i / 15)
    assert len(last) == 2
    by_class = {t.class_name: t.track_id for t in last}
    assert set(by_class) == {"person", "car"} and by_class["person"] != by_class["car"]


def test_class_smoothing_removes_a_single_flip():
    tm = manager(class_smoothing_window=9)
    classes = [0, 0, 0, 0, 2, 0, 0, 0]  # one frame where the detector says "car"
    out = []
    for i, c in enumerate(classes):
        out.append(tm.update(det([box(50 + 3 * i)], [0.9], [c]), i / 15, i / 15))
    flat = [t.class_name for frame in out for t in frame]
    assert flat and set(flat) == {"person"}


def test_track_survives_a_short_gap_with_the_same_id():
    tm = manager(lost_track_buffer=30)
    ids = []
    for i in range(20):
        if 8 <= i < 11:  # detector misses the object for three frames
            result = tm.update(Detections.empty(), i / 15, i / 15)
        else:
            result = tm.update(det([box(20 + 3 * i)], [0.9], [0]), i / 15, i / 15)
        ids += [t.track_id for t in result]
    assert len(set(ids)) == 1


def test_empty_detections_do_not_crash():
    tm = manager()
    for i in range(5):
        assert tm.update(Detections.empty(), i / 15, i / 15) == []


def test_low_confidence_boxes_never_start_a_track():
    tm = manager(track_activation_threshold=0.5, high_conf_det_threshold=0.5)
    for i in range(10):
        assert tm.update(det([box(10 + 3 * i)], [0.3], [0]), i / 15, i / 15) == []


def test_reset_clears_state():
    tm = manager()
    for i in range(6):
        tm.update(det([box(10 + 3 * i)], [0.9], [0]), i / 15, i / 15)
    tm.reset()
    assert tm.update(det([box(10)], [0.9], [0]), 10.0, 10.0) == []  # new track, not confirmed yet
