from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Barrier, Event, Thread, get_ident
from typing import Any

import pytest
from rq.timeouts import JobTimeoutException, TimerDeathPenalty

from designtoolkit.video_frames import ffmpeg_backend
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux process groups and /proc process states",
)

_INTERRUPTION_GUARD_SECONDS = 10


class _FatalLaunchInterruption(BaseException):
    pass


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        del timeout
        self.returncode = 0
        return "", ""


def _wait_until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _read_pid_when_ready(path: Path, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.01)
    raise AssertionError(f"PID file was not ready: {path}")


def _linux_process_state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None

    _command, separator, remaining = stat.rpartition(")")
    if not separator:
        raise AssertionError(f"Malformed /proc/{pid}/stat: {stat!r}")
    fields = remaining.split()
    if not fields:
        raise AssertionError(f"Missing state in /proc/{pid}/stat: {stat!r}")
    return fields[0]


def _process_is_running(pid: int) -> bool:
    return _linux_process_state(pid) not in {None, "Z"}


def _inject_main_when_factory_blocks(
    factory_blocked: Event,
    cancelled: Event,
    main_thread_id: int,
    interruption_type: type[BaseException],
    injection_results: list[int],
) -> None:
    while not factory_blocked.wait(0.01):
        if cancelled.is_set():
            return
    if cancelled.is_set():
        return
    set_async_exc = ctypes.pythonapi.PyThreadState_SetAsyncExc
    result = set_async_exc(
        ctypes.c_ulong(main_thread_id),
        ctypes.py_object(interruption_type),
    )
    injection_results.append(result)
    if result > 1:
        set_async_exc(ctypes.c_ulong(main_thread_id), ctypes.c_void_p())


def _force_cleanup(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _cooperative_process_tree_command(grandchild_pid_path: Path) -> list[str]:
    grandchild_code = (
        "from pathlib import Path; import os, sys, time; "
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii'); "
        "time.sleep(60)"
    )
    parent_code = (
        "import signal, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {grandchild_code!r}, "
        "sys.argv[1]]); "
        "signal.signal(signal.SIGTERM, "
        "lambda *_args: (child.wait(timeout=2), sys.exit(0))); "
        "time.sleep(60)"
    )
    return [sys.executable, "-c", parent_code, str(grandchild_pid_path)]


def _exiting_parent_command(grandchild_pid_path: Path) -> list[str]:
    grandchild_code = (
        "from pathlib import Path; import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii'); "
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}, "
        "sys.argv[1]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL)"
    )
    return [sys.executable, "-c", parent_code, str(grandchild_pid_path)]


@pytest.mark.parametrize(
    "interruption_type",
    [JobTimeoutException, _FatalLaunchInterruption],
    ids=["rq-timeout", "base-exception"],
)
def test_posix_interruption_during_popen_cleans_real_process_tree(
    interruption_type: type[BaseException],
    tmp_path: Path,
) -> None:
    grandchild_pid_path = tmp_path / "grandchild.pid"
    factory_blocked = Event()
    release_factory = Event()
    cancel_interruption = Event()
    injection_results: list[int] = []
    processes: list[subprocess.Popen[str]] = []
    grandchild_pids: list[int] = []
    interrupter = Thread(
        target=_inject_main_when_factory_blocks,
        args=(
            factory_blocked,
            cancel_interruption,
            get_ident(),
            interruption_type,
            injection_results,
        ),
        daemon=True,
    )

    def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen(command, **kwargs)
        processes.append(process)
        grandchild_pids.append(_read_pid_when_ready(grandchild_pid_path))
        factory_blocked.set()
        while not release_factory.wait(0.01):
            pass
        return process

    interrupter.start()
    try:
        with pytest.raises(interruption_type):
            with TimerDeathPenalty(
                _INTERRUPTION_GUARD_SECONDS,
                exception=interruption_type,
            ):
                FfmpegBackend(popen=popen).run(
                    _cooperative_process_tree_command(grandchild_pid_path)
                )
        assert factory_blocked.is_set()
        interrupter.join(timeout=2)
        assert injection_results == [1]
        assert not interrupter.is_alive()
        assert processes
        assert len(grandchild_pids) == 1
        grandchild_pid = grandchild_pids[0]

        release_factory.set()

        processes[0].wait(timeout=5)
        assert processes[0].returncode is not None
        assert _wait_until(lambda: not _process_is_running(grandchild_pid))
    finally:
        cancel_interruption.set()
        release_factory.set()
        interrupter.join(timeout=2)
        _force_cleanup(processes)


def test_posix_abandoned_launch_kills_group_after_parent_exits(
    tmp_path: Path,
) -> None:
    grandchild_pid_path = tmp_path / "surviving-grandchild.pid"
    factory_blocked = Event()
    release_factory = Event()
    cancel_interruption = Event()
    injection_results: list[int] = []
    processes: list[subprocess.Popen[str]] = []
    grandchild_pids: list[int] = []
    interrupter = Thread(
        target=_inject_main_when_factory_blocks,
        args=(
            factory_blocked,
            cancel_interruption,
            get_ident(),
            _FatalLaunchInterruption,
            injection_results,
        ),
        daemon=True,
    )

    def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen(command, **kwargs)
        processes.append(process)
        grandchild_pids.append(_read_pid_when_ready(grandchild_pid_path))
        assert _wait_until(lambda: process.poll() is not None)
        factory_blocked.set()
        while not release_factory.wait(0.01):
            pass
        return process

    interrupter.start()
    try:
        with pytest.raises(_FatalLaunchInterruption):
            with TimerDeathPenalty(
                _INTERRUPTION_GUARD_SECONDS,
                exception=_FatalLaunchInterruption,
            ):
                FfmpegBackend(popen=popen).run(
                    _exiting_parent_command(grandchild_pid_path)
                )
        assert factory_blocked.is_set()
        interrupter.join(timeout=2)
        assert injection_results == [1]
        assert not interrupter.is_alive()
        assert len(grandchild_pids) == 1
        grandchild_pid = grandchild_pids[0]
        assert _process_is_running(grandchild_pid)

        release_factory.set()

        processes[0].wait(timeout=5)
        assert processes[0].returncode is not None
        assert _wait_until(lambda: not _process_is_running(grandchild_pid))
    finally:
        cancel_interruption.set()
        release_factory.set()
        interrupter.join(timeout=2)
        _force_cleanup(processes)


def test_posix_launch_transaction_linearizes_ownership_100_times() -> None:
    for iteration in range(100):
        process = _FakeProcess()
        publish_barrier = Barrier(2)
        aborts: list[_FakeProcess] = []

        def popen(
            _command: list[str],
            publish_barrier: Barrier = publish_barrier,
            process: _FakeProcess = process,
            **_kwargs: object,
        ) -> _FakeProcess:
            publish_barrier.wait(timeout=2)
            return process

        transaction = ffmpeg_backend._PosixLaunchTransaction(
            ["ffmpeg"],
            popen,
            {"start_new_session": True},
            aborts.append,
        )
        transaction.start()
        publish_barrier.wait(timeout=2)

        if iteration % 2:
            result = transaction.wait()
            transaction.accept()
            assert result.process is process
        else:
            transaction.abandon()

        assert transaction._done.wait(2)
        transaction._thread.join(timeout=2)
        assert not transaction._thread.is_alive()
        assert aborts == ([] if iteration % 2 else [process])


def test_posix_launch_normal_result_and_error_are_preserved() -> None:
    process = _FakeProcess()
    result = FfmpegBackend(popen=lambda *_args, **_kwargs: process).run(["ffmpeg"])
    assert result.returncode == 0

    original_error = FileNotFoundError("ffmpeg is missing")

    def fail_launch(*_args: object, **_kwargs: object) -> _FakeProcess:
        raise original_error

    with pytest.raises(FileNotFoundError) as exc_info:
        FfmpegBackend(popen=fail_launch).run(["missing-ffmpeg"])

    assert exc_info.value is original_error


def test_posix_launch_cleanup_failure_does_not_replace_timeout(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CleanupFailure(BaseException):
        pass

    timeout = JobTimeoutException("worker timed out during POSIX launch")
    factory_blocked = Event()
    release_factory = Event()
    transactions: list[Any] = []
    real_transaction = ffmpeg_backend._PosixLaunchTransaction

    def create_transaction(*args: object, **kwargs: object) -> Any:
        transaction = real_transaction(*args, **kwargs)

        def interrupting_wait() -> Any:
            assert factory_blocked.wait(2)
            raise timeout

        transaction.wait = interrupting_wait
        transactions.append(transaction)
        return transaction

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        factory_blocked.set()
        assert release_factory.wait(2)
        return _FakeProcess()

    def fail_cleanup(_process: _FakeProcess) -> None:
        raise CleanupFailure("cleanup failed")

    monkeypatch.setattr(
        ffmpeg_backend,
        "_PosixLaunchTransaction",
        create_transaction,
    )
    monkeypatch.setattr(
        FfmpegBackend,
        "_abort_posix_launch",
        staticmethod(fail_cleanup),
    )
    caplog.set_level("ERROR", logger="designtoolkit.video_frames.ffmpeg_backend")

    try:
        with pytest.raises(JobTimeoutException) as exc_info:
            FfmpegBackend(popen=popen).run(["ffmpeg"])
        assert exc_info.value is timeout
    finally:
        release_factory.set()

    assert transactions[0]._done.wait(2)
    transactions[0]._thread.join(timeout=2)
    assert not transactions[0]._thread.is_alive()
    assert "Failed to clean abandoned POSIX FFmpeg launch" in caplog.text


def test_posix_abandoned_launch_stops_signaling_after_group_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapableProcess(_FakeProcess):
        pid = 4321

        def __init__(self) -> None:
            super().__init__()
            self.wait_calls: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            self.returncode = 0
            return 0

    process = ReapableProcess()
    signals: list[int] = []

    def missing_group(_process_group: int, sent_signal: int) -> None:
        signals.append(sent_signal)
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", missing_group)

    FfmpegBackend._abort_posix_launch(process)  # type: ignore[arg-type]

    assert signals == [signal.SIGTERM]
    assert process.wait_calls == [ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS]


def test_posix_abandoned_blocked_factories_leave_no_launcher_threads() -> None:
    transactions: list[Any] = []
    releases: list[Event] = []

    for _ in range(10):
        release = Event()
        releases.append(release)
        transaction = ffmpeg_backend._PosixLaunchTransaction(
            ["ffmpeg"],
            lambda *_args, release=release, **_kwargs: (
                release.wait(2) and _FakeProcess()
            ),
            {"start_new_session": True},
            lambda _process: None,
        )
        transactions.append(transaction)
        transaction.start()
        transaction.abandon()

    for release in releases:
        release.set()
    for transaction in transactions:
        assert transaction._done.wait(2)
        transaction._thread.join(timeout=2)
        assert not transaction._thread.is_alive()
