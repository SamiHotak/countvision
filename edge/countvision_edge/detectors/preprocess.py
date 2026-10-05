"""Image preprocessing for exported detection models."""

from __future__ import annotations

import cv2
import numpy as np

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def letterbox(
    image: np.ndarray, shape: tuple[int, int], color: int = 114, center: bool = True
) -> tuple[np.ndarray, float, tuple[float, float]]:
    """Resize keeping the aspect ratio and pad to ``shape`` = (height, width).

    ``center=True`` pads on both sides (YOLOv8/11). ``center=False`` puts the image in the
    top-left corner and pads only right and bottom (YOLOX).

    Returns (padded image, scale, (pad_left, pad_top)). To map a box back to the original
    image: ``x = (x_net - pad_left) / scale``.
    """
    height, width = image.shape[:2]
    new_h, new_w = shape
    scale = min(new_h / height, new_w / width)
    unpad_w, unpad_h = round(width * scale), round(height * scale)
    if (unpad_w, unpad_h) != (width, height):
        image = cv2.resize(image, (unpad_w, unpad_h), interpolation=cv2.INTER_LINEAR)
    if center:
        dw, dh = (new_w - unpad_w) / 2, (new_h - unpad_h) / 2
        top, bottom = round(dh - 0.1), round(dh + 0.1)
        left, right = round(dw - 0.1), round(dw + 0.1)
    else:
        top, left = 0, 0
        bottom, right = max(0, new_h - unpad_h), max(0, new_w - unpad_w)
    padded = cv2.copyMakeBorder(
        image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(color, color, color)
    )
    if padded.shape[:2] != (new_h, new_w):  # rounding safety net
        padded = cv2.resize(padded, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    return padded, scale, (float(left), float(top))


def to_tensor(image_bgr: np.ndarray, normalize: str = "none", dtype=np.float32) -> np.ndarray:
    """BGR uint8 HxWx3 -> float NCHW (1x3xHxW).

    ``normalize``:
      none      RGB, scaled to 0..1 (YOLOv8/11, RT-DETR)
      imagenet  RGB, 0..1, then ImageNet mean/std (RF-DETR)
      raw       BGR, 0..255, no scaling (YOLOX >= 0.1.1)
    """
    if normalize == "raw":
        return np.ascontiguousarray(image_bgr.transpose(2, 0, 1)[None]).astype(dtype, copy=False)
    rgb = image_bgr[:, :, ::-1].astype(np.float32) / 255.0
    if normalize == "imagenet":
        rgb = (rgb - IMAGENET_MEAN) / IMAGENET_STD
    tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])
    return tensor.astype(dtype, copy=False)
