from collections.abc import Iterator
from dataclasses import dataclass
from math import isfinite, nextafter
from numbers import Real
from pathlib import Path
from typing import Protocol

import numpy as np

from .models import KeyerError

SUPPORTED_VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".m4v", ".webm", ".mkv", ".avi"})


def _validate_positive_integer(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KeyerError(f"{name} must be a positive integer")


def _normalize_finite_real(value: object, error_message: str) -> float:
    if not isinstance(value, Real) or isinstance(value, bool):
        raise KeyerError(error_message)
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise KeyerError(error_message) from error
    if not isfinite(normalized):
        raise KeyerError(error_message)
    return normalized


def _validate_positive_real(name: str, value: object) -> None:
    normalized = _normalize_finite_real(
        value, f"{name} must be a finite positive number"
    )
    if normalized <= 0:
        raise KeyerError(f"{name} must be a finite positive number")


def _normalize_timestamp(value: object) -> float:
    return _normalize_finite_real(value, "timestamp must be a finite number")


@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    fps: float
    duration_seconds: float
    estimated_frame_count: int
    codec: str

    def __post_init__(self) -> None:
        _validate_positive_integer("width", self.width)
        _validate_positive_integer("height", self.height)
        _validate_positive_real("fps", self.fps)
        _validate_positive_real("duration_seconds", self.duration_seconds)
        _validate_positive_integer("estimated_frame_count", self.estimated_frame_count)
        if not isinstance(self.codec, str) or not self.codec.strip():
            raise KeyerError("codec must be a non-blank string")

    @property
    def frame_step_seconds(self) -> float:
        return 1.0 / self.fps


@dataclass(frozen=True)
class VideoKeyOptions:
    input_path: Path
    output_root: Path | None = None


@dataclass(frozen=True)
class VideoKeyResult:
    output_dir: Path
    manifest_path: Path
    frame_count: int
    metadata: VideoMetadata

    def __post_init__(self) -> None:
        _validate_positive_integer("frame_count", self.frame_count)


class VideoDecoder(Protocol):
    def probe(self, path: Path) -> VideoMetadata:
        raise NotImplementedError

    def read_frame(self, path: Path, timestamp_seconds: float) -> np.ndarray:
        raise NotImplementedError

    def read_preview_frame(self, path: Path, timestamp_seconds: float) -> np.ndarray:
        raise NotImplementedError

    def iter_frames(self, path: Path) -> tuple[VideoMetadata, Iterator[np.ndarray]]:
        raise NotImplementedError


class ProgressCallback(Protocol):
    def __call__(
        self,
        processed_frames: int,
        estimated_total: int | None,
        current_path: Path,
    ) -> None:
        raise NotImplementedError


def validate_timestamp(value: object, metadata: VideoMetadata) -> float:
    timestamp = _normalize_timestamp(value)
    if not 0 <= timestamp < metadata.duration_seconds:
        raise KeyerError("timestamp must satisfy 0 <= timestamp < duration_seconds")
    return timestamp


def last_frame_timestamp(metadata: VideoMetadata) -> float:
    timestamp = max(0.0, metadata.duration_seconds - metadata.frame_step_seconds)
    return min(timestamp, nextafter(metadata.duration_seconds, 0.0))


def clamp_timeline_timestamp(value: object, metadata: VideoMetadata) -> float:
    timestamp = _normalize_timestamp(value)
    return min(max(0.0, timestamp), last_frame_timestamp(metadata))


def validate_video_path_suffix(path: Path) -> None:
    if path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
        raise KeyerError("Input path must have a supported video suffix")
