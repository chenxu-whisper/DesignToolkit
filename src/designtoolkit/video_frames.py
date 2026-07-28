from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SUPPORTED_FORMATS = {"png", "jpg", "jpeg"}


class VideoFrameError(Exception):
    """Expected user-facing error while exporting video frames."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class BackendResult:
    returncode: int
    stderr: str = ""


@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    output_pattern: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class VideoFrameOptions:
    input_path: Path
    output_dir: Path | None = None
    image_format: str = "png"
    size: tuple[int, int] | None = None
    fps: float | None = None
    frames: int | None = None
    overwrite: bool = False


class FrameExportBackend(Protocol):
    def get_ffmpeg_exe(self) -> str:
        raise NotImplementedError

    def get_duration_seconds(self, video_path: Path) -> float | None:
        raise NotImplementedError

    def run(self, command: Sequence[str]) -> BackendResult:
        raise NotImplementedError


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", value.strip().lower())
    if match is None:
        raise VideoFrameError(
            f"Invalid size '{value}'. Expected WIDTHxHEIGHT, for example 1920x1080."
        )
    return int(match.group(1)), int(match.group(2))


def default_output_dir(video_path: Path) -> Path:
    return video_path.parent / f"{video_path.stem}_frames"
