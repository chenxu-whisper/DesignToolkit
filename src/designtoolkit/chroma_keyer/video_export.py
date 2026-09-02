from __future__ import annotations

import json
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

_OWNERSHIP_MARKER_NAME = ".designtoolkit-video-export-owner"
_CLEANUP_RETRY_DELAYS = (0.01, 0.02)
_WINDOWS_SHARING_VIOLATIONS = {5, 32, 33}


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


def _is_owned_directory(path: Path, ownership_token: str) -> bool:
    try:
        marker_contents = (path / _OWNERSHIP_MARKER_NAME).read_text(
            encoding="ascii"
        )
    except OSError:
        return False
    return marker_contents == ownership_token


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
    published_directory: Path | None = None
    ownership_token = uuid4().hex

    try:
        decoder.probe(input_path)

        output_root = options.output_root or input_path.parent
        if not output_root.exists() or not output_root.is_dir():
            raise KeyerError("Output root must exist and be a directory")

        final_dir = output_root / f"{input_path.stem}_keyed_frames"
        if final_dir.exists():
            raise KeyerError("Final output directory already exists")

        temporary_dir = _create_temporary_directory(output_root, input_path.stem)
        (temporary_dir / _OWNERSHIP_MARKER_NAME).write_text(
            ownership_token,
            encoding="ascii",
        )
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

        try:
            temporary_dir.replace(final_dir)
        except Exception:
            if _is_owned_directory(final_dir, ownership_token):
                published_directory = final_dir
            raise
        temporary_dir = None
        published_directory = final_dir
        (final_dir / _OWNERSHIP_MARKER_NAME).unlink()
        published_directory = None

        return VideoKeyResult(
            output_dir=final_dir,
            manifest_path=final_dir / "manifest.json",
            frame_count=processed_frames,
            metadata=metadata,
        )
    except KeyerError:
        raise
    except Exception as error:
        raise KeyerError("Video PNG sequence export failed") from error
    finally:
        if temporary_dir is not None:
            _remove_directory(temporary_dir)
        if published_directory is not None:
            _remove_directory(published_directory)
