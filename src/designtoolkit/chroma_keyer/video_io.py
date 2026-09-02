from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import numpy as np

from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

from .image_io import resize_preview_rgb
from .models import KeyerError
from .video_models import (
    VideoFrameStream,
    VideoMetadata,
    clamp_timeline_timestamp,
    validate_timestamp,
    validate_video_path_suffix,
)

ReadFrames = Callable[..., Iterator[object]]
AvailabilityValidator = Callable[[], object]


def _close_frames(frames: Iterator[object]) -> None:
    close = getattr(frames, "close", None)
    if callable(close):
        close()


def _validate_input_path(path: Path) -> None:
    validate_video_path_suffix(path)
    if not path.exists():
        raise KeyerError("Input path does not exist")
    if not path.is_file():
        raise KeyerError("Input path must be a file")


def _normalize_metadata(raw_metadata: object) -> VideoMetadata:
    try:
        if not isinstance(raw_metadata, Mapping):
            raise ValueError
        dimensions = raw_metadata.get("size", raw_metadata.get("source_size"))
        if (
            not isinstance(dimensions, Sequence)
            or isinstance(dimensions, (str, bytes))
            or len(dimensions) != 2
        ):
            raise ValueError
        width, height = dimensions
        fps = raw_metadata["fps"]
        duration = raw_metadata["duration"]
        codec = raw_metadata["codec"]
        if not isinstance(fps, (int, float)) or isinstance(fps, bool):
            raise ValueError
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            raise ValueError
        return VideoMetadata(
            width=width,
            height=height,
            fps=fps,
            duration_seconds=duration,
            estimated_frame_count=max(1, round(fps * duration)),
            codec=codec,
        )
    except (KeyError, TypeError, ValueError, KeyerError) as error:
        raise KeyerError("Video metadata is missing or invalid") from error


def _read_metadata(frames: Iterator[object]) -> VideoMetadata:
    try:
        raw_metadata = next(frames)
    except StopIteration as error:
        raise KeyerError("Video metadata is missing or invalid") from error
    except Exception as error:
        raise KeyerError("Video could not be opened") from error
    return _normalize_metadata(raw_metadata)


def _decode_frame(
    frame_bytes: object,
    *,
    width: int,
    height: int,
) -> np.ndarray:
    expected = width * height * 3
    if not isinstance(frame_bytes, (bytes, bytearray, memoryview)):
        raise KeyerError("Decoded video frame has an invalid byte length")
    if len(frame_bytes) != expected:
        raise KeyerError("Decoded video frame has an invalid byte length")
    try:
        return (
            np.frombuffer(frame_bytes, dtype=np.uint8)
            .reshape(height, width, 3)
            .copy()
        )
    except (BufferError, TypeError, ValueError) as error:
        raise KeyerError("Decoded video frame has an invalid byte length") from error


class _DecodedFrameIterator(Iterator[np.ndarray]):
    def __init__(self, frames: Iterator[object], metadata: VideoMetadata) -> None:
        self._frames = frames
        self._metadata = metadata
        self._closed = False

    def __iter__(self) -> _DecodedFrameIterator:
        return self

    def __next__(self) -> np.ndarray:
        if self._closed:
            raise StopIteration
        try:
            frame_bytes = next(self._frames)
        except StopIteration:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise KeyerError("Video frames could not be decoded") from error

        try:
            return _decode_frame(
                frame_bytes,
                width=self._metadata.width,
                height=self._metadata.height,
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _close_frames(self._frames)


class ImageioVideoDecoder:
    """Decode RGB video frames through imageio-ffmpeg's bundled executable."""

    def __init__(
        self,
        *,
        read_frames: ReadFrames = imageio_ffmpeg.read_frames,
        availability_validator: AvailabilityValidator | None = None,
    ) -> None:
        self._read_frames = read_frames
        self._availability_validator = (
            availability_validator or FfmpegBackend().get_ffmpeg_exe
        )

    def _open_frames(
        self,
        path: Path,
        *,
        input_params: list[str] | None = None,
        output_params: list[str] | None = None,
    ) -> Iterator[object]:
        _validate_input_path(path)
        try:
            self._availability_validator()
        except Exception as error:
            raise KeyerError("Bundled FFmpeg is unavailable") from error

        kwargs: dict[str, Any] = {"pix_fmt": "rgb24"}
        if input_params is not None:
            kwargs["input_params"] = input_params
        if output_params is not None:
            kwargs["output_params"] = output_params
        try:
            return self._read_frames(path, **kwargs)
        except Exception as error:
            raise KeyerError("Video could not be opened") from error

    def probe(self, path: Path) -> VideoMetadata:
        frames = self._open_frames(path)
        try:
            return _read_metadata(frames)
        finally:
            _close_frames(frames)

    def read_frame(self, path: Path, timestamp_seconds: float) -> np.ndarray:
        metadata = self.probe(path)
        timestamp = clamp_timeline_timestamp(
            validate_timestamp(timestamp_seconds, metadata),
            metadata,
        )
        frames = self._open_frames(
            path,
            input_params=["-ss", str(timestamp)],
            output_params=["-frames:v", "1"],
        )
        try:
            _read_metadata(frames)
            try:
                frame_bytes = next(frames)
            except StopIteration as error:
                raise KeyerError("Decoded video contains no frames") from error
            except Exception as error:
                raise KeyerError("Video frame could not be decoded") from error
            return _decode_frame(
                frame_bytes,
                width=metadata.width,
                height=metadata.height,
            )
        finally:
            _close_frames(frames)

    def read_preview_frame(
        self,
        path: Path,
        timestamp_seconds: float,
    ) -> np.ndarray:
        return resize_preview_rgb(self.read_frame(path, timestamp_seconds))

    def iter_frames(
        self,
        path: Path,
    ) -> tuple[VideoMetadata, VideoFrameStream]:
        frames = self._open_frames(path)
        try:
            metadata = _read_metadata(frames)
        except Exception:
            _close_frames(frames)
            raise
        return metadata, _DecodedFrameIterator(frames, metadata)
