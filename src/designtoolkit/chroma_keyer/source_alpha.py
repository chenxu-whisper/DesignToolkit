from __future__ import annotations

import re
import subprocess
from pathlib import Path
from threading import Thread

import numpy as np

from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

from .models import KeyerError
from .video_io import _validate_input_path
from .video_models import _normalize_timestamp

_PREVIEW_TIMEOUT_SECONDS = 15
_STDERR_LIMIT = 64 * 1024
_MAX_PREVIEW_DIMENSION = 8192
_MAX_PREVIEW_PIXELS = 4096 * 4096


def split_source_rgba(
    frame: np.ndarray, mode: str = "straight"
) -> tuple[np.ndarray, np.ndarray]:
    if mode not in ("straight", "premultiplied"):
        raise KeyerError("source alpha mode must be straight or premultiplied")
    if (
        not isinstance(frame, np.ndarray)
        or frame.dtype != np.uint8
        or frame.ndim != 3
        or frame.shape[2] != 4
        or min(frame.shape[:2]) <= 0
    ):
        raise KeyerError("Source frame must be a non-empty HxWx4 uint8 RGBA array")
    rgb, alpha = frame[..., :3], frame[..., 3]
    if mode == "premultiplied":
        restored = np.zeros(rgb.shape, dtype=np.float32)
        np.divide(
            rgb.astype(np.float32) * 255,
            alpha[..., None],
            out=restored,
            where=alpha[..., None] != 0,
        )
        rgb = np.rint(restored).clip(0, 255).astype(np.uint8)
    return rgb, alpha


def _run_preview_process(command: list[str], output_limit: int) -> tuple[bytes, str]:
    stdout, stderr = bytearray(), bytearray()
    errors: list[Exception] = []
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    ) as process:

        def drain(pipe, buffer, *, tail=False):
            try:
                while chunk := pipe.read(4096):
                    buffer.extend(chunk)
                    if tail:
                        del buffer[:-_STDERR_LIMIT]
                    elif len(buffer) > output_limit:
                        del buffer[output_limit:]
                        raise KeyerError("Source preview exceeded its output limit")
            except Exception as error:
                errors.append(error)
                if process.poll() is None:
                    process.kill()

        readers = [
            Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            Thread(
                target=drain,
                args=(process.stderr, stderr),
                kwargs={"tail": True},
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            try:
                process.wait(timeout=_PREVIEW_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise KeyerError("Source alpha preview timed out") from error
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
        if errors:
            raise errors[0]
        diagnostic = stderr.decode("utf-8", errors="replace")
        if process.returncode:
            if "Requested planes not available" in diagnostic:
                raise KeyerError("Source video has no native alpha channel")
            raise KeyerError("FFmpeg source preview failed: " + diagnostic)
        return bytes(stdout), diagnostic


def read_source_preview(
    path: Path, timestamp_seconds: float, mode: str = "straight"
) -> tuple[np.ndarray, np.ndarray]:
    timestamp = _normalize_timestamp(timestamp_seconds)
    if timestamp < 0:
        raise KeyerError("timestamp must be nonnegative")
    if mode not in ("straight", "premultiplied"):
        raise KeyerError("source alpha mode must be straight or premultiplied")
    _validate_input_path(path)
    try:
        executable = FfmpegBackend().get_ffmpeg_exe()
    except Exception as error:
        raise KeyerError("Bundled FFmpeg is unavailable") from error
    base = [
        executable,
        "-hide_banner",
        "-nostdin",
        "-ss",
        str(timestamp),
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
    ]
    # Inspect the native decoded format before any RGBA conversion can add alpha.
    _, diagnostic = _run_preview_process(
        [*base, "-vf", "showinfo,alphaextract", "-frames:v", "1", "-f", "null", "-"],
        0,
    )
    dimensions = re.search(r"showinfo[^\n]*\bn:\s*0\b[^\n]*\bs:(\d+)x(\d+)", diagnostic)
    if dimensions is None:
        raise KeyerError("Source alpha preview contains no decoded frame")
    width, height = map(int, dimensions.groups())
    if (
        min(width, height) <= 0
        or max(width, height) > _MAX_PREVIEW_DIMENSION
        or width * height > _MAX_PREVIEW_PIXELS
    ):
        raise KeyerError("Source preview dimensions exceed the decode limit")
    expected = width * height * 4
    data, _ = _run_preview_process(
        [
            *base,
            "-loglevel",
            "error",
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgba",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        expected,
    )
    if len(data) != expected:
        raise KeyerError("Decoded source preview has an invalid byte length")
    return split_source_rgba(
        np.frombuffer(data, np.uint8).reshape(height, width, 4), mode
    )
