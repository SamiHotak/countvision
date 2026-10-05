"""Short detector specs for evaluation and benchmarks, with the licence of each choice.

Format: ``type:model[@imgsz]``. Examples::

    ultralytics:yolo11n.pt          YOLO11n, PyTorch (AGPL-3.0)
    ultralytics:yolo11n.pt@480      same, smaller input
    onnx:yolo11n.onnx               exported YOLO11n through ONNX Runtime (still AGPL-3.0)
    openvino:yolo11n_openvino_model/yolo11n.xml
    onnx:yolox_tiny.onnx            YOLOX-Tiny (Apache-2.0)
    openvino:yolox_s.onnx           YOLOX-S through OpenVINO (Apache-2.0)
    rfdetr:nano                     RF-DETR Nano, PyTorch (Apache-2.0)
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import DetectorConfig
from ..errors import ConfigError

AGPL = "AGPL-3.0 (Ultralytics). Closed-source product needs an Ultralytics Enterprise licence."
APACHE_YOLOX = "Apache-2.0 (YOLOX code and official weights, Megvii)"
APACHE_RFDETR = (
    "Apache-2.0 (RF-DETR code and COCO checkpoints Nano..Large, Roboflow). "
    "XLarge/2XLarge use the Platform Model License, not Apache."
)

_SPEC = re.compile(r"^(?P<type>[a-z]+):(?P<model>[^@]+?)(?:@(?P<imgsz>\d+))?$")
RFDETR_VARIANTS = ("nano", "small", "medium", "base", "large")


def known_license(model: str, det_type: str) -> str:
    """Best guess of the model licence from its name. Unknown -> a warning text."""
    name = Path(model).name.lower()
    if det_type == "rfdetr" or "rfdetr" in name or "rf-detr" in name:
        return APACHE_RFDETR
    if "yolox" in name:
        return APACHE_YOLOX
    if det_type == "ultralytics" or re.search(r"yolo(v?\d+|11|12|26)", name) or "rtdetr" in name:
        return AGPL
    return "UNKNOWN - check the licence of these weights"


def parse_detector_spec(
    spec: str, *, conf: float = 0.2, device: str = "auto", models_dir: str | Path | None = None
) -> DetectorConfig:
    """Turn a spec string into a DetectorConfig. ``models_dir`` is searched for relative model
    paths that do not exist in the current folder."""
    match = _SPEC.match(spec.strip())
    if not match:
        raise ConfigError(f"Bad detector spec '{spec}'. Use type:model[@imgsz], e.g. onnx:yolox_tiny.onnx")
    det_type, model, imgsz = match["type"], match["model"], match["imgsz"]
    if det_type not in ("ultralytics", "onnx", "openvino", "rfdetr", "blobs"):
        raise ConfigError(f"Unknown detector type '{det_type}' in '{spec}'")
    data: dict = {"type": det_type, "conf": conf, "device": device}
    if det_type == "rfdetr":
        if model not in RFDETR_VARIANTS:
            raise ConfigError(f"RF-DETR variant must be one of {', '.join(RFDETR_VARIANTS)}")
        data.update(rfdetr_variant=model, model="pretrained")
    else:
        data["model"] = _resolve_model(model, models_dir)
    if imgsz:
        data["imgsz"] = int(imgsz)
    if det_type in ("onnx", "openvino"):
        data["model_license"] = known_license(model, det_type)
    return DetectorConfig.model_validate(data)


def spec_key(spec: str) -> str:
    """A file-name-safe id for a spec (used for cache folders and result names)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", spec.strip()).strip("_")


def spec_license(spec: str) -> str:
    match = _SPEC.match(spec.strip())
    if not match:
        return "UNKNOWN"
    return known_license(match["model"], match["type"])


def _resolve_model(model: str, models_dir: str | Path | None) -> str:
    path = Path(model)
    if path.exists() or models_dir is None or path.is_absolute():
        return model
    candidate = Path(models_dir) / model
    # Not found: keep the name. Ultralytics downloads official weights by name; the other
    # runtimes give a clear "could not load" error.
    return str(candidate) if candidate.exists() else model
