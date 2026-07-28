from __future__ import annotations

import math
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


class BackendUnavailableError(RuntimeError):
    """Expected backend availability failure before export can start."""


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


def _ffmpeg_output_stem(input_stem: str) -> str:
    return input_stem.replace("%", "%%")


def _ffmpeg_output_pattern(
    output_dir: Path,
    *,
    input_stem: str,
    image_format: str,
) -> Path:
    escaped_output_dir = str(output_dir).replace("%", "%%")
    return (
        Path(escaped_output_dir)
        / f"{_ffmpeg_output_stem(input_stem)}_%06d.{image_format}"
    )


def export_video_frames(
    options: VideoFrameOptions,
    backend: FrameExportBackend,
) -> ExportResult:
    input_path = options.input_path
    _validate_input_path(input_path)

    image_format = _normalize_image_format(options.image_format)
    _validate_sampling_options(options)
    try:
        ffmpeg_exe = backend.get_ffmpeg_exe()
    except BackendUnavailableError as exc:
        raise VideoFrameError(str(exc), exit_code=1) from exc

    effective_fps = _effective_fps(options, backend=backend, input_path=input_path)
    output_dir = options.output_dir or default_output_dir(input_path)
    _prepare_output_dir(output_dir, overwrite=options.overwrite)

    output_pattern = _ffmpeg_output_pattern(
        output_dir,
        input_stem=input_path.stem,
        image_format=image_format,
    )
    if options.overwrite:
        _remove_existing_output_frames(
            output_dir,
            input_stem=input_path.stem,
            image_format=image_format,
        )

    command = _build_ffmpeg_command(
        ffmpeg_exe=ffmpeg_exe,
        options=options,
        input_path=input_path,
        output_pattern=output_pattern,
        effective_fps=effective_fps,
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


def _validate_sampling_options(options: VideoFrameOptions) -> None:
    if options.fps is not None and options.frames is not None:
        raise VideoFrameError("--fps and --frames cannot be used together.")
    if options.fps is not None and not (math.isfinite(options.fps) and options.fps > 0):
        raise VideoFrameError("FPS must be greater than 0.")
    if options.frames is not None and options.frames <= 0:
        raise VideoFrameError("Frames must be greater than 0.")


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
        rf"{re.escape(input_stem)}_[0-9]{{6}}\.{re.escape(image_format)}"
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
    effective_fps: float | None,
) -> list[str]:
    command = [
        ffmpeg_exe,
        "-hide_banner",
        "-y" if options.overwrite else "-n",
        "-i",
        str(input_path),
    ]

    filters = _video_filters(options, effective_fps=effective_fps)
    if filters:
        command.extend(["-vf", ",".join(filters)])

    command.append(str(output_pattern))
    return command


def _video_filters(
    options: VideoFrameOptions,
    *,
    effective_fps: float | None,
) -> list[str]:
    filters: list[str] = []
    if effective_fps is not None:
        filters.append(f"fps={effective_fps:g}")
    if options.size is not None:
        width, height = options.size
        filters.append(f"scale={width}:{height}")
    return filters


def _effective_fps(
    options: VideoFrameOptions,
    *,
    backend: FrameExportBackend,
    input_path: Path,
) -> float | None:
    if options.frames is not None:
        duration_seconds = backend.get_duration_seconds(input_path)
        if duration_seconds is None or not (
            math.isfinite(duration_seconds) and duration_seconds > 0
        ):
            raise VideoFrameError(
                "Unable to determine video duration for --frames mode.",
                exit_code=1,
            )
        return options.frames / duration_seconds
    return options.fps
