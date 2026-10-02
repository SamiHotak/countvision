"""Detectors. The rest of the agent depends only on ``Detector``."""

from .base import Detector, DetectorInfo
from .factory import build_detector

__all__ = ["Detector", "DetectorInfo", "build_detector"]
