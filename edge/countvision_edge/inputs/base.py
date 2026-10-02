"""Frame source interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..types import Frame


@dataclass
class SourceStatus:
    """Health of a source, reported in the heartbeat."""

    connected: bool
    reconnects: int = 0
    last_frame_age_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    error: str | None = None


class FrameSource(ABC):
    """Produces frames. ``is_live`` sources keep running and reconnect; files end."""

    is_live: bool = False

    @abstractmethod
    def open(self) -> None:
        """Start the source. May raise SourceError."""

    @abstractmethod
    def read(self, timeout: float = 1.0) -> Frame | None:
        """Return the next frame.

        Live sources return the newest frame and drop older ones. They return None when
        no new frame arrived within ``timeout`` seconds. Raises EndOfStream at the end of
        a file or after close().
        """

    @abstractmethod
    def close(self) -> None:
        """Release all resources. Safe to call twice."""

    @abstractmethod
    def status(self) -> SourceStatus:
        """Current health."""

    def nominal_fps(self) -> float | None:
        """The stream's own frame rate if known."""
        return None

    def __enter__(self) -> FrameSource:
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
