"""RF-DETR detector (PyTorch). Permissive licence option for a commercial product.

LICENCE: RF-DETR code and the standard COCO checkpoints are Apache-2.0. Check the licence of
the exact checkpoint you ship (some larger sizes may use a different licence).

NOT TESTED in this repo's CI: it needs the ``rfdetr`` package (pulls PyTorch) and weights.
It is checked in phase 2 session A on Colab. The ONNX route (``type: onnx`` or ``openvino``
with an exported RF-DETR model) is covered by the decoder tests.
"""

from __future__ import annotations

import logging

import numpy as np

from ..errors import DetectorError
from ..types import Detections
from .base import Detector, DetectorInfo
from .coco import COCO91

log = logging.getLogger(__name__)

LICENSE = (
    "Apache-2.0 (RF-DETR code and standard COCO checkpoints). "
    "Check the licence of the exact checkpoint you deploy."
)


class RfDetrDetector(Detector):
    """Wraps ``rfdetr``. ``model`` is "" / "pretrained" for the COCO checkpoint of the chosen
    variant, or a path to your own fine-tuned checkpoint (.pth)."""

    def __init__(
        self,
        variant: str = "small",
        *,
        model: str = "",
        conf: float = 0.2,
        classes: list[str] | None = None,
    ) -> None:
        try:
            import rfdetr
        except ImportError as exc:
            raise DetectorError("rfdetr is not installed. Run: pip install rfdetr") from exc
        class_name = f"RFDETR{variant.capitalize()}"
        factory = getattr(rfdetr, class_name, None)
        if factory is None:
            raise DetectorError(
                f"This rfdetr version has no {class_name}. Update with: pip install -U rfdetr"
            )
        try:
            if model and model != "pretrained":
                self._model = factory(pretrain_weights=model)
            else:
                self._model = factory()
        except Exception as exc:  # noqa: BLE001
            raise DetectorError(f"Could not create {class_name}: {exc}") from exc
        names = getattr(self._model, "class_names", None)
        self.names = (
            {int(k): str(v) for k, v in dict(names).items()} if names else dict(COCO91)
        )
        self.conf = conf
        ids = self.class_ids(classes)
        self._class_filter = ids
        self.info = DetectorInfo(
            name=f"rfdetr:{variant}", runtime="pytorch", license=LICENSE, device="auto"
        )

    def detect(self, image: np.ndarray) -> Detections:
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        result = self._model.predict(rgb, threshold=self.conf)
        if result is None or len(result) == 0:
            return Detections.empty()
        detections = Detections.from_arrays(result.xyxy, result.confidence, result.class_id)
        if self._class_filter is not None:
            detections = detections.select(np.isin(detections.class_id, list(self._class_filter)))
        return detections

    def warmup(self) -> None:
        self.detect(np.zeros((560, 560, 3), dtype=np.uint8))
