from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from os import name as os_name
from pathlib import Path
from shutil import which
from threading import Event, RLock, Thread
from typing import Protocol

import imageio_ffmpeg

from designtoolkit.video_frames.core import BackendResult, BackendUnavailableError
from designtoolkit.web.output_limits import OutputBudgetGuard, OutputTargetMonitor

PopenFactory = Callable[..., subprocess.Popen[str]]
_LOGGER = logging.getLogger(__name__)
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_WINDOWS_TREE_POLL_SECONDS = 0.01
_WINDOWS_TASKKILL_TIMEOUTS_SECONDS = (5.0, 10.0)


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


@dataclass
class _WindowsProcessIdentity:
    pid: int
    parent_pid: int
    creation_time: int
    handle: object


@dataclass(frozen=True)
class _WindowsProcessEntry:
    pid: int
    parent_pid: int


class _WindowsProcessApi(Protocol):
    def snapshot(self) -> list[_WindowsProcessEntry]: ...

    def open_process(self, pid: int) -> _WindowsProcessIdentity | None: ...

    def exit_time(self, identity: _WindowsProcessIdentity) -> int | None: ...

    def is_active(self, identity: _WindowsProcessIdentity) -> bool: ...

    def terminate(self, identity: _WindowsProcessIdentity) -> None: ...

    def wait(
        self,
        identity: _WindowsProcessIdentity,
        timeout_seconds: float,
    ) -> None: ...

    def close(self, identity: _WindowsProcessIdentity) -> None: ...


class _WindowsProcessApiImpl:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        process_terminate = 0x0001
        synchronize = 0x00100000

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._process_access = (
            process_query_limited_information | process_terminate | synchronize
        )

        class ProcessEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        self._process_entry_type = ProcessEntry32
        self._configure_functions()

    def _configure_functions(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32

        kernel32.CreateToolhelp32Snapshot.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(self._process_entry_type),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(self._process_entry_type),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    @staticmethod
    def _filetime_value(filetime: object) -> int:
        return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime

    def _process_times(self, handle: object) -> tuple[int, int | None]:
        ctypes = self._ctypes
        wintypes = self._wintypes
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not self._kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        exit_value = self._filetime_value(exit_time)
        return self._filetime_value(creation), exit_value or None

    def snapshot(self) -> list[_WindowsProcessEntry]:
        ctypes = self._ctypes
        snapshot = self._kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        entries: list[_WindowsProcessEntry] = []
        try:
            entry = self._process_entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            more = self._kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                entries.append(
                    _WindowsProcessEntry(
                        pid=int(entry.th32ProcessID),
                        parent_pid=int(entry.th32ParentProcessID),
                    )
                )
                more = self._kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        return entries

    def open_process(self, pid: int) -> _WindowsProcessIdentity | None:
        handle = self._kernel32.OpenProcess(self._process_access, False, pid)
        if not handle:
            return None
        try:
            creation_time, _exit_time = self._process_times(handle)
        except OSError:
            self._kernel32.CloseHandle(handle)
            return None
        return _WindowsProcessIdentity(pid, 0, creation_time, handle)

    def exit_time(self, identity: _WindowsProcessIdentity) -> int | None:
        _creation_time, exit_time = self._process_times(identity.handle)
        return exit_time

    def is_active(self, identity: _WindowsProcessIdentity) -> bool:
        exit_code = self._wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            identity.handle,
            self._ctypes.byref(exit_code),
        ):
            return False
        return exit_code.value == 259

    def terminate(self, identity: _WindowsProcessIdentity) -> None:
        if self.is_active(identity) and not self._kernel32.TerminateProcess(
            identity.handle,
            1,
        ):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def wait(
        self,
        identity: _WindowsProcessIdentity,
        timeout_seconds: float,
    ) -> None:
        timeout_ms = min(round(timeout_seconds * 1000), 0xFFFFFFFE)
        self._kernel32.WaitForSingleObject(identity.handle, timeout_ms)

    def close(self, identity: _WindowsProcessIdentity) -> None:
        self._kernel32.CloseHandle(identity.handle)


def _run_windows_taskkill(pid: int, timeout_seconds: float) -> int:
    taskkill = subprocess.Popen(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return taskkill.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        taskkill.kill()
        try:
            taskkill.wait(timeout=timeout_seconds)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        raise


class _WindowsProcessTree:
    def __init__(
        self,
        root_pid: int,
        *,
        api: _WindowsProcessApi | None = None,
        poll_interval: float = _WINDOWS_TREE_POLL_SECONDS,
    ) -> None:
        self._root_pid = root_pid
        self._api = api or _WindowsProcessApiImpl()
        self._poll_interval = poll_interval
        self._identities: dict[int, _WindowsProcessIdentity] = {}
        self._lock = RLock()
        self._stop = Event()
        self._closed = False
        root = self._api.open_process(root_pid)
        if root is None:
            raise ProcessLookupError(root_pid)
        self._identities[root_pid] = root
        try:
            self._refresh()
        except BaseException:
            self._api.close(root)
            raise
        self._thread = Thread(
            target=self._watch,
            name=f"ffmpeg-tree-{root_pid}",
            daemon=True,
        )
        self._thread.start()

    def _watch(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self._refresh()
            except OSError:
                continue

    def _refresh(self) -> None:
        entries = self._api.snapshot()
        with self._lock:
            pending = {
                entry.pid: entry
                for entry in entries
                if entry.pid not in self._identities
            }
            captured = True
            while captured:
                captured = False
                for pid, entry in list(pending.items()):
                    parent = self._identities.get(entry.parent_pid)
                    if parent is None:
                        continue
                    identity = self._api.open_process(pid)
                    del pending[pid]
                    if identity is None:
                        continue
                    try:
                        parent_exit_time = self._api.exit_time(parent)
                    except BaseException:
                        self._api.close(identity)
                        raise
                    if identity.creation_time < parent.creation_time or (
                        parent_exit_time is not None
                        and identity.creation_time > parent_exit_time
                    ):
                        self._api.close(identity)
                        continue
                    identity.parent_pid = entry.parent_pid
                    self._identities[pid] = identity
                    captured = True

    def _shutdown_watcher(self) -> None:
        self._stop.set()
        self._thread.join(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)

    def _leaf_first(self) -> list[_WindowsProcessIdentity]:
        with self._lock:
            identities = list(self._identities.values())
            by_pid = dict(self._identities)

        def depth(identity: _WindowsProcessIdentity) -> int:
            result = 0
            parent_pid = identity.parent_pid
            visited = {identity.pid}
            while parent_pid in by_pid and parent_pid not in visited:
                visited.add(parent_pid)
                result += 1
                parent_pid = by_pid[parent_pid].parent_pid
            return result

        return sorted(identities, key=depth, reverse=True)

    def _terminate_captured(self) -> None:
        for identity in self._leaf_first():
            if not self._api.is_active(identity):
                continue
            try:
                self._api.terminate(identity)
            except OSError:
                continue
            self._api.wait(identity, _PROCESS_CLEANUP_TIMEOUT_SECONDS)

    def terminate(self) -> None:
        for timeout_seconds in _WINDOWS_TASKKILL_TIMEOUTS_SECONDS:
            try:
                if _run_windows_taskkill(self._root_pid, timeout_seconds) == 0:
                    break
            except (OSError, subprocess.TimeoutExpired):
                continue
        self._shutdown_watcher()
        try:
            self._refresh()
        except OSError:
            pass
        self._terminate_captured()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._shutdown_watcher()
        try:
            self._refresh()
        except OSError:
            pass
        self._terminate_captured()
        with self._lock:
            identities = list(self._identities.values())
            self._identities.clear()
        for identity in identities:
            self._api.close(identity)


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


def _create_windows_process_tree(
    process: subprocess.Popen[str],
) -> _WindowsProcessTree | None:
    if os_name != "nt":
        return None
    pid = getattr(process, "pid", None)
    if pid is None:
        return None
    try:
        return _WindowsProcessTree(pid)
    except OSError:
        return None


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
        process_tree_owner: _WindowsJob | _WindowsProcessTree | None
        process_tree_owner = _create_windows_job(process)
        if os_name == "nt" and process_tree_owner is None:
            process_tree_owner = _create_windows_process_tree(process)
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
            for timeout_seconds in _WINDOWS_TASKKILL_TIMEOUTS_SECONDS:
                try:
                    if _run_windows_taskkill(pid, timeout_seconds) == 0:
                        break
                except (OSError, subprocess.TimeoutExpired):
                    continue
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
        process_tree_owner: _WindowsJob | _WindowsProcessTree | None = None,
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
            if isinstance(process_tree_owner, _WindowsProcessTree):
                process_tree_owner.terminate()
                self._wait_for_process(process)
                return
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
