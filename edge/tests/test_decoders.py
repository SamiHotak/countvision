from __future__ import annotations

import numpy as np
import pytest

from countvision_edge.config import DetectorConfig
from countvision_edge.detectors.coco import COCO80, COCO91, resolve_names
from countvision_edge.detectors.decoders import (
    batched_nms,
    decode_detr,
    decode_yolo,
    decode_yolo_e2e,
    nms,
)
from countvision_edge.detectors.factory import build_detector
from countvision_edge.detectors.model_detector import resolve_decoder
from countvision_edge.detectors.preprocess import letterbox, to_tensor
from countvision_edge.errors import DetectorError


def yolo_output(rows, nc: int = 2, n: int = 50) -> np.ndarray:
    """Build a YOLOv8-style output (1, 4+nc, n) from rows (cx, cy, w, h, class, score)."""
    out = np.zeros((4 + nc, n), dtype=np.float32)
    for i, (cx, cy, w, h, cls, score) in enumerate(rows):
        out[:4, i] = (cx, cy, w, h)
        out[4 + cls, i] = score
    return out[None]


# --------------------------------------------------------------------------- basics


def test_nms_removes_overlapping_boxes_of_the_same_class():
    boxes = np.array([[0, 0, 100, 100], [5, 5, 105, 105], [300, 300, 400, 400]], dtype=np.float32)
    scores = np.array([0.8, 0.9, 0.7], dtype=np.float32)
    assert nms(boxes, scores, 0.5).tolist() == [1, 2]


def test_batched_nms_keeps_overlapping_boxes_of_different_classes():
    boxes = np.array([[0, 0, 100, 100], [5, 5, 105, 105]], dtype=np.float32)
    keep = batched_nms(boxes, np.array([0.9, 0.8]), np.array([0, 2]), 0.5)
    assert sorted(keep.tolist()) == [0, 1]


def test_letterbox_scale_and_padding_for_a_wide_image():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    padded, scale, pad = letterbox(image, (640, 640))
    assert padded.shape == (640, 640, 3) and scale == 0.5 and pad == (0.0, 140.0)


def test_letterbox_for_a_tall_image():
    padded, scale, pad = letterbox(np.zeros((1280, 720, 3), dtype=np.uint8), (640, 640))
    assert padded.shape == (640, 640, 3) and scale == 0.5 and pad == (140.0, 0.0)


def test_to_tensor_layout_and_imagenet_normalisation():
    image = np.zeros((4, 6, 3), dtype=np.uint8)
    image[:, :, 2] = 255  # BGR red
    plain = to_tensor(image)
    assert plain.shape == (1, 3, 4, 6) and plain.dtype == np.float32
    assert plain[0, 0].min() == 1.0 and plain[0, 1].max() == 0.0  # RGB order: R channel first
    normed = to_tensor(image, "imagenet")
    assert normed[0, 0, 0, 0] == pytest.approx((1.0 - 0.485) / 0.229)


def test_coco_names():
    assert COCO91[1] == "person" and COCO91[3] == "car" and len(COCO91) == 80
    assert COCO80[0] == "person" and COCO80[2] == "car" and COCO80[5] == "bus" and len(COCO80) == 80
    assert resolve_names(None, 91)[1] == "person"
    assert resolve_names(None, 80)[0] == "person"
    assert resolve_names(["a", "b"]) == {0: "a", 1: "b"}
    assert resolve_names(None, 3) == {0: "0", 1: "1", 2: "2"}


# --------------------------------------------------------------------------- YOLO decoder


def test_decode_yolo_maps_boxes_back_to_the_original_image():
    # Original 1280x720 image, object at (400,200)-(600,500). Network 640x640: scale 0.5, pad_y 140.
    net_cx, net_cy, net_w, net_h = 250.0, 315.0, 100.0, 150.0
    out = yolo_output([(net_cx, net_cy, net_w, net_h, 0, 0.9)])
    det = decode_yolo(out, conf_threshold=0.3, iou_threshold=0.5, class_filter=None,
                      scale=0.5, pad=(0.0, 140.0), orig_size=(1280, 720))
    assert len(det) == 1 and det.class_id[0] == 0
    assert det.xyxy[0] == pytest.approx([400, 200, 600, 500], abs=0.5)
    assert det.confidence[0] == pytest.approx(0.9)


def test_decode_yolo_threshold_class_filter_and_nms():
    out = yolo_output(
        [
            (100, 100, 50, 50, 0, 0.9),
            (102, 101, 50, 50, 0, 0.6),  # duplicate of the first: removed by NMS
            (300, 300, 50, 50, 1, 0.8),  # other class
            (400, 400, 50, 50, 0, 0.1),  # below threshold
        ]
    )
    common = {"conf_threshold": 0.3, "iou_threshold": 0.5, "scale": 1.0, "pad": (0.0, 0.0),
              "orig_size": (640, 640)}
    both = decode_yolo(out, class_filter=None, **common)
    assert sorted(both.class_id.tolist()) == [0, 1]
    only_class_1 = decode_yolo(out, class_filter={1}, **common)
    assert only_class_1.class_id.tolist() == [1]


def test_decode_yolo_accepts_channels_last_and_empty_output():
    out = yolo_output([(100, 100, 50, 50, 0, 0.9)])
    swapped = np.ascontiguousarray(out.transpose(0, 2, 1))
    det = decode_yolo(swapped, conf_threshold=0.3, iou_threshold=0.5, class_filter=None,
                      scale=1.0, pad=(0.0, 0.0), orig_size=(640, 640))
    assert len(det) == 1
    empty = decode_yolo(yolo_output([]), conf_threshold=0.3, iou_threshold=0.5, class_filter=None,
                        scale=1.0, pad=(0.0, 0.0), orig_size=(640, 640))
    assert len(empty) == 0


def test_decode_yolo_clips_boxes_to_the_image():
    out = yolo_output([(10, 10, 100, 100, 0, 0.9)])  # sticks out at the top-left
    det = decode_yolo(out, conf_threshold=0.3, iou_threshold=0.5, class_filter=None,
                      scale=1.0, pad=(0.0, 0.0), orig_size=(640, 480))
    assert det.xyxy[0].min() >= 0


def test_decode_yolo_e2e():
    out = np.array([[[100, 100, 200, 300, 0.9, 0], [10, 10, 50, 50, 0.1, 2], [300, 100, 340, 200, 0.8, 2]]],
                   dtype=np.float32)
    det = decode_yolo_e2e(out, conf_threshold=0.3, class_filter={2}, scale=0.5, pad=(0.0, 140.0),
                          orig_size=(1280, 720))
    assert det.class_id.tolist() == [2]
    assert det.xyxy[0] == pytest.approx([600, 0, 680, 120], abs=0.5)  # (300-0)/.5, (100-140)/.5 -> clipped to 0


# --------------------------------------------------------------------------- DETR decoder


def test_decode_detr_two_outputs_with_logits():
    boxes = np.zeros((1, 300, 4), dtype=np.float32)
    logits = np.full((1, 300, 91), -10.0, dtype=np.float32)
    boxes[0, 0] = (0.5, 0.5, 0.25, 0.5)
    logits[0, 0, 1] = 3.0  # class 1 = person in COCO91
    boxes[0, 1] = (0.2, 0.2, 0.1, 0.1)
    logits[0, 1, 3] = 2.0  # class 3 = car
    det = decode_detr([boxes, logits], conf_threshold=0.5, class_filter=None,
                      orig_size=(1280, 720), net_size=(560, 560))
    assert sorted(det.class_id.tolist()) == [1, 3]
    person = det.xyxy[det.class_id == 1][0]
    assert person == pytest.approx([480, 180, 800, 540], abs=0.5)
    assert det.confidence[det.class_id == 1][0] == pytest.approx(1 / (1 + np.exp(-3.0)), abs=1e-4)
    only_person = decode_detr([boxes, logits], conf_threshold=0.5, class_filter={1},
                              orig_size=(1280, 720), net_size=(560, 560))
    assert only_person.class_id.tolist() == [1]


def test_decode_detr_single_output_with_probabilities_and_pixel_boxes():
    out = np.zeros((1, 10, 4 + 3), dtype=np.float32)
    out[0, 0, :4] = (280, 280, 140, 280)  # pixels of a 560x560 network input
    out[0, 0, 4 + 2] = 0.9
    det = decode_detr([out], conf_threshold=0.5, class_filter=None, orig_size=(1120, 1120),
                      net_size=(560, 560))
    assert det.class_id.tolist() == [2]
    assert det.xyxy[0] == pytest.approx([420, 280, 700, 840], abs=0.5)


def test_decode_detr_nothing_found():
    boxes = np.zeros((1, 5, 4), dtype=np.float32)
    logits = np.full((1, 5, 3), -10.0, dtype=np.float32)
    assert len(decode_detr([boxes, logits], conf_threshold=0.5, class_filter=None,
                           orig_size=(100, 100), net_size=(64, 64))) == 0


def test_resolve_decoder_from_output_shapes():
    assert resolve_decoder("yolo", [(1, 84, 8400)]) == "yolo"
    assert resolve_decoder("auto", [(1, 84, 8400)]) == "yolo"
    assert resolve_decoder("auto", [(1, 300, 6)]) == "yolo_e2e"
    assert resolve_decoder("auto", [(1, 300, 4), (1, 300, 91)]) == "detr"
    assert resolve_decoder("auto", [(1, 300, 84)]) == "detr"  # (1, queries, 4+nc)
    assert resolve_decoder("auto", [(1, None, None)]) == "yolo"  # unknown: YOLO is the default


# --------------------------------------------------------------------------- ONNX Runtime end to end


def save_toy_model(path, outputs: list[np.ndarray], input_hw=(640, 640)) -> str:
    """An ONNX 'model' whose outputs are constants. Tests the whole detector around it."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    x = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, *input_hw])
    names = [f"output{i}" for i in range(len(outputs))]
    ys = [helper.make_tensor_value_info(n, TensorProto.FLOAT, list(o.shape)) for n, o in zip(names, outputs, strict=True)]
    inits = [numpy_helper.from_array(o.astype(np.float32), name=f"const{i}") for i, o in enumerate(outputs)]
    nodes = [helper.make_node("Identity", [f"const{i}"], [n]) for i, n in enumerate(names)]
    graph = helper.make_graph(nodes, "toy", [x], ys, initializer=inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(path))
    return str(path)


@pytest.fixture
def toy_yolo(tmp_path) -> str:
    out = yolo_output(
        [(250, 315, 100, 150, 0, 0.9), (400, 400, 60, 60, 2, 0.8)], nc=80, n=100
    )
    return save_toy_model(tmp_path / "yolo.onnx", [out])


@pytest.fixture
def toy_detr(tmp_path) -> str:
    boxes = np.zeros((1, 50, 4), dtype=np.float32)
    logits = np.full((1, 50, 91), -10.0, dtype=np.float32)
    boxes[0, 0] = (0.5, 0.5, 0.25, 0.5)
    logits[0, 0, 1] = 3.0
    return save_toy_model(tmp_path / "detr.onnx", [boxes, logits], input_hw=(560, 560))


def check_yolo_detector(detector):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    det = detector.detect(image)
    assert det.class_id.tolist() == [0]  # classes: [person] filters the car out
    assert det.xyxy[0] == pytest.approx([400, 200, 600, 500], abs=0.5)
    assert detector.names[0] == "person" and detector.names[2] == "car"
    assert detector.info.license == "test-license"


def check_detr_detector(detector):
    det = detector.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
    assert det.class_id.tolist() == [1]
    assert det.xyxy[0] == pytest.approx([480, 180, 800, 540], abs=0.5)
    assert detector.names[1] == "person"


def test_onnxruntime_yolo_detector_through_the_factory(toy_yolo):
    pytest.importorskip("onnxruntime")
    cfg = DetectorConfig(type="onnx", model=toy_yolo, conf=0.3, classes=["person"], model_license="test-license")
    detector = build_detector(cfg)
    assert detector.info.runtime == "onnxruntime" and detector.decoder == "yolo"
    check_yolo_detector(detector)
    detector.warmup()


def test_onnxruntime_detr_detector_through_the_factory(toy_detr):
    pytest.importorskip("onnxruntime")
    cfg = DetectorConfig(type="onnx", model=toy_detr, conf=0.5, model_license="test-license")
    detector = build_detector(cfg)
    assert detector.decoder == "detr" and detector.normalize == "imagenet"
    check_detr_detector(detector)


def test_openvino_detectors_give_the_same_results(toy_yolo, toy_detr):
    pytest.importorskip("openvino")
    yolo = build_detector(DetectorConfig(type="openvino", model=toy_yolo, conf=0.3,
                                         classes=["person"], model_license="test-license"))
    assert yolo.info.runtime == "openvino"
    check_yolo_detector(yolo)
    detr = build_detector(DetectorConfig(type="openvino", model=toy_detr, conf=0.5, model_license="x"))
    check_detr_detector(detr)


def test_unknown_class_name_is_a_helpful_error(toy_yolo):
    pytest.importorskip("onnxruntime")
    cfg = DetectorConfig(type="onnx", model=toy_yolo, classes=["persn"], model_license="x")
    with pytest.raises(DetectorError, match="no class named 'persn'"):
        build_detector(cfg)


def test_missing_model_file_is_a_clear_error(tmp_path):
    pytest.importorskip("onnxruntime")
    with pytest.raises(DetectorError, match="Model file not found"):
        build_detector(DetectorConfig(type="onnx", model=str(tmp_path / "nope.onnx"), model_license="x"))


def test_blobs_detector_info_and_missing_optional_packages():
    detector = build_detector(DetectorConfig(type="blobs"))
    assert detector.info.runtime == "opencv"
    import importlib.util

    if importlib.util.find_spec("ultralytics") is None:
        with pytest.raises(DetectorError, match="AGPL"):
            build_detector(DetectorConfig(type="ultralytics"))
    if importlib.util.find_spec("rfdetr") is None:
        with pytest.raises(DetectorError, match="rfdetr"):
            build_detector(DetectorConfig(type="rfdetr"))
