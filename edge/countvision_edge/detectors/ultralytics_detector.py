"""Ultralytics YOLO detector (PyTorch, or an exported ONNX / OpenVINO / TensorRT model).

LICENCE: the ultralytics package and the official YOLO weights are AGPL-3.0. A closed-source
paid product that uses them needs an Ultralytics Enterprise licence. Use the RF-DETR detector
(Apache-2.0) or your own permissively licensed model for the commercial product. This class is
the only place that imports ultralytics.
"""

from __future__ import annotations

import logging

import numpy as np

from ..errors import DetectorError
from ..types import Detections
from .base import Detector, DetectorInfo

log = logging.getLogger(__name__)

LICENSE = (
    "AGPL-3.0 (Ultralytics code and official weights). "
    "A closed-source paid product needs an Ultralytics Enterprise licence."
)


def runtime_from_path(model: str) -> str:
    """Guess the runtime from the model path."""
    path = model.lower().rstrip("/\\")
    if path.endswith(".onnx"):
        return "onnxruntime"
    if path.endswith("_openvino_model") or path.endswith(".xml"):
        return "openvino"
    if path.endswith(".engine"):
        return "tensorrt"
    return "pytorch"


class UltralyticsDetector(Detector):
    """Wraps ``ultralytics.YOLO``. The runtime follows the model file type:
    ``.pt`` = PyTorch (CUDA if available), ``.onnx`` = ONNX Runtime,
    ``*_openvino_model/`` = OpenVINO, ``.engine`` = TensorRT."""

    def __init__(
        self,
        model: str,
        *,
        imgsz: int = 640,
        conf: float = 0.2,
        iou: float = 0.5,
        device: str = "auto",
        classes: list[str] | None = None,
    ) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise DetectorError(
                "The ultralytics package is not installed. Run: pip install ultralytics\n"
                "Note: ultralytics is AGPL-3.0. For a closed-source product use type: rfdetr."
            ) from exc
        try:
            self._model = YOLO(model)
        except Exception as exc:  # noqa: BLE001
            raise DetectorError(f"Could not load model '{model}': {exc}") from exc
        self.names = {int(k): str(v) for k, v in dict(self._model.names).items()}
        self.imgsz, self.conf, self.iou = imgsz, conf, iou
        self._device = None if device == "auto" else device
        ids = self.class_ids(classes)
        self._classes = sorted(ids) if ids else None
        self.info = DetectorInfo(
            name=f"ultralytics:{model}",
            runtime=runtime_from_path(model),
            license=LICENSE,
            device=device,
        )

    def detect(self, image: np.ndarray) -> Detections:
        results = self._model.predict(
            image,
            imgsz=self.imgsz,
            conf=self.conf,
            iou=self.iou,
            classes=self._classes,
            device=self._device,
            verbose=False,
        )
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return Detections.empty()
        return Detections.from_arrays(
            boxes.xyxy.cpu().numpy(), boxes.conf.cpu().numpy(), boxes.cls.cpu().numpy()
        )

    def warmup(self) -> None:
        self.detect(np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8))
