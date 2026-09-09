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
from dataclasses import dataclass
from importlib import resources
from os import name as os_name
from pathlib import Path
from shutil import which
from threading import Event, Lock, RLock, Thread
from typing import Protocol

import imageio_ffmpeg

from designtoolkit.video_frames.core import BackendResult, BackendUnavailableError
from designtoolkit.web.output_limits import OutputBudgetGuard, OutputTargetMonitor

PopenFactory = Callable[..., subprocess.Popen[str]]
_LOGGER = logging.getLogger(__name__)
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_POSIX_LAUNCH_WAIT_POLL_SECONDS = 0.01
_WINDOWS_LAUNCH_CLEANUP_TIMEOUT_SECONDS = 5.0
_WINDOWS_LAUNCH_WAIT_POLL_SECONDS = 0.01
_WINDOWS_STARTUP_ERROR_RETURN_CODE = 126
_WINDOWS_STARTUP_ERROR_MAX_BYTES = 4096
_WINDOWS_HANDLE_INHERITANCE_LOCK = RLock()
_WINDOWS_LAUNCHER_EXECUTABLE = getattr(
    sys,
    "_base_executable",
    sys.executable,
) or sys.executable
_WINDOWS_LAUNCHER_CODE = """\
import base64
import ctypes
import json
import msvcrt
import os
import subprocess
import sys
from ctypes import wintypes

MAX_STATUS_BYTES = 4096
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]

open_event = kernel32.OpenEventW
open_event.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
open_event.restype = wintypes.HANDLE
create_process = kernel32.CreateProcessW
create_process.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
create_process.restype = wintypes.BOOL
get_std_handle = kernel32.GetStdHandle
get_std_handle.argtypes = [wintypes.DWORD]
get_std_handle.restype = wintypes.HANDLE
resume_thread = kernel32.ResumeThread
resume_thread.argtypes = [wintypes.HANDLE]
resume_thread.restype = wintypes.DWORD
terminate_process = kernel32.TerminateProcess
terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
terminate_process.restype = wintypes.BOOL
wait_for_single_object = kernel32.WaitForSingleObject
wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
wait_for_single_object.restype = wintypes.DWORD
get_exit_code_process = kernel32.GetExitCodeProcess
get_exit_code_process.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
]
get_exit_code_process.restype = wintypes.BOOL
close_handle = kernel32.CloseHandle
close_handle.argtypes = [wintypes.HANDLE]
close_handle.restype = wintypes.BOOL

def write_startup_error(error_code):
    exc = ctypes.WinError(error_code)

    def bounded_text(value):
        return None if value is None else str(value)[:256]

    error_number = exc.errno if isinstance(exc.errno, int) else error_code
    payload = {
        "type": type(exc).__name__[:64],
        "errno": error_number,
        "winerror": getattr(exc, "winerror", error_code),
        "filename": bounded_text(getattr(exc, "filename", None)),
        "strerror": bounded_text(exc.strerror),
        "message": bounded_text(str(exc)),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_STATUS_BYTES:
        encoded = json.dumps(
            {
                "type": "OSError",
                "errno": error_number,
                "winerror": error_code,
                "filename": None,
                "strerror": "Windows process creation failed",
                "message": "Windows process creation failed",
            },
            separators=(",", ":"),
        ).encode("ascii")
    view = memoryview(encoded)
    while view:
        written = os.write(status_fd, view)
        if written <= 0:
            break
        view = view[written:]

status_fd = msvcrt.open_osfhandle(int(sys.argv[2]), os.O_WRONLY)
try:
    event_handle = open_event(0x00100000, False, sys.argv[1])
    if not event_handle:
        raise SystemExit(125)
    try:
        if wait_for_single_object(event_handle, 0xFFFFFFFF) != 0:
            raise SystemExit(125)
    finally:
        close_handle(event_handle)

    command = json.loads(base64.b64decode(sys.argv[3]).decode("utf-8"))
    status_handle = msvcrt.get_osfhandle(status_fd)
    os.set_inheritable(status_fd, False)
    os.set_handle_inheritable(status_handle, False)

    startup_info = STARTUPINFOW()
    startup_info.cb = ctypes.sizeof(startup_info)
    startup_info.dwFlags = 0x00000100
    startup_info.hStdInput = get_std_handle(-10)
    startup_info.hStdOutput = get_std_handle(-11)
    startup_info.hStdError = get_std_handle(-12)
    process_info = PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    if not create_process(
        None,
        command_line,
        None,
        None,
        True,
        0x00000004,
        None,
        None,
        ctypes.byref(startup_info),
        ctypes.byref(process_info),
    ):
        write_startup_error(ctypes.get_last_error())
        raise SystemExit(126)

    os.close(status_fd)
    status_fd = -1
    if resume_thread(process_info.hThread) == 0xFFFFFFFF:
        terminate_process(process_info.hProcess, 126)
        wait_for_single_object(process_info.hProcess, 0xFFFFFFFF)
        close_handle(process_info.hThread)
        close_handle(process_info.hProcess)
        raise SystemExit(126)
    close_handle(process_info.hThread)
    wait_for_single_object(process_info.hProcess, 0xFFFFFFFF)
    exit_code = wintypes.DWORD()
    if not get_exit_code_process(process_info.hProcess, ctypes.byref(exit_code)):
        exit_code.value = 1
    close_handle(process_info.hProcess)
    raise SystemExit(exit_code.value)
finally:
    if status_fd >= 0:
        os.close(status_fd)
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
    status_write_handle: int,
    command: Sequence[str],
) -> list[str]:
    payload = base64.b64encode(
        json.dumps(list(command), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return [
        _WINDOWS_LAUNCHER_EXECUTABLE,
        "-c",
        _WINDOWS_LAUNCHER_CODE,
        event_name,
        str(status_write_handle),
        payload,
    ]


def _windows_startup_error(
    payload_bytes: bytes,
) -> OSError | None:
    if not payload_bytes:
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error_types: dict[str, type[OSError]] = {
        "FileNotFoundError": FileNotFoundError,
        "PermissionError": PermissionError,
        "OSError": OSError,
    }
    error_type = error_types.get(payload.get("type"))
    errno = payload.get("errno")
    winerror = payload.get("winerror")
    filename = payload.get("filename")
    strerror = payload.get("strerror")
    message = payload.get("message")
    if (
        error_type is None
        or not isinstance(errno, int)
        or (winerror is not None and not isinstance(winerror, int))
        or (filename is not None and not isinstance(filename, str))
        or not isinstance(strerror, str)
        or not isinstance(message, str)
    ):
        return None
    error = error_type(errno, strerror, filename, winerror)
    return error if str(error) == message else OSError(message)


class _WindowsContainmentError(Exception):
    def __init__(self, original_error: OSError | RuntimeError) -> None:
        super().__init__(str(original_error))
        self.original_error = original_error


class _WindowsLaunchGate:
    def __init__(
        self,
        *,
        api: _WindowsKernelApiProtocol,
        event_handle: object,
        event_name: str,
        status_read_fd: int,
        status_write_fd: int,
        status_write_handle: int,
        launcher_command: list[str],
    ) -> None:
        self._api = api
        self._event_handle = event_handle
        self._job_handle: object | None = None
        self._status_read_fd: int | None = status_read_fd
        self._status_write_fd: int | None = status_write_fd
        self._status_write_handle: int | None = status_write_handle
        self._lock = RLock()
        self._closed = False
        self._release_started = False
        self._released = False
        self.event_name = event_name
        self.launcher_command = launcher_command

    @classmethod
    def create(
        cls,
        command: Sequence[str],
        *,
        api: _WindowsKernelApiProtocol | None = None,
    ) -> _WindowsLaunchGate:
        import msvcrt

        kernel_api = api or _WindowsKernelApi()
        event_name = f"Local\\DesignToolkit.FFmpeg.{uuid.uuid4().hex}"
        event_handle = cls._call_containment_api(
            lambda: kernel_api.create_event(event_name)
        )
        status_read_fd: int | None = None
        status_write_fd: int | None = None
        try:
            status_read_fd, status_write_fd = os.pipe()
            status_write_handle = msvcrt.get_osfhandle(status_write_fd)
            os.set_blocking(status_read_fd, False)
            return cls(
                api=kernel_api,
                event_handle=event_handle,
                event_name=event_name,
                status_read_fd=status_read_fd,
                status_write_fd=status_write_fd,
                status_write_handle=status_write_handle,
                launcher_command=_windows_launcher_command(
                    event_name,
                    status_write_handle,
                    command,
                ),
            )
        except BaseException:
            for descriptor in (status_write_fd, status_read_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException:
                        pass
            try:
                kernel_api.close_handle(event_handle)
            except BaseException:
                pass
            raise

    def start_launcher(
        self,
        popen: PopenFactory,
        popen_kwargs: dict[str, object],
    ) -> subprocess.Popen[str]:
        with self._lock:
            status_write_fd = self._status_write_fd
            status_write_handle = self._status_write_handle
            if (
                self._closed
                or status_write_fd is None
                or status_write_handle is None
            ):
                raise _WindowsContainmentError(
                    RuntimeError("Windows launcher status writer is unavailable")
                )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [status_write_handle]}
        kwargs = {
            **popen_kwargs,
            "startupinfo": startupinfo,
            "close_fds": True,
        }
        with _WINDOWS_HANDLE_INHERITANCE_LOCK:
            try:
                os.set_handle_inheritable(status_write_handle, True)
                process = popen(self.launcher_command, **kwargs)
            finally:
                with self._lock:
                    owns_status_writer = self._status_write_fd == status_write_fd
                    if owns_status_writer:
                        self._status_write_fd = None
                        self._status_write_handle = None
                if owns_status_writer:
                    try:
                        os.set_handle_inheritable(status_write_handle, False)
                    except BaseException:
                        try:
                            _LOGGER.exception(
                                "Failed to reset parent FFmpeg status writer "
                                "inheritance"
                            )
                        except BaseException:
                            pass
                    try:
                        os.close(status_write_fd)
                    except BaseException:
                        try:
                            _LOGGER.exception(
                                "Failed to close parent FFmpeg startup status writer"
                            )
                        except BaseException:
                            pass
        return process

    def read_startup_error(self, returncode: int | None) -> OSError | None:
        with self._lock:
            status_read_fd = self._status_read_fd
            self._status_read_fd = None
        if status_read_fd is None:
            return None
        payload = bytearray()
        try:
            while len(payload) <= _WINDOWS_STARTUP_ERROR_MAX_BYTES:
                try:
                    chunk = os.read(
                        status_read_fd,
                        _WINDOWS_STARTUP_ERROR_MAX_BYTES + 1 - len(payload),
                    )
                except BlockingIOError as exc:
                    raise RuntimeError(
                        "Windows launcher status writer remained open"
                    ) from exc
                if not chunk:
                    break
                payload.extend(chunk)
        finally:
            os.close(status_read_fd)
        if len(payload) > _WINDOWS_STARTUP_ERROR_MAX_BYTES:
            raise RuntimeError("Windows launcher startup status exceeded its limit")
        if not payload:
            return None
        if returncode != _WINDOWS_STARTUP_ERROR_RETURN_CODE:
            raise RuntimeError(
                "Windows launcher reported startup failure with an invalid exit code"
            )
        return _windows_startup_error(bytes(payload))

    def prepare(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._closed:
                raise _WindowsContainmentError(
                    RuntimeError("Windows FFmpeg launch gate was closed")
                )
            if self._release_started:
                raise RuntimeError("Windows FFmpeg launch gate was already released")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise RuntimeError("Windows launcher process handle is unavailable")
        job_handle = self._call_containment_api(self._api.create_job)
        with self._lock:
            if self._closed:
                owns_job = False
            else:
                self._job_handle = job_handle
                owns_job = True
        if not owns_job:
            try:
                self._api.close_handle(job_handle)
            except BaseException:
                pass
            raise _WindowsContainmentError(
                RuntimeError("Windows FFmpeg launch gate was closed")
            )
        self._call_containment_api(lambda: self._api.set_job_kill_on_close(job_handle))
        self._require_owned_handle(
            "_job_handle",
            job_handle,
            "Windows FFmpeg Job handle was closed during preparation",
        )
        self._call_containment_api(
            lambda: self._api.assign_process(job_handle, process_handle)
        )
        self._require_owned_handle(
            "_job_handle",
            job_handle,
            "Windows FFmpeg Job handle was closed during assignment",
        )

    def release(self) -> None:
        with self._lock:
            if self._release_started:
                raise RuntimeError("Windows FFmpeg launch gate was already released")
            event_handle = self._event_handle
            if self._closed or event_handle is None:
                raise _WindowsContainmentError(
                    RuntimeError("Windows FFmpeg launch event was closed")
                )
            self._release_started = True
        self._call_containment_api(lambda: self._api.set_event(event_handle))
        self._require_owned_handle(
            "_event_handle",
            event_handle,
            "Windows FFmpeg launch event was closed during release",
        )
        with self._lock:
            self._released = True

    @staticmethod
    def _call_containment_api(operation: Callable[[], object]) -> object:
        try:
            return operation()
        except (OSError, RuntimeError) as exc:
            raise _WindowsContainmentError(exc) from exc

    def _require_owned_handle(
        self,
        attribute: str,
        expected: object,
        message: str,
    ) -> None:
        with self._lock:
            if getattr(self, attribute) is expected:
                return
        raise _WindowsContainmentError(RuntimeError(message))

    @property
    def has_job(self) -> bool:
        with self._lock:
            return self._job_handle is not None

    def close(self) -> None:
        with self._lock:
            job_handle = self._job_handle
            event_handle = self._event_handle
            status_write_fd = self._status_write_fd
            status_read_fd = self._status_read_fd
            self._job_handle = None
            self._event_handle = None
            self._status_write_fd = None
            self._status_write_handle = None
            self._status_read_fd = None
            self._closed = True
        errors: list[BaseException] = []
        if job_handle is not None:
            try:
                self._api.close_handle(job_handle)
            except BaseException as exc:
                errors.append(exc)
        if event_handle is not None:
            try:
                self._api.close_handle(event_handle)
            except BaseException as exc:
                errors.append(exc)
        for descriptor in (status_write_fd, status_read_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]


@dataclass(frozen=True)
class _WindowsLaunchResult:
    process: subprocess.Popen[str] | None = None
    gate: _WindowsLaunchGate | None = None
    exception: BaseException | None = None


@dataclass(frozen=True)
class _PosixLaunchResult:
    process: subprocess.Popen[str] | None = None
    exception: BaseException | None = None


class _PosixLaunchTransaction:
    def __init__(
        self,
        command: Sequence[str],
        popen: PopenFactory,
        popen_kwargs: dict[str, object],
        abort: Callable[[subprocess.Popen[str]], None],
    ) -> None:
        self._command = list(command)
        self._popen = popen
        self._popen_kwargs = dict(popen_kwargs)
        self._abort = abort
        self._ready = Event()
        self._decision = Event()
        self._done = Event()
        self._state_lock = Lock()
        self._state = "STARTING"
        self._result: _PosixLaunchResult | None = None
        self._thread = Thread(
            target=self._run,
            name="designtoolkit-ffmpeg-posix-launch",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def wait(self) -> _PosixLaunchResult:
        while not self._ready.wait(timeout=_POSIX_LAUNCH_WAIT_POLL_SECONDS):
            pass
        with self._state_lock:
            result = self._result
        if result is None:
            raise RuntimeError("POSIX FFmpeg launch produced no result")
        if result.exception is not None:
            raise result.exception
        if result.process is None:
            raise RuntimeError("POSIX FFmpeg launch result was incomplete")
        return result

    def accept(self) -> None:
        with self._state_lock:
            if self._state == "ABANDONED":
                raise RuntimeError("POSIX FFmpeg launch was already abandoned")
            if self._state != "PUBLISHED":
                raise RuntimeError("POSIX FFmpeg launch was not ready")
            self._state = "ACCEPTED"
        self._decision.set()

    def abandon(self) -> None:
        with self._state_lock:
            if self._state != "ACCEPTED":
                self._state = "ABANDONED"
        self._decision.set()

    @property
    def was_accepted(self) -> bool:
        with self._state_lock:
            return self._state == "ACCEPTED"

    def _run(self) -> None:
        process: subprocess.Popen[str] | None = None
        try:
            try:
                process = self._popen(self._command, **self._popen_kwargs)
            except BaseException as exc:
                with self._state_lock:
                    if self._state != "ABANDONED":
                        self._state = "FAILED"
                        self._result = _PosixLaunchResult(exception=exc)
                        self._ready.set()
                return

            with self._state_lock:
                if self._state == "ABANDONED":
                    should_abort = True
                else:
                    should_abort = False
                    self._state = "PUBLISHED"
                    self._result = _PosixLaunchResult(process=process)
                    self._ready.set()

            while not should_abort:
                self._decision.wait(timeout=_POSIX_LAUNCH_WAIT_POLL_SECONDS)
                with self._state_lock:
                    if self._state == "ACCEPTED":
                        return
                    should_abort = self._state == "ABANDONED"

            try:
                self._abort(process)
            except BaseException:
                try:
                    _LOGGER.exception("Failed to clean abandoned POSIX FFmpeg launch")
                except BaseException:
                    pass
        finally:
            self._done.set()


class _WindowsLaunchTransaction:
    def __init__(
        self,
        command: Sequence[str],
        gate_factory: Callable[[Sequence[str]], _WindowsLaunchGate],
        popen: PopenFactory,
        popen_kwargs: dict[str, object],
        abort: Callable[[subprocess.Popen[str], _WindowsLaunchGate], None],
    ) -> None:
        self._command = list(command)
        self._gate_factory = gate_factory
        self._popen = popen
        self._popen_kwargs = dict(popen_kwargs)
        self._abort = abort
        self._cancel = Event()
        self._ready = Event()
        self._decision = Event()
        self._done = Event()
        self._state_lock = Lock()
        self._abort_lock = Lock()
        self._state = "STARTING"
        self._accepted = False
        self._aborted = False
        self._process: subprocess.Popen[str] | None = None
        self._gate: _WindowsLaunchGate | None = None
        self._result: _WindowsLaunchResult | None = None
        self._thread = Thread(
            target=self._run,
            name="designtoolkit-ffmpeg-windows-launch",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def wait(self) -> _WindowsLaunchResult:
        while not self._ready.wait(timeout=_WINDOWS_LAUNCH_WAIT_POLL_SECONDS):
            pass
        result = self._result
        if result is None:
            raise RuntimeError("Windows FFmpeg launch produced no result")
        if result.exception is not None:
            raise result.exception
        if result.process is None or result.gate is None:
            raise RuntimeError("Windows FFmpeg launch result was incomplete")
        return result

    def accept(self) -> None:
        with self._state_lock:
            if self._cancel.is_set():
                raise RuntimeError("Windows FFmpeg launch was already cancelled")
            self._accepted = True
            self._state = "ACCEPTED"
        self._decision.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._contain_owned_launch()
        self._decision.set()

    @property
    def was_accepted(self) -> bool:
        with self._state_lock:
            return self._accepted

    def wait_for_cleanup(self) -> None:
        if self._done.wait(_WINDOWS_LAUNCH_CLEANUP_TIMEOUT_SECONDS):
            self._thread.join()

    def _run(self) -> None:
        gate: _WindowsLaunchGate | None = None
        process: subprocess.Popen[str] | None = None
        try:
            gate = self._gate_factory(self._command)
            with self._state_lock:
                self._gate = gate
            if self._cancel.is_set():
                self._close_owned_gate(gate)
                return

            process = gate.start_launcher(self._popen, self._popen_kwargs)
            with self._state_lock:
                self._process = process
            if self._cancel.is_set():
                self._abort_launch(process, gate)
                return

            gate.prepare(process)
            with self._state_lock:
                self._state = "PREPARED"
                if self._cancel.is_set():
                    cancelled_before_commit = True
                else:
                    self._state = "COMMITTING"
                    cancelled_before_commit = False
            if cancelled_before_commit:
                self._abort_launch(process, gate)
                return
            gate.release()
            with self._state_lock:
                if self._cancel.is_set():
                    cancelled_during_commit = True
                else:
                    cancelled_during_commit = False
                    self._state = "COMMITTED"
                    self._result = _WindowsLaunchResult(
                        process=process,
                        gate=gate,
                    )
                    self._ready.set()
            if cancelled_during_commit:
                self._abort_launch(process, gate)
                return

            self._decision.wait()
            with self._state_lock:
                cancelled_before_accept = (
                    self._cancel.is_set() and not self._accepted
                )
            if cancelled_before_accept:
                self._abort_launch(process, gate)
        except BaseException as exc:
            if process is not None and gate is not None:
                self._abort_launch(process, gate)
            elif gate is not None:
                self._close_owned_gate(gate)
            self._result = _WindowsLaunchResult(exception=exc)
            self._ready.set()
        finally:
            if self._result is None:
                self._result = _WindowsLaunchResult(
                    exception=RuntimeError("Windows FFmpeg launch was cancelled")
                )
                self._ready.set()
            self._done.set()

    def _contain_owned_launch(self) -> None:
        with self._abort_lock:
            if self._aborted:
                return
        with self._state_lock:
            process = self._process
            gate = self._gate
            self._gate = None
        if gate is not None:
            self._close_gate(gate)
        if process is not None:
            try:
                process.kill()
            except BaseException:
                try:
                    _LOGGER.exception(
                        "Failed to kill cancelled FFmpeg Windows launcher"
                    )
                except BaseException:
                    pass

    def _close_owned_gate(self, gate: _WindowsLaunchGate) -> None:
        with self._state_lock:
            if self._gate is not gate:
                return
            self._gate = None
        self._close_gate(gate)

    def _abort_launch(
        self,
        process: subprocess.Popen[str],
        gate: _WindowsLaunchGate,
    ) -> None:
        with self._abort_lock:
            if self._aborted:
                return
            self._aborted = True
        try:
            self._abort(process, gate)
        except BaseException:
            try:
                _LOGGER.exception("Failed to abort cancelled FFmpeg Windows launch")
            except BaseException:
                pass

    @staticmethod
    def _close_gate(gate: _WindowsLaunchGate) -> None:
        try:
            gate.close()
        except BaseException:
            try:
                _LOGGER.exception(
                    "Failed to close cancelled FFmpeg Windows launch gate"
                )
            except BaseException:
                pass


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
        posix_launch_transaction: _PosixLaunchTransaction | None = None
        launch_transaction: _WindowsLaunchTransaction | None = None
        process: subprocess.Popen[str] | None = None
        if os_name == "posix":
            posix_launch_transaction = _PosixLaunchTransaction(
                gated_command,
                self._popen,
                popen_kwargs,
                self._abort_posix_launch,
            )
        elif os_name == "nt":
            launch_transaction = _WindowsLaunchTransaction(
                gated_command,
                self._windows_launch_gate_factory,
                self._popen,
                popen_kwargs,
                self._abort_windows_launch,
            )
        try:
            if launch_transaction is not None:
                try:
                    launch_transaction.start()
                    launch_result = launch_transaction.wait()
                except _WindowsContainmentError as exc:
                    raise BackendUnavailableError(
                        "Windows FFmpeg launch containment could not be established."
                    ) from exc.original_error
                process = launch_result.process
                process_tree_owner = launch_result.gate
                if process is None or process_tree_owner is None:
                    raise RuntimeError("Windows FFmpeg launch result was incomplete")
                launch_transaction.accept()
            elif posix_launch_transaction is not None:
                posix_launch_transaction.start()
                launch_result = posix_launch_transaction.wait()
                process = launch_result.process
                if process is None:
                    raise RuntimeError("POSIX FFmpeg launch result was incomplete")
                posix_launch_transaction.accept()
            else:
                process = self._popen(gated_command, **popen_kwargs)

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
                if process_tree_owner is not None:
                    startup_error = process_tree_owner.read_startup_error(
                        process.returncode
                    )
                    if startup_error is not None:
                        raise startup_error
                if monitor is not None:
                    monitor.finish()
                return BackendResult(
                    returncode=(
                        process.returncode if process.returncode is not None else 1
                    ),
                    stderr=stderr or "",
                )
        except BaseException:
            if launch_transaction is not None:
                launch_transaction.cancel()
            if posix_launch_transaction is not None:
                posix_launch_transaction.abandon()
            if (
                process is not None
                and (
                    launch_transaction is None
                    or launch_transaction.was_accepted
                )
                and (
                    posix_launch_transaction is None
                    or posix_launch_transaction.was_accepted
                )
            ):
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
            if (
                process_tree_owner is not None
                and (
                    launch_transaction is None
                    or launch_transaction.was_accepted
                )
            ):
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
            if process.returncode is not None or not self._process_is_running(process):
                return
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                self._wait_for_process(process)
                return
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
        def attempt(description: str, cleanup: Callable[[], object]) -> None:
            try:
                cleanup()
            except BaseException:
                try:
                    _LOGGER.exception("Failed to %s", description)
                except BaseException:
                    pass

        if not process_tree_owner.has_job:
            attempt("terminate Windows FFmpeg launcher", process.terminate)
        attempt("close Windows FFmpeg launch gate", process_tree_owner.close)
        attempt(
            "wait for Windows FFmpeg launcher",
            lambda: process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS),
        )
        attempt("kill Windows FFmpeg launcher", process.kill)
        attempt(
            "reap Windows FFmpeg launcher",
            lambda: process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS),
        )

    @staticmethod
    def _abort_posix_launch(process: subprocess.Popen[str]) -> None:
        def attempt(description: str, cleanup: Callable[[], object]) -> None:
            try:
                cleanup()
            except ProcessLookupError:
                pass
            except BaseException:
                try:
                    _LOGGER.exception("Failed to %s", description)
                except BaseException:
                    pass

        def wait_for_process(description: str) -> bool:
            try:
                process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
            except ProcessLookupError:
                return True
            except subprocess.TimeoutExpired:
                return False
            except BaseException:
                try:
                    _LOGGER.exception("Failed to %s", description)
                except BaseException:
                    pass
                return False
            return True

        def signal_process_group(sent_signal: int, description: str) -> bool:
            try:
                os.killpg(pid, sent_signal)
            except ProcessLookupError:
                return False
            except BaseException:
                try:
                    _LOGGER.exception("Failed to %s", description)
                except BaseException:
                    pass
            return True

        pid = getattr(process, "pid", None)
        if pid is None:
            attempt("terminate abandoned POSIX FFmpeg launch", process.terminate)
            if wait_for_process("reap abandoned POSIX FFmpeg launch"):
                return
            attempt("kill abandoned POSIX FFmpeg launch", process.kill)
            wait_for_process("reap killed POSIX FFmpeg launch")
            return

        group_exists = signal_process_group(
            signal.SIGTERM,
            "terminate abandoned POSIX FFmpeg process group",
        )
        if group_exists:
            signal_process_group(
                _POSIX_SIGKILL,
                "kill abandoned POSIX FFmpeg process group",
            )
        wait_for_process("reap abandoned POSIX FFmpeg launch")
