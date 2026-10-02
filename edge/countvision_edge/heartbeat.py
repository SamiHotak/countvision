"""Heartbeat: FPS and CPU / GPU load, written to the buffer every few seconds."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from collections.abc import Callable

import psutil

from .inputs.base import SourceStatus
from .storage import SqliteBuffer

log = logging.getLogger(__name__)


def gpu_stats() -> tuple[float | None, float | None]:
    """(utilisation %, memory used MB) of the first NVIDIA GPU, or (None, None)."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None, None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip().splitlines()[0]
        util, mem = (float(v.strip()) for v in out.split(",")[:2])
        return util, mem
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return None, None


class HeartbeatMonitor:
    """Collects per-frame counters and emits one heartbeat row per ``interval_s``."""

    def __init__(
        self,
        camera_id: str,
        interval_s: float,
        buffer: SqliteBuffer,
        status_fn: Callable[[], SourceStatus] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.camera_id = camera_id
        self.interval_s = interval_s
        self.buffer = buffer
        self.status_fn = status_fn
        self._clock = clock
        self._monotonic = monotonic
        self.processed = 0
        self.skipped = 0
        self.dropped = 0
        self._proc_total_s = 0.0
        self._last_processed = 0
        self._last_proc_total = 0.0
        self._last_emit = monotonic()
        psutil.cpu_percent(None)  # prime: the first call always returns 0

    def frame_processed(self, seconds: float) -> None:
        self.processed += 1
        self._proc_total_s += seconds

    def frame_skipped(self) -> None:
        self.skipped += 1

    def frames_dropped(self, count: int) -> None:
        self.dropped += count

    def maybe_emit(self, force: bool = False) -> dict | None:
        """Write a heartbeat if the interval has passed (or ``force``). Returns the row."""
        now = self._monotonic()
        elapsed = now - self._last_emit
        if not force and elapsed < self.interval_s:
            return None
        frames = self.processed - self._last_processed
        proc_s = self._proc_total_s - self._last_proc_total
        fps = frames / elapsed if elapsed > 0 else 0.0
        proc_ms = (proc_s / frames * 1000.0) if frames else 0.0
        self._last_emit = now
        self._last_processed, self._last_proc_total = self.processed, self._proc_total_s
        status = self.status_fn() if self.status_fn else None
        gpu_util, gpu_mem = gpu_stats()
        beat = {
            "ts": self._clock(),
            "camera_id": self.camera_id,
            "fps": round(fps, 2),
            "proc_ms": round(proc_ms, 1),
            "frames_processed": self.processed,
            "frames_skipped": self.skipped,
            "frames_dropped": self.dropped,
            "cpu_pct": psutil.cpu_percent(None),
            "mem_pct": psutil.virtual_memory().percent,
            "gpu_util_pct": gpu_util,
            "gpu_mem_mb": gpu_mem,
            "connected": None if status is None else int(status.connected),
            "reconnects": None if status is None else status.reconnects,
            "frame_age_s": None if status is None else status.last_frame_age_s,
        }
        self.buffer.add_heartbeat(beat)
        log.info(
            "heartbeat %s: %.1f fps, %.0f ms/frame, cpu %.0f%%, skipped %d, dropped %d%s",
            self.camera_id, fps, proc_ms, beat["cpu_pct"], self.skipped, self.dropped,
            "" if status is None or status.connected else " | CAMERA OFFLINE",
        )
        return beat
