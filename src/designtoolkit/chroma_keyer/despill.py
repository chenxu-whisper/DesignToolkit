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


def _dominant_screen_channel(model: ScreenColorModel) -> int | None:
    mean_rgb = np.asarray(model.mean_rgb, dtype=np.float32)
    threshold = 0.05 * 255
    for channel in range(3):
        others = np.delete(mean_rgb, channel)
        if mean_rgb[channel] - np.max(others) >= threshold:
            return channel if channel in (1, 2) else None
    return None


def despill(
    rgb: np.ndarray, alpha: np.ndarray, model: ScreenColorModel, amount: float
) -> np.ndarray:
    """Suppress sampled screen color only along partially transparent edges."""
    rgb = _validate_rgb(rgb)
    alpha = _validate_alpha(alpha, rgb)
    amount = _validate_amount(amount)

    rgb_float = rgb.astype(np.float32)
    alpha_float = alpha.astype(np.float32) / 255.0
    edge = (alpha > 0) & (alpha < 255)
    background_contribution = 1.0 - alpha_float
    effect = np.where(edge, background_contribution * amount, 0.0)
    result = rgb_float.copy()

    dominant_channel = _dominant_screen_channel(model)
    if dominant_channel is None:
        neutral_luminance = float(np.mean(model.mean_rgb))
        result += effect[..., None] * (neutral_luminance - result)
    else:
        other_channels = [
            channel for channel in range(3) if channel != dominant_channel
        ]
        target = np.max(rgb_float[..., other_channels], axis=2)
        spill = np.maximum(rgb_float[..., dominant_channel] - target, 0.0)
        result[..., dominant_channel] -= effect * spill

    return np.rint(np.clip(result, 0.0, 255.0)).astype(np.uint8)
