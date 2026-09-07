from __future__ import annotations

import logging
import os
import signal
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
_LOGGER = logging.getLogger(__name__)
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0


class _WindowsJob:
    def __init__(
        self,
        handle: object,
        close_handle: Callable[[object], int],
    ) -> None:
        self._handle = handle
        self._close_handle = close_handle

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        self._close_handle(handle)


def _create_windows_job(
    process: subprocess.Popen[str],
) -> _WindowsJob | None:
    if os_name != "nt":
        return None
    process_handle = getattr(process, "_handle", None)
    if process_handle is None:
        return None

    import ctypes
    from ctypes import wintypes

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x00002000

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_job_information = kernel32.SetInformationJobObject
    set_job_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_job_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    job_handle = create_job(None, None)
    if not job_handle:
        return None
    owner = _WindowsJob(job_handle, close_handle)
    try:
        limits = ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            job_object_limit_kill_on_job_close
        )
        if not set_job_information(
            job_handle,
            job_object_extended_limit_information,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        if not assign_process(job_handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError:
        owner.close()
        return None
    return owner


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
        if os_name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os_name == "nt":
            popen_kwargs["creationflags"] = _WINDOWS_NEW_PROCESS_GROUP
        process = self._popen(
            _file_size_limited_command(command, self._max_file_bytes),
            **popen_kwargs,
        )
        process_tree_owner = _create_windows_job(process)
        try:
            while True:
                try:
                    _stdout, stderr = process.communicate(
                        timeout=self._poll_interval
                    )
                except subprocess.TimeoutExpired:
                    if self._output_guard is None:
                        continue
                    if monitor is not None:
                        monitor.poll()
                    else:
                        self._output_guard()
                    continue
                if monitor is not None:
                    monitor.finish()
                return BackendResult(
                    returncode=(
                        process.returncode if process.returncode is not None else 1
                    ),
                    stderr=stderr or "",
                )
        except BaseException:
            try:
                self._stop_process(process, process_tree_owner)
            except BaseException:
                try:
                    _LOGGER.exception(
                        "Failed to stop FFmpeg after worker interruption"
                    )
                except BaseException:
                    pass
            raise
        finally:
            if process_tree_owner is not None:
                try:
                    process_tree_owner.close()
                except BaseException:
                    try:
                        _LOGGER.exception("Failed to close FFmpeg Windows Job")
                    except BaseException:
                        pass

    @staticmethod
    def _process_is_running(process: subprocess.Popen[str]) -> bool:
        poll = getattr(process, "poll", None)
        if poll is not None:
            return poll() is None
        return process.returncode is None

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        pid = getattr(process, "pid", None)
        if os_name == "posix" and pid is not None:
            os.killpg(pid, signal.SIGTERM)
            return
        if os_name == "nt" and pid is not None:
            send_signal = getattr(process, "send_signal", None)
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if send_signal is not None and ctrl_break is not None:
                try:
                    send_signal(ctrl_break)
                    return
                except OSError:
                    pass
        process.terminate()

    def _kill_process_tree(self, process: subprocess.Popen[str]) -> None:
        pid = getattr(process, "pid", None)
        if os_name == "posix" and pid is not None:
            os.killpg(pid, _POSIX_SIGKILL)
            return
        if os_name == "nt" and pid is not None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            if not self._process_is_running(process):
                return
        process.kill()

    @staticmethod
    def _wait_for_process(process: subprocess.Popen[str]) -> bool:
        try:
            process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except ProcessLookupError:
            return True
        except subprocess.TimeoutExpired:
            return False
        return True

    def _stop_process(
        self,
        process: subprocess.Popen[str],
        process_tree_owner: _WindowsJob | None = None,
    ) -> None:
        pid = getattr(process, "pid", None)
        if os_name == "posix" and pid is not None:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._wait_for_process(process)
            try:
                os.killpg(pid, _POSIX_SIGKILL)
            except ProcessLookupError:
                pass
            self._wait_for_process(process)
            return

        if os_name == "nt":
            if self._process_is_running(process):
                try:
                    self._terminate_process_tree(process)
                except ProcessLookupError:
                    pass
            self._wait_for_process(process)
            if process_tree_owner is not None:
                process_tree_owner.close()
            elif pid is not None or self._process_is_running(process):
                try:
                    self._kill_process_tree(process)
                except ProcessLookupError:
                    pass
            self._wait_for_process(process)
            return

        if not self._process_is_running(process):
            return
        try:
            self._terminate_process_tree(process)
        except ProcessLookupError:
            return
        if not self._wait_for_process(process):
            try:
                self._kill_process_tree(process)
            except ProcessLookupError:
                return
            self._wait_for_process(process)
