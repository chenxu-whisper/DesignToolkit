from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest

from designtoolkit import ffmpeg_backend
from designtoolkit.ffmpeg_backend import FfmpegBackend
from designtoolkit.video_frames import BackendResult


def test_get_ffmpeg_exe_uses_imageio_ffmpeg(monkeypatch, tmp_path: Path) -> None:
    bundled_dir = tmp_path / "imageio_ffmpeg" / "binaries"
    bundled_dir.mkdir(parents=True)
    expected_path = str(bundled_dir / "ffmpeg.exe")
    calls: list[None] = []

    def fake_get_ffmpeg_exe() -> str:
        calls.append(None)
        return expected_path

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", fake_get_ffmpeg_exe)
    monkeypatch.setattr(
        ffmpeg_backend,
        "_bundled_binary_dir",
        lambda: bundled_dir,
        raising=False,
    )

    assert FfmpegBackend().get_ffmpeg_exe() == expected_path
    assert calls == [None]


@pytest.mark.parametrize(
    "returned_path",
    [
        pytest.param("ffmpeg", id="system-path-lookup"),
        pytest.param("external-absolute", id="external-absolute-path"),
    ],
)
def test_get_ffmpeg_exe_rejects_external_ffmpeg(
    monkeypatch,
    tmp_path: Path,
    returned_path: str,
) -> None:
    bundled_dir = tmp_path / "imageio_ffmpeg" / "binaries"
    bundled_dir.mkdir(parents=True)
    external_path = tmp_path / "external" / "ffmpeg.exe"
    imageio_path = "ffmpeg" if returned_path == "ffmpeg" else str(external_path)

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: imageio_path)
    monkeypatch.setattr(
        ffmpeg_backend,
        "_bundled_binary_dir",
        lambda: bundled_dir,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="Bundled FFmpeg"):
        FfmpegBackend().get_ffmpeg_exe()


def test_get_duration_seconds_uses_imageio_ffmpeg_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "input.mp4"
    preflight_calls: list[FfmpegBackend] = []
    calls: list[str] = []

    def fake_get_ffmpeg_exe(backend: FfmpegBackend) -> str:
        preflight_calls.append(backend)
        return "bundled-ffmpeg"

    def fake_count_frames_and_secs(path: str) -> tuple[int, float]:
        calls.append(path)
        return 240, 10.0

    monkeypatch.setattr(FfmpegBackend, "get_ffmpeg_exe", fake_get_ffmpeg_exe)
    monkeypatch.setattr(
        imageio_ffmpeg,
        "count_frames_and_secs",
        fake_count_frames_and_secs,
    )

    backend = FfmpegBackend()

    assert backend.get_duration_seconds(video_path) == 10.0
    assert preflight_calls == [backend]
    assert calls == [str(video_path)]


def test_get_duration_seconds_returns_none_when_probe_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "input.mp4"
    preflight_calls: list[FfmpegBackend] = []
    calls: list[str] = []

    def fake_get_ffmpeg_exe(backend: FfmpegBackend) -> str:
        preflight_calls.append(backend)
        return "bundled-ffmpeg"

    def fake_count_frames_and_secs(path: str) -> tuple[int, float]:
        calls.append(path)
        raise RuntimeError("probe failed")

    monkeypatch.setattr(FfmpegBackend, "get_ffmpeg_exe", fake_get_ffmpeg_exe)
    monkeypatch.setattr(
        imageio_ffmpeg,
        "count_frames_and_secs",
        fake_count_frames_and_secs,
    )

    backend = FfmpegBackend()

    assert backend.get_duration_seconds(video_path) is None
    assert preflight_calls == [backend]
    assert calls == [str(video_path)]


def test_get_duration_seconds_returns_none_without_probe_for_external_ffmpeg(
    monkeypatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "input.mp4"
    calls: list[str] = []

    def fake_get_ffmpeg_exe(backend: FfmpegBackend) -> str:
        raise RuntimeError("Bundled FFmpeg is unavailable")

    def fake_count_frames_and_secs(path: str) -> tuple[int, float]:
        calls.append(path)
        return 240, 10.0

    monkeypatch.setattr(FfmpegBackend, "get_ffmpeg_exe", fake_get_ffmpeg_exe)
    monkeypatch.setattr(
        imageio_ffmpeg,
        "count_frames_and_secs",
        fake_count_frames_and_secs,
    )

    assert FfmpegBackend().get_duration_seconds(video_path) is None
    assert calls == []


def test_run_invokes_subprocess_run_and_returns_backend_result(monkeypatch) -> None:
    command = ("ffmpeg", "-version")
    calls: list[dict[str, object]] = []

    def fake_run(
        received_command: list[str],
        *,
        capture_output: bool,
        text: bool,
        encoding: str | None = None,
        errors: str | None = None,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "command": received_command,
                "capture_output": capture_output,
                "text": text,
                "encoding": encoding,
                "errors": errors,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(
            args=received_command,
            returncode=7,
            stderr="ffmpeg failed\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = FfmpegBackend().run(command)

    assert calls == [
        {
            "command": ["ffmpeg", "-version"],
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        }
    ]
    assert result == BackendResult(returncode=7, stderr="ffmpeg failed\n")
