from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from designtoolkit.video_frames import BackendUnavailableError


class MediaResizeError(Exception):
    """Expected user-facing error while resizing media."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ResizeBackendResult:
    returncode: int
    stderr: str = ""


@dataclass(frozen=True)
class MediaResizeResult:
    output_path: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class MediaResizeOptions:
    input_path: Path
    size: tuple[int, int]
    output_path: Path | None = None


class ResizeBackend(Protocol):
    def get_ffmpeg_exe(self) -> str:
        raise NotImplementedError

    def run(self, command: Sequence[str]) -> ResizeBackendResult:
        raise NotImplementedError


def default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_resized{input_path.suffix}"


def resize_media(
    options: MediaResizeOptions,
    backend: ResizeBackend,
) -> MediaResizeResult:
    input_path = options.input_path
    _validate_input_path(input_path)
    output_path = options.output_path or default_output_path(input_path)
    _validate_output_path(input_path=input_path, output_path=output_path)

    try:
        ffmpeg_exe = backend.get_ffmpeg_exe()
    except BackendUnavailableError as exc:
        raise MediaResizeError(str(exc), exit_code=1) from exc

    command = _build_ffmpeg_command(
        ffmpeg_exe=ffmpeg_exe,
        input_path=input_path,
        output_path=output_path,
        size=options.size,
    )
    result = backend.run(command)
    if result.returncode != 0:
        detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
        raise MediaResizeError(
            f"FFmpeg failed with exit code {result.returncode}{detail}",
            exit_code=1,
        )

    return MediaResizeResult(output_path=output_path, command=tuple(command))


def _validate_input_path(input_path: Path) -> None:
    if not input_path.exists():
        raise MediaResizeError(f"Input media does not exist: {input_path}")
    if not input_path.is_file():
        raise MediaResizeError(f"Input path is not a file: {input_path}")


def _validate_output_path(*, input_path: Path, output_path: Path) -> None:
    if output_path == input_path:
        raise MediaResizeError(
            f"Output path must differ from input path: {output_path}"
        )
    if output_path.exists():
        raise MediaResizeError(f"Output path already exists: {output_path}")


def _build_ffmpeg_command(
    *,
    ffmpeg_exe: str,
    input_path: Path,
    output_path: Path,
    size: tuple[int, int],
) -> list[str]:
    width, height = size
    return [
        ffmpeg_exe,
        "-hide_banner",
        "-n",
        "-i",
        str(input_path),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2",
        "-map",
        "0",
        "-c:a",
        "copy",
        str(output_path),
    ]
