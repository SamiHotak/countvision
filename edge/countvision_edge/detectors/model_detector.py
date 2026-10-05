"""Detector for exported models (ONNX / OpenVINO): backend + preprocessing + decoder."""

from __future__ import annotations

import logging

import numpy as np

from ..errors import DetectorError
from ..types import Detections
from .backends import InferenceBackend, Shape
from .base import Detector, DetectorInfo
from .coco import resolve_names
from .decoders import decode_detr, decode_yolo, decode_yolo_e2e, decode_yolox, yolox_anchor_count
from .preprocess import letterbox, to_tensor

log = logging.getLogger(__name__)


def resolve_decoder(
    requested: str, output_shapes: list[Shape], net_size: tuple[int, int] | None = None
) -> str:
    """Pick the decoder from the output layout when ``requested`` is "auto".

    ``net_size`` (width, height) lets us recognise YOLOX: its row count equals the number of
    grid cells for strides 8, 16 and 32.
    """
    if requested != "auto":
        return requested
    if len(output_shapes) >= 2:
        return "detr"
    shape = output_shapes[0] if output_shapes else ()
    if len(shape) == 3 and None not in shape[1:]:
        _, a, b = shape
        if b == 6 and a is not None and a > 6:
            return "yolo_e2e"
        if net_size is not None and b is not None and b > 5 and a == yolox_anchor_count(net_size):
            return "yolox"
        if a is not None and b is not None and a > b:
            return "detr"  # (1, queries, 4+nc)
    return "yolo"


def _num_classes(decoder: str, shapes: list[Shape]) -> int | None:
    try:
        if decoder == "yolo" and shapes:
            s = shapes[0]
            return (min(s[1], s[2]) - 4) if None not in s[1:] else None
        if decoder == "yolox" and shapes:
            return shapes[0][-1] - 5
        if decoder == "detr":
            if len(shapes) >= 2:
                logits = next(s for s in shapes if s and s[-1] != 4)
                return logits[-1]
            return shapes[0][-1] - 4
    except (IndexError, StopIteration, TypeError):
        return None
    return None


class ModelDetector(Detector):
    """Runs an exported detection model through an InferenceBackend.

    Preprocessing: YOLO-style decoders use a letterbox resize and scale to 0..1. YOLOX uses a
    top-left letterbox and raw BGR 0..255. The DETR decoder uses a plain resize (RF-DETR)
    with ImageNet normalisation, or none for ``normalize="none"`` (for example Ultralytics
    RT-DETR).
    """

    def __init__(
        self,
        backend: InferenceBackend,
        *,
        model_name: str,
        decoder: str = "auto",
        names_spec=None,
        conf: float = 0.2,
        iou: float = 0.5,
        classes: list[str] | None = None,
        imgsz: int = 640,
        normalize: str = "auto",
        license_text: str = "unknown",
    ) -> None:
        self.backend = backend
        shapes = backend.output_shapes()
        height = backend.input_shape[2] if len(backend.input_shape) == 4 else None
        width = backend.input_shape[3] if len(backend.input_shape) == 4 else None
        self.net_size = (int(width or imgsz), int(height or imgsz))  # (w, h)
        self.decoder = resolve_decoder(decoder, shapes, self.net_size)
        self.conf, self.iou = conf, iou
        if normalize == "auto":
            normalize = {"detr": "imagenet", "yolox": "raw"}.get(self.decoder, "none")
        self.normalize = normalize
        self.names = resolve_names(names_spec, _num_classes(self.decoder, shapes))
        self._class_filter = self.class_ids(classes)
        self.info = DetectorInfo(
            name=model_name, runtime=backend.name, license=license_text, device=backend.device
        )
        log.info(
            "Model %s: decoder=%s input=%sx%s classes=%d",
            model_name, self.decoder, self.net_size[0], self.net_size[1], len(self.names),
        )

    def detect(self, image: np.ndarray) -> Detections:
        orig_size = (int(image.shape[1]), int(image.shape[0]))
        if self.decoder == "detr":
            import cv2

            resized = cv2.resize(image, self.net_size, interpolation=cv2.INTER_LINEAR)
            tensor = to_tensor(resized, self.normalize)
            outputs = self.backend.run(tensor)
            return decode_detr(
                outputs,
                conf_threshold=self.conf,
                class_filter=self._class_filter,
                orig_size=orig_size,
                net_size=self.net_size,
            )
        top_left = self.decoder == "yolox"
        padded, scale, pad = letterbox(image, (self.net_size[1], self.net_size[0]), center=not top_left)
        outputs = self.backend.run(to_tensor(padded, self.normalize))
        if self.decoder == "yolox":
            return decode_yolox(
                outputs[0],
                conf_threshold=self.conf,
                iou_threshold=self.iou,
                class_filter=self._class_filter,
                scale=scale,
                pad=pad,
                orig_size=orig_size,
                net_size=self.net_size,
            )
        if self.decoder == "yolo_e2e":
            return decode_yolo_e2e(
                outputs[0],
                conf_threshold=self.conf,
                class_filter=self._class_filter,
                scale=scale,
                pad=pad,
                orig_size=orig_size,
            )
        if self.decoder == "yolo":
            return decode_yolo(
                outputs[0],
                conf_threshold=self.conf,
                iou_threshold=self.iou,
                class_filter=self._class_filter,
                scale=scale,
                pad=pad,
                orig_size=orig_size,
            )
        raise DetectorError(f"Unknown decoder: {self.decoder}")

    def warmup(self) -> None:
        dummy = np.zeros((self.net_size[1], self.net_size[0], 3), dtype=np.uint8)
        self.detect(dummy)
