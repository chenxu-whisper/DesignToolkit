from __future__ import annotations

import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from rq.timeouts import JobTimeoutException, TimerDeathPenalty

from designtoolkit.media_resize.core import MediaResizeOptions, resize_media
from designtoolkit.video_frames import ffmpeg_backend
from designtoolkit.video_frames.core import VideoFrameOptions, export_video_frames
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend
from designtoolkit.web import output_limits
from designtoolkit.web.output_limits import OutputBudgetGuard


def _ppm(
    width: int,
    height: int,
    pixels: bytes,
    *,
    comment: bytes = b"",
) -> bytes:
    comment_line = b"# " + comment + b"\n" if comment else b""
    return (
        b"P6\n"
        + comment_line
        + f"{width} {height}\n255\n".encode("ascii")
        + pixels
    )


def test_ppm_parser_accepts_comments_and_every_chunk_boundary() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import PpmStreamParser

    payload = _ppm(2, 1, b"\x00\x01\x02\x03\x04\x05", comment=b"frame")
    parser = PpmStreamParser()
    frames = []

    for byte in payload:
        frames.extend(parser.feed(bytes([byte])))
    parser.finish()

    assert [(frame.width, frame.height, frame.pixels) for frame in frames] == [
        (2, 1, b"\x00\x01\x02\x03\x04\x05")
    ]


def test_ppm_parser_emits_multiple_frames_from_arbitrary_chunks() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import PpmStreamParser

    payload = _ppm(1, 1, b"abc") + _ppm(1, 2, b"defghi")
    parser = PpmStreamParser()

    frames = parser.feed(payload[:7])
    frames.extend(parser.feed(payload[7:19]))
    frames.extend(parser.feed(payload[19:]))
    parser.finish()

    assert [frame.pixels for frame in frames] == [b"abc", b"defghi"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"P3\n1 1\n255\nabc", "P6"),
        (b" P6\n1 1\n255\nabc", "P6"),
        (b"P6\n0 1\n255\n", "dimensions"),
        (b"P6\n1 nope\n255\n", "height"),
        (b"P6\n99999999999 1\n255\n", "width"),
        (b"P6\n1 1\n65535\nabc", "maxval"),
        (b"P6\n100000 100000\n255\n", "frame size"),
    ],
)
def test_ppm_parser_rejects_malformed_or_oversized_headers(
    payload: bytes,
    message: str,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PpmStreamParser,
    )

    parser = PpmStreamParser()

    with pytest.raises(FfmpegOutputError, match=message):
        parser.feed(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b"P",
        b"P6\n1",
        b"P6\n1 1\n255\nab",
        _ppm(1, 1, b"abc") + b"P6\n",
    ],
)
def test_ppm_parser_rejects_truncated_stream(payload: bytes) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PpmStreamParser,
    )

    parser = PpmStreamParser()
    parser.feed(payload)

    with pytest.raises(FfmpegOutputError, match="truncated"):
        parser.finish()


def test_ppm_parser_rejects_oversized_header_without_unbounded_buffering() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PpmStreamParser,
    )

    parser = PpmStreamParser()

    with pytest.raises(FfmpegOutputError, match="header"):
        parser.feed(b"P6\n#" + b"x" * 70_000)


@pytest.mark.parametrize(
    ("suffix", "pillow_format"),
    [
        (".png", "PNG"),
        (".jpg", "JPEG"),
        (".jpeg", "JPEG"),
        (".webp", "WEBP"),
        (".bmp", "BMP"),
        (".tif", "TIFF"),
        (".tiff", "TIFF"),
    ],
)
def test_guarded_single_image_rewrites_to_ppm_and_pillow_decodes(
    tmp_path: Path,
    suffix: str,
    pillow_format: str,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / f"output{suffix}"
    original = ["ffmpeg", "-hide_banner", "-i", "input", str(target)]
    session = GuardedFfmpegOutput(original, OutputBudgetGuard(tmp_path, 1_000_000))

    assert session.command == [
        *original[:-1],
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "ppm",
        "pipe:1",
    ]

    session.consume(_ppm(2, 1, b"\xff\x00\x00\x00\xff\x00"))
    session.finish()

    with Image.open(target) as image:
        image.load()
        assert image.format == pillow_format
        assert image.size == (2, 1)


def test_guarded_sequence_publishes_numbered_frames_with_escaped_percent(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    pattern = tmp_path / "clip%%take_%03d.png"
    session = GuardedFfmpegOutput(
        ["ffmpeg", "-i", "input", str(pattern)],
        OutputBudgetGuard(tmp_path, 1_000_000),
    )

    assert "-frames:v" not in session.command
    assert session.command[-5:] == [
        "-f",
        "image2pipe",
        "-vcodec",
        "ppm",
        "pipe:1",
    ]
    session.consume(_ppm(1, 1, b"\x01\x02\x03") + _ppm(1, 1, b"\x04\x05\x06"))
    session.finish()

    assert (tmp_path / "clip%take_001.png").exists()
    assert (tmp_path / "clip%take_002.png").exists()
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "target",
    [
        "output.gif",
        "frames_%06d.webp",
        "frames_%00d.png",
        "frames_%d.png",
        "output",
    ],
)
def test_guarded_output_rejects_unknown_target_before_launch(
    tmp_path: Path,
    target: str,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        GuardedFfmpegOutput,
    )

    with pytest.raises(FfmpegOutputError, match="unsupported"):
        GuardedFfmpegOutput(
            ["ffmpeg", str(tmp_path / target)],
            OutputBudgetGuard(tmp_path, 1_000_000),
        )


def test_guarded_image_rollback_removes_only_this_run_outputs(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    preexisting = tmp_path / "keep.txt"
    preexisting.write_text("keep", encoding="ascii")
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    session = GuardedFfmpegOutput(
        ["ffmpeg", str(tmp_path / "frame_%02d.jpg")],
        guard,
    )
    session.consume(_ppm(1, 1, b"abc") + _ppm(1, 1, b"def"))
    assert guard.used_bytes > 0

    session.rollback()

    assert preexisting.read_text(encoding="ascii") == "keep"
    assert not list(tmp_path.glob("frame_*.jpg"))
    assert not list(tmp_path.glob(".*.tmp"))
    assert guard.used_bytes == 0


def test_guarded_output_refuses_to_replace_preexisting_target(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        GuardedFfmpegOutput,
    )

    target = tmp_path / "output.png"
    target.write_bytes(b"preexisting")

    with pytest.raises(FfmpegOutputError, match="already exists"):
        GuardedFfmpegOutput(
            ["ffmpeg", str(target)],
            OutputBudgetGuard(tmp_path, 1_000_000),
        )

    assert target.read_bytes() == b"preexisting"


def test_guarded_rename_race_does_not_delete_preexisting_target_on_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / "output.png"
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    session = GuardedFfmpegOutput(["ffmpeg", str(target)], guard)
    original_rename = guard.rename

    def race(source: str | Path, destination: str | Path) -> None:
        target.write_bytes(b"preexisting")
        original_rename(source, destination)

    monkeypatch.setattr(guard, "rename", race)

    with pytest.raises(FileExistsError):
        session.consume(_ppm(1, 1, b"abc"))
    session.rollback()

    assert target.read_bytes() == b"preexisting"


def test_guarded_rename_interruption_after_move_is_fully_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RenameInterrupted(BaseException):
        pass

    target = tmp_path / "output.png"
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    original_replace = output_limits.os.replace

    def move_then_interrupt(source: str | Path, destination: str | Path) -> None:
        original_replace(source, destination)
        raise RenameInterrupted

    monkeypatch.setattr(output_limits.os, "replace", move_then_interrupt)
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory(
            [_StreamingPopen([_ppm(1, 1, b"abc")])],
            [],
        ),
    )
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")

    with pytest.raises(RenameInterrupted):
        backend.run(["ffmpeg", str(target)])

    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


def test_guarded_output_rejects_target_outside_budget_root_before_temp_creation(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.mp4"

    with pytest.raises(ValueError, match="outside output root"):
        GuardedFfmpegOutput(
            ["ffmpeg", str(outside)],
            OutputBudgetGuard(root, 1_000_000),
        )

    assert not outside.exists()
    assert list(tmp_path.iterdir()) == [root]


@pytest.mark.parametrize(
    ("suffix", "output_args"),
    [
        (
            ".mp4",
            ["-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"],
        ),
        (
            ".mov",
            ["-movflags", "+frag_keyframe+empty_moov", "-f", "mov", "pipe:1"],
        ),
        (
            ".m4v",
            ["-movflags", "+frag_keyframe+empty_moov", "-f", "ipod", "pipe:1"],
        ),
        (".avi", ["-f", "avi", "pipe:1"]),
        (".mkv", ["-f", "matroska", "pipe:1"]),
        (".webm", ["-f", "webm", "pipe:1"]),
    ],
)
def test_guarded_video_uses_explicit_pipe_muxer_and_atomic_publish(
    tmp_path: Path,
    suffix: str,
    output_args: list[str],
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / f"output{suffix}"
    original = ["ffmpeg", "-i", "input", str(target)]
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    session = GuardedFfmpegOutput(original, guard)

    assert session.command == [*original[:-1], *output_args]
    assert not list(tmp_path.iterdir())
    session.consume(b"container")
    assert not target.exists()
    assert guard.used_bytes == 9

    session.finish()

    assert target.read_bytes() == b"container"
    assert not list(tmp_path.glob(".*.tmp"))
    assert guard.used_bytes == 9


def test_guarded_video_rejects_chunk_before_any_over_budget_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput
    from designtoolkit.web.output_limits import OutputLimitExceeded

    guard = OutputBudgetGuard(tmp_path, max_bytes=10, safety_bytes=1)
    observed_sizes: list[int] = []
    original_write = guard.write

    def write(file: object, path: str | Path, data: bytes) -> int:
        try:
            return original_write(file, path, data)  # type: ignore[arg-type]
        finally:
            observed_sizes.append(
                sum(
                    candidate.stat().st_size
                    for candidate in tmp_path.iterdir()
                    if candidate.is_file()
                )
            )

    monkeypatch.setattr(guard, "write", write)
    target = tmp_path / "output.mp4"
    session = GuardedFfmpegOutput(["ffmpeg", str(target)], guard)
    session.consume(b"12345678")

    with pytest.raises(OutputLimitExceeded, match="10-byte limit"):
        session.consume(b"oversized chunk")

    assert max(observed_sizes) <= 9
    assert not target.exists()
    session.rollback()
    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


class _BytesPipe:
    def __init__(self, chunks: list[bytes | BaseException]) -> None:
        self._chunks = chunks
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self) -> None:
        self.closed = True


class _StreamingPopen:
    def __init__(
        self,
        stdout: list[bytes | BaseException],
        *,
        stderr: list[bytes | BaseException] | None = None,
        returncode: int = 0,
    ) -> None:
        self.stdout = _BytesPipe(stdout)
        self.stderr = _BytesPipe(stderr or [])
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self.returncode = self._final_returncode
        return self._final_returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = 1

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = 1


def _streaming_popen_factory(
    processes: list[_StreamingPopen],
    calls: list[tuple[list[str], dict[str, Any]]],
):
    def popen(command: list[str], **kwargs: Any) -> _StreamingPopen:
        calls.append((command, kwargs))
        return processes.pop(0)

    return popen


def test_guarded_backend_streams_binary_stdout_to_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StreamingPopen(
        [b"first", b"-second"],
        stderr=[b"diagnostic"],
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []
    target = tmp_path / "output.mp4"
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 1_000_000),
        popen=_streaming_popen_factory([process], calls),
    )

    result = backend.run(["ffmpeg", "-i", "input", str(target)])

    assert result.returncode == 0
    assert result.stderr == "diagnostic"
    assert target.read_bytes() == b"first-second"
    assert calls[0][0][-5:] == [
        "-movflags",
        "+frag_keyframe+empty_moov",
        "-f",
        "mp4",
        "pipe:1",
    ]
    assert calls[0][1]["stdout"] is not None
    assert calls[0][1]["stderr"] is not None
    assert "text" not in calls[0][1]
    assert "encoding" not in calls[0][1]


def test_guarded_backend_rejects_unknown_target_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 100),
        popen=_streaming_popen_factory([], calls),
    )

    with pytest.raises(RuntimeError, match="unsupported"):
        backend.run(["ffmpeg", str(tmp_path / "output.gif")])

    assert calls == []


def test_guarded_backend_bounds_stderr_while_continuing_to_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = b"a" * (2 * 1024 * 1024) + b"tail"
    process = _StreamingPopen([b"video"], stderr=[stderr])
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 1_000_000),
        popen=_streaming_popen_factory([process], []),
    )

    result = backend.run(["ffmpeg", str(tmp_path / "output.webm")])

    assert len(result.stderr.encode("utf-8")) <= 1024 * 1024
    assert result.stderr.endswith("tail")
    assert process.stderr.closed


def test_guarded_backend_rolls_back_nonzero_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StreamingPopen(
        [_ppm(1, 1, b"abc")],
        stderr=[b"decode failed"],
        returncode=1,
    )
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    result = backend.run(["ffmpeg", str(tmp_path / "frame_%06d.png")])

    assert result.returncode == 1
    assert result.stderr == "decode failed"
    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


def test_guarded_backend_stops_and_rolls_back_on_budget_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.web.output_limits import OutputLimitExceeded

    process = _StreamingPopen([b"x" * 100])
    guard = OutputBudgetGuard(tmp_path, max_bytes=50, safety_bytes=1)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    with pytest.raises(OutputLimitExceeded, match="50-byte limit"):
        backend.run(["ffmpeg", str(tmp_path / "output.avi")])

    assert process.terminate_called
    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


def test_guarded_backend_stops_and_rolls_back_on_reader_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")
    process = _StreamingPopen([b"partial", timeout])
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", str(tmp_path / "output.mkv")])

    assert exc_info.value is timeout
    assert process.terminate_called
    assert process.stdout.closed
    assert process.stderr.closed
    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


def test_guarded_backend_sequence_stops_at_exact_aggregate_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.web.output_limits import OutputLimitExceeded

    encoded = BytesIO()
    Image.frombytes("RGB", (1, 1), b"abc").save(encoded, format="PNG")
    frame_bytes = len(encoded.getvalue())
    frame_count = 200
    accepted_frames = 100
    guard = OutputBudgetGuard(
        tmp_path,
        max_bytes=frame_bytes * accepted_frames + 1,
        safety_bytes=1,
    )
    observed_sizes: list[int] = []
    original_write = guard.write

    def write(file: object, path: str | Path, data: bytes) -> int:
        try:
            return original_write(file, path, data)  # type: ignore[arg-type]
        finally:
            observed_sizes.append(
                sum(
                    candidate.stat().st_size
                    for candidate in tmp_path.iterdir()
                    if candidate.is_file()
                )
            )

    monkeypatch.setattr(guard, "write", write)
    process = _StreamingPopen([_ppm(1, 1, b"abc") * frame_count])
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    with pytest.raises(OutputLimitExceeded):
        backend.run(["ffmpeg", str(tmp_path / "frame_%06d.png")])

    assert max(observed_sizes) == frame_bytes * accepted_frames
    assert process.terminate_called
    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


def test_webm_decoder_fallback_recovers_full_budget_after_failed_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "input.webm"
    input_path.write_bytes(b"input")
    output_dir = tmp_path / "frames"
    first = _StreamingPopen(
        [_ppm(1, 1, b"bad")],
        stderr=[
            b"libvpx-vp9: Bitstream not supported by this decoder"
        ],
        returncode=1,
    )
    second = _StreamingPopen([_ppm(1, 1, b"\x01\x02\x03")])
    calls: list[tuple[list[str], dict[str, Any]]] = []
    guard = OutputBudgetGuard(output_dir, 1_000_000)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([first, second], calls),
    )

    result = export_video_frames(
        VideoFrameOptions(
            input_path=input_path,
            output_dir=output_dir,
            image_format="png",
        ),
        backend,
    )

    frames = list(output_dir.glob("input_*.png"))
    assert len(calls) == 2
    assert len(frames) == 1
    with Image.open(frames[0]) as image:
        image.load()
        assert image.getpixel((0, 0)) == (1, 2, 3)
    assert guard.used_bytes == frames[0].stat().st_size
    assert result.output_pattern == output_dir / "input_%06d.png"


def test_guarded_backend_never_uses_prlimit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StreamingPopen([b"video"])
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(
        ffmpeg_backend,
        "which",
        lambda _executable: pytest.fail("guarded backend must not use prlimit"),
    )
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 100),
        popen=_streaming_popen_factory([process], calls),
        max_file_bytes=99,
    )

    result = backend.run(["ffmpeg", str(tmp_path / "output.avi")])

    assert result.returncode == 0
    assert calls[0][0][0] == "ffmpeg"
    assert "prlimit" not in calls[0][0]


@pytest.mark.parametrize(
    "suffix",
    [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"],
)
def test_bundled_ffmpeg_guarded_single_image_formats(
    tmp_path: Path,
    suffix: str,
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (16, 8), "red").save(source)
    target = tmp_path / f"image{suffix}"
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 10_000_000)
    )

    result = resize_media(
        MediaResizeOptions(
            input_path=source,
            output_path=target,
            size=(8, 8),
        ),
        backend,
    )

    assert result.output_path == target
    with Image.open(target) as image:
        image.load()
        assert image.size == (8, 4)


@pytest.mark.parametrize("suffix", [".png", ".jpg"])
def test_bundled_ffmpeg_guarded_image_sequences(
    tmp_path: Path,
    suffix: str,
) -> None:
    pattern = tmp_path / f"frame_%03d{suffix}"
    plain_backend = FfmpegBackend()
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 10_000_000)
    )

    result = backend.run(
        [
            plain_backend.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=8x8:rate=3:duration=1",
            "-frames:v",
            "3",
            str(pattern),
        ]
    )

    assert result.returncode == 0, result.stderr
    frames = sorted(tmp_path.glob(f"frame_*{suffix}"))
    assert len(frames) == 3
    for frame in frames:
        with Image.open(frame) as image:
            image.load()
            assert image.size == (8, 8)


@pytest.mark.parametrize(
    ("suffix", "codec"),
    [
        (".mp4", "libx264"),
        (".mov", "libx264"),
        (".m4v", "libx264"),
        (".avi", "mpeg4"),
        (".mkv", "libx264"),
        (".webm", "libvpx-vp9"),
    ],
)
def test_bundled_ffmpeg_guarded_video_formats(
    tmp_path: Path,
    suffix: str,
    codec: str,
) -> None:
    target = tmp_path / f"video{suffix}"
    plain_backend = FfmpegBackend()
    ffmpeg_exe = plain_backend.get_ffmpeg_exe()
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 10_000_000)
    )

    result = backend.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=16x16:rate=5:duration=0.4",
            "-an",
            "-c:v",
            codec,
            str(target),
        ]
    )
    decode = subprocess.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(target),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert decode.returncode == 0, decode.stderr.decode(errors="replace")


@pytest.mark.timeout(15)
def test_guarded_backend_drains_real_stderr_flood_without_deadlock(
    tmp_path: Path,
) -> None:
    marker = "retained-tail"
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'video'); "
        "sys.stdout.buffer.flush(); "
        f"sys.stderr.buffer.write(b'x' * (2 * 1024 * 1024) + {marker.encode()!r}); "
        "sys.stderr.buffer.flush()"
    )
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 1_000_000)
    )

    result = backend.run(
        [sys.executable, "-c", script, str(tmp_path / "output.avi")]
    )

    assert result.returncode == 0
    assert len(result.stderr.encode("utf-8")) <= 1024 * 1024
    assert result.stderr.endswith(marker)


@pytest.mark.timeout(15)
def test_guarded_backend_real_timeout_stops_process_and_removes_temp(
    tmp_path: Path,
) -> None:
    class ProbeTimeout(JobTimeoutException):
        pass

    processes: list[subprocess.Popen[bytes]] = []

    def popen(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(command, **kwargs)
        processes.append(process)
        return process

    script = (
        "import sys, time; "
        "sys.stdout.buffer.write(b'partial'); "
        "sys.stdout.buffer.flush(); "
        "time.sleep(60)"
    )
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 1_000_000),
        popen=popen,
        poll_interval=0.01,
    )

    started_at = time.monotonic()
    with pytest.raises(ProbeTimeout):
        with TimerDeathPenalty(0.2, exception=ProbeTimeout):
            backend.run(
                [sys.executable, "-c", script, str(tmp_path / "output.mkv")]
            )
    elapsed = time.monotonic() - started_at

    assert elapsed < 5
    assert processes
    assert processes[0].poll() is not None
    assert not list(tmp_path.iterdir())
