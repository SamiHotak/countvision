"""YOLOX decoder (Apache-2.0 model family): grid decoding, auto detection, preprocessing."""

from __future__ import annotations

import math

import numpy as np
import pytest

from countvision_edge.detectors.decoders import decode_yolox, yolox_anchor_count
from countvision_edge.detectors.model_detector import ModelDetector, resolve_decoder
from countvision_edge.detectors.preprocess import letterbox, to_tensor

NET = (64, 64)  # (w, h): rows = 8*8 + 4*4 + 2*2 = 84


def _empty_output(nc: int = 80) -> np.ndarray:
    out = np.zeros((1, yolox_anchor_count(NET), 5 + nc), dtype=np.float32)
    out[0, :, 2:4] = -5.0  # tiny boxes
    return out


def _put(out: np.ndarray, row: int, dx: float, dy: float, w_px: float, h_px: float,
         stride: int, obj: float, cls: int, score: float) -> None:
    out[0, row, 0], out[0, row, 1] = dx, dy
    out[0, row, 2], out[0, row, 3] = math.log(w_px / stride), math.log(h_px / stride)
    out[0, row, 4] = obj
    out[0, row, 5 + cls] = score


def test_anchor_count() -> None:
    assert yolox_anchor_count((416, 416)) == 3549
    assert yolox_anchor_count((640, 640)) == 8400
    assert yolox_anchor_count(NET) == 84


def test_decode_grid_cell_to_pixels() -> None:
    out = _empty_output()
    # stride 8 grid is 8x8; cell (x=3, y=2) is row 2*8+3 = 19
    _put(out, 19, 0.5, 0.5, 16, 24, 8, obj=0.9, cls=0, score=0.8)
    det = decode_yolox(out, conf_threshold=0.3, iou_threshold=0.5, class_filter=None,
                       scale=1.0, pad=(0.0, 0.0), orig_size=(64, 64), net_size=NET)
    assert len(det) == 1
    cx, cy = (3 + 0.5) * 8, (2 + 0.5) * 8
    np.testing.assert_allclose(det.xyxy[0], [cx - 8, cy - 12, cx + 8, cy + 12], atol=1e-3)
    assert det.class_id[0] == 0
    assert det.confidence[0] == pytest.approx(0.72, abs=1e-5)  # obj * cls


def test_decode_uses_stride_16_rows_and_scale() -> None:
    out = _empty_output()
    # stride 16 grid starts after 64 rows; cell (1, 1) = row 64 + 1*4 + 1 = 69
    _put(out, 69, 0.0, 0.0, 32, 32, 16, obj=1.0, cls=2, score=0.9)
    det = decode_yolox(out, conf_threshold=0.3, iou_threshold=0.5, class_filter=None,
                       scale=0.5, pad=(0.0, 0.0), orig_size=(128, 128), net_size=NET)
    np.testing.assert_allclose(det.xyxy[0], [0, 0, 64, 64], atol=1e-3)  # (16-16)/0.5 .. (16+16)/0.5
    assert det.class_id[0] == 2


def test_low_score_filter_class_filter_and_nms() -> None:
    out = _empty_output()
    _put(out, 19, 0.5, 0.5, 16, 24, 8, obj=0.9, cls=0, score=0.9)
    _put(out, 20, -0.4, 0.5, 16, 24, 8, obj=0.9, cls=0, score=0.5)  # same person, lower score
    _put(out, 40, 0.5, 0.5, 8, 8, 8, obj=0.2, cls=0, score=0.5)  # 0.1 < threshold
    _put(out, 50, 0.5, 0.5, 8, 8, 8, obj=0.9, cls=3, score=0.9)  # filtered class
    det = decode_yolox(out, conf_threshold=0.3, iou_threshold=0.5, class_filter={0},
                       scale=1.0, pad=(0.0, 0.0), orig_size=(64, 64), net_size=NET)
    assert len(det) == 1


def test_wrong_row_count_is_a_clear_error() -> None:
    out = np.zeros((1, 10, 85), dtype=np.float32)
    with pytest.raises(ValueError, match="expected 84"):
        decode_yolox(out, conf_threshold=0.3, iou_threshold=0.5, class_filter=None,
                     scale=1.0, pad=(0.0, 0.0), orig_size=(64, 64), net_size=NET)


def test_resolve_decoder_recognises_yolox_but_not_detr() -> None:
    assert resolve_decoder("auto", [(1, 3549, 85)], (416, 416)) == "yolox"
    assert resolve_decoder("auto", [(1, 300, 84)], (560, 560)) == "detr"
    assert resolve_decoder("auto", [(1, 84, 8400)], (640, 640)) == "yolo"


def test_top_left_letterbox_and_raw_tensor() -> None:
    image = np.full((50, 100, 3), 200, dtype=np.uint8)
    image[:, :, 0] = 10  # blue channel
    padded, scale, pad = letterbox(image, (64, 64), center=False)
    assert pad == (0.0, 0.0) and scale == pytest.approx(0.64)
    assert padded.shape == (64, 64, 3)
    assert (padded[40:, :, :] == 114).all()  # padding only at the bottom
    tensor = to_tensor(padded, "raw")
    assert tensor.shape == (1, 3, 64, 64)
    assert tensor[0, 0, 0, 0] == 10.0  # still BGR and 0..255


class _FakeBackend:
    name = "fake"
    device = "cpu"
    input_shape = (1, 3, 64, 64)

    def __init__(self) -> None:
        self.seen: np.ndarray | None = None

    def output_shapes(self):
        return [(1, 84, 85)]

    def run(self, tensor: np.ndarray):
        self.seen = tensor
        out = _empty_output()
        _put(out, 19, 0.5, 0.5, 16, 24, 8, obj=0.9, cls=0, score=0.9)
        return [out]


def test_model_detector_auto_yolox_maps_boxes_back() -> None:
    backend = _FakeBackend()
    det = ModelDetector(backend, model_name="fake", license_text="Apache-2.0")
    assert det.decoder == "yolox" and det.normalize == "raw"
    image = np.zeros((32, 128, 3), dtype=np.uint8)  # scale 0.5, image at the top
    result = det.detect(image)
    assert backend.seen is not None and backend.seen.max() <= 255.0
    # net box (20, 8, 36, 32) -> /0.5 = (40, 16, 72, 64) -> clipped to height 32
    np.testing.assert_allclose(result.xyxy[0], [40, 16, 72, 32], atol=1e-3)
    assert det.names[0] == "person"
