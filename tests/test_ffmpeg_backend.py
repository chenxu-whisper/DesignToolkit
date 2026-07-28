from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

from designtoolkit.ffmpeg_backend import FfmpegBackend
from designtoolkit.video_frames import BackendResult


def test_get_ffmpeg_exe_uses_imageio_ffmpeg(monkeypatch) -> None:
    expected_path = r"C:\tools\ffmpeg.exe"
    calls: list[None] = []

    def fake_get_ffmpeg_exe() -> str:
        calls.append(None)
        return expected_path

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", fake_get_ffmpeg_exe)

    assert FfmpegBackend().get_ffmpeg_exe() == expected_path
    assert calls == [None]


def test_get_duration_seconds_uses_imageio_ffmpeg_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "input.mp4"
    calls: list[str] = []

    def fake_count_frames_and_secs(path: str) -> tuple[int, float]:
        calls.append(path)
        return 240, 10.0

    monkeypatch.setattr(
        imageio_ffmpeg,
        "count_frames_and_secs",
        fake_count_frames_and_secs,
    )

    assert FfmpegBackend().get_duration_seconds(video_path) == 10.0
    assert calls == [str(video_path)]


def test_get_duration_seconds_returns_none_when_probe_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "input.mp4"
    calls: list[str] = []

    def fake_count_frames_and_secs(path: str) -> tuple[int, float]:
        calls.append(path)
        raise RuntimeError("probe failed")

    monkeypatch.setattr(
        imageio_ffmpeg,
        "count_frames_and_secs",
        fake_count_frames_and_secs,
    )

    assert FfmpegBackend().get_duration_seconds(video_path) is None
    assert calls == [str(video_path)]


def test_run_invokes_subprocess_run_and_returns_backend_result(monkeypatch) -> None:
    command = ("ffmpeg", "-version")
    calls: list[dict[str, object]] = []

    def fake_run(
        received_command: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "command": received_command,
                "capture_output": capture_output,
                "text": text,
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
            "check": False,
        }
    ]
    assert result == BackendResult(returncode=7, stderr="ffmpeg failed\n")
