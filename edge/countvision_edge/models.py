"""Known model files: where to download them, their checksum and their licence.

    countvision-edge models list
    countvision-edge models download yolox_tiny --dir models

A detector config whose ``model`` file is missing but whose file name is in this list is
downloaded automatically on first start (checksum verified). Only permissively licensed
models are listed here, so the default install never pulls AGPL weights.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import DetectorError

log = logging.getLogger(__name__)

_YOLOX = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/"


@dataclass(frozen=True)
class ModelInfo:
    name: str
    file: str
    url: str
    sha256: str
    size_mb: float
    license: str
    note: str


MODELS: dict[str, ModelInfo] = {
    m.name: m
    for m in (
        ModelInfo("yolox_tiny", "yolox_tiny.onnx", _YOLOX + "yolox_tiny.onnx",
                  "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7", 20.2,
                  "Apache-2.0 (Megvii YOLOX)",
                  "Default. 416 px input, ~28 FPS on a 2017 laptop CPU (phase 2 A)."),
        ModelInfo("yolox_nano", "yolox_nano.onnx", _YOLOX + "yolox_nano.onnx",
                  "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d", 3.7,
                  "Apache-2.0 (Megvii YOLOX)",
                  "Fastest, clearly less accurate. Only for very weak hardware."),
        ModelInfo("yolox_s", "yolox_s.onnx", _YOLOX + "yolox_s.onnx",
                  "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063", 35.9,
                  "Apache-2.0 (Megvii YOLOX)",
                  "Most accurate here, 640 px input. Needs a strong CPU or a GPU."),
    )
}


def find_by_file(path: str | Path) -> ModelInfo | None:
    """The known model whose file name matches ``path`` (any folder), or None."""
    name = Path(str(path)).name
    return next((m for m in MODELS.values() if m.file == name), None)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    info: ModelInfo,
    target: str | Path,
    *,
    opener: Callable[[str], object] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download ``info`` to ``target`` (a file path) and verify the checksum.

    The file appears only when it is complete and correct (written to ``.part`` first).
    """
    out = Path(target)
    if out.is_file() and sha256_of(out) == info.sha256:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    open_url = opener or (lambda url: urllib.request.urlopen(  # noqa: S310 - fixed https URLs
        urllib.request.Request(url, headers={"User-Agent": "countvision-edge"}), timeout=60))
    log.info("Downloading %s (%.0f MB, %s) ...", info.file, info.size_mb, info.license)
    try:
        with open_url(info.url) as response, part.open("wb") as handle:
            total = int(getattr(response, "headers", {}).get("Content-Length") or 0)
            done = 0
            while chunk := response.read(1 << 20):
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        actual = sha256_of(part)
        if actual != info.sha256:
            raise DetectorError(
                f"Download of {info.file} is damaged (checksum {actual[:12]}..., expected "
                f"{info.sha256[:12]}...). Try again."
            )
        part.replace(out)
    except OSError as exc:
        raise DetectorError(
            f"Could not download {info.file} from {info.url}: {exc}\n"
            f"Download it by hand and save it as {out}"
        ) from exc
    finally:
        part.unlink(missing_ok=True)
    return out


def ensure_model(path: str | Path) -> Path:
    """Make sure a model file exists. Known models are downloaded when missing."""
    file = Path(str(path))
    if file.exists():
        return file
    info = find_by_file(file)
    if info is None:
        raise DetectorError(f"Model file not found: {file}")
    print(f"First start: downloading the detector model {info.file} ({info.size_mb:.0f} MB, "
          f"{info.license}) ...", flush=True)
    return download(info, file)
