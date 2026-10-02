"""Detector interface. Detectors are swappable; the rest of the agent only sees this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from ..errors import DetectorError
from ..types import Detections


@dataclass(frozen=True)
class DetectorInfo:
    """What is running. ``license`` is logged at start-up so the licence impact is visible."""

    name: str
    runtime: str  # pytorch | onnxruntime | openvino | tensorrt | opencv
    license: str
    device: str = "cpu"


class Detector(ABC):
    """Finds objects in one BGR frame."""

    names: dict[int, str]
    info: DetectorInfo

    @abstractmethod
    def detect(self, image: np.ndarray) -> Detections:
        """Detect objects in a BGR uint8 image. Boxes are in pixels of that image."""

    def warmup(self) -> None:  # noqa: B027 - optional hook
        """Run one dummy inference so the first real frame is not slow."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""

    def class_ids(self, names: list[str] | None) -> set[int] | None:
        """Map class names to ids. None means no filter. Unknown names are an error."""
        if not names:
            return None
        by_name = {name.lower(): cid for cid, name in self.names.items()}
        ids: set[int] = set()
        for name in names:
            cid = by_name.get(name.lower())
            if cid is None:
                available = ", ".join(sorted(by_name)[:40])
                raise DetectorError(
                    f"The model has no class named '{name}'. Available classes: {available}"
                )
            ids.add(cid)
        return ids
