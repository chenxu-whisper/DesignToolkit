from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import numpy as np
import pytest
from pytest import raises

from designtoolkit.chroma_keyer.models import KeyerError
from designtoolkit.chroma_keyer.video_io import ImageioVideoDecoder
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

VALID_METADATA = {
    "codec": "h264",
    "source_size": (4, 2),
    "size": (4, 2),
    "fps": 25.0,
    "duration": 2.0,
}
FRAME_BYTES = bytes(range(24))


class TrackedFrames(Iterator[object]):
    def __init__(self, items: list[object]) -> None:
        self._items = iter(items)
        self.closed = False
        self.next_count = 0

    def __iter__(self) -> TrackedFrames:
        return self

    def __next__(self) -> object:
        self.next_count += 1
        return next(self._items)

    def close(self) -> None:
        self.closed = True


class FakeReadFrames:
    def __init__(self, *responses: list[object]) -> None:
        self._responses = iter(responses)
        self.calls: list[tuple[Path, dict[str, object]]] = []
        self.generators: list[TrackedFrames] = []

    def __call__(self, path: Path, **kwargs: object) -> TrackedFrames:
        self.calls.append((path, kwargs))
        generator = TrackedFrames(next(self._responses))
        self.generators.append(generator)
        return generator


def make_video_path(tmp_path: Path, suffix: str = ".mp4") -> Path:
    path = tmp_path / f"clip{suffix}"
    path.write_bytes(b"fake video")
    return path


def make_decoder(
    read_frames: Callable[..., Iterator[object]],
    validator: Callable[[], object] | None = None,
) -> ImageioVideoDecoder:
    return ImageioVideoDecoder(
        read_frames=read_frames,
        availability_validator=validator or (lambda: "bundled-ffmpeg"),
    )


def test_probe_normalizes_metadata_validates_backend_and_closes(
    tmp_path: Path,
) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames([dict(VALID_METADATA)])
    validator_calls = 0

    def validate_backend() -> str:
        nonlocal validator_calls
        validator_calls += 1
        return "bundled-ffmpeg"

    metadata = make_decoder(read_frames, validate_backend).probe(path)

    assert metadata.width == 4
    assert metadata.height == 2
    assert metadata.fps == 25.0
    assert metadata.duration_seconds == 2.0
    assert metadata.estimated_frame_count == 50
    assert metadata.codec == "h264"
    assert validator_calls == 1
    assert read_frames.calls == [(path, {"pix_fmt": "rgb24"})]
    assert read_frames.generators[0].closed


def test_probe_uses_size_when_source_size_is_missing_and_estimates_one_frame(
    tmp_path: Path,
) -> None:
    path = make_video_path(tmp_path, ".MOV")
    raw_metadata = {
        "codec": "mpeg4",
        "size": [3, 2],
        "fps": 0.25,
        "duration": 0.25,
    }
    read_frames = FakeReadFrames([raw_metadata])

    metadata = make_decoder(read_frames).probe(path)

    assert (metadata.width, metadata.height) == (3, 2)
    assert metadata.estimated_frame_count == 1


@pytest.mark.parametrize(
    ("path_factory", "message"),
    [
        (lambda root: root / "clip.png", "supported video suffix"),
        (lambda root: root / "missing.mp4", "Input path does not exist"),
        (lambda root: root / "directory.mp4", "Input path must be a file"),
    ],
)
def test_probe_rejects_unsupported_missing_and_directory_paths_before_backend(
    tmp_path: Path,
    path_factory: Callable[[Path], Path],
    message: str,
) -> None:
    path = path_factory(tmp_path)
    if path.name == "directory.mp4":
        path.mkdir()
    validator_calls = 0

    def validate_backend() -> str:
        nonlocal validator_calls
        validator_calls += 1
        return "bundled-ffmpeg"

    with raises(KeyerError, match=message):
        make_decoder(FakeReadFrames(), validate_backend).probe(path)

    assert validator_calls == 0


def test_probe_reports_unavailable_bundled_ffmpeg_without_opening_video(
    tmp_path: Path,
) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames()

    def unavailable() -> None:
        raise RuntimeError("missing executable")

    with raises(KeyerError, match="^Bundled FFmpeg is unavailable$"):
        make_decoder(read_frames, unavailable).probe(path)

    assert read_frames.calls == []


@pytest.mark.parametrize(
    "raw_metadata",
    [
        {},
        {"codec": "h264", "size": (4,), "fps": 25.0, "duration": 2.0},
        {"codec": "h264", "size": (True, 2), "fps": 25.0, "duration": 2.0},
        {"codec": "h264", "size": (4, 0), "fps": 25.0, "duration": 2.0},
        {"codec": "h264", "size": (4, 2), "fps": 0.0, "duration": 2.0},
        {"codec": "h264", "size": (4, 2), "fps": 25.0, "duration": 0.0},
        {"codec": "", "size": (4, 2), "fps": 25.0, "duration": 2.0},
    ],
)
def test_probe_rejects_missing_or_invalid_metadata_and_closes(
    tmp_path: Path,
    raw_metadata: Mapping[str, object],
) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames([dict(raw_metadata)])

    with raises(KeyerError, match="^Video metadata is missing or invalid$"):
        make_decoder(read_frames).probe(path)

    assert read_frames.generators[0].closed


def test_probe_rejects_a_stream_without_metadata_and_closes(tmp_path: Path) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames([])

    with raises(KeyerError, match="^Video metadata is missing or invalid$"):
        make_decoder(read_frames).probe(path)

    assert read_frames.generators[0].closed


def test_read_frame_seeks_once_and_returns_writable_rgb_copy(tmp_path: Path) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames(
        [dict(VALID_METADATA)],
        [dict(VALID_METADATA), FRAME_BYTES],
    )

    frame = make_decoder(read_frames).read_frame(path, 0.5)

    assert frame.shape == (2, 4, 3)
    assert frame.dtype == np.uint8
    assert frame.flags.c_contiguous
    assert frame.flags.writeable
    assert frame.tolist() == np.arange(24, dtype=np.uint8).reshape(2, 4, 3).tolist()
    frame[0, 0, 0] = 255
    assert read_frames.calls[1] == (
        path,
        {
            "pix_fmt": "rgb24",
            "input_params": ["-ss", "0.5"],
            "output_params": ["-frames:v", "1"],
        },
    )
    assert all(generator.closed for generator in read_frames.generators)


def test_preview_uses_identical_seek_and_bounds_large_frame(
    tmp_path: Path,
) -> None:
    path = make_video_path(tmp_path)
    large_metadata = {
        **VALID_METADATA,
        "source_size": (1600, 1200),
        "size": (1600, 1200),
    }
    large_frame = np.zeros((1200, 1600, 3), dtype=np.uint8).tobytes()
    source_reader = FakeReadFrames(
        [dict(large_metadata)],
        [dict(large_metadata), large_frame],
    )
    preview_reader = FakeReadFrames(
        [dict(large_metadata)],
        [dict(large_metadata), large_frame],
    )
    source = make_decoder(source_reader)
    preview = make_decoder(preview_reader)

    source.read_frame(path, 1.25)
    bounded = preview.read_preview_frame(path, 1.25)

    assert source_reader.calls[1][1] == preview_reader.calls[1][1]
    assert bounded.shape == (720, 960, 3)
    assert bounded.dtype == np.uint8
    assert bounded.flags.c_contiguous


@pytest.mark.parametrize("timestamp", [-0.01, 2.0])
def test_read_frame_rejects_timestamps_against_probed_metadata(
    tmp_path: Path,
    timestamp: float,
) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames([dict(VALID_METADATA)])

    with raises(KeyerError, match="timestamp"):
        make_decoder(read_frames).read_frame(path, timestamp)

    assert len(read_frames.calls) == 1
    assert read_frames.generators[0].closed


@pytest.mark.parametrize("frame_bytes", [b"", bytes(23), bytes(25)])
def test_read_frame_rejects_zero_or_invalid_frame_bytes_and_closes(
    tmp_path: Path,
    frame_bytes: bytes,
) -> None:
    path = make_video_path(tmp_path)
    response = [dict(VALID_METADATA)]
    if frame_bytes:
        response.append(frame_bytes)
    read_frames = FakeReadFrames([dict(VALID_METADATA)], response)

    message = (
        "Decoded video contains no frames"
        if not frame_bytes
        else "Decoded video frame has an invalid byte length"
    )
    with raises(KeyerError, match=f"^{message}$"):
        make_decoder(read_frames).read_frame(path, 0.0)

    assert all(generator.closed for generator in read_frames.generators)


def test_iter_frames_is_lazy_ordered_and_closes_on_completion(tmp_path: Path) -> None:
    path = make_video_path(tmp_path)
    first = bytes(range(24))
    second = bytes(reversed(range(24)))
    read_frames = FakeReadFrames([dict(VALID_METADATA), first, second])

    metadata, frames = make_decoder(read_frames).iter_frames(path)

    assert metadata.estimated_frame_count == 50
    assert read_frames.generators[0].next_count == 1
    assert not read_frames.generators[0].closed

    decoded = list(frames)

    assert [frame[0, 0].tolist() for frame in decoded] == [[0, 1, 2], [23, 22, 21]]
    assert read_frames.generators[0].closed


def test_iter_frames_closes_when_caller_closes_before_reading_frames(
    tmp_path: Path,
) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames([dict(VALID_METADATA), FRAME_BYTES])

    _, frames = make_decoder(read_frames).iter_frames(path)
    frames.close()  # type: ignore[attr-defined]

    assert read_frames.generators[0].next_count == 1
    assert read_frames.generators[0].closed


def test_iter_frames_closes_after_invalid_frame_or_source_error(tmp_path: Path) -> None:
    path = make_video_path(tmp_path)

    invalid_reader = FakeReadFrames([dict(VALID_METADATA), bytes(23)])
    _, invalid_frames = make_decoder(invalid_reader).iter_frames(path)
    with raises(
        KeyerError,
        match="^Decoded video frame has an invalid byte length$",
    ):
        next(invalid_frames)
    assert invalid_reader.generators[0].closed

    class RaisingFrames(TrackedFrames):
        def __next__(self) -> object:
            if self.next_count == 1:
                self.next_count += 1
                raise RuntimeError("decoder failed")
            return super().__next__()

    generator = RaisingFrames([dict(VALID_METADATA)])

    def read_frames(*args: object, **kwargs: object) -> RaisingFrames:
        return generator

    _, failing_frames = make_decoder(read_frames).iter_frames(path)
    with raises(KeyerError, match="^Video frames could not be decoded$"):
        next(failing_frames)
    assert generator.closed


def test_iter_frames_allows_zero_frames_and_closes(tmp_path: Path) -> None:
    path = make_video_path(tmp_path)
    read_frames = FakeReadFrames([dict(VALID_METADATA)])

    _, frames = make_decoder(read_frames).iter_frames(path)

    assert list(frames) == []
    assert read_frames.generators[0].closed


def _available_encoders(ffmpeg_exe: str) -> str:
    completed = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"Unable to inspect bundled FFmpeg encoders: {completed.stderr}")
    return completed.stdout


@pytest.mark.parametrize(
    ("suffix", "encoder"),
    [(".mp4", "mpeg4"), (".mov", "mpeg4"), (".webm", "libvpx-vp9")],
)
def test_bundled_ffmpeg_probes_and_decodes_tiny_video(
    tmp_path: Path,
    suffix: str,
    encoder: str,
) -> None:
    ffmpeg_exe = FfmpegBackend().get_ffmpeg_exe()
    encoder_lines = _available_encoders(ffmpeg_exe)
    if not any(
        line.split()[1] == encoder
        for line in encoder_lines.splitlines()
        if len(line.split()) >= 2
    ):
        pytest.skip(f"Bundled FFmpeg does not provide encoder {encoder}")

    path = tmp_path / f"tiny{suffix}"
    command = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x00ff00:s=16x12:r=5:d=0.6",
        "-an",
        "-c:v",
        encoder,
        "-pix_fmt",
        "yuv420p",
        "-y",
        str(path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    decoder = ImageioVideoDecoder()
    metadata = decoder.probe(path)
    frame = decoder.read_frame(path, 0.0)
    iter_metadata, frames = decoder.iter_frames(path)
    first_iterated_frame = next(frames)
    frames.close()  # type: ignore[attr-defined]

    assert (metadata.width, metadata.height) == (16, 12)
    assert metadata.fps == pytest.approx(5.0)
    assert metadata.duration_seconds > 0
    assert metadata.estimated_frame_count >= 1
    assert frame.shape == (12, 16, 3)
    assert frame.dtype == np.uint8
    assert iter_metadata == metadata
    assert first_iterated_frame.shape == (12, 16, 3)
