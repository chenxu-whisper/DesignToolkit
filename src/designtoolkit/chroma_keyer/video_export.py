from __future__ import annotations

import ctypes
import errno
import json
import os
import sys
from contextlib import closing
from pathlib import Path
from shutil import rmtree
from time import sleep
from uuid import uuid4

from . import core
from .image_io import write_rgba_png
from .models import KeyerError, KeyerParams, ScreenColorModel
from .video_models import (
    ProgressCallback,
    VideoDecoder,
    VideoKeyOptions,
    VideoKeyResult,
    VideoMetadata,
)

_CLEANUP_RETRY_DELAYS = (0.01, 0.02)
_WINDOWS_SHARING_VIOLATIONS = {5, 32, 33}
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_UNSUPPORTED_RENAME_ERRNOS = {
    errno.EINVAL,
    errno.ENOSYS,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
}


def _create_temporary_directory(output_root: Path, input_stem: str) -> Path:
    while True:
        temporary_dir = output_root / (
            f".{input_stem}_keyed_frames.{uuid4().hex}.tmp"
        )
        try:
            temporary_dir.mkdir()
        except FileExistsError:
            continue
        return temporary_dir


def _remove_directory(path: Path) -> None:
    cleanup_error: OSError | None = None
    for attempt in range(len(_CLEANUP_RETRY_DELAYS) + 1):
        try:
            rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            cleanup_error = error
            is_transient = isinstance(error, PermissionError) or (
                getattr(error, "winerror", None) in _WINDOWS_SHARING_VIOLATIONS
            )
            if not is_transient or attempt == len(_CLEANUP_RETRY_DELAYS):
                break
            sleep(_CLEANUP_RETRY_DELAYS[attempt])

    raise KeyerError(f"Failed to clean export directory: {path}") from cleanup_error


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform != "linux":
        raise KeyerError(
            "Atomic no-replace directory publication is unavailable "
            f"on platform {sys.platform}"
        )

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as error:
        raise KeyerError(
            "Atomic no-replace directory publication is unavailable: "
            "libc renameat2 is missing"
        ) from error

    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return

    error_number = ctypes.get_errno()
    native_error = OSError(
        error_number,
        os.strerror(error_number),
        destination,
    )
    if error_number in _UNSUPPORTED_RENAME_ERRNOS:
        raise KeyerError(
            "Atomic no-replace directory publication is unavailable: "
            "renameat2 RENAME_NOREPLACE is unsupported"
        ) from native_error
    raise native_error


def _manifest(
    input_path: Path,
    metadata: VideoMetadata,
    frame_count: int,
    model: ScreenColorModel,
    params: KeyerParams,
) -> dict[str, object]:
    return {
        "version": 1,
        "source_path": str(input_path),
        "width": metadata.width,
        "height": metadata.height,
        "fps": metadata.fps,
        "duration_seconds": metadata.duration_seconds,
        "frame_count": frame_count,
        "pattern": f"{input_path.stem}_keyed_%06d.png",
        "screen_color": list(model.samples[0]),
        "params": {
            "screen_gain": float(params.screen_gain),
            "screen_balance": float(params.screen_balance),
            "clip_black": float(params.clip_black),
            "clip_white": float(params.clip_white),
            "shrink_grow": int(params.shrink_grow),
            "feather": int(params.feather),
            "despill_amount": float(params.despill_amount),
        },
    }


def key_video_to_png_sequence(
    options: VideoKeyOptions,
    decoder: VideoDecoder,
    model: ScreenColorModel,
    params: KeyerParams,
    progress_callback: ProgressCallback | None = None,
) -> VideoKeyResult:
    """Stream a keyed video into an atomically published RGBA PNG sequence."""
    input_path = options.input_path
    temporary_dir: Path | None = None

    try:
        decoder.probe(input_path)

        output_root = options.output_root or input_path.parent
        if not output_root.exists() or not output_root.is_dir():
            raise KeyerError("Output root must exist and be a directory")

        final_dir = output_root / f"{input_path.stem}_keyed_frames"
        if final_dir.exists():
            raise KeyerError("Final output directory already exists")

        temporary_dir = _create_temporary_directory(output_root, input_path.stem)
        metadata, frame_stream = decoder.iter_frames(input_path)
        processed_frames = 0

        with closing(frame_stream):
            for frame in frame_stream:
                keyed_rgb = alpha = None
                try:
                    processed_frames += 1
                    output_path = temporary_dir / (
                        f"{input_path.stem}_keyed_{processed_frames:06d}.png"
                    )
                    keyed_rgb, alpha = core.key_rgb(frame, model, params)
                    write_rgba_png(output_path, keyed_rgb, alpha)
                    if progress_callback is not None:
                        progress_callback(
                            processed_frames,
                            metadata.estimated_frame_count,
                            output_path,
                        )
                finally:
                    del frame, keyed_rgb, alpha

        if processed_frames == 0:
            raise KeyerError("Decoded video contains no frames")

        manifest_path = temporary_dir / "manifest.json"
        with manifest_path.open("w", encoding="utf-8", newline="\n") as manifest_file:
            json.dump(
                _manifest(input_path, metadata, processed_frames, model, params),
                manifest_file,
                ensure_ascii=False,
                indent=2,
            )
            manifest_file.write("\n")

        result = VideoKeyResult(
            output_dir=final_dir,
            manifest_path=final_dir / "manifest.json",
            frame_count=processed_frames,
            metadata=metadata,
        )
        _publish_directory_no_replace(temporary_dir, final_dir)
        temporary_dir = None
        return result
    except KeyerError:
        raise
    except Exception as error:
        raise KeyerError("Video PNG sequence export failed") from error
    finally:
        if temporary_dir is not None:
            _remove_directory(temporary_dir)
