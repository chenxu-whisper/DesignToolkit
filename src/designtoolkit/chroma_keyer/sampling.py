from math import floor, isfinite
from numbers import Real

import numpy as np

from .models import KeyerError, ScreenColorModel


def _validate_preview_dimension(name: str, value: object) -> float:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise KeyerError(f"{name} must be a finite number")
    dimension = float(value)
    if dimension <= 0:
        raise KeyerError(f"{name} must be positive")
    return dimension


def _validate_source_dimension(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KeyerError(f"{name} must be a positive integer")
    return value


def _validate_coordinate(
    name: str, value: object, dimension_name: str, dimension: int
) -> float:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise KeyerError(f"{name} must be a finite number")
    coordinate = float(value)
    if not 0 <= coordinate < dimension:
        raise KeyerError(f"{name} must lie within the {dimension_name}")
    return coordinate


def map_preview_point(
    x: float,
    y: float,
    preview_width: float,
    preview_height: float,
    source_width: int,
    source_height: int,
) -> tuple[int, int]:
    """Map an in-bounds preview coordinate to its source image pixel."""
    preview_width = _validate_preview_dimension("preview_width", preview_width)
    preview_height = _validate_preview_dimension("preview_height", preview_height)
    source_width = _validate_source_dimension("source_width", source_width)
    source_height = _validate_source_dimension("source_height", source_height)
    x = _validate_coordinate("x", x, "preview width", preview_width)
    y = _validate_coordinate("y", y, "preview height", preview_height)

    source_x = min(floor(x * source_width / preview_width), source_width - 1)
    source_y = min(floor(y * source_height / preview_height), source_height - 1)
    return source_x, source_y


def screen_model_from_samples(samples: object) -> ScreenColorModel:
    """Build a screen-color model from validated RGB samples."""
    validated_model = ScreenColorModel(
        mean_rgb=(0.0, 0.0, 0.0),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=samples,  # type: ignore[arg-type]
    )
    if len(validated_model.samples) != 1:
        raise KeyerError("samples must contain exactly one screen color")

    sample = validated_model.samples[0]
    screen = np.asarray(sample, dtype=np.float32) / np.float32(255.0)
    dominant = int(np.argmax(screen))
    other_channels = [channel for channel in range(3) if channel != dominant]
    screen_others = screen[other_channels]
    balanced_screen_other = (
        np.float32(0.5) * np.max(screen_others)
        + np.float32(0.5) * np.min(screen_others)
    )
    if screen[dominant] - balanced_screen_other < np.float32(0.05):
        raise KeyerError("请选择具有明显主色的纯色幕背景")

    return ScreenColorModel(
        mean_rgb=tuple(float(channel) for channel in sample),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=validated_model.samples,
    )
