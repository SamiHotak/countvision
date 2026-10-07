"""Create a detector from the config."""

from __future__ import annotations

import logging

from ..config import DetectorConfig
from ..errors import DetectorError
from .base import Detector
from .blobs import BlobDetector

log = logging.getLogger(__name__)

_UNKNOWN_LICENSE = (
    "UNKNOWN. An exported model has the licence of the weights it came from "
    "(YOLO weights = AGPL-3.0). Set detector.model_license."
)


def build_detector(cfg: DetectorConfig) -> Detector:
    """Create the detector and log its licence."""
    detector = _create(cfg)
    log.info(
        "Detector: %s | runtime: %s | device: %s",
        detector.info.name, detector.info.runtime, detector.info.device,
    )
    log.info("Detector licence: %s", detector.info.license)
    return detector


def _create(cfg: DetectorConfig) -> Detector:
    if cfg.type == "blobs":
        return BlobDetector()
    if cfg.type == "ultralytics":
        from .ultralytics_detector import UltralyticsDetector

        return UltralyticsDetector(
            cfg.model,
            imgsz=cfg.imgsz,
            conf=cfg.conf,
            iou=cfg.iou,
            device=cfg.device,
            classes=cfg.classes,
        )
    if cfg.type == "rfdetr":
        from .rfdetr_detector import RfDetrDetector

        return RfDetrDetector(
            cfg.rfdetr_variant,
            model="" if cfg.model in ("", "pretrained", "yolo11n.pt") else cfg.model,
            conf=cfg.conf,
            classes=cfg.classes,
        )
    if cfg.type in ("onnx", "openvino"):
        from ..models import ensure_model
        from .backends import OnnxRuntimeBackend, OpenVinoBackend
        from .model_detector import ModelDetector

        ensure_model(cfg.model)  # known models (YOLOX) are downloaded on first start

        backend_cls = OnnxRuntimeBackend if cfg.type == "onnx" else OpenVinoBackend
        backend = backend_cls(cfg.model, cfg.device, cfg.threads)
        license_text = cfg.model_license or _UNKNOWN_LICENSE
        if cfg.model_license is None:
            log.warning("detector.model_license is not set. %s", _UNKNOWN_LICENSE)
        return ModelDetector(
            backend,
            model_name=f"{cfg.type}:{cfg.model}",
            decoder=cfg.decoder,
            names_spec=cfg.names,
            conf=cfg.conf,
            iou=cfg.iou,
            classes=cfg.classes,
            imgsz=cfg.imgsz,
            normalize=cfg.normalize,
            license_text=license_text,
        )
    raise DetectorError(f"Unknown detector type: {cfg.type}")
