import cv2
import numpy as np

from .models import (
    KeyerError,
    KeyerParams,
    ScreenColorModel,
    dominant_screen_channel,
)


def _validate_rgb(rgb: object) -> np.ndarray:
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3:
        raise KeyerError("rgb must be a uint8 HxWx3 array")
    if rgb.shape[2] != 3:
        raise KeyerError("rgb must be a uint8 HxWx3 array")
    return rgb


def _validate_alpha(alpha: object) -> np.ndarray:
    if not isinstance(alpha, np.ndarray) or alpha.dtype != np.uint8 or alpha.ndim != 2:
        raise KeyerError("alpha must be a uint8 HxW array")
    if alpha.size == 0:
        raise KeyerError("alpha must be a non-empty uint8 HxW array")
    return alpha


def generate_alpha(
    rgb: np.ndarray, model: ScreenColorModel, params: KeyerParams
) -> np.ndarray:
    """Generate an opaque-foreground alpha matte from sampled screen chroma."""
    rgb = _validate_rgb(rgb)
    rgb_float = rgb.astype(np.float32) / np.float32(255.0)
    dominant = dominant_screen_channel(model.mean_rgb)
    screen = np.asarray(model.mean_rgb, dtype=np.float32) / np.float32(255.0)
    other_channels = [channel for channel in range(3) if channel != dominant]
    balance = np.float32(params.screen_balance)

    screen_others = screen[other_channels]
    balanced_screen_other = (
        (np.float32(1.0) - balance) * np.max(screen_others)
        + balance * np.min(screen_others)
    )
    screen_saturation = screen[dominant] - balanced_screen_other

    pixel_others = rgb_float[..., other_channels]
    balanced_pixel_other = (
        (np.float32(1.0) - balance) * np.max(pixel_others, axis=2)
        + balance * np.min(pixel_others, axis=2)
    )
    pixel_saturation = rgb_float[..., dominant] - balanced_pixel_other
    coverage = np.clip(
        np.float32(params.screen_gain) * pixel_saturation / screen_saturation,
        np.float32(0.0),
        np.float32(1.0),
    )
    alpha = np.float32(1.0) - coverage
    return np.rint(alpha * np.float32(255.0)).astype(np.uint8)


def refine_alpha(alpha: np.ndarray, params: KeyerParams) -> np.ndarray:
    """Apply clip remapping, morphology, and feathering to an alpha matte."""
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
        operation = cv2.dilate if params.shrink_grow > 0 else cv2.erode
        transformed = operation(refined, kernel)
        refined = (refined + transformed) / 2.0

    if params.feather:
        kernel_size = 2 * params.feather + 1
        refined = cv2.GaussianBlur(refined, (kernel_size, kernel_size), 0)

    return np.rint(np.clip(refined, 0.0, 1.0) * 255.0).astype(np.uint8)
