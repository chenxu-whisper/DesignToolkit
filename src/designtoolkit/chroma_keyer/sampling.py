from math import floor, isfinite
from numbers import Real

import numpy as np

from .models import KeyerError, ScreenColorModel


def _validate_dimension(name: str, value: object) -> int:
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
    preview_width: int,
    preview_height: int,
    source_width: int,
    source_height: int,
) -> tuple[int, int]:
    """Map an in-bounds preview coordinate to its source image pixel."""
    preview_width = _validate_dimension("preview_width", preview_width)
    preview_height = _validate_dimension("preview_height", preview_height)
    source_width = _validate_dimension("source_width", source_width)
    source_height = _validate_dimension("source_height", source_height)
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
    rgb_samples = np.asarray(validated_model.samples, dtype=np.float32)
    mean_rgb = tuple(float(value) for value in rgb_samples.mean(axis=0))
    stddev_rgb = tuple(float(value) for value in rgb_samples.std(axis=0))

    return ScreenColorModel(
        mean_rgb=mean_rgb,
        stddev_rgb=stddev_rgb,
        samples=validated_model.samples,
    )
