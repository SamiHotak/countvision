"""Speed benchmark: detector x runtime x input size, on THIS machine.

    countvision-edge bench --video eval/videos/people-detection.mp4 \\
        --model onnx:yolox_tiny.onnx --model ultralytics:yolo11n.pt@640 --label laptop

For every model it measures, on the same real frames (resized to 1280x720 = a typical
camera stream):

* detector time per frame (mean, p50, p95) -> detector FPS
* full pipeline time per frame (detector + ByteTrack + line/zone analytics) -> pipeline FPS
* how many cameras this machine could run at 10 and at 15 FPS (simple estimate: one
  detector, frames processed one after the other, no batching)

Results go to a JSON file that ``countvision-edge eval report`` turns into tables.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import psutil

from ..config import CameraConfig, SourceConfig
from ..detectors import build_detector
from ..detectors.base import Detector
from ..errors import CountVisionError, SourceError
from ..pipeline import CameraPipeline
from ..storage import SqliteBuffer
from ..types import Detections, Frame
from .specs import parse_detector_spec, spec_license

log = logging.getLogger(__name__)


@dataclass
class BenchResult:
    spec: str
    license: str
    runtime: str = ""
    device: str = ""
    imgsz: int | None = None
    frame_size: str = ""
    iterations: int = 0
    det_mean_ms: float | None = None
    det_p50_ms: float | None = None
    det_p95_ms: float | None = None
    det_fps: float | None = None
    pipe_mean_ms: float | None = None
    pipe_fps: float | None = None
    cameras_at_10fps: int | None = None
    cameras_at_15fps: int | None = None
    load_s: float | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


class _TimedDetector(Detector):
    """Proxy that measures how long the real detector takes per call."""

    def __init__(self, inner: Detector) -> None:
        self.inner = inner
        self.names = inner.names
        self.info = inner.info
        self.times_ms: list[float] = []

    def detect(self, image: np.ndarray) -> Detections:
        started = time.perf_counter()
        result = self.inner.detect(image)
        self.times_ms.append((time.perf_counter() - started) * 1000.0)
        return result

    def warmup(self) -> None:
        self.inner.warmup()

    def close(self) -> None:
        self.inner.close()


def load_frames(video: str | Path | None, count: int, size: tuple[int, int]) -> list[np.ndarray]:
    """``count`` consecutive frames (every 2nd frame, like a ~10-15 FPS live stream) resized to
    ``size`` = (width, height). Without a video: a synthetic street-like noise image."""
    if video is None:
        rng = np.random.default_rng(0)
        base = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
        return [np.roll(base, i * 4, axis=1) for i in range(count)]
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SourceError(f"Could not open video: {video}")
    frames: list[np.ndarray] = []
    try:
        index = 0
        while len(frames) < count:
            ok, image = cap.read()
            if not ok:
                if not frames:
                    raise SourceError(f"No frames in {video}")
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop a short clip
                index = 0
                continue
            if index % 2 == 0:
                frames.append(cv2.resize(image, size, interpolation=cv2.INTER_LINEAR))
            index += 1
    finally:
        cap.release()
    return frames


def _stats(values: list[float]) -> tuple[float, float, float]:
    arr = np.asarray(values, dtype=np.float64)
    return float(arr.mean()), float(np.percentile(arr, 50)), float(np.percentile(arr, 95))


def bench_one(
    spec: str,
    frames: list[np.ndarray],
    *,
    warmup: int = 5,
    device: str = "auto",
    models_dir: str | Path | None = None,
    threads: int = 0,
) -> BenchResult:
    """Benchmark one detector spec. Errors are captured in the result, never raised."""
    result = BenchResult(spec=spec, license=spec_license(spec))
    try:
        cfg = parse_detector_spec(spec, conf=0.2, device=device, models_dir=models_dir)
        if threads:
            cfg = cfg.model_copy(update={"threads": threads})
        started = time.perf_counter()
        inner = build_detector(cfg)
        inner.warmup()
        result.load_s = round(time.perf_counter() - started, 2)
    except Exception as exc:  # noqa: BLE001 - one broken model must not stop the benchmark
        result.error = f"{type(exc).__name__}: {exc}"[:400]
        return result
    detector = _TimedDetector(inner)
    result.runtime, result.device = inner.info.runtime, inner.info.device
    result.imgsz = getattr(inner, "imgsz", None) or getattr(inner, "net_size", (cfg.imgsz,))[0]
    if cfg.type == "rfdetr":
        result.imgsz = None
        result.notes.append("RF-DETR uses its own input size")
    height, width = frames[0].shape[:2]
    result.frame_size = f"{width}x{height}"
    cam = CameraConfig(
        id="bench",
        source=SourceConfig(uri="bench.mp4", kind="file"),
        classes=[c for c in ("person", "car") if c in {n.lower() for n in inner.names.values()}] or
        [next(iter(inner.names.values()))],
        lines=[{"name": "line", "p1": [0.5, 0.0], "p2": [0.5, 1.0]}],
        zones=[{"name": "zone", "polygon": [[0, 0.5], [1, 0.5], [1, 1], [0, 1]]}],
        heatmap={"enabled": False},
        scheduler={"target_fps": 10.0, "adaptive": False},
    )
    buffer = SqliteBuffer(":memory:")
    try:
        pipeline = CameraPipeline(cam, detector, buffer, data_dir=".", heartbeat_interval_s=1e9)
        pipeline.set_stream_rate(10.0)
        for i in range(min(warmup, len(frames))):
            detector.detect(frames[i])
        detector.times_ms.clear()
        total_ms = []
        for i, image in enumerate(frames):
            out = pipeline.process(Frame(image, i, 1_700_000_000.0 + i / 10.0, i / 10.0))
            total_ms.append(out.proc_ms)
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"[:400]
        return result
    finally:
        buffer.close()
        detector.close()
    result.iterations = len(total_ms)
    mean, p50, p95 = _stats(detector.times_ms)
    result.det_mean_ms, result.det_p50_ms, result.det_p95_ms = round(mean, 2), round(p50, 2), round(p95, 2)
    result.det_fps = round(1000.0 / mean, 1)
    pipe_mean = float(np.mean(total_ms))
    result.pipe_mean_ms = round(pipe_mean, 2)
    result.pipe_fps = round(1000.0 / pipe_mean, 1)
    # Keep 15% headroom, like the live scheduler does.
    result.cameras_at_10fps = int(0.85 * result.pipe_fps // 10)
    result.cameras_at_15fps = int(0.85 * result.pipe_fps // 15)
    return result


def machine_info() -> dict:
    """What this benchmark ran on. No personal data: CPU/GPU model, cores, RAM, versions."""
    info = {
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu": _cpu_name(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "gpu": _gpu_name(),
        "packages": {},
    }
    for name in ("onnxruntime", "openvino", "torch", "ultralytics", "rfdetr", "opencv-python"):
        try:
            from importlib.metadata import version

            info["packages"][name] = version(name)
        except Exception:  # noqa: BLE001
            continue
    return info


def _cpu_name() -> str:
    if sys.platform.startswith("win"):
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    if sys.platform == "darwin":
        try:
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.processor() or "unknown"


def _gpu_name() -> str | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5)
        names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
        return ", ".join(names) or None
    except (OSError, subprocess.SubprocessError):
        return None


def run_bench(
    specs: list[str],
    *,
    video: str | Path | None,
    frames: int = 60,
    frame_size: tuple[int, int] = (1280, 720),
    device: str = "auto",
    models_dir: str | Path | None = None,
    threads: int = 0,
    label: str = "",
    progress=None,
) -> dict:
    if not specs:
        raise CountVisionError("Give at least one --model")
    images = load_frames(video, frames, frame_size)
    results = []
    for spec in specs:
        if progress:
            progress(spec)
        results.append(asdict(bench_one(spec, images, device=device, models_dir=models_dir, threads=threads)))
    return {
        "kind": "bench",
        "label": label or "machine",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),  # noqa: UP017
        "video": Path(video).name if video else "synthetic",
        "frames": frames,
        "machine": machine_info(),
        "threads": threads or "runtime default",
        "results": results,
    }


def save_json(data: dict, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return out
