from __future__ import annotations

import math
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import designtoolkit.video_frames as video_frames
from designtoolkit.video_frames import (
    BackendResult,
    VideoFrameError,
    VideoFrameOptions,
    default_output_dir,
    export_video_frames,
    parse_size,
)
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend


class FakeBackend:
    def __init__(
        self,
        duration_seconds: float | None = 10.0,
        run_result: BackendResult | None = None,
        ffmpeg_error: Exception | None = None,
    ) -> None:
        self.duration_seconds = duration_seconds
        self.run_result = run_result or BackendResult(returncode=0)
        self.ffmpeg_error = ffmpeg_error
        self.commands: list[list[str]] = []

    def get_ffmpeg_exe(self) -> str:
        if self.ffmpeg_error is not None:
            raise self.ffmpeg_error
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


def png_alpha_extrema(ffmpeg_exe: str, image_path: Path) -> tuple[int, int]:
    decoded = subprocess.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-i",
            str(image_path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr.decode("utf-8", errors="replace")
    alpha_values = decoded.stdout[3::4]
    assert alpha_values
    return min(alpha_values), max(alpha_values)


def backend_unavailable_error_type() -> type[Exception]:
    error_type = getattr(video_frames, "BackendUnavailableError", None)
    assert error_type is not None, "BackendUnavailableError should be defined"
    assert issubclass(error_type, RuntimeError)
    return error_type


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

    output_dir = tmp_path / "clip%done_frames"
    output_pattern = Path(str(output_dir).replace("%", "%%")) / "clip%%done_%06d.png"
    assert result.output_dir == output_dir
    assert result.output_pattern == output_pattern
    assert output_dir.is_dir()
    assert backend.commands[0][-1] == str(output_pattern)


def test_export_escapes_percent_in_custom_output_dir_pattern(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    output_dir = tmp_path / "custom%frames"
    backend = FakeBackend()

    result = export_video_frames(
        VideoFrameOptions(input_path=video_path, output_dir=output_dir),
        backend,
    )

    output_pattern = Path(str(output_dir).replace("%", "%%")) / "demo_%06d.png"
    assert result.output_dir == output_dir
    assert result.output_pattern == output_pattern
    assert output_dir.is_dir()
    assert backend.commands[0][-1] == str(output_pattern)


def test_export_uses_requested_format(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "demo.mov")
    backend = FakeBackend()

    result = export_video_frames(
        VideoFrameOptions(input_path=video_path, image_format="jpg"),
        backend,
    )

    assert result.output_pattern == tmp_path / "demo_frames" / "demo_%06d.jpg"


def test_export_webm_to_png_uses_vp9_alpha_decoder_before_input(
    tmp_path: Path,
) -> None:
    video_path = create_video(tmp_path / "transparent.webm")
    backend = FakeBackend()

    export_video_frames(VideoFrameOptions(input_path=video_path), backend)

    command = backend.commands[0]
    assert command[3:7] == ["-c:v", "libvpx-vp9", "-i", str(video_path)]


def test_export_webm_to_jpg_does_not_use_alpha_decoder(tmp_path: Path) -> None:
    video_path = create_video(tmp_path / "transparent.webm")
    backend = FakeBackend()

    export_video_frames(
        VideoFrameOptions(input_path=video_path, image_format="jpg"),
        backend,
    )

    command = backend.commands[0]
    assert "-c:v" not in command
    assert "libvpx-vp9" not in command


def test_export_transparent_webm_to_png_preserves_alpha(tmp_path: Path) -> None:
    video_path = Path("assets/video/test.webm")
    assert video_path.is_file()
    output_dir = tmp_path / "frames"
    backend = FfmpegBackend()
    ffmpeg_exe = backend.get_ffmpeg_exe()

    export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            output_dir=output_dir,
            fps=1.0,
        ),
        backend,
    )

    frame_path = output_dir / "test_000001.png"
    assert frame_path.is_file()
    assert png_alpha_extrema(ffmpeg_exe, frame_path) == (0, 255)


def test_export_vp8_webm_to_png_falls_back_to_automatic_decoder(
    tmp_path: Path,
) -> None:
    backend = FfmpegBackend()
    ffmpeg_exe = backend.get_ffmpeg_exe()
    video_path = tmp_path / "vp8.webm"
    output_dir = tmp_path / "frames"

    created = subprocess.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=16x16:rate=1",
            "-t",
            "1",
            "-c:v",
            "libvpx",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert created.returncode == 0, created.stderr

    result = export_video_frames(
        VideoFrameOptions(
            input_path=video_path,
            output_dir=output_dir,
            fps=1.0,
        ),
        backend,
    )

    assert (output_dir / "vp8_000001.png").is_file()
    assert "-c:v" not in result.command
    assert "libvpx-vp9" not in result.command


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
        ({"fps": -1.0}, "FPS must be greater than 0."),
        ({"frames": 0}, "Frames must be greater than 0."),
        ({"frames": -1}, "Frames must be greater than 0."),
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


@pytest.mark.parametrize("fps", [math.nan, math.inf])
def test_export_rejects_non_finite_fps_before_running_backend(
    tmp_path: Path,
    fps: float,
) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend()

    with pytest.raises(VideoFrameError) as exc_info:
        export_video_frames(VideoFrameOptions(input_path=video_path, fps=fps), backend)

    assert str(exc_info.value) == "FPS must be greater than 0."
    assert exc_info.value.exit_code == 2
    assert backend.commands == []
    assert not (tmp_path / "demo_frames").exists()


@pytest.mark.parametrize("duration_seconds", [None, 0.0, math.nan, math.inf])
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
    assert not (tmp_path / "demo_frames").exists()


@pytest.mark.parametrize("duration_seconds", [None, 0.0, math.nan, math.inf])
def test_export_keeps_existing_frames_when_duration_unavailable_with_overwrite(
    tmp_path: Path,
    duration_seconds: float | None,
) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    existing_frame = output_dir / "demo_000001.png"
    existing_frame.write_bytes(b"old frame")
    backend = FakeBackend(duration_seconds=duration_seconds)

    with pytest.raises(
        VideoFrameError,
        match="Unable to determine video duration for --frames mode.",
    ):
        export_video_frames(
            VideoFrameOptions(
                input_path=video_path,
                output_dir=output_dir,
                frames=120,
                overwrite=True,
            ),
            backend,
        )

    assert existing_frame.exists()
    assert backend.commands == []


def test_export_maps_backend_unavailable_before_output_side_effects(
    tmp_path: Path,
) -> None:
    video_path = create_video(tmp_path / "demo.mp4")
    backend = FakeBackend(
        ffmpeg_error=backend_unavailable_error_type()("Bundled FFmpeg unavailable")
    )

    with pytest.raises(VideoFrameError, match="Bundled FFmpeg unavailable") as exc_info:
        export_video_frames(VideoFrameOptions(input_path=video_path), backend)

    assert exc_info.value.exit_code == 1
    assert backend.commands == []
    assert not (tmp_path / "demo_frames").exists()


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
