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


def export_video_frames(
    options: VideoFrameOptions,
    backend: FrameExportBackend,
) -> ExportResult:
    input_path = options.input_path
    _validate_input_path(input_path)

    image_format = _normalize_image_format(options.image_format)
    output_dir = options.output_dir or default_output_dir(input_path)
    _prepare_output_dir(output_dir, overwrite=options.overwrite)

    output_pattern = output_dir / f"{input_path.stem}_%06d.{image_format}"
    if options.overwrite:
        _remove_existing_output_frames(
            output_dir,
            input_stem=input_path.stem,
            image_format=image_format,
        )

    command = _build_ffmpeg_command(
        ffmpeg_exe=backend.get_ffmpeg_exe(),
        options=options,
        input_path=input_path,
        output_pattern=output_pattern,
    )

    result = backend.run(command)
    if result.returncode != 0:
        detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
        raise VideoFrameError(
            f"FFmpeg failed with exit code {result.returncode}{detail}",
            exit_code=1,
        )

    return ExportResult(
        output_dir=output_dir,
        output_pattern=output_pattern,
        command=tuple(command),
    )


def _validate_input_path(input_path: Path) -> None:
    if not input_path.exists():
        raise VideoFrameError(f"Input video does not exist: {input_path}")
    if not input_path.is_file():
        raise VideoFrameError(f"Input path is not a file: {input_path}")


def _normalize_image_format(image_format: str) -> str:
    normalized = image_format.strip().lower().lstrip(".")
    if normalized not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise VideoFrameError(
            f"Unsupported output format '{image_format}'. "
            f"Supported formats: {supported}."
        )
    return normalized


def _prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise VideoFrameError(f"Output path is not a directory: {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    if not overwrite and any(output_dir.iterdir()):
        raise VideoFrameError(
            f"Output directory is not empty: {output_dir}. Use overwrite to continue."
        )


def _remove_existing_output_frames(
    output_dir: Path,
    *,
    input_stem: str,
    image_format: str,
) -> None:
    output_name_pattern = re.compile(
        rf"{re.escape(input_stem)}_\d{{6}}\.{re.escape(image_format)}"
    )
    for path in output_dir.iterdir():
        if path.is_file() and output_name_pattern.fullmatch(path.name):
            path.unlink()


def _build_ffmpeg_command(
    *,
    ffmpeg_exe: str,
    options: VideoFrameOptions,
    input_path: Path,
    output_pattern: Path,
) -> list[str]:
    command = [
        ffmpeg_exe,
        "-hide_banner",
        "-y" if options.overwrite else "-n",
        "-i",
        str(input_path),
    ]

    filters = _video_filters(options)
    if filters:
        command.extend(["-vf", ",".join(filters)])

    command.append(str(output_pattern))
    return command


def _video_filters(options: VideoFrameOptions) -> list[str]:
    filters: list[str] = []
    fps = _effective_fps(options)
    if fps is not None:
        filters.append(f"fps={fps:g}")
    if options.size is not None:
        width, height = options.size
        filters.append(f"scale={width}:{height}")
    return filters


def _effective_fps(options: VideoFrameOptions) -> float | None:
    return options.fps
