"""Exception types used across the edge agent."""

from __future__ import annotations


class CountVisionError(Exception):
    """Base class for all expected errors."""


class ConfigError(CountVisionError):
    """The configuration file is missing, malformed or inconsistent."""


class SourceError(CountVisionError):
    """A frame source could not be opened or read."""


class EndOfStream(CountVisionError):  # noqa: N818 - control-flow signal, not an error name
    """The source has no more frames (end of a video file, or the source was closed)."""


class DetectorError(CountVisionError):
    """A detector could not be created or failed to run."""
