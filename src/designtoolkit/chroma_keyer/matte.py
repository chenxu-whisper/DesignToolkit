import cv2
import numpy as np

from .models import KeyerError, KeyerParams, ScreenColorModel


def _validate_rgb(rgb: object) -> np.ndarray:
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3:
        raise KeyerError("rgb must be a uint8 HxWx3 array")
    if rgb.shape[2] != 3:
        raise KeyerError("rgb must be a uint8 HxWx3 array")
    return rgb


def _validate_alpha(alpha: object) -> np.ndarray:
    if not isinstance(alpha, np.ndarray) or alpha.dtype != np.uint8 or alpha.ndim != 2:
        raise KeyerError("alpha must be a uint8 HxW array")
    return alpha


def generate_alpha(
    rgb: np.ndarray, model: ScreenColorModel, params: KeyerParams
) -> np.ndarray:
    """Generate an opaque-foreground alpha matte from RGB distance."""
    rgb = _validate_rgb(rgb)
    normalized_rgb = rgb.astype(np.float32) / 255.0
    mean_rgb = np.asarray(model.mean_rgb, dtype=np.float32) / 255.0
    distance = np.linalg.norm(normalized_rgb - mean_rgb, axis=2)

    variance = float(np.mean(model.stddev_rgb)) / 255.0
    lower = float(np.clip(params.tolerance + variance, 0.0, 1.0))
    upper = max(lower + 0.01, min(1.0, lower + params.softness))
    position = np.clip((distance - lower) / (upper - lower), 0.0, 1.0)
    smoothstep = position * position * (3.0 - 2.0 * position)
    return np.rint(smoothstep * 255.0).astype(np.uint8)


def refine_alpha(alpha: np.ndarray, params: KeyerParams) -> np.ndarray:
    """Apply clip remapping, binary morphology, and feathering to an alpha matte."""
    alpha = _validate_alpha(alpha)
    refined = alpha.astype(np.float32) / 255.0
    refined = np.clip(
        (refined - params.clip_black) / (params.clip_white - params.clip_black),
        0.0,
        1.0,
    )

    if params.shrink_grow:
        kernel_size = 2 * abs(params.shrink_grow) + 1
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
        )
        binary = (refined >= 0.5).astype(np.uint8) * 255
        operation = cv2.dilate if params.shrink_grow > 0 else cv2.erode
        transformed = operation(binary, kernel).astype(np.float32) / 255.0
        refined = (refined + transformed) / 2.0

    if params.feather:
        kernel_size = 2 * params.feather + 1
        refined = cv2.GaussianBlur(refined, (kernel_size, kernel_size), 0)

    return np.rint(np.clip(refined, 0.0, 1.0) * 255.0).astype(np.uint8)
