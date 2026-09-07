from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from importlib import resources
from os import name as os_name
from pathlib import Path
from shutil import which

import imageio_ffmpeg

from designtoolkit.video_frames.core import BackendResult, BackendUnavailableError
from designtoolkit.web.output_limits import OutputBudgetGuard, OutputTargetMonitor

PopenFactory = Callable[..., subprocess.Popen[str]]


def _bundled_binary_dir() -> Path:
    return Path(resources.files("imageio_ffmpeg.binaries"))


def _is_relative_to(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _file_size_limited_command(
    command: Sequence[str],
    max_bytes: int | None,
) -> list[str]:
    if os_name != "posix" or max_bytes is None:
        return list(command)
    prlimit = which("prlimit")
    if prlimit is None:
        return list(command)
    return [prlimit, f"--fsize={max_bytes}", "--", *command]


class FfmpegBackend:
    def __init__(
        self,
        *,
        output_guard: OutputBudgetGuard | Callable[[], None] | None = None,
        poll_interval: float = 0.25,
        popen: PopenFactory = subprocess.Popen,
        max_file_bytes: int | None = None,
    ) -> None:
        self._output_guard = output_guard
        self._poll_interval = poll_interval
        self._popen = popen
        self._max_file_bytes = max_file_bytes

    def get_ffmpeg_exe(self) -> str:
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise BackendUnavailableError(
                "Bundled FFmpeg could not be located."
            ) from exc

        ffmpeg_path = Path(ffmpeg_exe)
        if not ffmpeg_path.is_absolute():
            raise BackendUnavailableError(
                "Bundled FFmpeg is required, but imageio_ffmpeg returned "
                f"a non-absolute executable path: {ffmpeg_exe}"
            )

        try:
            resolved_ffmpeg_exe = ffmpeg_path.resolve()
            bundled_dir = _bundled_binary_dir().resolve()
        except Exception as exc:
            raise BackendUnavailableError(
                "Bundled FFmpeg could not be located."
            ) from exc

        if not resolved_ffmpeg_exe.is_file():
            raise BackendUnavailableError(
                "Bundled FFmpeg is required, but imageio_ffmpeg returned "
                f"a missing executable: {ffmpeg_exe}"
            )

        if not _is_relative_to(resolved_ffmpeg_exe, bundled_dir):
            raise BackendUnavailableError(
                "Bundled FFmpeg is required, but imageio_ffmpeg returned "
                f"an external executable: {ffmpeg_exe}"
            )

        return str(resolved_ffmpeg_exe)

    def get_duration_seconds(self, video_path: Path) -> float | None:
        try:
            self.get_ffmpeg_exe()
            _, duration_seconds = imageio_ffmpeg.count_frames_and_secs(str(video_path))
        except Exception:
            return None
        return float(duration_seconds)

    def run(self, command: Sequence[str]) -> BackendResult:
        monitor = (
            OutputTargetMonitor(self._output_guard, command[-1])
            if isinstance(self._output_guard, OutputBudgetGuard)
            else None
        )
        popen_kwargs: dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        process = self._popen(
            _file_size_limited_command(command, self._max_file_bytes),
            **popen_kwargs,
        )
        while True:
            try:
                _stdout, stderr = process.communicate(timeout=self._poll_interval)
            except subprocess.TimeoutExpired:
                if self._output_guard is None:
                    continue
                try:
                    if monitor is not None:
                        monitor.poll()
                    else:
                        self._output_guard()
                except Exception:
                    self._stop_process(process)
                    raise
                continue
            if monitor is not None:
                monitor.finish()
            return BackendResult(
                returncode=process.returncode if process.returncode is not None else 1,
                stderr=stderr or "",
            )

    def _stop_process(self, process: subprocess.Popen[str]) -> None:
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self._poll_interval)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                return
            try:
                process.wait()
            except ProcessLookupError:
                return
