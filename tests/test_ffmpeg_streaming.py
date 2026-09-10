from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from threading import Event
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


def _pam(
    width: int,
    height: int,
    pixels: bytes,
    *,
    depth: int = 4,
    maxval: int = 255,
    tuple_type: str = "RGB_ALPHA",
    extra_header_lines: tuple[bytes, ...] = (),
) -> bytes:
    header_lines = (
        b"P7",
        f"WIDTH {width}".encode("ascii"),
        f"HEIGHT {height}".encode("ascii"),
        f"DEPTH {depth}".encode("ascii"),
        f"MAXVAL {maxval}".encode("ascii"),
        f"TUPLTYPE {tuple_type}".encode("ascii"),
        *extra_header_lines,
        b"ENDHDR",
    )
    return b"\n".join(header_lines) + b"\n" + pixels


def test_pam_parser_accepts_rgba_at_every_chunk_boundary() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import PamStreamParser

    pixels = b"\x00\x01\x02\x03\x04\x05\x06\x07"
    payload = _pam(2, 1, pixels)
    parser = PamStreamParser()
    frames = []

    for byte in payload:
        frames.extend(parser.feed(bytes([byte])))
    parser.finish()

    assert [(frame.width, frame.height, frame.pixels) for frame in frames] == [
        (2, 1, pixels)
    ]


def test_pam_parser_emits_multiple_frames_from_arbitrary_chunks() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import PamStreamParser

    payload = _pam(1, 1, b"abcd") + _pam(1, 2, b"efghijkl")
    parser = PamStreamParser()

    frames = parser.feed(payload[:7])
    frames.extend(parser.feed(payload[7:19]))
    frames.extend(parser.feed(payload[19:]))
    parser.finish()

    assert [frame.pixels for frame in frames] == [b"abcd", b"efghijkl"]


def test_pam_parser_preallocates_and_detaches_one_real_4k_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.video_frames import ffmpeg_streaming

    width = 3840
    height = 2160
    frame_bytes = width * height * 4
    allocations: list[tuple[int, bytearray]] = []
    builtin_bytearray = bytearray

    def tracked_bytearray(size: int = 0) -> bytearray:
        allocation = builtin_bytearray(size)
        allocations.append((size, allocation))
        return allocation

    monkeypatch.setattr(
        ffmpeg_streaming,
        "bytearray",
        tracked_bytearray,
        raising=False,
    )
    parser = ffmpeg_streaming.PamStreamParser(max_frame_bytes=frame_bytes)
    payload = _pam(width, height, b"\x7f" * frame_bytes)

    frames = parser.feed(payload)
    parser.finish()

    frame_allocations = [
        allocation for size, allocation in allocations if size == frame_bytes
    ]
    assert len(frame_allocations) == 1
    assert len(frames) == 1
    assert frames[0].pixels is frame_allocations[0]
    assert isinstance(frames[0].pixels, builtin_bytearray)
    assert frames[0].pixels[:4] == b"\x7f" * 4


def test_guarded_image_uses_pillow_frombuffer_without_copying_rgba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    observed_buffers: list[object] = []
    original_frombuffer = Image.frombuffer

    def recording_frombuffer(
        mode: str,
        size: tuple[int, int],
        data: object,
        *args: object,
        **kwargs: object,
    ) -> Image.Image:
        observed_buffers.append(data)
        return original_frombuffer(mode, size, data, *args, **kwargs)

    monkeypatch.setattr(Image, "frombuffer", recording_frombuffer)
    target = tmp_path / "output.png"
    session = GuardedFfmpegOutput(
        ["ffmpeg", str(target)],
        OutputBudgetGuard(tmp_path, 1_000_000),
    )

    session.consume(_pam(1, 1, b"\x01\x02\x03\x04"))
    buffered_frame = session._single_frame
    assert buffered_frame is not None
    session.finish()

    assert observed_buffers == [buffered_frame.pixels]
    assert observed_buffers[0] is buffered_frame.pixels


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"P6\n1 1\n255\nabc", "P7"),
        (b" P7\nWIDTH 1\n", "P7"),
        (_pam(0, 1, b""), "dimensions"),
        (
            b"P7\nWIDTH 1\nHEIGHT nope\nDEPTH 4\nMAXVAL 255\n"
            b"TUPLTYPE RGB_ALPHA\nENDHDR\n",
            "HEIGHT",
        ),
        (_pam(99_999_999_999, 1, b""), "WIDTH"),
        (_pam(1, 1, b"abcd", maxval=65_535), "MAXVAL"),
        (_pam(1, 1, b"abc", depth=3, tuple_type="RGB"), "DEPTH"),
        (_pam(1, 1, b"abcd", tuple_type="RGB"), "TUPLTYPE"),
        (_pam(100_000, 100_000, b""), "pixel count"),
        (_pam(16_384, 16_384, b""), "pixel count"),
        (_pam(1, 100_000_001, b""), "pixel count"),
        (
            b"P7\nWIDTH 1\nHEIGHT 1\nDEPTH 4\nMAXVAL 255\nENDHDR\n",
            "TUPLTYPE",
        ),
        (_pam(1, 1, b"abcd", extra_header_lines=(b"WIDTH 1",)), "duplicate"),
        (_pam(1, 1, b"abcd", extra_header_lines=(b"COMMENT nope",)), "key"),
    ],
)
def test_pam_parser_rejects_malformed_or_oversized_headers(
    payload: bytes,
    message: str,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PamStreamParser,
    )

    parser = PamStreamParser()

    with pytest.raises(FfmpegOutputError, match=message):
        parser.feed(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b"P",
        b"P7\nWIDTH 1",
        _pam(1, 1, b"abc"),
        _pam(1, 1, b"abcd") + b"P7\n",
    ],
)
def test_pam_parser_rejects_truncated_stream(payload: bytes) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PamStreamParser,
    )

    parser = PamStreamParser()
    parser.feed(payload)

    with pytest.raises(FfmpegOutputError, match="truncated"):
        parser.finish()


def test_pam_parser_rejects_oversized_header_without_unbounded_buffering() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PamStreamParser,
    )

    parser = PamStreamParser()

    with pytest.raises(FfmpegOutputError, match="header"):
        parser.feed(b"P7\nWIDTH " + b"1" * 70_000)


def test_pam_parser_rejects_complete_oversized_header() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PamStreamParser,
    )

    parser = PamStreamParser()
    payload = (
        b"P7\nWIDTH 1\nHEIGHT 1\nDEPTH 4\nMAXVAL 255\nTUPLTYPE "
        + b"A" * 70_000
        + b"\nENDHDR\n"
    )

    with pytest.raises(FfmpegOutputError, match="header"):
        parser.feed(payload)


def test_pam_parser_rejects_oversized_integer_token_before_conversion() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PamStreamParser,
    )

    parser = PamStreamParser()
    payload = (
        b"P7\nWIDTH "
        + b"1" * 1_000
        + b"\nHEIGHT 1\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
    )

    with pytest.raises(FfmpegOutputError, match="WIDTH integer token"):
        parser.feed(payload)


@pytest.mark.parametrize(
    ("width", "height", "max_frame_bytes"),
    [
        (3840, 2160, 3840 * 2160 * 4),
        (10_000, 10_000, 100_000_000 * 4),
        (20_000, 2, 20_000 * 2 * 4),
        (1, 100_000_000, 100_000_000 * 4),
    ],
)
def test_pam_parser_accepts_hosted_raw_media_limits(
    width: int,
    height: int,
    max_frame_bytes: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.video_frames import ffmpeg_streaming

    class SparseFrameBuffer:
        def __init__(self, size: int) -> None:
            self._size = size

        def __len__(self) -> int:
            return self._size

    builtin_bytearray = bytearray
    requested_sizes: list[int] = []

    def sparse_bytearray(size: int = 0) -> bytearray | SparseFrameBuffer:
        requested_sizes.append(size)
        if size == 0:
            return builtin_bytearray()
        return SparseFrameBuffer(size)

    monkeypatch.setattr(
        ffmpeg_streaming,
        "bytearray",
        sparse_bytearray,
        raising=False,
    )
    parser = ffmpeg_streaming.PamStreamParser(max_frame_bytes=max_frame_bytes)

    parser.feed(_pam(width, height, b""))

    assert requested_sizes == [0, width * height * 4]
    with pytest.raises(ffmpeg_streaming.FfmpegOutputError, match="truncated"):
        parser.finish()


def test_pam_parser_rejects_frame_above_configured_limit_before_pixels() -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        PamStreamParser,
    )

    parser = PamStreamParser(max_frame_bytes=15)

    with pytest.raises(FfmpegOutputError, match="frame size"):
        parser.feed(_pam(2, 2, b""))


def test_guarded_pam_applies_encoded_budget_after_parsing_raw_frame(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput
    from designtoolkit.web.output_limits import OutputLimitExceeded

    session = GuardedFfmpegOutput(
        ["ffmpeg", str(tmp_path / "output.png")],
        OutputBudgetGuard(tmp_path, max_bytes=16, safety_bytes=1),
    )
    session.consume(_pam(2, 2, b"\x00" * 16))

    with pytest.raises(OutputLimitExceeded):
        session.finish()

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("suffix", "pillow_format"),
    [
        (".png", "PNG"),
        (".jpg", "JPEG"),
        (".jpeg", "JPEG"),
        (".webp", "WEBP"),
        (".bmp", "BMP"),
        (".gif", "GIF"),
        (".tif", "TIFF"),
        (".tiff", "TIFF"),
    ],
)
def test_guarded_single_image_rewrites_to_rgba_pam_and_pillow_decodes(
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

    session.consume(
        _pam(
            2,
            1,
            b"\xff\x00\x00\x11\x00\xff\x00\xcc",
        )
    )
    session.finish()

    with Image.open(target) as image:
        image.load()
        assert image.format == pillow_format
        assert image.size == (2, 1)
        if suffix in {".png", ".webp", ".tif", ".tiff"}:
            assert image.mode == "RGBA"
            assert image.getchannel("A").tobytes() == b"\x11\xcc"
        elif suffix == ".gif":
            assert image.convert("RGBA").getchannel("A").tobytes() == b"\x00\xff"
        else:
            assert image.mode == "RGB"


def test_guarded_single_image_rejects_multiple_frames_without_publishing(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import (
        FfmpegOutputError,
        GuardedFfmpegOutput,
    )

    target = tmp_path / "output.gif"
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    session = GuardedFfmpegOutput(["ffmpeg", str(target)], guard)

    assert "-frames:v" not in session.command
    with pytest.raises(FfmpegOutputError, match="multiple frames"):
        session.consume(
            _pam(1, 1, b"\x01\x02\x03\x04")
            + _pam(1, 1, b"\x05\x06\x07\x08")
        )

    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    assert guard.used_bytes == 0


def test_guarded_sequence_publishes_numbered_frames_with_escaped_percent(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    pattern = tmp_path / "clip%%take_%03d.png"
    session = GuardedFfmpegOutput(
        ["ffmpeg", "-i", "input", str(pattern)],
        OutputBudgetGuard(tmp_path, 1_000_000),
        output_kind="sequence",
    )

    assert "-frames:v" not in session.command
    assert session.command[-7:] == [
        "-f",
        "image2pipe",
        "-vcodec",
        "pam",
        "-pix_fmt",
        "rgba",
        "pipe:1",
    ]
    session.consume(
        _pam(1, 1, b"\x01\x02\x03\x04")
        + _pam(1, 1, b"\x05\x06\x07\x08")
    )
    assert not list(tmp_path.glob("clip%take_*.png"))
    assert len(list(tmp_path.glob(".*.tmp"))) == 2

    session.finish()

    first = tmp_path / "clip%take_001.png"
    assert first.exists()
    assert (tmp_path / "clip%take_002.png").exists()
    with Image.open(first) as image:
        image.load()
        assert image.getpixel((0, 0)) == (1, 2, 3, 4)
    assert not list(tmp_path.glob(".*.tmp"))


def test_guarded_sequence_unescapes_percent_in_directory_and_stem(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    output_dir = tmp_path / "real%temp"
    output_dir.mkdir()
    escaped_dir = str(output_dir).replace("%", "%%")
    pattern = Path(escaped_dir) / "clip%%take_%03d.png"
    session = GuardedFfmpegOutput(
        ["ffmpeg", str(pattern)],
        OutputBudgetGuard(output_dir, 1_000_000),
        output_kind="sequence",
    )

    session.consume(_pam(1, 1, b"\x01\x02\x03\x04"))
    session.finish()

    assert (output_dir / "clip%take_001.png").exists()
    assert not Path(escaped_dir).exists()


def test_guarded_single_image_allows_literal_percent(tmp_path: Path) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / "100%done.png"
    session = GuardedFfmpegOutput(
        ["ffmpeg", "-i", "input", str(target)],
        OutputBudgetGuard(tmp_path, 1_000_000),
    )

    session.consume(_pam(1, 1, b"\x01\x02\x03\x04"))
    session.finish()

    assert target.exists()


def test_guarded_single_video_allows_literal_percent(tmp_path: Path) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / "100%done.mp4"
    session = GuardedFfmpegOutput(
        ["ffmpeg", "-i", "input", str(target)],
        OutputBudgetGuard(tmp_path, 1_000_000),
    )

    session.consume(b"container")
    session.finish()

    assert target.read_bytes() == b"container"


@pytest.mark.parametrize("suffix", [".png", ".mp4"])
def test_guarded_single_output_treats_sequence_marker_as_literal(
    tmp_path: Path,
    suffix: str,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / f"clip_%02d{suffix}"
    session = GuardedFfmpegOutput(
        ["ffmpeg", "-i", "input", str(target)],
        OutputBudgetGuard(tmp_path, 1_000_000),
        output_kind="single",
    )

    if suffix == ".png":
        session.consume(_pam(1, 1, b"\x01\x02\x03\x04"))
    else:
        session.consume(b"container")
    session.finish()

    assert target.exists()
    assert not (tmp_path / f"clip_01{suffix}").exists()


@pytest.mark.parametrize(
    "target",
    [
        "frames_%02d_%03d.png",
        "frames_%02d_%d.png",
        "frames_%02d_%00d.png",
        "frames_%02d_%06x.png",
    ],
)
def test_guarded_output_rejects_invalid_sequence_markers(
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
            output_kind="sequence",
        )


@pytest.mark.parametrize(
    "target",
    [
        "literal_%00d.png",
        "literal_%d.png",
        "literal_%06x.png",
    ],
)
def test_guarded_single_image_treats_invalid_marker_as_literal_percent(
    tmp_path: Path,
    target: str,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    output = tmp_path / target
    session = GuardedFfmpegOutput(
        ["ffmpeg", str(output)],
        OutputBudgetGuard(tmp_path, 1_000_000),
    )

    session.consume(_pam(1, 1, b"\x01\x02\x03\x04"))
    session.finish()

    assert output.exists()


@pytest.mark.parametrize(
    "target",
    [
        "output.xyz",
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
        output_kind="sequence",
    )
    session.consume(_pam(1, 1, b"abcd") + _pam(1, 1, b"efgh"))
    assert guard.used_bytes > 0
    assert not list(tmp_path.glob("frame_*.jpg"))
    assert len(list(tmp_path.glob(".*.tmp"))) == 2

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


def test_guarded_publish_race_does_not_delete_preexisting_target_on_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / "output.png"
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    session = GuardedFfmpegOutput(["ffmpeg", str(target)], guard)
    original_publish = guard.publish

    def race(source: str | Path, destination: str | Path) -> None:
        target.write_bytes(b"preexisting")
        original_publish(source, destination)

    monkeypatch.setattr(guard, "publish", race)

    session.consume(_pam(1, 1, b"abcd"))
    with pytest.raises(FileExistsError):
        session.finish()
    session.rollback()

    assert target.read_bytes() == b"preexisting"


def test_guarded_sequence_publish_conflict_leaves_own_partial_for_task_cleanup(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    task_root = tmp_path / "task"
    guard = OutputBudgetGuard(task_root, 1_000_000)
    session = GuardedFfmpegOutput(
        ["ffmpeg", str(task_root / "frame_%02d.png")],
        guard,
        output_kind="sequence",
    )
    session.consume(_pam(1, 1, b"abcd") + _pam(1, 1, b"efgh"))
    conflict = task_root / "frame_02.png"
    conflict.write_bytes(b"concurrent")

    with pytest.raises(FileExistsError):
        session.finish()
    session.rollback()

    first = task_root / "frame_01.png"
    assert first.is_file()
    assert conflict.read_bytes() == b"concurrent"
    assert not list(task_root.glob(".*.tmp"))
    assert guard.used_bytes == first.stat().st_size


def test_guarded_publish_interruption_leaves_final_for_task_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PublishInterrupted(BaseException):
        pass

    task_root = tmp_path / "task"
    target = task_root / "output.png"
    guard = OutputBudgetGuard(task_root, 1_000_000)

    def interrupt_after_link(_parent: Path) -> None:
        raise PublishInterrupted

    monkeypatch.setattr(
        output_limits,
        "_fsync_parent_directory",
        interrupt_after_link,
    )
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory(
            [_StreamingPopen([_pam(1, 1, b"abcd")])],
            [],
        ),
    )
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")

    with pytest.raises(PublishInterrupted):
        backend.run(["ffmpeg", str(target)])

    assert target.is_file()
    assert not list(task_root.glob(".*.tmp"))
    assert guard.used_bytes == target.stat().st_size

    target.unlink()
    task_root.rmdir()
    guard.check_tree()

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


@pytest.mark.parametrize("suffix", [".png", ".mp4"])
def test_guarded_rollback_never_deletes_published_single_output(
    tmp_path: Path,
    suffix: str,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import GuardedFfmpegOutput

    target = tmp_path / f"output{suffix}"
    session = GuardedFfmpegOutput(
        ["ffmpeg", str(target)],
        OutputBudgetGuard(tmp_path, 1_000_000),
    )
    session.consume(
        _pam(1, 1, b"abcd") if suffix == ".png" else b"container"
    )
    session.finish()

    session.rollback()

    assert target.is_file()
    assert not list(tmp_path.glob(".*.tmp"))


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


class _DelayedEofPipe:
    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._released = Event()
        self.read_started = Event()
        self.released_before_natural_eof = False
        self.done = False
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        self.read_started.set()
        self.released_before_natural_eof = self._released.wait(self._delay)
        self.done = True
        return b""

    def close(self) -> None:
        self.closed = True
        self._released.set()


class _PollingStreamingPopen:
    def __init__(
        self,
        stdout: _BytesPipe | _DelayedEofPipe,
        stderr: _BytesPipe | _DelayedEofPipe,
        *,
        wait_timeouts: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls: list[float | None] = []
        self.wait_timeouts = wait_timeouts
        self.wait_timeouts_observed = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.returncode is not None:
            return self.returncode
        if timeout is not None and self.wait_timeouts_observed < self.wait_timeouts:
            self.wait_timeouts_observed += 1
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = 1

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = 1


class _HealthGuard(OutputBudgetGuard):
    def __init__(
        self,
        root: Path,
        *,
        fail_after: int | None = None,
        failure_when: Callable[[], bool] | None = None,
        max_bytes: int = 1_000_000,
    ) -> None:
        super().__init__(root, max_bytes=max_bytes)
        self.fail_after = fail_after
        self.failure_when = failure_when
        self.checks = 0
        self.max_observed_bytes = 0
        self.failure = RuntimeError("resource heartbeat failed")

    def check_health(self) -> None:
        self.checks += 1
        if (
            self.fail_after is not None
            and self.checks >= self.fail_after
        ) or (self.failure_when is not None and self.failure_when()):
            raise self.failure

    def write(
        self,
        file: Any,
        path: str | Path,
        data: bytes | bytearray | memoryview,
    ) -> int:
        written = super().write(file, path, data)
        self.max_observed_bytes = max(self.max_observed_bytes, self.used_bytes)
        return written


def _streaming_popen_factory(
    processes: list[_StreamingPopen],
    calls: list[tuple[list[str], dict[str, Any]]],
):
    def popen(command: list[str], **kwargs: Any) -> _StreamingPopen:
        calls.append((command, kwargs))
        return processes.pop(0)

    return popen


def test_guarded_backend_stops_idle_process_on_health_failure_before_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _DelayedEofPipe(0.25)
    process = _PollingStreamingPopen(stdout, _BytesPipe([]))
    guard = _HealthGuard(tmp_path, fail_after=1)
    target = tmp_path / "output.avi"
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=lambda *_args, **_kwargs: process,
        poll_interval=0.01,
    )

    with pytest.raises(RuntimeError, match="resource heartbeat failed") as exc_info:
        backend.run(["ffmpeg", str(target)])

    assert exc_info.value is guard.failure
    assert stdout.released_before_natural_eof
    assert process.terminate_called
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_guarded_backend_stops_stream_growth_on_health_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StreamingPopen([b"x"] * 100)
    guard = _HealthGuard(tmp_path, fail_after=12, max_bytes=16)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
        poll_interval=0.01,
    )

    with pytest.raises(RuntimeError, match="resource heartbeat failed") as exc_info:
        backend.run(["ffmpeg", str(tmp_path / "output.avi")])

    assert exc_info.value is guard.failure
    assert process.terminate_called
    assert guard.max_observed_bytes <= 15
    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


def test_guarded_backend_checks_health_while_waiting_for_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _PollingStreamingPopen(
        _BytesPipe([]),
        _BytesPipe([]),
        wait_timeouts=1,
    )
    guard = _HealthGuard(
        tmp_path,
        failure_when=lambda: process.wait_timeouts_observed > 0,
    )
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=lambda *_args, **_kwargs: process,
        poll_interval=0.01,
    )

    with pytest.raises(RuntimeError, match="resource heartbeat failed") as exc_info:
        backend.run(["ffmpeg", str(tmp_path / "output.avi")])

    assert exc_info.value is guard.failure
    assert process.wait_timeouts_observed == 1
    assert process.terminate_called
    assert not list(tmp_path.iterdir())


def test_guarded_backend_checks_health_while_draining_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = _DelayedEofPipe(0.25)
    process = _PollingStreamingPopen(_BytesPipe([]), stderr)
    guard = _HealthGuard(
        tmp_path,
        failure_when=lambda: process.returncode is not None and not stderr.done,
    )
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=lambda *_args, **_kwargs: process,
        poll_interval=0.01,
    )

    with pytest.raises(RuntimeError, match="resource heartbeat failed") as exc_info:
        backend.run(["ffmpeg", str(tmp_path / "output.avi")])

    assert exc_info.value is guard.failure
    assert stderr.read_started.is_set()
    assert not list(tmp_path.iterdir())


def test_guarded_backend_checks_health_through_normal_stream_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = _DelayedEofPipe(0.05)
    process = _PollingStreamingPopen(
        _BytesPipe([b"first", b"-second"]),
        stderr,
        wait_timeouts=1,
    )
    guard = _HealthGuard(tmp_path)
    target = tmp_path / "output.avi"
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=lambda *_args, **_kwargs: process,
        poll_interval=0.01,
    )

    result = backend.run(["ffmpeg", str(target)])

    assert result.returncode == 0
    assert target.read_bytes() == b"first-second"
    assert guard.checks >= 10
    assert process.wait_timeouts_observed == 1


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
        backend.run(["ffmpeg", str(tmp_path / "output.xyz")])

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
        [_pam(1, 1, b"abcd")],
        stderr=[b"decode failed"],
        returncode=1,
    )
    guard = OutputBudgetGuard(tmp_path, 1_000_000)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    result = backend.run_guarded(
        ["ffmpeg", str(tmp_path / "frame_%06d.png")],
        output_kind="sequence",
    )

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
    Image.frombytes("RGBA", (1, 1), b"abcd").save(encoded, format="PNG")
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
    process = _StreamingPopen([_pam(1, 1, b"abcd") * frame_count])
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    with pytest.raises(OutputLimitExceeded):
        backend.run_guarded(
            ["ffmpeg", str(tmp_path / "frame_%06d.png")],
            output_kind="sequence",
        )

    assert max(observed_sizes) == frame_bytes * accepted_frames
    assert process.terminate_called
    assert not list(tmp_path.iterdir())
    assert guard.used_bytes == 0


def test_guarded_png_uses_encoded_budget_not_raw_rgba_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pixels = b"\x00\x80\xff\xff" * 10_000
    process = _StreamingPopen([_pam(100, 100, pixels)])
    guard = OutputBudgetGuard(tmp_path, max_bytes=1_000)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    result = backend.run_guarded(["ffmpeg", str(tmp_path / "output.png")])

    assert result.returncode == 0
    assert (tmp_path / "output.png").stat().st_size < 1_000


def test_guarded_sequence_has_a_hard_file_count_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from designtoolkit.video_frames import ffmpeg_streaming
    from designtoolkit.video_frames.ffmpeg_streaming import FfmpegOutputError

    monkeypatch.setattr(ffmpeg_streaming, "HOSTED_MAX_FRAME_OUTPUTS", 2)
    process = _StreamingPopen([_pam(1, 1, b"abcd") * 3])
    guard = OutputBudgetGuard(tmp_path, max_bytes=1_000_000)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_streaming_popen_factory([process], []),
    )

    with pytest.raises(FfmpegOutputError, match="more than 2"):
        backend.run_guarded(
            ["ffmpeg", str(tmp_path / "frame_%06d.png")],
            output_kind="sequence",
        )

    assert process.terminate_called
    assert not list(tmp_path.iterdir())


def test_webm_decoder_fallback_recovers_full_budget_after_failed_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "input%take.webm"
    input_path.write_bytes(b"input")
    output_dir = tmp_path / "frames"
    first = _StreamingPopen(
        [_pam(1, 1, b"bad\xff")],
        stderr=[
            b"libvpx-vp9: Bitstream not supported by this decoder"
        ],
        returncode=1,
    )
    second = _StreamingPopen([_pam(1, 1, b"\x01\x02\x03\x04")])
    calls: list[tuple[list[str], dict[str, Any]]] = []
    guard = OutputBudgetGuard(output_dir, 1_000_000)

    def popen(command: list[str], **kwargs: Any) -> _StreamingPopen:
        if calls:
            assert not list(output_dir.glob("input%take_*.png"))
            assert not list(output_dir.glob(".*.tmp"))
            assert guard.remaining_bytes == 999_999
        calls.append((command, kwargs))
        return [first, second][len(calls) - 1]

    monkeypatch.setattr(ffmpeg_backend, "os_name", "other")
    backend = FfmpegBackend(
        output_guard=guard,
        popen=popen,
    )

    result = export_video_frames(
        VideoFrameOptions(
            input_path=input_path,
            output_dir=output_dir,
            image_format="png",
        ),
        backend,
    )

    frames = list(output_dir.glob("input%take_*.png"))
    assert len(calls) == 2
    assert len(frames) == 1
    with Image.open(frames[0]) as image:
        image.load()
        assert image.getpixel((0, 0)) == (1, 2, 3, 4)
    assert guard.used_bytes == frames[0].stat().st_size
    assert result.output_pattern == output_dir / "input%%take_%06d.png"


def test_video_frame_export_uses_guarded_extension_for_primary_and_fallback(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.core import BackendResult

    class RecordingBackend:
        def __init__(self) -> None:
            self.output_kinds: list[str] = []

        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def get_duration_seconds(self, _video_path: Path) -> float | None:
            return None

        def run(self, _command: list[str]) -> BackendResult:
            pytest.fail("guarded extension should handle the command")

        def run_guarded(
            self,
            _command: list[str],
            *,
            output_kind: str,
        ) -> BackendResult:
            self.output_kinds.append(output_kind)
            if len(self.output_kinds) == 1:
                return BackendResult(
                    returncode=1,
                    stderr=(
                        "libvpx-vp9: Bitstream not supported by this decoder"
                    ),
                )
            return BackendResult(returncode=0)

    input_path = tmp_path / "input.webm"
    input_path.write_bytes(b"input")
    backend = RecordingBackend()

    export_video_frames(
        VideoFrameOptions(
            input_path=input_path,
            output_dir=tmp_path / "frames",
            image_format="png",
        ),
        backend,
    )

    assert backend.output_kinds == ["sequence", "sequence"]


def test_video_frame_export_supports_legacy_run_backend(tmp_path: Path) -> None:
    from designtoolkit.video_frames.core import BackendResult

    class LegacyBackend:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def get_duration_seconds(self, _video_path: Path) -> float | None:
            return None

        def run(self, command: list[str]) -> BackendResult:
            self.commands.append(command)
            return BackendResult(returncode=0)

    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"input")
    backend = LegacyBackend()

    export_video_frames(
        VideoFrameOptions(
            input_path=input_path,
            output_dir=tmp_path / "frames",
        ),
        backend,
    )

    assert len(backend.commands) == 1


def test_media_resize_uses_guarded_extension(tmp_path: Path) -> None:
    from designtoolkit.media_resize.core import ResizeBackendResult

    class RecordingBackend:
        def __init__(self) -> None:
            self.output_kinds: list[str] = []

        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def run(self, _command: list[str]) -> ResizeBackendResult:
            pytest.fail("guarded extension should handle the command")

        def run_guarded(
            self,
            _command: list[str],
            *,
            output_kind: str,
        ) -> ResizeBackendResult:
            self.output_kinds.append(output_kind)
            return ResizeBackendResult(returncode=0)

    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"input")
    backend = RecordingBackend()

    resize_media(
        MediaResizeOptions(
            input_path=input_path,
            output_path=tmp_path / "clip_%02d.png",
            size=(10, 10),
        ),
        backend,
    )

    assert backend.output_kinds == ["single"]


def test_media_resize_supports_legacy_run_backend(tmp_path: Path) -> None:
    from designtoolkit.media_resize.core import ResizeBackendResult

    class LegacyBackend:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def run(self, command: list[str]) -> ResizeBackendResult:
            self.commands.append(command)
            return ResizeBackendResult(returncode=0)

    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"input")
    backend = LegacyBackend()

    resize_media(
        MediaResizeOptions(
            input_path=input_path,
            output_path=tmp_path / "output.png",
            size=(10, 10),
        ),
        backend,
    )

    assert len(backend.commands) == 1


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
    [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"],
)
def test_bundled_ffmpeg_guarded_single_image_formats(
    tmp_path: Path,
    suffix: str,
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (16, 8), "red").save(source)
    target = tmp_path / f"image_%02d{suffix}"
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


def test_bundled_ffmpeg_guarded_wide_image_round_trips_through_pillow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wide-source.png"
    Image.new("RGB", (20_000, 2), "red").save(source)
    target = tmp_path / "wide-output.png"

    result = resize_media(
        MediaResizeOptions(
            input_path=source,
            output_path=target,
            size=(20_000, 2),
        ),
        FfmpegBackend(
            output_guard=OutputBudgetGuard(tmp_path, 10_000_000)
        ),
    )

    assert result.output_path == target
    with Image.open(target) as image:
        image.load()
        assert image.size == (20_000, 2)


def test_bundled_ffmpeg_guarded_gif_resize_preserves_basic_transparency(
    tmp_path: Path,
) -> None:
    source = tmp_path / "transparent.png"
    source_pixels = Image.new("RGBA", (4, 2))
    source_pixels.putdata(
        [
            (255, 0, 0, 0),
            (255, 0, 0, 0),
            (0, 255, 0, 255),
            (0, 255, 0, 255),
            (255, 0, 0, 0),
            (255, 0, 0, 0),
            (0, 255, 0, 255),
            (0, 255, 0, 255),
        ]
    )
    source_pixels.save(source)
    target = tmp_path / "resized.gif"

    result = resize_media(
        MediaResizeOptions(
            input_path=source,
            output_path=target,
            size=(4, 2),
        ),
        FfmpegBackend(
            output_guard=OutputBudgetGuard(tmp_path, 10_000_000)
        ),
    )

    assert result.output_path == target
    with Image.open(target) as image:
        image.load()
        alpha = image.convert("RGBA").getchannel("A")
        assert image.format == "GIF"
        assert image.size == (4, 2)
        assert set(alpha.tobytes()) == {0, 255}


def test_bundled_ffmpeg_guarded_static_gif_resize_succeeds(
    tmp_path: Path,
) -> None:
    source = tmp_path / "static.gif"
    Image.new("RGB", (8, 4), "red").save(source, format="GIF")
    target = tmp_path / "resized.gif"

    result = resize_media(
        MediaResizeOptions(
            input_path=source,
            output_path=target,
            size=(4, 4),
        ),
        FfmpegBackend(
            output_guard=OutputBudgetGuard(tmp_path, 10_000_000)
        ),
    )

    assert result.output_path == target
    with Image.open(target) as image:
        image.load()
        assert image.format == "GIF"
        assert image.size == (4, 2)


def test_bundled_ffmpeg_guarded_animated_gif_resize_fails_without_output(
    tmp_path: Path,
) -> None:
    from designtoolkit.video_frames.ffmpeg_streaming import FfmpegOutputError

    source = tmp_path / "animated.gif"
    frames = [
        Image.new("RGB", (8, 4), color)
        for color in ("red", "blue")
    ]
    frames[0].save(
        source,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=10,
    )
    target = tmp_path / "resized.gif"
    guard = OutputBudgetGuard(tmp_path, 10_000_000)

    with pytest.raises(FfmpegOutputError, match="multiple frames"):
        resize_media(
            MediaResizeOptions(
                input_path=source,
                output_path=target,
                size=(4, 4),
            ),
            FfmpegBackend(output_guard=guard),
        )

    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    assert guard.used_bytes == 0


def test_bundled_ffmpeg_guarded_transparent_png_resize_preserves_alpha(
    tmp_path: Path,
) -> None:
    source = tmp_path / "transparent.png"
    source_pixels = Image.new("RGBA", (4, 2))
    source_pixels.putdata(
        [
            (255, 0, 0, 0),
            (255, 0, 0, 64),
            (255, 0, 0, 128),
            (255, 0, 0, 255),
            (0, 255, 0, 0),
            (0, 255, 0, 64),
            (0, 255, 0, 128),
            (0, 255, 0, 255),
        ]
    )
    source_pixels.save(source)
    target = tmp_path / "resized.png"
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, 10_000_000)
    )

    result = resize_media(
        MediaResizeOptions(
            input_path=source,
            output_path=target,
            size=(4, 2),
        ),
        backend,
    )

    assert result.output_path == target
    with Image.open(target) as image:
        image.load()
        assert image.mode == "RGBA"
        assert image.getchannel("A").tobytes() == bytes(
            [0, 64, 128, 255, 0, 64, 128, 255]
        )


def test_bundled_ffmpeg_guarded_alpha_webm_sequence_preserves_alpha(
    tmp_path: Path,
) -> None:
    plain_backend = FfmpegBackend()
    ffmpeg_exe = plain_backend.get_ffmpeg_exe()
    source = tmp_path / "alpha.webm"
    encode = subprocess.run(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red@0.25:s=8x8:r=1:d=1,format=rgba",
            "-c:v",
            "libvpx-vp9",
            "-pix_fmt",
            "yuva420p",
            "-auto-alt-ref",
            "0",
            str(source),
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert encode.returncode == 0, encode.stderr.decode(errors="replace")

    output_dir = tmp_path / "frames"
    result = export_video_frames(
        VideoFrameOptions(
            input_path=source,
            output_dir=output_dir,
            image_format="png",
        ),
        FfmpegBackend(
            output_guard=OutputBudgetGuard(output_dir, 10_000_000)
        ),
    )

    frames = sorted(output_dir.glob("alpha_*.png"))
    assert result.output_pattern == output_dir / "alpha_%06d.png"
    assert len(frames) == 1
    with Image.open(frames[0]) as image:
        image.load()
        alpha_values = set(image.getchannel("A").tobytes())
        assert image.mode == "RGBA"
        assert len(alpha_values) == 1
        assert 0 < alpha_values.pop() < 255


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

    result = backend.run_guarded(
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
        ],
        output_kind="sequence",
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
    target = tmp_path / f"video_%02d{suffix}"
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
