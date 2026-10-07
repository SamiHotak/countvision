"""Privacy helpers for the only place where a picture is shown: the local preview.

Nothing here stores an image. The preview JPEG lives in memory only while a browser watches.
"""

from __future__ import annotations

from collections.abc import Iterable

import cv2
import numpy as np


def pixelate_boxes(
    image: np.ndarray, boxes: Iterable[tuple[float, float, float, float]], *, margin: float = 0.1,
    block: int = 12,
) -> np.ndarray:
    """Pixelate every box in place (boxes grown by ``margin`` on each side). Returns the image.

    Pixelation (not a soft blur) is used on purpose: it cannot be undone by sharpening.
    """
    height, width = image.shape[:2]
    for x1, y1, x2, y2 in boxes:
        mx, my = (x2 - x1) * margin, (y2 - y1) * margin
        a, b = max(0, int(x1 - mx)), max(0, int(y1 - my))
        c, d = min(width, int(x2 + mx) + 1), min(height, int(y2 + my) + 1)
        if c - a < 2 or d - b < 2:
            continue
        roi = image[b:d, a:c]
        small = cv2.resize(roi, (max(1, (c - a) // block), max(1, (d - b) // block)),
                           interpolation=cv2.INTER_AREA)
        image[b:d, a:c] = cv2.resize(small, (c - a, d - b), interpolation=cv2.INTER_NEAREST)
    return image


def limit_width(image: np.ndarray, max_width: int) -> np.ndarray:
    """Scale down to at most ``max_width`` pixels wide (never up)."""
    height, width = image.shape[:2]
    if width <= max_width:
        return image
    new_h = max(1, int(round(height * max_width / width)))
    return cv2.resize(image, (max_width, new_h), interpolation=cv2.INTER_AREA)
