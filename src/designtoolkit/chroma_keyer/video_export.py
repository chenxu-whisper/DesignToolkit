from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from shutil import rmtree
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
        "params": asdict(params),
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
    remove_published_directory = False

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
            remove_published_directory = (
                not temporary_dir.exists() and final_dir.exists()
            )
            raise
        temporary_dir = None

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
            rmtree(temporary_dir, ignore_errors=True)
        if remove_published_directory:
            rmtree(final_dir, ignore_errors=True)
