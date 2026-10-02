"""Decoders: turn raw model outputs into Detections (pure NumPy, no framework needed).

Supported output layouts
  yolo      YOLOv8 / YOLO11 / YOLO12 style, one output (1, 4+nc, N) or (1, N, 4+nc):
            box centre x, y, width, height in network pixels, then one score per class.
            NMS is applied here.
  yolo_e2e  NMS-free YOLO export (YOLOv10, YOLO26): one output (1, N, 6) =
            x1, y1, x2, y2, score, class in network pixels.
  detr      DETR family (RF-DETR, RT-DETR): either two outputs, boxes (1, Q, 4) and class
            logits (1, Q, nc), or one output (1, Q, 4+nc). Boxes are normalised centre x, y,
            width, height. No NMS.
"""

from __future__ import annotations

import numpy as np

from ..types import Detections


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> np.ndarray:
    """Greedy non-maximum suppression. Returns the indices to keep, best score first."""
    if len(boxes) == 0:
        return np.zeros((0,), dtype=int)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        inter_w = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        inter_h = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = inter_w * inter_h
        iou = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[iou <= iou_threshold]
    return np.asarray(keep, dtype=int)


def batched_nms(
    boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, iou_threshold: float
) -> np.ndarray:
    """NMS per class (boxes of different classes never suppress each other)."""
    if len(boxes) == 0:
        return np.zeros((0,), dtype=int)
    offset = classes.astype(np.float32)[:, None] * (float(boxes.max()) + 1.0)
    return nms(boxes + offset, scores, iou_threshold)


def _cxcywh_to_xyxy(b: np.ndarray) -> np.ndarray:
    out = np.empty_like(b)
    out[:, 0] = b[:, 0] - b[:, 2] / 2
    out[:, 1] = b[:, 1] - b[:, 3] / 2
    out[:, 2] = b[:, 0] + b[:, 2] / 2
    out[:, 3] = b[:, 1] + b[:, 3] / 2
    return out


def _clip(xyxy: np.ndarray, width: int, height: int) -> np.ndarray:
    xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, width)
    xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, height)
    return xyxy


def _finish(xyxy, conf, cls, width, height, max_det) -> Detections:
    xyxy = _clip(xyxy, width, height)
    valid = (xyxy[:, 2] - xyxy[:, 0] > 1) & (xyxy[:, 3] - xyxy[:, 1] > 1)
    xyxy, conf, cls = xyxy[valid], conf[valid], cls[valid]
    if len(conf) > max_det:
        top = np.argsort(conf)[::-1][:max_det]
        xyxy, conf, cls = xyxy[top], conf[top], cls[top]
    return Detections.from_arrays(xyxy, conf, cls)


def decode_yolo(
    output: np.ndarray,
    *,
    conf_threshold: float,
    iou_threshold: float,
    class_filter: set[int] | None,
    scale: float,
    pad: tuple[float, float],
    orig_size: tuple[int, int],
    max_det: int = 300,
) -> Detections:
    """Decode a YOLOv8/11-style output. ``orig_size`` is (width, height)."""
    pred = np.asarray(output, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    if pred.shape[0] < pred.shape[1]:  # (4+nc, N) -> (N, 4+nc)
        pred = pred.T
    scores_all = pred[:, 4:]
    cls = scores_all.argmax(axis=1)
    conf = scores_all[np.arange(len(pred)), cls]
    mask = conf >= conf_threshold
    if class_filter is not None:
        mask &= np.isin(cls, list(class_filter))
    if not mask.any():
        return Detections.empty()
    boxes, conf, cls = _cxcywh_to_xyxy(pred[mask, :4]), conf[mask], cls[mask]
    keep = batched_nms(boxes, conf, cls, iou_threshold)[:max_det]
    boxes, conf, cls = boxes[keep], conf[keep], cls[keep]
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad[0]) / scale
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad[1]) / scale
    return _finish(boxes, conf, cls, orig_size[0], orig_size[1], max_det)


def decode_yolo_e2e(
    output: np.ndarray,
    *,
    conf_threshold: float,
    class_filter: set[int] | None,
    scale: float,
    pad: tuple[float, float],
    orig_size: tuple[int, int],
    max_det: int = 300,
) -> Detections:
    """Decode an NMS-free YOLO output of rows (x1, y1, x2, y2, score, class)."""
    pred = np.asarray(output, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    mask = pred[:, 4] >= conf_threshold
    if class_filter is not None:
        mask &= np.isin(pred[:, 5].astype(int), list(class_filter))
    pred = pred[mask]
    if len(pred) == 0:
        return Detections.empty()
    boxes = pred[:, :4].copy()
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad[0]) / scale
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad[1]) / scale
    return _finish(boxes, pred[:, 4], pred[:, 5].astype(int), orig_size[0], orig_size[1], max_det)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50.0, 50.0)))


def decode_detr(
    outputs: list[np.ndarray],
    *,
    conf_threshold: float,
    class_filter: set[int] | None,
    orig_size: tuple[int, int],
    net_size: tuple[int, int],
    max_det: int = 300,
) -> Detections:
    """Decode DETR-style outputs. ``orig_size`` and ``net_size`` are (width, height).

    Two outputs (boxes, logits): scores = sigmoid(logits).
    One output (1, Q, 4+nc): scores are used as they are, or passed through a sigmoid when
    they are outside 0..1.
    Boxes are normalised 0..1 (centre x, y, w, h). Values above 2 are taken as pixels of the
    network input.
    """
    if len(outputs) >= 2:
        arrays = [np.asarray(o, dtype=np.float32) for o in outputs]
        boxes_arr = next(a for a in arrays if a.shape[-1] == 4)
        logits = next(a for a in arrays if a is not boxes_arr)
        boxes = boxes_arr.reshape(-1, 4)
        scores = _sigmoid(logits.reshape(boxes.shape[0], -1))
    else:
        single = np.asarray(outputs[0], dtype=np.float32).reshape(-1, np.asarray(outputs[0]).shape[-1])
        boxes, scores = single[:, :4], single[:, 4:]
        if scores.size and (scores.max() > 1.0 or scores.min() < 0.0):
            scores = _sigmoid(scores)
    cls = scores.argmax(axis=1)
    conf = scores[np.arange(len(scores)), cls]
    mask = conf >= conf_threshold
    if class_filter is not None:
        mask &= np.isin(cls, list(class_filter))
    if not mask.any():
        return Detections.empty()
    boxes, conf, cls = boxes[mask].copy(), conf[mask], cls[mask]
    if boxes.max() > 2.0:  # pixels of the network input -> normalised
        boxes[:, [0, 2]] /= net_size[0]
        boxes[:, [1, 3]] /= net_size[1]
    xyxy = _cxcywh_to_xyxy(boxes)
    xyxy[:, [0, 2]] *= orig_size[0]
    xyxy[:, [1, 3]] *= orig_size[1]
    return _finish(xyxy, conf, cls, orig_size[0], orig_size[1], max_det)
