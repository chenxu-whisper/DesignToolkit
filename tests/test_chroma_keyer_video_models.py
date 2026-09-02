from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from inspect import signature
from math import inf, nan, nextafter
from pathlib import Path

import numpy as np
from pytest import mark, raises

from designtoolkit.chroma_keyer import (
    SUPPORTED_VIDEO_SUFFIXES,
    ProgressCallback,
    VideoDecoder,
    VideoFrameStream,
    VideoKeyOptions,
    VideoKeyResult,
    VideoMetadata,
    clamp_timeline_timestamp,
    last_frame_timestamp,
    validate_timestamp,
    validate_video_path_suffix,
)
from designtoolkit.chroma_keyer.models import KeyerError


def metadata(**overrides: object) -> VideoMetadata:
    values = {
        "width": 1920,
        "height": 1080,
        "fps": 25.0,
        "duration_seconds": 4.0,
        "estimated_frame_count": 100,
        "codec": "h264",
    }
    values.update(overrides)
    return VideoMetadata(**values)


def test_video_metadata_accepts_valid_values_and_exposes_frame_step() -> None:
    value = VideoMetadata(
        width=1920,
        height=1080,
        fps=25.0,
        duration_seconds=4.0,
        estimated_frame_count=100,
        codec="h264",
    )

    assert value.duration_seconds == 4.0
    assert value.frame_step_seconds == 0.04


@mark.parametrize(("field", "value"), [("fps", 25), ("duration_seconds", 4)])
def test_video_metadata_normalizes_integer_real_fields_to_float(
    field: str, value: int
) -> None:
    video_metadata = metadata(**{field: value})

    assert getattr(video_metadata, field) == float(value)
    assert type(getattr(video_metadata, field)) is float


def test_video_metadata_is_immutable() -> None:
    value = metadata()

    with raises(FrozenInstanceError):
        value.width = 1280  # type: ignore[misc]


@mark.parametrize("field", ["width", "height", "estimated_frame_count"])
@mark.parametrize("value", [True, 0, -1, 1.5])
def test_video_metadata_rejects_invalid_positive_integer_fields(
    field: str, value: object
) -> None:
    with raises(KeyerError, match=rf"^{field} must be a positive integer$"):
        metadata(**{field: value})


@mark.parametrize("field", ["fps", "duration_seconds"])
@mark.parametrize("value", [True, 0.0, -1.0, nan, inf, -inf, 10**400, "25"])
def test_video_metadata_rejects_invalid_positive_real_fields(
    field: str, value: object
) -> None:
    with raises(KeyerError, match=rf"^{field} must be a finite positive number$"):
        metadata(**{field: value})


@mark.parametrize("codec", ["", " ", "\t", None, 264])
def test_video_metadata_rejects_blank_or_non_string_codec(codec: object) -> None:
    with raises(KeyerError, match="^codec must be a non-blank string$"):
        metadata(codec=codec)


def test_video_key_options_preserve_paths_and_default_output_root() -> None:
    input_path = Path("clip.mov")
    output_root = Path("frames")

    assert VideoKeyOptions(input_path) == VideoKeyOptions(input_path, None)
    assert VideoKeyOptions(input_path, output_root).output_root == output_root


def test_video_key_result_preserves_contract_values() -> None:
    video_metadata = metadata()
    result = VideoKeyResult(
        output_dir=Path("frames"),
        manifest_path=Path("frames/manifest.json"),
        frame_count=100,
        metadata=video_metadata,
    )

    assert result.output_dir == Path("frames")
    assert result.manifest_path == Path("frames/manifest.json")
    assert result.frame_count == 100
    assert result.metadata is video_metadata


@mark.parametrize("frame_count", [True, 0, -1, 1.5])
def test_video_key_result_rejects_invalid_frame_count(frame_count: object) -> None:
    with raises(KeyerError, match="^frame_count must be a positive integer$"):
        VideoKeyResult(
            output_dir=Path("frames"),
            manifest_path=Path("frames/manifest.json"),
            frame_count=frame_count,
            metadata=metadata(),
        )


def test_validate_timestamp_accepts_start_and_value_before_duration() -> None:
    video_metadata = metadata()
    final_valid_value = nextafter(video_metadata.duration_seconds, 0.0)

    assert validate_timestamp(0, video_metadata) == 0.0
    assert validate_timestamp(final_valid_value, video_metadata) == final_valid_value


def test_validate_timestamp_rejects_rounded_integer_duration_endpoint() -> None:
    duration_seconds = 2**53 + 1
    video_metadata = metadata(duration_seconds=duration_seconds)

    with raises(
        KeyerError,
        match=r"^timestamp must satisfy 0 <= timestamp < duration_seconds$",
    ):
        validate_timestamp(duration_seconds, video_metadata)


@mark.parametrize("value", [True, nan, inf, -inf, 10**400, "1"])
def test_validate_timestamp_rejects_non_finite_real_values(value: object) -> None:
    with raises(KeyerError, match="^timestamp must be a finite number$"):
        validate_timestamp(value, metadata())


@mark.parametrize("value", [-0.01, 4.0, 4.01])
def test_validate_timestamp_rejects_values_outside_video(value: float) -> None:
    with raises(
        KeyerError,
        match=r"^timestamp must satisfy 0 <= timestamp < duration_seconds$",
    ):
        validate_timestamp(value, metadata())


def test_last_frame_timestamp_is_one_frame_before_duration() -> None:
    assert last_frame_timestamp(metadata()) == 3.96


def test_last_frame_timestamp_never_reaches_duration_after_rounding() -> None:
    video_metadata = metadata(duration_seconds=1e20, fps=1e20)

    assert last_frame_timestamp(video_metadata) == nextafter(1e20, 0.0)


def test_last_frame_timestamp_clamps_short_video_to_zero() -> None:
    assert last_frame_timestamp(metadata(duration_seconds=0.01, fps=25.0)) == 0.0


@mark.parametrize(
    ("value", "expected"),
    [(-1.0, 0.0), (0.0, 0.0), (1.25, 1.25), (4.0, 3.96), (100.0, 3.96)],
)
def test_clamp_timeline_timestamp_clamps_to_available_frame_range(
    value: float, expected: float
) -> None:
    assert clamp_timeline_timestamp(value, metadata()) == expected


@mark.parametrize("value", [True, nan, inf, -inf, 10**400, "1"])
def test_clamp_timeline_timestamp_rejects_non_finite_real_values(
    value: object,
) -> None:
    with raises(KeyerError, match="^timestamp must be a finite number$"):
        clamp_timeline_timestamp(value, metadata())


def test_supported_video_suffixes_are_exactly_the_contract_set() -> None:
    assert SUPPORTED_VIDEO_SUFFIXES == frozenset(
        {".mov", ".mp4", ".m4v", ".webm", ".mkv", ".avi"}
    )


@mark.parametrize(
    "path",
    [
        Path("clip.mov"),
        Path("clip.MP4"),
        Path("clip.M4v"),
        Path("clip.WEBM"),
        Path("clip.MkV"),
        Path("clip.AVI"),
    ],
)
def test_validate_video_path_suffix_accepts_supported_case_variants(
    path: Path,
) -> None:
    assert validate_video_path_suffix(path) is None


@mark.parametrize("path", [Path("clip"), Path("clip.png"), Path("clip.mp4.txt")])
def test_validate_video_path_suffix_rejects_unsupported_suffix(path: Path) -> None:
    with raises(
        KeyerError,
        match="^Input path must have a supported video suffix$",
    ):
        validate_video_path_suffix(path)


def test_video_decoder_protocol_declares_required_methods() -> None:
    assert set(VideoDecoder.__dict__) >= {
        "probe",
        "read_frame",
        "read_preview_frame",
        "iter_frames",
    }
    assert list(signature(VideoDecoder.probe).parameters) == [
        "self",
        "path",
    ]
    assert list(signature(VideoDecoder.read_frame).parameters) == [
        "self",
        "path",
        "timestamp_seconds",
    ]
    assert list(signature(VideoDecoder.read_preview_frame).parameters) == [
        "self",
        "path",
        "timestamp_seconds",
    ]
    assert list(signature(VideoDecoder.iter_frames).parameters) == [
        "self",
        "path",
    ]


def test_video_frame_stream_protocol_exposes_deterministic_cleanup() -> None:
    assert issubclass(VideoFrameStream, Iterator)
    assert list(signature(VideoFrameStream.close).parameters) == ["self"]
    assert VideoFrameStream.close.__annotations__ == {"return": None}


def test_progress_callback_protocol_is_callable_with_progress_contract() -> None:
    assert list(signature(ProgressCallback.__call__).parameters) == [
        "self",
        "processed_frames",
        "estimated_total",
        "current_path",
    ]


def test_protocol_annotations_include_numpy_frames_and_iterator_result() -> None:
    assert VideoDecoder.probe.__annotations__ == {
        "path": Path,
        "return": VideoMetadata,
    }
    assert VideoDecoder.read_frame.__annotations__ == {
        "path": Path,
        "timestamp_seconds": float,
        "return": np.ndarray,
    }
    assert VideoDecoder.read_preview_frame.__annotations__ == {
        "path": Path,
        "timestamp_seconds": float,
        "return": np.ndarray,
    }
    assert VideoDecoder.iter_frames.__annotations__["path"] is Path
    assert VideoDecoder.read_frame.__annotations__["return"] is np.ndarray
    assert VideoDecoder.read_preview_frame.__annotations__["return"] is np.ndarray
    assert (
        VideoDecoder.iter_frames.__annotations__["return"]
        == tuple[VideoMetadata, VideoFrameStream]
    )
    assert ProgressCallback.__call__.__annotations__ == {
        "processed_frames": int,
        "estimated_total": int | None,
        "current_path": Path,
        "return": None,
    }
