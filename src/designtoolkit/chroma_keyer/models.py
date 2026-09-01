from collections.abc import Sequence
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


def _validate_screen_gain(value: float) -> None:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(value):
        raise KeyerError("screen_gain must be a finite number")
    if not 0 <= value <= 2:
        raise KeyerError("screen_gain must be between 0 and 2")


def _validate_range(name: str, value: int, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise KeyerError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise KeyerError(f"{name} must be between {minimum} and {maximum}")


def _normalize_rgb_statistics(
    name: str, values: object
) -> tuple[float, float, float]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise KeyerError(f"{name} must be a sequence of three values")
    if len(values) != 3:
        raise KeyerError(f"{name} must be a sequence of three values")
    for value in values:
        if (
            not isinstance(value, Real)
            or isinstance(value, bool)
            or not isfinite(value)
        ):
            raise KeyerError(f"{name} values must be finite numbers")
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def dominant_screen_channel(mean_rgb: object) -> int:
    """Return the clearly dominant channel of an RGB screen color."""
    normalized_rgb = tuple(
        channel / 255.0
        for channel in _normalize_rgb_statistics("mean_rgb", mean_rgb)
    )
    dominant = max(range(3), key=normalized_rgb.__getitem__)
    strongest_other = max(
        channel
        for index, channel in enumerate(normalized_rgb)
        if index != dominant
    )
    if normalized_rgb[dominant] - strongest_other < 0.05:
        raise KeyerError("请选择具有明显主色的纯色幕背景")
    return dominant


def _normalize_samples(values: object) -> tuple[tuple[int, int, int], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise KeyerError("samples must be a sequence of samples")
    if not values:
        raise KeyerError("samples must not be empty")

    samples = []
    for sample in values:
        if not isinstance(sample, Sequence) or isinstance(sample, (str, bytes)):
            raise KeyerError("each sample must contain exactly three channels")
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
        samples.append(tuple(sample))
    return tuple(samples)  # type: ignore[return-value]


@dataclass(frozen=True)
class KeyerParams:
    screen_gain: float = 1.0
    screen_balance: float = 0.5
    clip_black: float = 0.0
    clip_white: float = 1.0
    shrink_grow: int = 0
    feather: int = 0
    despill_amount: float = 1.0

    def __post_init__(self) -> None:
        _validate_screen_gain(self.screen_gain)
        _validate_unit_interval("screen_balance", self.screen_balance)
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
        object.__setattr__(
            self, "mean_rgb", _normalize_rgb_statistics("mean_rgb", self.mean_rgb)
        )
        object.__setattr__(
            self,
            "stddev_rgb",
            _normalize_rgb_statistics("stddev_rgb", self.stddev_rgb),
        )
        object.__setattr__(self, "samples", _normalize_samples(self.samples))


@dataclass(frozen=True)
class KeyImageOptions:
    input_path: Path
    output_path: Path | None = None


@dataclass(frozen=True)
class KeyImageResult:
    output_path: Path
