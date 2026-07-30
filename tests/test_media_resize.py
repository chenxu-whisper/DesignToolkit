from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import designtoolkit.media_resize as media_resize
from designtoolkit.media_resize import (
    MediaResizeError,
    MediaResizeOptions,
    ResizeBackendResult,
    default_output_path,
    resize_media,
)
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend


class FakeResizeBackend:
    def __init__(
        self,
        run_result: ResizeBackendResult | None = None,
        ffmpeg_error: Exception | None = None,
    ) -> None:
        self.run_result = run_result or ResizeBackendResult(returncode=0)
        self.ffmpeg_error = ffmpeg_error
        self.commands: list[list[str]] = []

    def get_ffmpeg_exe(self) -> str:
        if self.ffmpeg_error is not None:
            raise self.ffmpeg_error
        return "ffmpeg"

    def run(self, command: Sequence[str]) -> ResizeBackendResult:
        self.commands.append(list(command))
        return self.run_result


def create_media(path: Path) -> Path:
    path.write_bytes(b"media")
    return path


def backend_unavailable_error_type() -> type[Exception]:
    error_type = getattr(media_resize, "BackendUnavailableError", None)
    assert error_type is not None, "BackendUnavailableError should be exported"
    assert issubclass(error_type, RuntimeError)
    return error_type


def test_default_output_path_uses_input_parent_stem_and_suffix(tmp_path: Path) -> None:
    input_path = tmp_path / "demo.mp4"

    assert default_output_path(input_path) == tmp_path / "demo_resized.mp4"


def test_resize_uses_default_output_and_aspect_ratio_scale_filter(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend()

    result = resize_media(
        MediaResizeOptions(input_path=input_path, size=(800, 600)),
        backend,
    )

    output_path = tmp_path / "demo_resized.mp4"
    assert result.output_path == output_path
    assert result.command == tuple(backend.commands[0])
    assert backend.commands == [
        [
            "ffmpeg",
            "-hide_banner",
            "-n",
            "-i",
            str(input_path),
            "-vf",
            "scale=800:600:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2",
            "-map",
            "0",
            "-c:a",
            "copy",
            str(output_path),
        ]
    ]


def test_resize_uses_explicit_output_path(tmp_path: Path) -> None:
    input_path = create_media(tmp_path / "photo.png")
    output_path = tmp_path / "small.png"
    backend = FakeResizeBackend()

    result = resize_media(
        MediaResizeOptions(
            input_path=input_path,
            size=(320, 240),
            output_path=output_path,
        ),
        backend,
    )

    assert result.output_path == output_path
    assert backend.commands[0][-1] == str(output_path)


def test_resize_rejects_image_input_with_video_output_suffix(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "photo.png")
    output_path = tmp_path / "photo.mp4"
    backend = FakeResizeBackend()

    with pytest.raises(
        MediaResizeError,
        match="Output media type must match input media type",
    ) as exc:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                size=(320, 240),
                output_path=output_path,
            ),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_rejects_video_input_with_image_output_suffix(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "clip.mp4")
    output_path = tmp_path / "clip.png"
    backend = FakeResizeBackend()

    with pytest.raises(
        MediaResizeError,
        match="Output media type must match input media type",
    ) as exc:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                size=(320, 240),
                output_path=output_path,
            ),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_allows_explicit_image_output_in_different_image_format(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "photo.png")
    output_path = tmp_path / "photo.jpg"
    backend = FakeResizeBackend()

    result = resize_media(
        MediaResizeOptions(
            input_path=input_path,
            size=(320, 240),
            output_path=output_path,
        ),
        backend,
    )

    assert result.output_path == output_path
    assert backend.commands[0][-1] == str(output_path)


def test_resize_allows_explicit_video_output_in_different_video_format(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "clip.mp4")
    output_path = tmp_path / "clip.mov"
    backend = FakeResizeBackend()

    result = resize_media(
        MediaResizeOptions(
            input_path=input_path,
            size=(320, 240),
            output_path=output_path,
        ),
        backend,
    )

    assert result.output_path == output_path
    assert backend.commands[0][-1] == str(output_path)


def test_resize_h264_video_with_odd_bounding_box_uses_encoder_safe_dimensions(
    tmp_path: Path,
) -> None:
    backend = FfmpegBackend()
    ffmpeg_exe = backend.get_ffmpeg_exe()
    input_path = tmp_path / "source.mp4"
    output_path = tmp_path / "out.mp4"

    created = subprocess.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=1",
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(input_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert created.returncode == 0, created.stderr

    result = resize_media(
        MediaResizeOptions(
            input_path=input_path,
            size=(101, 100),
            output_path=output_path,
        ),
        backend,
    )

    assert result.output_path == output_path
    assert output_path.is_file()


def test_resize_rejects_missing_input(tmp_path: Path) -> None:
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Input media does not exist") as exc:
        resize_media(
            MediaResizeOptions(input_path=tmp_path / "missing.mp4", size=(800, 600)),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_rejects_input_path_that_is_not_file(tmp_path: Path) -> None:
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Input path is not a file") as exc:
        resize_media(MediaResizeOptions(input_path=tmp_path, size=(800, 600)), backend)

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_rejects_existing_output_path(tmp_path: Path) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    output_path = create_media(tmp_path / "demo_resized.mp4")
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Output path already exists") as exc:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                size=(800, 600),
                output_path=output_path,
            ),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_rejects_output_path_equal_to_input_path(tmp_path: Path) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Output path must differ") as exc:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                size=(800, 600),
                output_path=input_path,
            ),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_maps_backend_unavailable_before_running_ffmpeg(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend(
        ffmpeg_error=backend_unavailable_error_type()("Bundled FFmpeg unavailable")
    )

    with pytest.raises(MediaResizeError, match="Bundled FFmpeg unavailable") as exc:
        resize_media(
            MediaResizeOptions(input_path=input_path, size=(800, 600)), backend
        )

    assert exc.value.exit_code == 1
    assert backend.commands == []


def test_resize_maps_ffmpeg_failure_to_exit_code_one_with_stderr(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend(
        run_result=ResizeBackendResult(
            returncode=7,
            stderr="resize failed\n",
        )
    )

    with pytest.raises(MediaResizeError, match="resize failed") as exc:
        resize_media(
            MediaResizeOptions(input_path=input_path, size=(800, 600)), backend
        )

    assert exc.value.exit_code == 1
    assert "FFmpeg failed with exit code 7" in str(exc.value)
    assert "resize failed" in str(exc.value)
