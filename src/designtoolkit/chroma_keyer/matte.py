import cv2
import numpy as np

from .models import KeyerError, KeyerParams, ScreenColorModel

SCREEN_CHROMA_BLEND_START = np.float32(0.03)
SCREEN_CHROMA_BLEND_END = np.float32(0.10)


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


def _rgb_distance_confidence(rgb_float: np.ndarray, mean_rgb: np.ndarray) -> np.ndarray:
    return np.linalg.norm(rgb_float - mean_rgb, axis=2) / np.sqrt(np.float32(3.0))


def _remap_confidence(
    confidence: np.ndarray, lower: np.float32, upper: np.float32
) -> np.ndarray:
    position = np.clip(
        (confidence - lower) / (upper - lower), np.float32(0.0), np.float32(1.0)
    )
    return position * position * (
        np.float32(3.0) - np.float32(2.0) * position
    )


def generate_alpha(
    rgb: np.ndarray, model: ScreenColorModel, params: KeyerParams
) -> np.ndarray:
    """Generate an opaque-foreground alpha matte from sampled screen chroma."""
    rgb = _validate_rgb(rgb)
    rgb_float = rgb.astype(np.float32) / np.float32(255.0)
    mean_rgb = np.asarray(model.mean_rgb, dtype=np.float32)
    normalized_mean_rgb = mean_rgb / np.float32(255.0)
    centered_screen = normalized_mean_rgb - np.mean(
        normalized_mean_rgb, dtype=np.float32
    )
    screen_chroma = np.linalg.norm(centered_screen)
    rgb_distance_confidence = _rgb_distance_confidence(rgb_float, normalized_mean_rgb)
    variance = np.mean(np.asarray(model.stddev_rgb, dtype=np.float32)) / np.float32(
        255.0
    )
    lower = np.float32(
        np.clip(
            np.float32(params.tolerance) + variance,
            np.float32(0.0),
            np.float32(1.0),
        )
    )
    upper = np.float32(
        max(
            lower + np.float32(0.01),
            min(np.float32(1.0), lower + np.float32(params.softness)),
        )
    )
    distance_alpha = _remap_confidence(rgb_distance_confidence, lower, upper)

    if screen_chroma > SCREEN_CHROMA_BLEND_START:
        screen_direction = centered_screen / screen_chroma
        centered_pixels = rgb_float - np.mean(
            rgb_float, axis=2, keepdims=True, dtype=np.float32
        )
        projection = np.sum(
            centered_pixels * screen_direction, axis=2, dtype=np.float32
        )
        axis_confidence = np.clip(
            np.float32(1.0) - projection / screen_chroma,
            np.float32(0.0),
            np.float32(1.0),
        )
        axis_alpha = _remap_confidence(axis_confidence, lower, upper)
        residual = centered_pixels - projection[..., np.newaxis] * screen_direction
        pixel_chroma = np.linalg.norm(centered_pixels, axis=2)
        residual_confidence = np.clip(
            np.linalg.norm(residual, axis=2)
            / np.maximum(
                pixel_chroma,
                max(screen_chroma, np.float32(0.10)),
            ),
            np.float32(0.0),
            np.float32(1.0),
        )
        residual_alpha = _remap_confidence(residual_confidence, lower, upper)
        colored_alpha = np.maximum(axis_alpha, residual_alpha)
        blend_position = np.clip(
            (screen_chroma - SCREEN_CHROMA_BLEND_START)
            / (SCREEN_CHROMA_BLEND_END - SCREEN_CHROMA_BLEND_START),
            np.float32(0.0),
            np.float32(1.0),
        )
        projection_weight = (
            blend_position
            * blend_position
            * (np.float32(3.0) - np.float32(2.0) * blend_position)
        )
        alpha = (
            distance_alpha * (np.float32(1.0) - projection_weight)
            + colored_alpha * projection_weight
        )
    else:
        alpha = distance_alpha

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
