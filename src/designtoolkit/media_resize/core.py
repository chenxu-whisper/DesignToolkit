from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Protocol

from designtoolkit.video_frames import BackendUnavailableError

IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"})
AUDIO_CODEC_BY_VIDEO_SUFFIX = {
    ".avi": "libmp3lame",
    ".m4v": "aac",
    ".mkv": "aac",
    ".mov": "aac",
    ".mp4": "aac",
    ".webm": "libopus",
}


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

    def run(
        self,
        command: Sequence[str],
    ) -> ResizeBackendResult:
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
    media_type = _validate_output_path(
        input_path=input_path,
        output_path=output_path,
    )
    if media_type == "video" and min(options.size) < 2:
        raise MediaResizeError("Video width and height must each be at least 2")

    try:
        temporary_directory, temporary_path = _create_temporary_output_path(output_path)
    except OSError as exc:
        raise MediaResizeError(
            f"Temporary output directory could not be created for {output_path}: {exc}",
            exit_code=1,
        ) from exc

    published_identity: tuple[int, int] | None = None
    try:
        try:
            ffmpeg_exe = backend.get_ffmpeg_exe()
        except BackendUnavailableError as exc:
            raise MediaResizeError(str(exc), exit_code=1) from exc

        command = _build_ffmpeg_command(
            ffmpeg_exe=ffmpeg_exe,
            input_path=input_path,
            output_path=temporary_path,
            size=options.size,
            media_type=media_type,
        )
        result = _run_backend(backend, command)
        if result.returncode != 0:
            detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
            raise MediaResizeError(
                f"FFmpeg failed with exit code {result.returncode}{detail}",
                exit_code=1,
            )
        published_identity = _publish_output_no_replace(temporary_path, output_path)
    except BaseException as primary_error:
        cleanup_error = _cleanup_temporary_output(
            temporary_directory,
            temporary_path,
        )
        if cleanup_error is not None:
            primary_error.add_note(
                _temporary_cleanup_error_context(
                    temporary_directory,
                    cleanup_error,
                )
            )
        raise

    cleanup_error = _cleanup_temporary_output(
        temporary_directory,
        temporary_path,
    )
    if cleanup_error is not None:
        cleanup_context = _temporary_cleanup_error_context(
            temporary_directory,
            cleanup_error,
        )
        if published_identity is not None:
            if not _path_has_identity(output_path, published_identity):
                raise MediaResizeError(
                    f"{cleanup_context}. rollback skipped because final output "
                    f"was replaced: {output_path}",
                    exit_code=1,
                ) from cleanup_error
            try:
                output_path.unlink(missing_ok=True)
            except OSError as rollback_error:
                error = MediaResizeError(
                    "Temporary output cleanup and rollback failed; "
                    f"final output may remain at {output_path}: {rollback_error}",
                    exit_code=1,
                )
                error.add_note(cleanup_context)
                raise error from rollback_error
        raise MediaResizeError(
            f"{cleanup_context}. Published output was rolled back: {output_path}",
            exit_code=1,
        ) from cleanup_error

    public_command = (*command[:-1], str(output_path))
    return MediaResizeResult(output_path=output_path, command=public_command)


def _create_temporary_output_path(output_path: Path) -> tuple[Path, Path]:
    temporary_directory = Path(
        mkdtemp(
            prefix=f".{output_path.stem}.",
            dir=output_path.parent,
        )
    )
    temporary_path = temporary_directory / f"output{output_path.suffix}"
    return temporary_directory, temporary_path


def _cleanup_temporary_output(
    temporary_directory: Path,
    temporary_path: Path,
) -> OSError | None:
    try:
        temporary_path.unlink(missing_ok=True)
        temporary_directory.rmdir()
    except OSError as cleanup_error:
        return cleanup_error
    return None


def _temporary_cleanup_error_context(
    temporary_directory: Path,
    cleanup_error: OSError,
) -> str:
    return f"Temporary output cleanup failed for {temporary_directory}: {cleanup_error}"


def _publish_output_no_replace(
    temporary_path: Path,
    output_path: Path,
) -> tuple[int, int]:
    try:
        completed_identity = _path_identity(temporary_path)
    except OSError as exc:
        raise MediaResizeError(
            f"Resized media could not be published: {output_path}",
            exit_code=1,
        ) from exc

    try:
        os.link(temporary_path, output_path)
    except FileExistsError as exc:
        raise MediaResizeError(f"Output path already exists: {output_path}") from exc
    except OSError as exc:
        return _publish_output_with_reservation(
            temporary_path,
            output_path,
            completed_identity=completed_identity,
            link_error=exc,
        )
    return completed_identity


def _publish_output_with_reservation(
    temporary_path: Path,
    output_path: Path,
    *,
    completed_identity: tuple[int, int],
    link_error: OSError,
) -> tuple[int, int]:
    try:
        descriptor = os.open(
            output_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise MediaResizeError(f"Output path already exists: {output_path}") from exc
    except OSError as exc:
        raise MediaResizeError(
            f"Resized media could not be published: {output_path}",
            exit_code=1,
        ) from exc

    try:
        reservation_identity = _stat_identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)

    try:
        if not _path_has_identity(output_path, reservation_identity):
            raise OSError("Output reservation was replaced before publication")
        os.replace(temporary_path, output_path)
        return completed_identity
    except OSError as exc:
        cleanup_error = _unlink_if_owned(output_path, reservation_identity)
        error = MediaResizeError(
            f"Resized media could not be published: {output_path}",
            exit_code=1,
        )
        error.add_note(f"Hard-link publication failed: {link_error}")
        if cleanup_error is not None:
            error.add_note(f"Output reservation cleanup failed: {cleanup_error}")
        raise error from exc


def _path_identity(path: Path) -> tuple[int, int]:
    return _stat_identity(os.stat(path, follow_symlinks=False))


def _stat_identity(stat_result: os.stat_result) -> tuple[int, int]:
    return stat_result.st_dev, stat_result.st_ino


def _path_has_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        return _path_identity(path) == identity
    except OSError:
        return False


def _unlink_if_owned(
    path: Path,
    identity: tuple[int, int],
) -> OSError | None:
    if not _path_has_identity(path, identity):
        return None
    try:
        path.unlink()
    except OSError as exc:
        return exc
    return None


def _run_backend(
    backend: ResizeBackend,
    command: Sequence[str],
) -> ResizeBackendResult:
    guarded_run = getattr(backend, "run_guarded", None)
    if callable(guarded_run):
        return guarded_run(command, output_kind="single")
    return backend.run(command)


def _validate_input_path(input_path: Path) -> None:
    if not input_path.exists():
        raise MediaResizeError(f"Input media does not exist: {input_path}")
    if not input_path.is_file():
        raise MediaResizeError(f"Input path is not a file: {input_path}")


def _validate_output_path(*, input_path: Path, output_path: Path) -> str | None:
    if output_path == input_path:
        raise MediaResizeError(
            f"Output path must differ from input path: {output_path}"
        )
    if output_path.exists():
        raise MediaResizeError(f"Output path already exists: {output_path}")
    input_media_type = _media_type_for_suffix(input_path.suffix)
    output_media_type = _media_type_for_suffix(output_path.suffix)
    if (
        input_media_type is not None
        and output_media_type is not None
        and input_media_type != output_media_type
    ):
        raise MediaResizeError(
            "Output media type must match input media type: "
            f"{input_path.suffix} -> {output_path.suffix}"
        )
    return input_media_type or output_media_type


def _media_type_for_suffix(suffix: str) -> str | None:
    normalized_suffix = suffix.lower()
    if normalized_suffix in IMAGE_SUFFIXES:
        return "image"
    if normalized_suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def _build_ffmpeg_command(
    *,
    ffmpeg_exe: str,
    input_path: Path,
    output_path: Path,
    size: tuple[int, int],
    media_type: str | None,
) -> list[str]:
    width, height = size
    scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease"
    command = [
        ffmpeg_exe,
        "-hide_banner",
        "-n",
        "-i",
        str(input_path),
        "-vf",
        scale,
    ]
    if media_type == "video":
        command[command.index("-vf") + 1] += ":force_divisible_by=2"
        input_suffix = input_path.suffix.lower()
        output_suffix = output_path.suffix.lower()
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
            ]
        )
        if input_suffix == output_suffix:
            command.extend(["-c:a", "copy"])
        elif audio_codec := AUDIO_CODEC_BY_VIDEO_SUFFIX.get(output_suffix):
            command.extend(["-c:a", audio_codec])
    elif media_type is None:
        command[command.index("-vf") + 1] += ":force_divisible_by=2"
        command.extend(["-map", "0", "-c:a", "copy"])
    command.append(str(output_path))
    return command
