from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import isfinite
from numbers import Integral, Real
from pathlib import Path

from PIL import Image

from designtoolkit.chroma_keyer.video_io import ImageioVideoDecoder
from designtoolkit.chroma_keyer.video_models import VideoMetadata

_IMAGE_SUFFIXES = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})


@dataclass(frozen=True)
class HostedMediaLimits:
    max_image_pixels: int = 100_000_000
    max_video_width: int = 3840
    max_video_height: int = 2160
    max_video_duration_seconds: float = 1800.0


class MediaLimitError(ValueError):
    """A user-facing hosted media rejection with a stable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


VideoProbe = Callable[[Path], VideoMetadata]


class HostedMediaValidator:
    def __init__(
        self,
        limits: HostedMediaLimits | None = None,
        *,
        video_probe: VideoProbe | None = None,
    ) -> None:
        self.limits = limits or HostedMediaLimits()
        self._video_probe = video_probe or ImageioVideoDecoder().probe

    def validate(self, path: Path) -> VideoMetadata | None:
        suffix = path.suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            self._validate_image(path)
            return None
        if suffix in _VIDEO_SUFFIXES:
            return self._validate_video(path)
        raise MediaLimitError("invalid_media", "Media type is unsupported")

    def validate_many(
        self,
        paths: Iterable[Path],
    ) -> list[VideoMetadata | None]:
        return [self.validate(path) for path in paths]

    def _validate_image(self, path: Path) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    width, height = image.size
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise MediaLimitError(
                "image_pixels_exceeded",
                f"Image exceeds the {self.limits.max_image_pixels} pixels limit",
            ) from exc
        except (OSError, SyntaxError, ValueError) as exc:
            raise MediaLimitError(
                "invalid_media",
                "Image could not be read",
            ) from exc

        if not _is_positive_integer(width) or not _is_positive_integer(height):
            raise MediaLimitError("invalid_media", "Image dimensions are invalid")
        if width * height > self.limits.max_image_pixels:
            raise MediaLimitError(
                "image_pixels_exceeded",
                f"Image exceeds the {self.limits.max_image_pixels} pixels limit",
            )

    def _validate_video(self, path: Path) -> VideoMetadata:
        try:
            metadata = self._video_probe(path)
            width = metadata.width
            height = metadata.height
            duration = metadata.duration_seconds
        except Exception as exc:
            raise MediaLimitError(
                "invalid_media",
                "Video metadata could not be read",
            ) from exc

        if (
            not _is_positive_integer(width)
            or not _is_positive_integer(height)
            or not _is_positive_finite_real(duration)
        ):
            raise MediaLimitError(
                "invalid_media",
                "Video metadata is invalid",
            )
        if (
            width > self.limits.max_video_width
            or height > self.limits.max_video_height
        ):
            raise MediaLimitError(
                "video_dimensions_exceeded",
                "Video exceeds the "
                f"{self.limits.max_video_width}x{self.limits.max_video_height} "
                "dimension limit",
            )
        if duration > self.limits.max_video_duration_seconds:
            raise MediaLimitError(
                "video_duration_exceeded",
                "Video exceeds the "
                f"{self.limits.max_video_duration_seconds:g} second duration limit",
            )
        return metadata


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool) and value > 0


def _is_positive_finite_real(value: object) -> bool:
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and isfinite(float(value))
        and value > 0
    )
