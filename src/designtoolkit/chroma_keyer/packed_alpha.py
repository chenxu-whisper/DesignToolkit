import numpy as np

from .image_io import _validate_rgb
from .models import KeyerError


def pack_alpha_frame(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Pack grayscale Alpha on the left and straight RGB on the right."""
    rgb = _validate_rgb(rgb)
    if (
        not isinstance(alpha, np.ndarray)
        or alpha.ndim != 2
        or alpha.shape != rgb.shape[:2]
    ):
        raise KeyerError("alpha must be an HxW array matching rgb")
    if alpha.dtype == np.uint8:
        alpha_bytes = alpha
    elif np.issubdtype(alpha.dtype, np.floating):
        if not np.isfinite(alpha).all() or np.any(alpha < 0) or np.any(alpha > 1):
            raise KeyerError("alpha must be finite and within [0, 1]")
        alpha_bytes = np.floor(alpha.astype(np.float64) * 255 + 0.5).astype(np.uint8)
    else:
        raise KeyerError("alpha must be uint8 or normalized floating point")
    height, width = rgb.shape[:2]
    stored_width = width + width % 2
    stored_height = height + height % 2
    packed = np.zeros((stored_height, 2 * stored_width, 3), dtype=np.uint8)
    packed[:height, :width] = alpha_bytes[..., None]
    # Extend color edges into padding so H.264 does not darken original edge pixels.
    packed[:, stored_width:] = np.pad(
        rgb, ((0, height % 2), (0, width % 2), (0, 0)), mode="edge"
    )
    return packed
