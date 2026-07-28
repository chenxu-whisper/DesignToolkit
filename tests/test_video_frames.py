from pathlib import Path

import pytest

from designtoolkit.video_frames import (
    VideoFrameError,
    default_output_dir,
    parse_size,
)


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
