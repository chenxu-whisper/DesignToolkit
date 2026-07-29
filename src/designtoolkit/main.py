from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from designtoolkit.media_resize import (
    MediaResizeError,
    MediaResizeOptions,
    MediaResizeResult,
    ResizeBackend,
    resize_media,
)
from designtoolkit.video_frames import (
    ExportResult,
    FrameExportBackend,
    VideoFrameError,
    VideoFrameOptions,
    export_video_frames,
    parse_size,
)

Exporter = Callable[[VideoFrameOptions, FrameExportBackend], ExportResult]
MediaResizer = Callable[[MediaResizeOptions, ResizeBackend], MediaResizeResult]
BackendFactory = Callable[[], FrameExportBackend | ResizeBackend]


def main(
    argv: Sequence[str] | None = None,
    *,
    exporter: Exporter = export_video_frames,
    media_resizer: MediaResizer = resize_media,
    backend_factory: BackendFactory | None = None,
) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return _system_exit_code(exc)

    if args.command is None:
        print("DesignToolkit is ready.")
        return 0

    if args.command == "video-to-frames":
        try:
            backend = _create_backend(backend_factory)
            result = exporter(_video_frame_options(args), backend)
        except VideoFrameError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code

        print(f"Exported frames to {result.output_dir}")
        return 0

    if args.command == "resize-media":
        try:
            backend = _create_backend(backend_factory)
            result = media_resizer(_media_resize_options(args), backend)
        except MediaResizeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code

        print(f"Resized media to {result.output_path}")
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="designtoolkit")
    subparsers = parser.add_subparsers(dest="command")

    video_to_frames = subparsers.add_parser(
        "video-to-frames",
        help="Export a video to image frames.",
    )
    video_to_frames.add_argument("video_path", type=Path)
    video_to_frames.add_argument("--output-dir", type=Path)
    video_to_frames.add_argument(
        "--format",
        choices=("png", "jpg", "jpeg"),
        default="png",
        dest="image_format",
    )
    video_to_frames.add_argument("--size", type=_parse_size_argument)
    video_to_frames.add_argument("--fps", type=float)
    video_to_frames.add_argument("--frames", type=int)
    video_to_frames.add_argument("--overwrite", action="store_true")

    resize_media_parser = subparsers.add_parser(
        "resize-media",
        help="Resize a media file.",
    )
    resize_media_parser.add_argument("input_path", type=Path)
    resize_media_parser.add_argument("size", type=_parse_size_argument)
    resize_media_parser.add_argument("output_path", nargs="?", type=Path)

    return parser


def _parse_size_argument(value: str) -> tuple[int, int]:
    try:
        return parse_size(value)
    except VideoFrameError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _create_backend(
    backend_factory: BackendFactory | None,
) -> FrameExportBackend | ResizeBackend:
    if backend_factory is not None:
        return backend_factory()

    from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

    return FfmpegBackend()


def _video_frame_options(args: argparse.Namespace) -> VideoFrameOptions:
    return VideoFrameOptions(
        input_path=args.video_path,
        output_dir=args.output_dir,
        image_format=args.image_format,
        size=args.size,
        fps=args.fps,
        frames=args.frames,
        overwrite=args.overwrite,
    )


def _media_resize_options(args: argparse.Namespace) -> MediaResizeOptions:
    return MediaResizeOptions(
        input_path=args.input_path,
        size=args.size,
        output_path=args.output_path,
    )


def _system_exit_code(exc: SystemExit) -> int:
    if isinstance(exc.code, int):
        return exc.code
    if exc.code is None:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
