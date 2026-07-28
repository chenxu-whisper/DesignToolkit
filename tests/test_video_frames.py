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
    def __init__(self, duration_seconds: float | None = 10.0) -> None:
        self.duration_seconds = duration_seconds
        self.commands: list[list[str]] = []

    def get_ffmpeg_exe(self) -> str:
        return "ffmpeg"

    def get_duration_seconds(self, video_path: Path) -> float | None:
        return self.duration_seconds

    def run(self, command: Sequence[str]) -> BackendResult:
        self.commands.append(list(command))
        return BackendResult(returncode=0)


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
