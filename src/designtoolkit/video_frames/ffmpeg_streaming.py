from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from designtoolkit.web.output_limits import (
    OutputBudgetGuard,
    _parse_sequential_pattern,
)

_MAX_PPM_HEADER_BYTES = 64 * 1024
_MAX_PPM_DIMENSION = 16_384
_MAX_PPM_FRAME_BYTES = 25 * 1024 * 1024
_PPM_WHITESPACE = b" \t\r\n\v\f"
_SEQUENCE_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
_SINGLE_IMAGE_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}
_VIDEO_MUXERS = {
    ".mp4": "mp4",
    ".mov": "mov",
    ".m4v": "ipod",
    ".avi": "avi",
    ".mkv": "matroska",
    ".webm": "webm",
}
_FRAGMENTED_MOV_SUFFIXES = frozenset({".mp4", ".mov", ".m4v"})


class FfmpegOutputError(RuntimeError):
    """Raised when streamed FFmpeg output violates the guarded protocol."""


@dataclass(frozen=True)
class PpmFrame:
    width: int
    height: int
    pixels: bytes


class PpmStreamParser:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._frame_width: int | None = None
        self._frame_height: int | None = None
        self._frame_bytes: int | None = None

    def feed(self, data: bytes) -> list[PpmFrame]:
        self._buffer.extend(data)
        frames: list[PpmFrame] = []
        while True:
            if self._frame_bytes is None:
                header = _parse_ppm_header(self._buffer)
                if header is None:
                    if len(self._buffer) > _MAX_PPM_HEADER_BYTES:
                        raise FfmpegOutputError(
                            "FFmpeg PPM header exceeds its size limit"
                        )
                    return frames
                header_bytes, width, height = header
                del self._buffer[:header_bytes]
                self._frame_width = width
                self._frame_height = height
                self._frame_bytes = width * height * 3

            if len(self._buffer) < self._frame_bytes:
                return frames
            pixels = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]
            frames.append(
                PpmFrame(
                    width=self._required_dimension(self._frame_width),
                    height=self._required_dimension(self._frame_height),
                    pixels=pixels,
                )
            )
            self._frame_width = None
            self._frame_height = None
            self._frame_bytes = None

    def finish(self) -> None:
        if self._buffer or self._frame_bytes is not None:
            raise FfmpegOutputError("FFmpeg produced a truncated PPM stream")

    @staticmethod
    def _required_dimension(value: int | None) -> int:
        if value is None:
            raise RuntimeError("PPM parser frame dimensions are unavailable")
        return value


class _BudgetedFile:
    def __init__(
        self,
        file: BinaryIO,
        path: Path,
        guard: OutputBudgetGuard,
    ) -> None:
        self._file = file
        self._path = path
        self._guard = guard

    def write(self, data: bytes | bytearray | memoryview) -> int:
        return self._guard.write(self._file, self._path, data)

    def tell(self) -> int:
        return self._file.tell()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._file.seek(offset, whence)

    def truncate(self, size: int | None = None) -> int:
        return self._guard.truncate(self._file, self._path, size)

    def flush(self) -> None:
        self._file.flush()


class GuardedFfmpegOutput:
    def __init__(
        self,
        command: Sequence[str],
        guard: OutputBudgetGuard,
    ) -> None:
        if not command:
            raise FfmpegOutputError("FFmpeg command has no output target")
        self._guard = guard
        self._original_target = str(command[-1])
        self._sequence = _parse_sequential_pattern(self._original_target)
        self._target = Path(self._original_target)
        guard.validate_path(self._target)
        self._parser: PpmStreamParser | None = None
        self._image_format: str | None = None
        self._single_image = False
        self._frame_index = _sequence_start_number(command)
        self._frame_count = 0
        self._temporary_paths: list[Path] = []
        self._published_paths: list[Path] = []
        self._is_video = False
        self._video_file: BinaryIO | None = None
        self._video_temporary: Path | None = None

        suffix = self._target.suffix.lower()
        if self._sequence is not None:
            _prefix, width, _pattern_suffix = self._sequence
            if width <= 0 or suffix not in _SEQUENCE_IMAGE_SUFFIXES:
                raise FfmpegOutputError(
                    f"FFmpeg guarded output target is unsupported: "
                    f"{self._original_target}"
                )
            self._parser = PpmStreamParser()
            self._image_format = _SINGLE_IMAGE_FORMATS[suffix]
            output_args = ["-f", "image2pipe", "-vcodec", "ppm", "pipe:1"]
        elif suffix in _SINGLE_IMAGE_FORMATS:
            if "%" in self._original_target:
                raise FfmpegOutputError(
                    f"FFmpeg guarded output target is unsupported: "
                    f"{self._original_target}"
                )
            if self._target.exists():
                raise FfmpegOutputError(
                    f"FFmpeg output target already exists: {self._target}"
                )
            self._parser = PpmStreamParser()
            self._image_format = _SINGLE_IMAGE_FORMATS[suffix]
            self._single_image = True
            output_args = [
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "ppm",
                "pipe:1",
            ]
        elif suffix in _VIDEO_MUXERS:
            if "%" in self._original_target:
                raise FfmpegOutputError(
                    f"FFmpeg guarded output target is unsupported: "
                    f"{self._original_target}"
                )
            if self._target.exists():
                raise FfmpegOutputError(
                    f"FFmpeg output target already exists: {self._target}"
                )
            output_args = []
            if suffix in _FRAGMENTED_MOV_SUFFIXES:
                output_args.extend(
                    ["-movflags", "+frag_keyframe+empty_moov"]
                )
            output_args.extend(["-f", _VIDEO_MUXERS[suffix], "pipe:1"])
            self._is_video = True
        else:
            raise FfmpegOutputError(
                f"FFmpeg guarded output target is unsupported: "
                f"{self._original_target}"
            )
        self.command = [*command[:-1], *output_args]

    def consume(self, data: bytes) -> None:
        if self._is_video:
            if self._video_file is None:
                self._open_video_temporary()
            if self._video_temporary is None:
                raise RuntimeError("Guarded video temporary path is unavailable")
            view = memoryview(data)
            while view:
                written = self._guard.write(
                    self._video_file,
                    self._video_temporary,
                    view,
                )
                if written == 0:
                    raise OSError("Guarded FFmpeg output write made no progress")
                view = view[written:]
            return
        if self._parser is None:
            raise RuntimeError("Guarded output sink is unavailable")
        for frame in self._parser.feed(data):
            if self._single_image and self._frame_count:
                raise FfmpegOutputError(
                    "FFmpeg produced multiple frames for a single image target"
                )
            target = self._frame_target()
            self._write_image(frame, target)
            self._frame_count += 1

    def finish(self) -> None:
        if self._is_video:
            if self._video_file is None:
                self._open_video_temporary()
            temporary = self._video_temporary
            if temporary is None:
                raise RuntimeError("Guarded video temporary path is unavailable")
            self._video_file.flush()
            os.fsync(self._video_file.fileno())
            self._video_file.close()
            self._video_file = None
            self._published_paths.append(self._target)
            self._guard.rename(temporary, self._target)
            self._temporary_paths.remove(temporary)
            self._video_temporary = None
            return
        if self._parser is None:
            raise RuntimeError("Guarded image parser is unavailable")
        self._parser.finish()
        if self._single_image and self._frame_count != 1:
            raise FfmpegOutputError(
                "FFmpeg produced no frame for a single image target"
            )

    def rollback(self) -> None:
        errors: list[BaseException] = []
        if self._video_file is not None:
            try:
                self._video_file.close()
            except BaseException as exc:
                errors.append(exc)
            self._video_file = None
        for path in reversed(self._published_paths):
            try:
                self._guard.rollback_created(path)
            except BaseException as exc:
                errors.append(exc)
        for path in reversed(self._temporary_paths):
            try:
                self._rollback_path(path)
            except BaseException as exc:
                errors.append(exc)
        self._temporary_paths.clear()
        self._published_paths.clear()
        self._video_temporary = None
        if errors:
            raise errors[0]

    def _open_video_temporary(self) -> None:
        self._target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._target.with_name(
            f".{self._target.name}.{uuid.uuid4().hex}.tmp"
        )
        self._temporary_paths.append(temporary)
        self._video_temporary = temporary
        file = temporary.open("x+b")
        try:
            self._guard.register_created(file, temporary)
        except BaseException:
            file.close()
            temporary.unlink(missing_ok=True)
            raise
        self._video_file = file

    def _frame_target(self) -> Path:
        if self._sequence is None:
            return self._target
        prefix, width, suffix = self._sequence
        frame_number = self._frame_index + self._frame_count
        return Path(f"{prefix}{frame_number:0{width}d}{suffix}")

    def _write_image(self, frame: PpmFrame, target: Path) -> None:
        if target.exists():
            raise FfmpegOutputError(
                f"FFmpeg output target already exists: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            f".{target.name}.{uuid.uuid4().hex}.tmp"
        )
        self._temporary_paths.append(temporary)
        try:
            with temporary.open("x+b") as raw_file:
                self._guard.register_created(raw_file, temporary)
                file = _BudgetedFile(raw_file, temporary, self._guard)
                with Image.frombytes(
                    "RGB",
                    (frame.width, frame.height),
                    frame.pixels,
                ) as image:
                    image.save(file, format=self._image_format)
                file.flush()
                os.fsync(raw_file.fileno())
            self._published_paths.append(target)
            self._guard.rename(temporary, target)
            self._temporary_paths.remove(temporary)
        except BaseException:
            self._rollback_path(temporary)
            raise

    def _rollback_path(self, path: Path) -> None:
        try:
            self._guard.rollback_created(path)
        finally:
            path.unlink(missing_ok=True)
            if path in self._temporary_paths:
                self._temporary_paths.remove(path)


def _sequence_start_number(command: Sequence[str]) -> int:
    for index, argument in enumerate(command[:-1]):
        if argument != "-start_number":
            continue
        try:
            value = int(command[index + 1])
        except (IndexError, ValueError) as exc:
            raise FfmpegOutputError(
                "FFmpeg sequence start number is invalid"
            ) from exc
        if value < 0:
            raise FfmpegOutputError(
                "FFmpeg sequence start number must not be negative"
            )
        return value
    return 1


def _parse_ppm_header(
    buffer: bytearray,
) -> tuple[int, int, int] | None:
    if not buffer:
        return None
    if buffer[0] != ord("P"):
        raise FfmpegOutputError("FFmpeg output must use binary PPM P6")
    if len(buffer) < 2:
        return None
    if buffer[1] != ord("6"):
        raise FfmpegOutputError("FFmpeg output must use binary PPM P6")
    if len(buffer) < 3:
        return None
    if buffer[2] not in _PPM_WHITESPACE:
        raise FfmpegOutputError("FFmpeg output must use binary PPM P6")

    tokens: list[bytes] = [b"P6"]
    index = 2
    while len(tokens) < 4:
        index = _skip_ppm_separators(buffer, index)
        if index >= len(buffer):
            return None
        token_start = index
        while (
            index < len(buffer)
            and buffer[index] not in _PPM_WHITESPACE
            and buffer[index] != ord("#")
        ):
            index += 1
        if index == len(buffer):
            return None
        if index == token_start:
            raise FfmpegOutputError("FFmpeg produced a malformed PPM header")
        tokens.append(bytes(buffer[token_start:index]))

    if buffer[index] not in _PPM_WHITESPACE:
        raise FfmpegOutputError("FFmpeg PPM maxval requires a separator")
    header_bytes = index + 1
    if buffer[index] == ord("\r"):
        if header_bytes == len(buffer):
            return None
        if buffer[header_bytes] == ord("\n"):
            header_bytes += 1

    if tokens[0] != b"P6":
        raise FfmpegOutputError("FFmpeg output must use binary PPM P6")
    width = _parse_ppm_integer(tokens[1], "width")
    height = _parse_ppm_integer(tokens[2], "height")
    maxval = _parse_ppm_integer(tokens[3], "maxval")
    if width <= 0 or height <= 0:
        raise FfmpegOutputError("FFmpeg PPM dimensions must be positive")
    frame_bytes = width * height * 3
    if frame_bytes > _MAX_PPM_FRAME_BYTES:
        raise FfmpegOutputError("FFmpeg PPM frame size exceeds its hard limit")
    if width > _MAX_PPM_DIMENSION or height > _MAX_PPM_DIMENSION:
        raise FfmpegOutputError("FFmpeg PPM dimensions exceed their hard limit")
    if maxval != 255:
        raise FfmpegOutputError("FFmpeg PPM maxval must be 255")
    return header_bytes, width, height


def _skip_ppm_separators(buffer: bytearray, index: int) -> int:
    while True:
        while index < len(buffer) and buffer[index] in _PPM_WHITESPACE:
            index += 1
        if index >= len(buffer) or buffer[index] != ord("#"):
            return index
        newline = buffer.find(b"\n", index + 1)
        if newline < 0:
            return len(buffer)
        index = newline + 1


def _parse_ppm_integer(token: bytes, name: str) -> int:
    if (
        not token
        or len(token) > 10
        or any(byte < ord("0") or byte > ord("9") for byte in token)
    ):
        raise FfmpegOutputError(f"FFmpeg PPM {name} is not a decimal integer")
    return int(token)
