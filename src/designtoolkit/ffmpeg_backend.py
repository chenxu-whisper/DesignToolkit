from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import imageio_ffmpeg

from designtoolkit.video_frames import BackendResult


class FfmpegBackend:
    def get_ffmpeg_exe(self) -> str:
        return imageio_ffmpeg.get_ffmpeg_exe()

    def get_duration_seconds(self, video_path: Path) -> float | None:
        try:
            _, duration_seconds = imageio_ffmpeg.count_frames_and_secs(str(video_path))
        except Exception:
            return None
        return float(duration_seconds)

    def run(self, command: Sequence[str]) -> BackendResult:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
        return BackendResult(
            returncode=completed.returncode,
            stderr=completed.stderr or "",
        )
