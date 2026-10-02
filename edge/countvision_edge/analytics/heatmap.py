"""Heatmap of where objects spend time (seconds per grid cell)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..types import Point


class Heatmap:
    """Accumulates presence time on a coarse grid. Stores no images and no identities."""

    def __init__(self, frame_size: tuple[int, int], grid_width: int = 192, blur_sigma: float = 1.5):
        self.blur_sigma = blur_sigma
        self.grid_width = grid_width
        self.set_frame_size(frame_size)
        self.grid = np.zeros((self.grid_height, self.grid_width), dtype=np.float32)

    def set_frame_size(self, frame_size: tuple[int, int]) -> None:
        self.width, self.height = frame_size
        self.grid_height = max(1, round(self.grid_width * self.height / max(1, self.width)))
        if hasattr(self, "grid") and self.grid.shape != (self.grid_height, self.grid_width):
            self.grid = np.zeros((self.grid_height, self.grid_width), dtype=np.float32)

    def add(self, points: list[Point], dt: float) -> None:
        """Add ``dt`` seconds of presence at each point."""
        for x, y in points:
            gx = int(min(self.grid_width - 1, max(0, x / self.width * self.grid_width)))
            gy = int(min(self.grid_height - 1, max(0, y / self.height * self.grid_height)))
            self.grid[gy, gx] += dt

    def reset(self) -> None:
        self.grid[:] = 0

    def _normalized(self) -> np.ndarray:
        grid = self.grid
        if self.blur_sigma > 0:
            grid = cv2.GaussianBlur(grid, (0, 0), self.blur_sigma)
        peak = float(grid.max())
        if peak <= 0:
            return np.zeros_like(grid, dtype=np.uint8)
        return np.clip(grid / peak * 255.0, 0, 255).astype(np.uint8)

    def render(self, size: tuple[int, int] | None = None) -> np.ndarray:
        """Colour image (BGR) of the heatmap, scaled to ``size`` (width, height)."""
        color = cv2.applyColorMap(self._normalized(), cv2.COLORMAP_TURBO)
        target = size or (self.width, self.height)
        return cv2.resize(color, target, interpolation=cv2.INTER_CUBIC)

    def overlay(self, image: np.ndarray, alpha: float = 0.45) -> np.ndarray:
        """Blend the heatmap over ``image``. Cells without presence stay transparent."""
        h, w = image.shape[:2]
        heat = self.render((w, h))
        mask = cv2.resize(self._normalized(), (w, h), interpolation=cv2.INTER_LINEAR) > 8
        out = image.copy()
        blended = cv2.addWeighted(image, 1 - alpha, heat, alpha, 0)
        out[mask] = blended[mask]
        return out

    def save(self, directory: str | Path, name: str) -> tuple[Path, Path]:
        """Write ``<name>.npy`` (raw seconds per cell) and ``<name>.png`` (colour)."""
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        npy, png = folder / f"{name}.npy", folder / f"{name}.png"
        np.save(npy, self.grid)
        cv2.imwrite(str(png), self.render())
        return npy, png
