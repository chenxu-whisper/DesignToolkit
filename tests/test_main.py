from __future__ import annotations

from pathlib import Path

import imageio_ffmpeg

from designtoolkit.ffmpeg_backend import FfmpegBackend
from designtoolkit.main import main
from designtoolkit.video_frames import ExportResult, VideoFrameError, VideoFrameOptions


class DummyBackend:
    pass


def test_main_without_subcommand_reports_ready(capsys) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "DesignToolkit is ready.\n"
    assert captured.err == ""


def test_main_help_returns_zero(capsys) -> None:
    exit_code = main(["--help"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "usage:" in captured.out
    assert captured.err == ""


def test_video_to_frames_help_returns_zero_without_backend(capsys) -> None:
    def unavailable_exporter(
        options: VideoFrameOptions,
        backend: DummyBackend,
    ) -> ExportResult:
        raise AssertionError("exporter should not be called for help")

    def unavailable_backend_factory() -> DummyBackend:
        raise AssertionError("backend should not be created for help")

    exit_code = main(
        ["video-to-frames", "--help"],
        exporter=unavailable_exporter,
        backend_factory=unavailable_backend_factory,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    for option in (
        "--format",
        "--output-dir",
        "--size",
        "--fps",
        "--frames",
        "--overwrite",
    ):
        assert option in captured.out
    assert captured.err == ""


def test_video_to_frames_passes_complete_options_to_exporter(
    tmp_path: Path,
    capsys,
) -> None:
    video_path = tmp_path / "input.mp4"
    output_dir = tmp_path / "frames"
    backend = DummyBackend()
    calls: list[tuple[VideoFrameOptions, DummyBackend]] = []

    def exporter(
        options: VideoFrameOptions,
        received_backend: DummyBackend,
    ) -> ExportResult:
        calls.append((options, received_backend))
        return ExportResult(
            output_dir=output_dir,
            output_pattern=output_dir / "frame_%06d.jpg",
            command=("ffmpeg",),
        )

    exit_code = main(
        [
            "video-to-frames",
            str(video_path),
            "--output-dir",
            str(output_dir),
            "--format",
            "jpg",
            "--size",
            "1920x1080",
            "--fps",
            "12.5",
            "--overwrite",
        ],
        exporter=exporter,
        backend_factory=lambda: backend,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"Exported frames to {output_dir}\n"
    assert captured.err == ""
    assert calls == [
        (
            VideoFrameOptions(
                input_path=video_path,
                output_dir=output_dir,
                image_format="jpg",
                size=(1920, 1080),
                fps=12.5,
                frames=None,
                overwrite=True,
            ),
            backend,
        )
    ]


def test_video_to_frames_sets_frame_count(tmp_path: Path, capsys) -> None:
    video_path = tmp_path / "input.mp4"
    backend = DummyBackend()
    observed_options: list[VideoFrameOptions] = []

    def exporter(options: VideoFrameOptions, received_backend: DummyBackend):
        assert received_backend is backend
        observed_options.append(options)
        return ExportResult(
            output_dir=tmp_path / "frames",
            output_pattern=tmp_path / "frames" / "frame_%06d.png",
            command=("ffmpeg",),
        )

    exit_code = main(
        ["video-to-frames", str(video_path), "--frames", "120"],
        exporter=exporter,
        backend_factory=lambda: backend,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert observed_options[0].frames == 120


def test_video_to_frames_uses_ffmpeg_backend_by_default(
    tmp_path: Path,
    capsys,
) -> None:
    video_path = tmp_path / "input.mp4"
    output_dir = tmp_path / "frames"
    received_backends: list[FfmpegBackend] = []

    def fake_exporter(
        options: VideoFrameOptions,
        backend: FfmpegBackend,
    ) -> ExportResult:
        assert options.input_path == video_path
        assert isinstance(backend, FfmpegBackend)
        received_backends.append(backend)
        return ExportResult(
            output_dir=output_dir,
            output_pattern=output_dir / "frame_%06d.png",
            command=("ffmpeg",),
        )

    exit_code = main(["video-to-frames", str(video_path)], exporter=fake_exporter)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"Exported frames to {output_dir}\n"
    assert captured.err == ""
    assert len(received_backends) == 1


def test_video_to_frames_default_backend_reports_bundled_ffmpeg_unavailable(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    video_path = tmp_path / "input.mp4"
    video_path.write_bytes(b"video")

    def fake_get_ffmpeg_exe() -> str:
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", fake_get_ffmpeg_exe)

    exit_code = main(["video-to-frames", str(video_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Bundled FFmpeg" in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "input_frames").exists()


def test_video_to_frames_returns_video_frame_error_exit_code(
    tmp_path: Path,
    capsys,
) -> None:
    video_path = tmp_path / "input.mp4"

    def exporter(options: VideoFrameOptions, backend: DummyBackend) -> ExportResult:
        raise VideoFrameError("bad input", exit_code=2)

    exit_code = main(
        ["video-to-frames", str(video_path)],
        exporter=exporter,
        backend_factory=DummyBackend,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: bad input\n"


def test_video_to_frames_rejects_bad_size_before_exporter(
    tmp_path: Path,
    capsys,
) -> None:
    video_path = tmp_path / "input.mp4"
    exporter_called = False

    def exporter(options: VideoFrameOptions, backend: DummyBackend) -> ExportResult:
        nonlocal exporter_called
        exporter_called = True
        return ExportResult(
            output_dir=tmp_path / "frames",
            output_pattern=tmp_path / "frames" / "frame_%06d.png",
            command=("ffmpeg",),
        )

    exit_code = main(
        ["video-to-frames", str(video_path), "--size", "bad"],
        exporter=exporter,
        backend_factory=DummyBackend,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "Invalid size 'bad'" in captured.err
    assert not exporter_called
