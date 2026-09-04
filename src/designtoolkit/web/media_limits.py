from __future__ import annotations

import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import isfinite
from numbers import Integral, Real
from pathlib import Path
from threading import Lock

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
_DEFAULT_CACHE_SIZE = 128


@dataclass(frozen=True)
class _ValidatedMedia:
    media_type: str
    width: int
    height: int
    video_metadata: VideoMetadata | None = None


class HostedMediaValidator:
    def __init__(
        self,
        limits: HostedMediaLimits | None = None,
        *,
        video_probe: VideoProbe | None = None,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self.limits = limits or HostedMediaLimits()
        self._video_probe = video_probe or ImageioVideoDecoder().probe
        self._cache_size = cache_size
        self._cache: OrderedDict[
            tuple[Path, int, int],
            _ValidatedMedia,
        ] = OrderedDict()
        self._cache_lock = Lock()

    def validate(self, path: Path) -> VideoMetadata | None:
        resolved_path = path.resolve()
        suffix = resolved_path.suffix.lower()
        if suffix not in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES:
            raise MediaLimitError("invalid_media", "Media type is unsupported")

        try:
            stat = resolved_path.stat()
        except OSError as exc:
            raise MediaLimitError("invalid_media", "Media could not be read") from exc
        cache_key = (resolved_path, stat.st_size, stat.st_mtime_ns)

        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached.video_metadata

            validated = self._validate_uncached(resolved_path, suffix)
            self._cache[cache_key] = validated
            self._cache.move_to_end(cache_key)
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return validated.video_metadata

    def validate_video(self, path: Path) -> VideoMetadata:
        if path.suffix.lower() not in _VIDEO_SUFFIXES:
            raise MediaLimitError("invalid_media", "Media type is not a video")
        metadata = self.validate(path)
        if metadata is None:
            raise MediaLimitError("invalid_media", "Video metadata could not be read")
        return metadata

    def _validate_uncached(self, path: Path, suffix: str) -> _ValidatedMedia:
        if suffix in _IMAGE_SUFFIXES:
            width, height = self._validate_image(path)
            return _ValidatedMedia("image", width, height)
        metadata = self._validate_video(path)
        return _ValidatedMedia(
            "video",
            metadata.width,
            metadata.height,
            metadata,
        )

    def validate_many(
        self,
        paths: Iterable[Path],
    ) -> list[VideoMetadata | None]:
        resolved_paths = [path.resolve() for path in paths]
        validated: dict[Path, VideoMetadata | None] = {}
        for path in resolved_paths:
            if path not in validated:
                validated[path] = self.validate(path)
        return [validated[path] for path in resolved_paths]

    def _validate_image(self, path: Path) -> tuple[int, int]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    width, height = image.size
                    frame_count = image.n_frames
        except Image.DecompressionBombError as exc:
            raise MediaLimitError(
                "image_pixels_exceeded",
                f"Image exceeds the {self.limits.max_image_pixels} pixels limit",
            ) from exc
        except (OSError, SyntaxError, ValueError) as exc:
            raise MediaLimitError(
                "invalid_media",
                "Image could not be read",
            ) from exc

        if frame_count > 1:
            raise MediaLimitError(
                "animated_image",
                "Animated images are unsupported",
            )
        if not _is_positive_integer(width) or not _is_positive_integer(height):
            raise MediaLimitError("invalid_media", "Image dimensions are invalid")
        if width * height > self.limits.max_image_pixels:
            raise MediaLimitError(
                "image_pixels_exceeded",
                f"Image exceeds the {self.limits.max_image_pixels} pixels limit",
            )
        return int(width), int(height)

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
