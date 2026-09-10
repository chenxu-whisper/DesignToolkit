from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from PIL import Image

from designtoolkit.media_constraints import (
    HOSTED_MAX_FRAME_OUTPUTS,
    HOSTED_MAX_IMAGE_PIXELS,
    HOSTED_MAX_IMAGE_RGBA_BYTES,
    HOSTED_MAX_VIDEO_RGBA_FRAME_BYTES,
)
from designtoolkit.web.output_limits import (
    OutputBudgetGuard,
    _parse_sequential_pattern,
)

_MAX_PAM_HEADER_BYTES = 64 * 1024
_MAX_PAM_INTEGER_BYTES = len(str(HOSTED_MAX_IMAGE_PIXELS))
_PAM_FEED_CHUNK_BYTES = 64 * 1024
_PAM_HEADER_KEYS = frozenset(
    {b"WIDTH", b"HEIGHT", b"DEPTH", b"MAXVAL", b"TUPLTYPE"}
)
_SEQUENCE_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
_SINGLE_IMAGE_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".gif": "GIF",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}
_RGB_ONLY_IMAGE_FORMATS = frozenset({"JPEG", "BMP"})
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
class PamFrame:
    width: int
    height: int
    pixels: bytearray


@dataclass(frozen=True)
class _PendingPublication:
    temporary: Path
    target: Path


class PamStreamParser:
    def __init__(
        self,
        *,
        max_frame_bytes: int = HOSTED_MAX_IMAGE_RGBA_BYTES,
    ) -> None:
        if max_frame_bytes < 0:
            raise ValueError("max_frame_bytes must not be negative")
        self._max_frame_bytes = max_frame_bytes
        self._header_buffer = bytearray()
        self._frame_width: int | None = None
        self._frame_height: int | None = None
        self._frame_buffer: bytearray | None = None
        self._frame_offset = 0

    def feed(self, data: bytes) -> list[PamFrame]:
        frames: list[PamFrame] = []
        view = memoryview(data)
        while view or (
            self._frame_buffer is None and self._header_buffer
        ):
            if self._frame_buffer is None:
                header = _parse_pam_header(
                    self._header_buffer,
                    max_frame_bytes=self._max_frame_bytes,
                )
                if header is not None:
                    header_bytes, width, height, frame_bytes = header
                    buffered = self._header_buffer[header_bytes:]
                    self._header_buffer.clear()
                    self._frame_width = width
                    self._frame_height = height
                    self._frame_buffer = bytearray(frame_bytes)
                    buffered_pixels = min(len(buffered), frame_bytes)
                    if buffered_pixels:
                        self._frame_buffer[:buffered_pixels] = buffered[
                            :buffered_pixels
                        ]
                    self._header_buffer.extend(buffered[buffered_pixels:])
                    self._frame_offset = buffered_pixels
                    if self._frame_offset == frame_bytes:
                        frames.append(self._detach_frame())
                    continue
                if not view:
                    break
                header_capacity = (
                    _MAX_PAM_HEADER_BYTES + 1 - len(self._header_buffer)
                )
                chunk = view[: min(_PAM_FEED_CHUNK_BYTES, header_capacity)]
                view = view[len(chunk) :]
                self._header_buffer.extend(chunk)
                if len(self._header_buffer) > _MAX_PAM_HEADER_BYTES:
                    raise FfmpegOutputError(
                        "FFmpeg PAM header exceeds its size limit"
                    )
                continue

            if not view:
                break
            remaining = len(self._frame_buffer) - self._frame_offset
            chunk = view[: min(_PAM_FEED_CHUNK_BYTES, remaining)]
            view = view[len(chunk) :]
            next_offset = self._frame_offset + len(chunk)
            self._frame_buffer[self._frame_offset : next_offset] = chunk
            self._frame_offset = next_offset
            if self._frame_offset == len(self._frame_buffer):
                frames.append(self._detach_frame())
        return frames

    def _detach_frame(self) -> PamFrame:
        pixels = self._frame_buffer
        if pixels is None:
            raise RuntimeError("PAM parser frame buffer is unavailable")
        frame = PamFrame(
            width=self._required_dimension(self._frame_width),
            height=self._required_dimension(self._frame_height),
            pixels=pixels,
        )
        self._frame_width = None
        self._frame_height = None
        self._frame_buffer = None
        self._frame_offset = 0
        return frame

    def finish(self) -> None:
        if self._header_buffer or self._frame_buffer is not None:
            raise FfmpegOutputError("FFmpeg produced a truncated PAM stream")

    @staticmethod
    def _required_dimension(value: int | None) -> int:
        if value is None:
            raise RuntimeError("PAM parser frame dimensions are unavailable")
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
        *,
        output_kind: Literal["single", "sequence"] = "single",
        max_frame_bytes: int | None = None,
    ) -> None:
        if not command:
            raise FfmpegOutputError("FFmpeg command has no output target")
        if output_kind not in {"single", "sequence"}:
            raise ValueError(f"Unsupported FFmpeg output kind: {output_kind}")
        if max_frame_bytes is not None and max_frame_bytes < 0:
            raise ValueError("max_frame_bytes must not be negative")
        self._guard = guard
        self._original_target = str(command[-1])
        self._sequence: tuple[str, int, str] | None = None
        if output_kind == "sequence":
            try:
                self._sequence = _parse_sequential_pattern(
                    self._original_target
                )
            except ValueError as exc:
                raise FfmpegOutputError(
                    f"FFmpeg guarded output target is unsupported: "
                    f"{self._original_target}"
                ) from exc
            if self._sequence is None:
                raise FfmpegOutputError(
                    f"FFmpeg guarded output target is unsupported: "
                    f"{self._original_target}"
                )
        if self._sequence is None:
            self._target = Path(self._original_target)
        else:
            prefix, width, suffix = self._sequence
            self._target = Path(f"{prefix}%0{width}d{suffix}")
        guard.validate_path(self._target)
        self._parser: PamStreamParser | None = None
        self._image_format: str | None = None
        self._single_image = False
        self._single_frame: PamFrame | None = None
        self._frame_index = _sequence_start_number(command)
        self._frame_count = 0
        self._pending_publications: list[_PendingPublication] = []
        self._is_video = False
        self._video_file: BinaryIO | None = None
        self._video_temporary: Path | None = None

        suffix = self._target.suffix.lower()
        raw_media_limit = (
            HOSTED_MAX_VIDEO_RGBA_FRAME_BYTES
            if output_kind == "sequence"
            else HOSTED_MAX_IMAGE_RGBA_BYTES
        )
        parser_frame_limit = min(
            raw_media_limit,
            (
                max_frame_bytes
                if max_frame_bytes is not None
                else raw_media_limit
            ),
        )
        if output_kind == "sequence":
            if self._sequence is None:
                raise RuntimeError("FFmpeg sequence contract has no pattern")
            _prefix, width, _pattern_suffix = self._sequence
            if width <= 0 or suffix not in _SEQUENCE_IMAGE_SUFFIXES:
                raise FfmpegOutputError(
                    f"FFmpeg guarded output target is unsupported: "
                    f"{self._original_target}"
                )
            self._parser = PamStreamParser(
                max_frame_bytes=parser_frame_limit
            )
            self._image_format = _SINGLE_IMAGE_FORMATS[suffix]
            output_args = [
                "-f",
                "image2pipe",
                "-vcodec",
                "pam",
                "-pix_fmt",
                "rgba",
                "pipe:1",
            ]
        elif suffix in _SINGLE_IMAGE_FORMATS:
            if self._target.exists():
                raise FfmpegOutputError(
                    f"FFmpeg output target already exists: {self._target}"
                )
            self._parser = PamStreamParser(
                max_frame_bytes=parser_frame_limit
            )
            self._image_format = _SINGLE_IMAGE_FORMATS[suffix]
            self._single_image = True
            output_args = [
                "-fps_mode",
                "passthrough",
                "-f",
                "image2pipe",
                "-vcodec",
                "pam",
                "-pix_fmt",
                "rgba",
                "pipe:1",
            ]
        elif suffix in _VIDEO_MUXERS:
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
            if self._single_image:
                if self._single_frame is not None:
                    raise FfmpegOutputError(
                        "FFmpeg produced multiple frames for a single image target"
                    )
                self._single_frame = frame
                self._frame_count += 1
                continue
            if self._frame_count >= HOSTED_MAX_FRAME_OUTPUTS:
                raise FfmpegOutputError(
                    "FFmpeg produced more than "
                    f"{HOSTED_MAX_FRAME_OUTPUTS} output frames"
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
            self._publish_pending()
            return
        if self._parser is None:
            raise RuntimeError("Guarded image parser is unavailable")
        self._parser.finish()
        if self._single_image and self._frame_count != 1:
            raise FfmpegOutputError(
                "FFmpeg produced no frame for a single image target"
            )
        if self._single_image:
            frame = self._single_frame
            if frame is None:
                raise RuntimeError("Guarded single image frame is unavailable")
            self._write_image(frame, self._target)
            self._single_frame = None
        self._publish_pending()

    def rollback(self) -> None:
        errors: list[BaseException] = []
        if self._video_file is not None:
            try:
                self._video_file.close()
            except BaseException as exc:
                errors.append(exc)
            self._video_file = None
        for publication in reversed(self._pending_publications):
            try:
                self._guard.discard_temporary(publication.temporary)
            except BaseException as exc:
                errors.append(exc)
        self._pending_publications.clear()
        self._video_temporary = None
        if errors:
            raise errors[0]

    def _open_video_temporary(self) -> None:
        self._target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._target.with_name(
            f".{self._target.name}.{uuid.uuid4().hex}.tmp"
        )
        file = temporary.open("x+b")
        try:
            self._guard.register_created(file, temporary)
        except BaseException:
            file.close()
            temporary.unlink(missing_ok=True)
            raise
        self._pending_publications.append(
            _PendingPublication(temporary, self._target)
        )
        self._video_temporary = temporary
        self._video_file = file

    def _frame_target(self) -> Path:
        if self._sequence is None:
            return self._target
        prefix, width, suffix = self._sequence
        frame_number = self._frame_index + self._frame_count
        return Path(f"{prefix}{frame_number:0{width}d}{suffix}")

    def _write_image(self, frame: PamFrame, target: Path) -> None:
        if target.exists():
            raise FfmpegOutputError(
                f"FFmpeg output target already exists: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            f".{target.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x+b") as raw_file:
                self._guard.register_created(raw_file, temporary)
                file = _BudgetedFile(raw_file, temporary, self._guard)
                with Image.frombuffer(
                    "RGBA",
                    (frame.width, frame.height),
                    frame.pixels,
                    "raw",
                    "RGBA",
                    0,
                    1,
                ) as image:
                    if self._image_format in _RGB_ONLY_IMAGE_FORMATS:
                        with image.convert("RGB") as rgb_image:
                            rgb_image.save(file, format=self._image_format)
                    elif self._image_format == "GIF":
                        _save_rgba_gif(image, file)
                    else:
                        image.save(file, format=self._image_format)
                file.flush()
                os.fsync(raw_file.fileno())
            self._pending_publications.append(
                _PendingPublication(temporary, target)
            )
        except BaseException:
            self._guard.discard_temporary(temporary)
            raise

    def _publish_pending(self) -> None:
        for publication in self._pending_publications:
            self._guard.publish(
                publication.temporary,
                publication.target,
            )


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


def _parse_pam_header(
    buffer: bytearray,
    *,
    max_frame_bytes: int,
) -> tuple[int, int, int, int] | None:
    if not buffer:
        return None
    first_newline = buffer.find(b"\n")
    if first_newline < 0:
        if not b"P7".startswith(buffer):
            raise FfmpegOutputError("FFmpeg output must use PAM P7")
        return None
    if _pam_header_line(buffer, 0, first_newline) != b"P7":
        raise FfmpegOutputError("FFmpeg output must use PAM P7")

    fields: dict[bytes, bytes] = {}
    index = first_newline + 1
    while True:
        newline = buffer.find(b"\n", index)
        if newline < 0:
            return None
        if newline + 1 > _MAX_PAM_HEADER_BYTES:
            raise FfmpegOutputError(
                "FFmpeg PAM header exceeds its size limit"
            )
        line = _pam_header_line(buffer, index, newline)
        index = newline + 1
        if line == b"ENDHDR":
            break
        parts = line.split()
        if len(parts) != 2:
            raise FfmpegOutputError("FFmpeg produced a malformed PAM header")
        key, value = parts
        if key not in _PAM_HEADER_KEYS:
            raise FfmpegOutputError(f"FFmpeg PAM header key is unsupported: {key!r}")
        if key in fields:
            raise FfmpegOutputError(
                f"FFmpeg PAM header contains duplicate {key.decode('ascii')}"
            )
        fields[key] = value

    missing = _PAM_HEADER_KEYS.difference(fields)
    if missing:
        missing_names = ", ".join(
            key.decode("ascii") for key in sorted(missing)
        )
        raise FfmpegOutputError(
            f"FFmpeg PAM header is missing {missing_names}"
        )

    width = _parse_pam_integer(fields[b"WIDTH"], "WIDTH")
    height = _parse_pam_integer(fields[b"HEIGHT"], "HEIGHT")
    depth = _parse_pam_integer(fields[b"DEPTH"], "DEPTH")
    maxval = _parse_pam_integer(fields[b"MAXVAL"], "MAXVAL")
    if width <= 0 or height <= 0:
        raise FfmpegOutputError("FFmpeg PAM dimensions must be positive")
    if depth != 4:
        raise FfmpegOutputError("FFmpeg PAM DEPTH must be 4")
    if maxval != 255:
        raise FfmpegOutputError("FFmpeg PAM MAXVAL must be 255")
    if fields[b"TUPLTYPE"] != b"RGB_ALPHA":
        raise FfmpegOutputError("FFmpeg PAM TUPLTYPE must be RGB_ALPHA")
    pixels = _checked_pam_product(
        width,
        height,
        HOSTED_MAX_IMAGE_PIXELS,
        "pixel count",
    )
    frame_bytes = _checked_pam_product(
        pixels,
        depth,
        max_frame_bytes,
        "frame size",
    )
    return index, width, height, frame_bytes


def _pam_header_line(buffer: bytearray, start: int, end: int) -> bytes:
    line = bytes(buffer[start:end])
    return line[:-1] if line.endswith(b"\r") else line


def _parse_pam_integer(token: bytes, name: str) -> int:
    if len(token) > _MAX_PAM_INTEGER_BYTES:
        raise FfmpegOutputError(
            f"FFmpeg PAM {name} integer token exceeds its size limit"
        )
    if not token or any(
        byte < ord("0") or byte > ord("9") for byte in token
    ):
        raise FfmpegOutputError(f"FFmpeg PAM {name} is not a decimal integer")
    return int(token)


def _checked_pam_product(
    left: int,
    right: int,
    limit: int,
    name: str,
) -> int:
    if left > limit // right:
        raise FfmpegOutputError(f"FFmpeg PAM {name} exceeds its hard limit")
    return left * right


def _save_rgba_gif(image: Image.Image, file: _BudgetedFile) -> None:
    with image.convert(
        "P",
        palette=Image.Palette.ADAPTIVE,
        colors=255,
    ) as paletted:
        with image.getchannel("A").point(
            lambda alpha: 255 if alpha < 128 else 0
        ) as transparent:
            paletted.paste(255, mask=transparent)
        paletted.save(file, format="GIF", transparency=255)
