from dataclasses import dataclass
from math import isfinite
from numbers import Real
from pathlib import Path


class KeyerError(Exception):
    """Raised when a chroma-keyer contract is invalid."""


def _validate_unit_interval(name: str, value: float) -> None:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise KeyerError(f"{name} must be a finite number")
    if not 0 <= value <= 1:
        raise KeyerError(f"{name} must be between 0 and 1")


def _validate_range(name: str, value: int, minimum: int, maximum: int) -> None:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise KeyerError(f"{name} must be a finite number")
    if not minimum <= value <= maximum:
        raise KeyerError(f"{name} must be between {minimum} and {maximum}")


def _validate_rgb_statistics(name: str, values: tuple[float, float, float]) -> None:
    if len(values) != 3:
        raise KeyerError(f"{name} must contain exactly three values")
    for value in values:
        if (
            not isinstance(value, Real)
            or isinstance(value, bool)
            or not isfinite(value)
        ):
            raise KeyerError(f"{name} values must be finite numbers")


@dataclass(frozen=True)
class KeyerParams:
    tolerance: float = 0.25
    softness: float = 0.15
    clip_black: float = 0.0
    clip_white: float = 1.0
    shrink_grow: int = 0
    feather: int = 0
    despill_amount: float = 0.5

    def __post_init__(self) -> None:
        _validate_unit_interval("tolerance", self.tolerance)
        _validate_unit_interval("softness", self.softness)
        _validate_unit_interval("clip_black", self.clip_black)
        _validate_unit_interval("clip_white", self.clip_white)
        _validate_range("shrink_grow", self.shrink_grow, -10, 10)
        _validate_range("feather", self.feather, 0, 10)
        _validate_unit_interval("despill_amount", self.despill_amount)
        if self.clip_black >= self.clip_white:
            raise KeyerError("clip_black must be less than clip_white")


@dataclass(frozen=True)
class ScreenColorModel:
    mean_rgb: tuple[float, float, float]
    stddev_rgb: tuple[float, float, float]
    samples: tuple[tuple[int, int, int], ...]

    def __post_init__(self) -> None:
        _validate_rgb_statistics("mean_rgb", self.mean_rgb)
        _validate_rgb_statistics("stddev_rgb", self.stddev_rgb)
        if not self.samples:
            raise KeyerError("samples must not be empty")
        for sample in self.samples:
            if len(sample) != 3:
                raise KeyerError("each sample must contain exactly three channels")
            for channel in sample:
                if (
                    not isinstance(channel, int)
                    or isinstance(channel, bool)
                    or not 0 <= channel <= 255
                ):
                    raise KeyerError(
                        "sample channels must be integers between 0 and 255"
                    )


@dataclass(frozen=True)
class KeyImageOptions:
    input_path: Path
    output_path: Path | None = None


@dataclass(frozen=True)
class KeyImageResult:
    output_path: Path
