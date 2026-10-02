"""Frame sources."""

from __future__ import annotations

from ..config import SourceConfig
from .base import FrameSource, SourceStatus
from .file_source import FileSource
from .live_source import Backoff, LiveSource

__all__ = ["Backoff", "FileSource", "FrameSource", "LiveSource", "SourceStatus", "create_source"]


def create_source(cfg: SourceConfig) -> FrameSource:
    """Create the right source for a source config."""
    if cfg.kind == "file":
        return FileSource(cfg.uri, start_time=cfg.start_epoch(), realtime=cfg.realtime)
    return LiveSource(cfg)
