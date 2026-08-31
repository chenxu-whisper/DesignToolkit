from math import isfinite
from numbers import Real

import numpy as np

from .models import KeyerError, ScreenColorModel


def _validate_rgb(rgb: object) -> np.ndarray:
    if (
        not isinstance(rgb, np.ndarray)
        or rgb.dtype != np.uint8
        or rgb.ndim != 3
        or rgb.shape[2] != 3
    ):
        raise KeyerError("rgb must be a uint8 HxWx3 array")
    return rgb


def _validate_alpha(alpha: object, rgb: np.ndarray) -> np.ndarray:
    if not isinstance(alpha, np.ndarray) or alpha.dtype != np.uint8 or alpha.ndim != 2:
        raise KeyerError("alpha must be a uint8 HxW array")
    if alpha.shape != rgb.shape[:2]:
        raise KeyerError("alpha dimensions must match rgb")
    return alpha


def _validate_amount(amount: object) -> float:
    if not isinstance(amount, Real) or isinstance(amount, bool) or not isfinite(amount):
        raise KeyerError("amount must be a finite number between 0 and 1")
    if not 0 <= amount <= 1:
        raise KeyerError("amount must be between 0 and 1")
    return float(amount)


def despill(
    rgb: np.ndarray, alpha: np.ndarray, model: ScreenColorModel, amount: float
) -> np.ndarray:
    """Recover foreground RGB from the sampled background on fractional edges."""
    rgb = _validate_rgb(rgb)
    alpha = _validate_alpha(alpha, rgb)
    amount = _validate_amount(amount)

    rgb_float = rgb.astype(np.float32)
    alpha_float = alpha.astype(np.float32) / 255.0
    alpha_expanded = alpha_float[..., None]
    background = np.asarray(model.mean_rgb, dtype=np.float32)
    safe_alpha = np.maximum(alpha_expanded, 1.0 / 255.0)
    recovered = np.clip(
        (rgb_float - (1.0 - alpha_expanded) * background) / safe_alpha,
        0.0,
        255.0,
    )
    edge = ((alpha > 0) & (alpha < 255))[..., None]
    result = np.where(
        edge,
        rgb_float + amount * (recovered - rgb_float),
        rgb_float,
    )

    return np.rint(np.clip(result, 0.0, 255.0)).astype(np.uint8)
