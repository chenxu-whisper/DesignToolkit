from __future__ import annotations

import base64
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from importlib import resources
from os import name as os_name
from pathlib import Path
from shutil import which
from typing import Protocol

import imageio_ffmpeg

from designtoolkit.video_frames.core import BackendResult, BackendUnavailableError
from designtoolkit.web.output_limits import OutputBudgetGuard, OutputTargetMonitor

PopenFactory = Callable[..., subprocess.Popen[str]]
_LOGGER = logging.getLogger(__name__)
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_WINDOWS_LAUNCHER_CODE = """\
import base64
import ctypes
import json
import subprocess
import sys
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
open_event = kernel32.OpenEventW
open_event.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
open_event.restype = wintypes.HANDLE
wait_for_single_object = kernel32.WaitForSingleObject
wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
wait_for_single_object.restype = wintypes.DWORD
close_handle = kernel32.CloseHandle
close_handle.argtypes = [wintypes.HANDLE]
close_handle.restype = wintypes.BOOL

event_handle = open_event(0x00100000, False, sys.argv[1])
if not event_handle:
    raise SystemExit(125)
try:
    if wait_for_single_object(event_handle, 0xFFFFFFFF) != 0:
        raise SystemExit(125)
finally:
    close_handle(event_handle)

command = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
raise SystemExit(subprocess.Popen(command, shell=False).wait())
"""


class _WindowsKernelApiProtocol(Protocol):
    def create_event(self, name: str) -> object: ...

    def create_job(self) -> object: ...

    def set_job_kill_on_close(self, job_handle: object) -> None: ...

    def assign_process(
        self,
        job_handle: object,
        process_handle: object,
    ) -> None: ...

    def set_event(self, event_handle: object) -> None: ...

    def close_handle(self, handle: object) -> None: ...


class _WindowsKernelApi:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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

        self._extended_limit_information = ExtendedLimitInformation
        self._configure_functions(wintypes)

    def _configure_functions(self, wintypes: object) -> None:
        ctypes = self._ctypes
        kernel32 = self._kernel32
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def _raise_last_error(self) -> None:
        raise self._ctypes.WinError(self._ctypes.get_last_error())

    def create_event(self, name: str) -> object:
        handle = self._kernel32.CreateEventW(None, False, False, name)
        if not handle:
            self._raise_last_error()
        return handle

    def create_job(self) -> object:
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            self._raise_last_error()
        return handle

    def set_job_kill_on_close(self, job_handle: object) -> None:
        limits = self._extended_limit_information()
        limits.basic_limit_information.limit_flags = 0x00002000
        if not self._kernel32.SetInformationJobObject(
            job_handle,
            9,
            self._ctypes.byref(limits),
            self._ctypes.sizeof(limits),
        ):
            self._raise_last_error()

    def assign_process(
        self,
        job_handle: object,
        process_handle: object,
    ) -> None:
        if not self._kernel32.AssignProcessToJobObject(
            job_handle,
            process_handle,
        ):
            self._raise_last_error()

    def set_event(self, event_handle: object) -> None:
        if not self._kernel32.SetEvent(event_handle):
            self._raise_last_error()

    def close_handle(self, handle: object) -> None:
        self._kernel32.CloseHandle(handle)


def _windows_launcher_command(
    event_name: str,
    command: Sequence[str],
) -> list[str]:
    payload = base64.b64encode(
        json.dumps(list(command), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return [sys.executable, "-c", _WINDOWS_LAUNCHER_CODE, event_name, payload]


class _WindowsLaunchGate:
    def __init__(
        self,
        *,
        api: _WindowsKernelApiProtocol,
        event_handle: object,
        launcher_command: list[str],
    ) -> None:
        self._api = api
        self._event_handle = event_handle
        self._job_handle: object | None = None
        self._released = False
        self.launcher_command = launcher_command

    @classmethod
    def create(
        cls,
        command: Sequence[str],
        *,
        api: _WindowsKernelApiProtocol | None = None,
    ) -> _WindowsLaunchGate:
        kernel_api = api or _WindowsKernelApi()
        event_name = f"Local\\DesignToolkit.FFmpeg.{uuid.uuid4().hex}"
        event_handle = kernel_api.create_event(event_name)
        return cls(
            api=kernel_api,
            event_handle=event_handle,
            launcher_command=_windows_launcher_command(event_name, command),
        )

    def claim_and_release(self, process: subprocess.Popen[str]) -> None:
        if self._released:
            raise RuntimeError("Windows FFmpeg launch gate was already released")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise RuntimeError("Windows launcher process handle is unavailable")
        job_handle = self._api.create_job()
        self._job_handle = job_handle
        self._api.set_job_kill_on_close(job_handle)
        self._api.assign_process(job_handle, process_handle)
        self._api.set_event(self._event_handle)
        self._released = True

    def close(self) -> None:
        job_handle = self._job_handle
        if job_handle is not None:
            self._job_handle = None
            self._api.close_handle(job_handle)
        event_handle = self._event_handle
        if event_handle is not None:
            self._event_handle = None
            self._api.close_handle(event_handle)


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
        windows_launch_gate_factory: (
            Callable[[Sequence[str]], _WindowsLaunchGate] | None
        ) = None,
    ) -> None:
        self._output_guard = output_guard
        self._poll_interval = poll_interval
        self._popen = popen
        self._max_file_bytes = max_file_bytes
        self._windows_launch_gate_factory = (
            windows_launch_gate_factory or _WindowsLaunchGate.create
        )

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
        gated_command = _file_size_limited_command(command, self._max_file_bytes)
        process_tree_owner: _WindowsLaunchGate | None = None
        if os_name == "nt":
            try:
                process_tree_owner = self._windows_launch_gate_factory(gated_command)
            except Exception as exc:
                raise BackendUnavailableError(
                    "Windows FFmpeg launch containment could not be established."
                ) from exc
            try:
                process = self._popen(
                    process_tree_owner.launcher_command,
                    **popen_kwargs,
                )
            except BaseException:
                process_tree_owner.close()
                raise
            try:
                process_tree_owner.claim_and_release(process)
            except Exception as exc:
                self._abort_windows_launch(process, process_tree_owner)
                raise BackendUnavailableError(
                    "Windows FFmpeg launch containment could not be established."
                ) from exc
        else:
            process = self._popen(gated_command, **popen_kwargs)
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
        process.terminate()

    def _kill_process_tree(self, process: subprocess.Popen[str]) -> None:
        pid = getattr(process, "pid", None)
        if os_name == "posix" and pid is not None:
            os.killpg(pid, _POSIX_SIGKILL)
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
        process_tree_owner: _WindowsLaunchGate | None = None,
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
            if process_tree_owner is not None:
                process_tree_owner.close()
            if not self._wait_for_process(process):
                try:
                    process.kill()
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

    @classmethod
    def _abort_windows_launch(
        cls,
        process: subprocess.Popen[str],
        process_tree_owner: _WindowsLaunchGate,
    ) -> None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        finally:
            process_tree_owner.close()
        if cls._wait_for_process(process):
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        cls._wait_for_process(process)
