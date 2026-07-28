from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from designtoolkit.video_frames import (
    BackendResult,
    VideoFrameError,
    VideoFrameOptions,
    default_output_dir,
    export_video_frames,
    parse_size,
)


class FakeBackend:
    def __init__(
        self,
        duration_seconds: float | None = 10.0,
        run_result: BackendResult | None = None,
    ) -> None:
        self.duration_seconds = duration_seconds
        self.run_result = run_result or BackendResult(returncode=0)
        self.commands: list[list[str]] = []

    def get_ffmpeg_exe(self) -> str:
        return "ffmpeg"

    def get_duration_seconds(self, video_path: Path) -> float | None:
        return self.duration_seconds

    def run(self, command: Sequence[str]) -> BackendResult:
        self.commands.append(list(command))
        return self.run_result


def create_video(path: Path) -> Path:
    path.write_bytes(b"video")
    return path


def video_filter(command: list[str]) -> str:
    return command[command.index("-vf") + 1]


def test_parse_size_accepts_width_x_height() -> None:
    assert parse_size("1920x1080") == (1920, 1080)


def test_parse_size_accepts_uppercase_separator() -> None:
    assert parse_size("1280X720") == (1280, 720)


@pytest.mark.parametrize("value", ["1920*1080", "abc", "0x1080", "1920x0"])
def test_parse_size_rejects_invalid_values(value: str) -> None:
    with pytest.raises(VideoFrameError, match="Invalid size"):
        parse_size(value)


def test_default_output_dir_uses_video_parent_and_stem(tmp_path: Path) -> None:
    video_path = tmp_path / "demo.mp4"

    assert default_output_dir(video_path) == tmp_path / "demo_frames"


def test_export_uses_default_format_dir_size_and_frame_rate(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend()

    result = export_video_frames(VideoFrameOptions(input_path=video_path), backend)

    output_dir = tmp_path / "demo_frames"
    output_pattern = output_dir / "demo_%06d.png"
    assert result.output_dir == output_dir
    assert result.output_pattern == output_pattern
    assert output_dir.is_dir()
    assert backend.commands == [
        [
            "ffmpeg",
            "-hide_banner",
            "-n",
            "-i",
            str(video_path),
            str(output_pattern),
        ]
    ]
    assert "-vf" not in backend.commands[0]


def test_export_escapes_percent_in_ffmpeg_output_pattern(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "clip%done.mp4")
    backend = FakeBackend()

    result = export_video_frames(VideoFrameOptions(input_path=video_path), backend)

    output_pattern = tmp_path / "clip%done_frames" / "clip%%done_%06d.png"
    assert result.output_pattern == output_pattern
    assert backend.commands[0][-1] == str(output_pattern)


def test_export_uses_requested_format(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mov")
    backend = FakeBackend()

    result = export_video_frames(
        VideoFrameOptions(input_path=video_path, image_format="jpg"),
        backend,
    )

    assert result.output_pattern == tmp_path / "demo_frames" / "demo_%06d.jpg"


def test_export_uses_fps_and_size_filters(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend()

    export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            size=(1920, 1080),
            fps=12.0,
        ),
        backend,
    )

    assert video_filter(backend.commands[0]) == "fps=12,scale=1920:1080"


def test_export_converts_total_frames_to_effective_fps(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend(duration_seconds=10.0)

    export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            frames=120,
        ),
        backend,
    )

    assert video_filter(backend.commands[0]) == "fps=12"


def test_export_rejects_fps_and_frames_together(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend()

    with pytest.raises(VideoFrameError, match="cannot be used together") as exc_info:
        export_video_frames(
            VideoFrameOptions(
                input_path=video_path,
                fps=12.0,
                frames=120,
            ),
            backend,
        )

    assert exc_info.value.exit_code == 2
    assert backend.commands == []


@pytest.mark.parametrize(
    ("option_kwargs", "message"),
    [
        ({"fps": 0.0}, "FPS must be greater than 0."),
        ({"frames": 0}, "Frames must be greater than 0."),
    ],
)
def test_export_rejects_non_positive_sampling_values(
    tmp_path: Path,
    option_kwargs: dict[str, float | int],
    message: str,
) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend()

    with pytest.raises(VideoFrameError) as exc_info:
        export_video_frames(
            VideoFrameOptions(input_path=video_path, **option_kwargs),
            backend,
        )

    assert str(exc_info.value) == message
    assert exc_info.value.exit_code == 2
    assert backend.commands == []


@pytest.mark.parametrize("duration_seconds", [None, 0.0])
def test_export_rejects_frames_mode_without_positive_duration(
    tmp_path: Path,
    duration_seconds: float | None,
) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend(duration_seconds=duration_seconds)

    with pytest.raises(
        VideoFrameError,
        match="Unable to determine video duration for --frames mode.",
    ) as exc_info:
        export_video_frames(
            VideoFrameOptions(
                input_path=video_path,
                frames=120,
            ),
            backend,
        )

    assert exc_info.value.exit_code == 1
    assert backend.commands == []


def test_export_uses_custom_output_dir_and_overwrite_flag(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    backend = FakeBackend()

    result = export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            output_dir=output_dir,
            overwrite=True,
        ),
        backend,
    )

    assert result.output_dir == output_dir
    assert backend.commands[0][2] == "-y"


def test_export_removes_matching_frames_when_overwriting(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    matching_frame = output_dir / "demo_000123.png"
    unrelated_files = [
        output_dir / "demo_000123.jpg",
        output_dir / "demo_123.png",
        output_dir / "other_000123.png",
        output_dir / "notes.txt",
    ]
    matching_frame.write_bytes(b"old frame")
    for path in unrelated_files:
        path.write_bytes(b"keep")
    backend = FakeBackend()

    export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            output_dir=output_dir,
            overwrite=True,
        ),
        backend,
    )

    assert not matching_frame.exists()
    assert all(path.exists() for path in unrelated_files)


def test_export_removes_percent_stem_frames_when_overwriting(
    tmp_path: Path,
) -> None:
    video_path = create_video(tmp_path / "clip%done.mp4")
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    matching_frame = output_dir / "clip%done_000001.png"
    unrelated_files = [
        output_dir / "clip%%done_000001.png",
        output_dir / "clip%done_000001.jpg",
        output_dir / "clip%done_123.png",
        output_dir / "other_000001.png",
    ]
    matching_frame.write_bytes(b"old frame")
    for path in unrelated_files:
        path.write_bytes(b"keep")
    backend = FakeBackend()

    result = export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            output_dir=output_dir,
            overwrite=True,
        ),
        backend,
    )

    output_pattern = output_dir / "clip%%done_%06d.png"
    assert result.output_pattern == output_pattern
    assert backend.commands[0][-1] == str(output_pattern)
    assert not matching_frame.exists()
    assert all(path.exists() for path in unrelated_files)


def test_export_keeps_non_ascii_digit_frames_when_overwriting(
    tmp_path: Path,
) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    fullwidth_digits_frame = output_dir / "demo_１２３４５６.png"
    fullwidth_digits_frame.write_bytes(b"keep")
    backend = FakeBackend()

    export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            output_dir=output_dir,
            overwrite=True,
        ),
        backend,
    )

    assert fullwidth_digits_frame.exists()


def test_export_rejects_missing_input_video(tmp_path: Path) -> None:
    backend = FakeBackend()

    with pytest.raises(VideoFrameError, match="Input video does not exist"):
        export_video_frames(
            VideoFrameOptions(input_path=tmp_path / "missing.mp4"), backend
        )


def test_export_rejects_input_path_that_is_not_file(tmp_path: Path) -> None:
    backend = FakeBackend()

    with pytest.raises(VideoFrameError, match="Input path is not a file"):
        export_video_frames(VideoFrameOptions(input_path=tmp_path), backend)


def test_export_rejects_non_empty_output_dir_without_overwrite(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    (output_dir / "old.png").write_bytes(b"old")
    backend = FakeBackend()

    with pytest.raises(VideoFrameError, match="Output directory is not empty"):
        export_video_frames(
            VideoFrameOptions(input_path=video_path, output_dir=output_dir),
            backend,
        )


def test_export_maps_backend_failure_to_exit_code_one_with_stderr(
    tmp_path: Path,
) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend(
        run_result=BackendResult(
            returncode=7,
            stderr="encoder failed while writing frames\n",
        )
    )

    with pytest.raises(VideoFrameError, match="encoder failed") as exc_info:
        export_video_frames(VideoFrameOptions(input_path=video_path), backend)

    assert exc_info.value.exit_code == 1
    assert "FFmpeg failed with exit code 7" in str(exc_info.value)
    assert "encoder failed while writing frames" in str(exc_info.value)


def test_export_rejects_unsupported_image_format(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend()

    with pytest.raises(VideoFrameError, match="Unsupported output format"):
        export_video_frames(
            VideoFrameOptions(input_path=video_path, image_format="gif"),
            backend,
        )


def test_export_rejects_output_path_that_is_not_directory(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    output_path = tmp_path / "frames"
    output_path.write_text("not a directory")
    backend = FakeBackend()

    with pytest.raises(VideoFrameError, match="Output path is not a directory"):
        export_video_frames(
            VideoFrameOptions(input_path=video_path, output_dir=output_path),
            backend,
        )
