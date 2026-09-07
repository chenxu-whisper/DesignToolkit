from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from rq.timeouts import JobTimeoutException

from designtoolkit.video_frames import ffmpeg_backend
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend
from designtoolkit.web.output_limits import (
    OutputBudgetGuard,
    OutputLimitExceeded,
    OutputTargetMonitor,
)


def test_budget_rejects_aggregate_files_at_safety_boundary(tmp_path: Path) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=8, safety_bytes=1)
    assert guard.remaining_bytes == 7
    first = tmp_path / "one.bin"
    first.write_bytes(b"1234")
    guard.account(first)
    assert guard.remaining_bytes == 3
    second = tmp_path / "two.bin"
    second.write_bytes(b"5678")

    with pytest.raises(OutputLimitExceeded, match="8-byte limit"):
        guard.account(second)

    assert guard.used_bytes == 8


def test_repeated_account_only_adds_file_growth(tmp_path: Path) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=100)
    output = tmp_path / "output.bin"
    output.write_bytes(b"1234")

    guard.account(output)
    guard.account(output)
    output.write_bytes(b"1234567")
    guard.account(output)

    assert guard.used_bytes == 7


def test_account_tracks_only_positive_deltas_after_shrink(tmp_path: Path) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=100)
    output = tmp_path / "output.bin"
    output.write_bytes(b"12345")
    guard.account(output)
    output.write_bytes(b"12")
    guard.account(output)
    output.write_bytes(b"1234")
    guard.account(output)

    assert guard.used_bytes == 7


def test_account_is_thread_safe_for_distinct_files(tmp_path: Path) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=10_000)
    outputs = [tmp_path / f"{index}.bin" for index in range(50)]
    for output in outputs:
        output.write_bytes(b"1234")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(guard.account, outputs))

    assert guard.used_bytes == 200


def test_account_same_growing_file_cannot_restore_stale_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=100)
    output = tmp_path / "output.bin"
    output.write_bytes(b"1234")
    first_observed = Event()
    second_observed = Event()
    release_first = Event()
    original_identity = guard._identity

    def controlled_identity(path: Path, device: int, inode: int):
        if not first_observed.is_set():
            first_observed.set()
            assert release_first.wait(timeout=5)
        else:
            second_observed.set()
        return original_identity(path, device, inode)

    monkeypatch.setattr(guard, "_identity", controlled_identity)
    with ThreadPoolExecutor(max_workers=2) as executor:
        stale_account = executor.submit(guard.account, output)
        assert first_observed.wait(timeout=5)
        output.write_bytes(b"1234567")
        current_account = executor.submit(guard.account, output)
        if second_observed.wait(timeout=0.2):
            current_account.result(timeout=5)
        release_first.set()
        stale_account.result(timeout=5)
        current_account.result(timeout=5)

    guard.account(output)

    assert guard.used_bytes == 7


def test_account_rejects_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    guard = OutputBudgetGuard(root, max_bytes=100)

    with pytest.raises(ValueError, match="outside output root"):
        guard.account(outside)


def test_account_rejects_symlink_that_escapes_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    link = root / "link.bin"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    guard = OutputBudgetGuard(root, max_bytes=100)

    with pytest.raises(ValueError, match="outside output root"):
        guard.account(link)


def test_check_tree_finds_unaccounted_nested_output(tmp_path: Path) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=5, safety_bytes=1)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "one.bin").write_bytes(b"123")
    (nested / "two.bin").write_bytes(b"45")

    with pytest.raises(OutputLimitExceeded, match="5-byte limit"):
        guard.check_tree()

    assert guard.used_bytes == 5


def test_hard_links_are_not_double_counted(tmp_path: Path) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=100)
    output = tmp_path / "output.bin"
    alias = tmp_path / "alias.bin"
    output.write_bytes(b"1234")
    os.link(output, alias)

    guard.account(output)
    guard.account(alias)

    assert guard.used_bytes == 4


def test_single_output_monitor_accounts_only_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=100)
    target = tmp_path / "output.mp4"
    unrelated = tmp_path / "sidecar.bin"
    target.write_bytes(b"1234")
    unrelated.write_bytes(b"ignored")
    accounted: list[Path] = []
    original_account = guard.account

    def account(path: Path) -> None:
        original_account(path)
        accounted.append(path)

    monkeypatch.setattr(guard, "account", account)
    monitor = OutputTargetMonitor(guard, target)

    monitor.poll()
    target.write_bytes(b"123456")
    monitor.poll()

    assert accounted == [target, target]
    assert guard.used_bytes == 6


def test_sequence_monitor_rechecks_only_tail_and_new_contiguous_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=100)
    pattern = tmp_path / "clip%%take_%06d.png"
    first = tmp_path / "clip%take_000001.png"
    second = tmp_path / "clip%take_000002.png"
    third = tmp_path / "clip%take_000003.png"
    first.write_bytes(b"1")
    second.write_bytes(b"22")
    accounted: list[Path] = []
    original_account = guard.account

    def account(path: Path) -> None:
        original_account(path)
        accounted.append(path)

    monkeypatch.setattr(guard, "account", account)
    monitor = OutputTargetMonitor(guard, pattern)

    monitor.poll()
    monitor.poll()
    third.write_bytes(b"333")
    monitor.poll()

    assert accounted == [first, second, second, second, third]
    assert guard.used_bytes == 6


def test_sequence_monitor_stats_only_tail_and_new_successors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=100)
    pattern = tmp_path / "clip_%06d.png"
    frames = [tmp_path / f"clip_{index:06d}.png" for index in range(1, 6)]
    frames[0].write_bytes(b"1")
    attempted: list[Path] = []
    original_account = guard.account

    def account(path: Path) -> None:
        attempted.append(path)
        original_account(path)

    monkeypatch.setattr(guard, "account", account)
    monitor = OutputTargetMonitor(guard, pattern)

    monitor.poll()
    frames[1].write_bytes(b"2")
    frames[2].write_bytes(b"3")
    frames[3].write_bytes(b"4")
    monitor.poll()
    monitor.poll()

    assert attempted == [
        frames[0],
        frames[1],
        frames[0],
        frames[1],
        frames[2],
        frames[3],
        frames[4],
        frames[3],
        frames[4],
    ]


def test_sequence_monitor_leaves_gap_for_final_tree_check(tmp_path: Path) -> None:
    guard = OutputBudgetGuard(tmp_path, max_bytes=6, safety_bytes=1)
    pattern = tmp_path / "clip_%06d.png"
    (tmp_path / "clip_000001.png").write_bytes(b"1")
    (tmp_path / "clip_000003.png").write_bytes(b"12345")
    monitor = OutputTargetMonitor(guard, pattern)

    monitor.poll()
    assert guard.used_bytes == 1

    with pytest.raises(OutputLimitExceeded, match="6-byte limit"):
        monitor.finish()


class FakePopen:
    def __init__(
        self,
        communicate_results: list[tuple[str, str] | BaseException],
        *,
        returncode: int = 0,
        wait_times_out: bool = False,
    ) -> None:
        self._communicate_results = communicate_results
        self._final_returncode = returncode
        self._wait_times_out = wait_times_out
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls: list[float | None] = []

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        del timeout
        if not self._communicate_results:
            self.returncode = self._final_returncode
            return "", ""
        result = self._communicate_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        self.returncode = self._final_returncode
        return result

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self._wait_times_out and not self.kill_called:
            raise subprocess.TimeoutExpired("ffmpeg", 0.01)
        self.returncode = self._final_returncode
        return self._final_returncode


def _popen_factory(
    process: FakePopen,
    calls: list[tuple[list[str], dict[str, Any]]],
):
    def create(command: list[str], **kwargs: Any) -> FakePopen:
        calls.append((command, kwargs))
        return process

    return create


class FakeProcessTreeOwner:
    def __init__(self) -> None:
        self.close_calls = 0
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_calls += 1


def test_windows_job_assignment_failure_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    class FakeFunction:
        def __init__(self, result: int) -> None:
            self.result = result
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args: object) -> int:
            self.calls.append(args)
            return self.result

    class FakeKernel32:
        CreateJobObjectW = FakeFunction(123)
        SetInformationJobObject = FakeFunction(1)
        AssignProcessToJobObject = FakeFunction(0)
        CloseHandle = FakeFunction(1)

    process = FakePopen([])
    process._handle = 456  # type: ignore[attr-defined]
    kernel32 = FakeKernel32()
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    monkeypatch.setattr(
        ctypes,
        "WinError",
        lambda _error: PermissionError("access denied"),
    )

    owner = ffmpeg_backend._create_windows_job(process)  # type: ignore[arg-type]

    assert owner is None
    assert kernel32.AssignProcessToJobObject.calls == [(123, 456)]
    assert kernel32.CloseHandle.calls == [(123,)]


def test_ffmpeg_backend_stops_when_active_sequence_tail_grows(
    tmp_path: Path,
) -> None:
    first = tmp_path / "frame_000001.png"
    first.write_bytes(b"1")

    class GrowingSequencePopen(FakePopen):
        def __init__(self) -> None:
            super().__init__([])
            self.communicate_calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            del timeout
            self.communicate_calls += 1
            if self.communicate_calls == 2:
                first.write_bytes(b"x" * 80)
            if self.communicate_calls <= 2:
                raise subprocess.TimeoutExpired("ffmpeg", 0.01)
            raise AssertionError("FFmpeg was allowed to exit before rejection")

    process = GrowingSequencePopen()
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(
            tmp_path,
            max_bytes=80,
            safety_bytes=1,
        ),
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(OutputLimitExceeded, match="80-byte limit"):
        backend.run(["ffmpeg", str(tmp_path / "frame_%06d.png")])

    assert process.terminate_called
    assert process.communicate_calls == 2


def test_ffmpeg_backend_preserves_captured_stderr_on_success() -> None:
    process = FakePopen([("ignored stdout", "diagnostic")], returncode=3)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    backend = FfmpegBackend(popen=_popen_factory(process, calls))

    result = backend.run(["ffmpeg", "out.mp4"])

    assert result.returncode == 3
    assert result.stderr == "diagnostic"
    assert calls[0][0] == ["ffmpeg", "out.mp4"]
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.PIPE
    assert calls[0][1]["encoding"] == "utf-8"
    assert not process.terminate_called
    assert not process.kill_called


def test_ffmpeg_backend_closes_windows_job_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakePopen([("", "")])
    owner = FakeProcessTreeOwner()
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(
        ffmpeg_backend,
        "_create_windows_job",
        lambda _process: owner,
        raising=False,
    )
    backend = FfmpegBackend(popen=_popen_factory(process, []))

    result = backend.run(["ffmpeg", "out.mp4"])

    assert result.returncode == 0
    assert owner.close_calls == 1


def test_ffmpeg_backend_closes_windows_job_when_root_waits_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")
    process = FakePopen([timeout])
    owner = FakeProcessTreeOwner()
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(
        ffmpeg_backend,
        "_create_windows_job",
        lambda _process: owner,
        raising=False,
    )
    backend = FfmpegBackend(popen=_popen_factory(process, []))

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert process.wait_calls == [
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ]
    assert owner.close_calls == 1


def test_ffmpeg_backend_stops_process_and_preserves_rq_timeout() -> None:
    timeout = JobTimeoutException("worker timed out")
    process = FakePopen([timeout], wait_times_out=True)
    backend = FfmpegBackend(
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert process.terminate_called
    assert process.kill_called
    assert process.wait_calls == [
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ]


def test_ffmpeg_backend_logs_cleanup_failure_without_masking_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timeout = JobTimeoutException("worker timed out")

    class StopFailurePopen(FakePopen):
        def terminate(self) -> None:
            super().terminate()
            raise RuntimeError("terminate failed")

    process = StopFailurePopen([timeout])
    backend = FfmpegBackend(popen=_popen_factory(process, []))
    caplog.set_level("ERROR", logger="designtoolkit.video_frames.ffmpeg_backend")

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert process.terminate_called
    assert "Failed to stop FFmpeg after worker interruption" in caplog.text


def test_ffmpeg_backend_polls_guard_between_timeouts() -> None:
    process = FakePopen(
        [
            subprocess.TimeoutExpired("ffmpeg", 0.01),
            ("", ""),
        ]
    )
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1

    backend = FfmpegBackend(
        output_guard=guard,
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    result = backend.run(["ffmpeg", "out.mp4"])

    assert result.returncode == 0
    assert guard_calls == 1
    assert not process.terminate_called


def test_ffmpeg_backend_runs_full_tree_check_after_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakePopen([("", "")])
    target = tmp_path / "clip_000001.png"
    target.write_bytes(b"1")
    (tmp_path / "unmonitored.bin").write_bytes(b"12345")
    guard = OutputBudgetGuard(tmp_path, max_bytes=6, safety_bytes=1)
    check_calls = 0
    original_check_tree = guard.check_tree

    def check_tree() -> None:
        nonlocal check_calls
        check_calls += 1
        original_check_tree()

    monkeypatch.setattr(guard, "check_tree", check_tree)
    backend = FfmpegBackend(
        output_guard=guard,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(OutputLimitExceeded, match="6-byte limit"):
        backend.run(["ffmpeg", str(tmp_path / "clip_%06d.png")])

    assert check_calls == 1


def test_ffmpeg_backend_terminates_when_guard_rejects() -> None:
    process = FakePopen([subprocess.TimeoutExpired("ffmpeg", 0.01)])

    def reject() -> None:
        raise OutputLimitExceeded("output budget exhausted")

    backend = FfmpegBackend(
        output_guard=reject,
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(OutputLimitExceeded, match="budget exhausted"):
        backend.run(["ffmpeg", "out.mp4"])

    assert process.terminate_called
    assert not process.kill_called


def test_ffmpeg_backend_kills_process_that_ignores_termination() -> None:
    process = FakePopen(
        [subprocess.TimeoutExpired("ffmpeg", 0.01)],
        wait_times_out=True,
    )
    backend = FfmpegBackend(
        output_guard=lambda: (_ for _ in ()).throw(
            OutputLimitExceeded("output budget exhausted")
        ),
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(OutputLimitExceeded):
        backend.run(["ffmpeg", "out.mp4"])

    assert process.terminate_called
    assert process.kill_called


@pytest.mark.parametrize(
    "race_at",
    ["terminate", "wait", "kill", "final_wait"],
)
def test_ffmpeg_backend_preserves_limit_error_during_exit_race(
    race_at: str,
) -> None:
    class RacingPopen(FakePopen):
        def __init__(self) -> None:
            super().__init__([subprocess.TimeoutExpired("ffmpeg", 0.01)])
            self.wait_calls = 0

        def terminate(self) -> None:
            super().terminate()
            if race_at == "terminate":
                raise ProcessLookupError

        def kill(self) -> None:
            super().kill()
            if race_at == "kill":
                raise ProcessLookupError

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if race_at == "wait":
                raise ProcessLookupError
            if self.wait_calls == 1 and race_at in {"kill", "final_wait"}:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            if race_at == "final_wait":
                raise ProcessLookupError
            return super().wait(timeout)

    process = RacingPopen()
    backend = FfmpegBackend(
        output_guard=lambda: (_ for _ in ()).throw(
            OutputLimitExceeded("primary output limit error")
        ),
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(OutputLimitExceeded, match="primary output limit error"):
        backend.run(["ffmpeg", "out.mp4"])


def test_ffmpeg_backend_wraps_posix_command_without_preexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakePopen([("", "encode failed")], returncode=23)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(
        ffmpeg_backend,
        "which",
        lambda executable: "/usr/bin/prlimit" if executable == "prlimit" else None,
    )
    backend = FfmpegBackend(
        popen=_popen_factory(process, calls),
        max_file_bytes=123_456,
    )

    result = backend.run(["ffmpeg", "-i", "input.mp4", "out.mp4"])

    assert calls[0][0] == [
        "/usr/bin/prlimit",
        "--fsize=123456",
        "--",
        "ffmpeg",
        "-i",
        "input.mp4",
        "out.mp4",
    ]
    assert "preexec_fn" not in calls[0][1]
    assert calls[0][1]["start_new_session"] is True
    assert result.returncode == 23
    assert result.stderr == "encode failed"


def test_ffmpeg_backend_signals_posix_process_group_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")

    class GroupedPopen(FakePopen):
        pid = 4321

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            self.returncode = 0
            return 0

    process = GroupedPopen([timeout])
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sent_signal: signals.append(
            (process_group, sent_signal)
        ),
        raising=False,
    )
    backend = FfmpegBackend(
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, 9),
    ]
    assert not process.terminate_called
    assert not process.kill_called


def test_ffmpeg_backend_kills_posix_group_when_root_waits_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")

    class GroupedPopen(FakePopen):
        pid = 4321

    process = GroupedPopen([timeout])
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sent_signal: signals.append(
            (process_group, sent_signal)
        ),
        raising=False,
    )
    backend = FfmpegBackend(popen=_popen_factory(process, []))

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, 9),
    ]
    assert process.wait_calls == [
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ]


def test_ffmpeg_backend_uses_taskkill_when_job_assignment_fails_and_root_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")

    class GroupedPopen(FakePopen):
        pid = 4321

        def __init__(self) -> None:
            super().__init__([timeout])
            self.signals: list[int] = []

        def send_signal(self, sent_signal: int) -> None:
            self.signals.append(sent_signal)

    process = GroupedPopen()
    taskkill_calls: list[tuple[int, float]] = []
    ctrl_break = 1
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", ctrl_break, raising=False)
    monkeypatch.setattr(
        ffmpeg_backend,
        "_create_windows_job",
        lambda _process: None,
        raising=False,
    )
    monkeypatch.setattr(
        ffmpeg_backend,
        "_run_windows_taskkill",
        lambda pid, timeout_seconds: taskkill_calls.append((pid, timeout_seconds)) or 0,
    )
    backend = FfmpegBackend(popen=_popen_factory(process, []))

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert process.signals == [ctrl_break]
    assert taskkill_calls == [(process.pid, 5.0)]
    assert process.wait_calls == [
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ]


def test_ffmpeg_backend_falls_back_when_windows_taskkill_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")

    class GroupedPopen(FakePopen):
        pid = 4321

        def __init__(self) -> None:
            super().__init__([timeout], wait_times_out=True)
            self.signals: list[int] = []

        def send_signal(self, sent_signal: int) -> None:
            self.signals.append(sent_signal)

    process = GroupedPopen()
    taskkill_calls: list[tuple[int, float]] = []
    ctrl_break = 1

    def taskkill(pid: int, timeout_seconds: float) -> int:
        taskkill_calls.append((pid, timeout_seconds))
        raise subprocess.TimeoutExpired("taskkill", timeout_seconds)

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", ctrl_break, raising=False)
    monkeypatch.setattr(ffmpeg_backend, "_run_windows_taskkill", taskkill)
    backend = FfmpegBackend(
        poll_interval=0.01,
        popen=_popen_factory(process, []),
    )

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert process.signals == [ctrl_break]
    assert taskkill_calls == [(process.pid, 5.0), (process.pid, 10.0)]
    cleanup_timeout = ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS
    assert cleanup_timeout > backend._poll_interval
    assert process.kill_called
    assert process.wait_calls == [cleanup_timeout, cleanup_timeout]


def test_windows_tree_terminates_captured_descendant_after_root_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Identity:
        def __init__(
            self,
            pid: int,
            parent_pid: int,
            creation_time: int,
        ) -> None:
            self.pid = pid
            self.parent_pid = parent_pid
            self.creation_time = creation_time
            self.handle = f"handle-{pid}"

    class FakeWindowsProcessApi:
        def __init__(self) -> None:
            self.identities = {
                4321: Identity(4321, 1, 100),
                5432: Identity(5432, 4321, 200),
                6543: Identity(6543, 5432, 225),
            }
            self.active = {4321, 5432, 6543}
            self.terminated: list[int] = []
            self.closed: list[str] = []

        def snapshot(self) -> list[Identity]:
            return list(self.identities.values())

        def open_process(self, pid: int) -> Identity | None:
            return self.identities.get(pid)

        def exit_time(self, identity: Identity) -> int | None:
            if identity.pid == 4321 and identity.pid not in self.active:
                return 250
            return None

        def is_active(self, identity: Identity) -> bool:
            return identity.pid in self.active

        def terminate(self, identity: Identity) -> None:
            self.terminated.append(identity.pid)
            self.active.discard(identity.pid)

        def wait(self, identity: Identity, timeout_seconds: float) -> None:
            del identity, timeout_seconds

        def close(self, identity: Identity) -> None:
            self.closed.append(identity.handle)

    api = FakeWindowsProcessApi()
    taskkill_timeouts: list[float] = []

    def time_out_taskkill(pid: int, timeout_seconds: float) -> int:
        assert pid == 4321
        taskkill_timeouts.append(timeout_seconds)
        api.active.discard(pid)
        api.identities[7654] = Identity(7654, 4321, 300)
        api.active.add(7654)
        raise subprocess.TimeoutExpired("taskkill", timeout_seconds)

    monkeypatch.setattr(
        ffmpeg_backend,
        "_run_windows_taskkill",
        time_out_taskkill,
        raising=False,
    )
    tree = ffmpeg_backend._WindowsProcessTree(  # type: ignore[attr-defined]
        4321,
        api=api,
        poll_interval=60,
    )

    tree.terminate()
    tree.close()

    assert taskkill_timeouts == [5.0, 10.0]
    assert api.terminated == [6543, 5432]
    assert sorted(set(api.closed)) == [
        "handle-4321",
        "handle-5432",
        "handle-6543",
        "handle-7654",
    ]
    assert api.closed.count("handle-7654") == 2


def test_windows_taskkill_timeout_kills_and_reaps_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungTaskkill:
        returncode = 1

        def __init__(self) -> None:
            self.kill_called = False
            self.wait_calls: list[float] = []

        def wait(self, timeout: float) -> int:
            self.wait_calls.append(timeout)
            if not self.kill_called:
                raise subprocess.TimeoutExpired("taskkill", timeout)
            return self.returncode

        def kill(self) -> None:
            self.kill_called = True

    taskkill = HungTaskkill()
    popen_calls: list[tuple[list[str], dict[str, Any]]] = []

    def create(command: list[str], **kwargs: Any) -> HungTaskkill:
        popen_calls.append((command, kwargs))
        return taskkill

    monkeypatch.setattr(subprocess, "Popen", create)

    with pytest.raises(subprocess.TimeoutExpired):
        ffmpeg_backend._run_windows_taskkill(4321, 5.0)  # type: ignore[attr-defined]

    assert taskkill.kill_called
    assert taskkill.wait_calls == [5.0, 5.0]
    assert popen_calls[0][0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    assert popen_calls[0][1]["stdout"] is subprocess.DEVNULL
    assert popen_calls[0][1]["stderr"] is subprocess.DEVNULL


def test_windows_tree_closes_candidate_handle_when_capture_fails() -> None:
    class Identity:
        def __init__(self, pid: int, parent_pid: int) -> None:
            self.pid = pid
            self.parent_pid = parent_pid
            self.creation_time = pid
            self.handle = f"handle-{pid}"

    class FailingWindowsProcessApi:
        def __init__(self) -> None:
            self.identities = {
                100: Identity(100, 1),
                200: Identity(200, 100),
            }
            self.closed: list[str] = []

        def snapshot(self) -> list[Identity]:
            return list(self.identities.values())

        def open_process(self, pid: int) -> Identity:
            return self.identities[pid]

        def exit_time(self, identity: Identity) -> int | None:
            del identity
            raise OSError("GetProcessTimes failed")

        def close(self, identity: Identity) -> None:
            self.closed.append(identity.handle)

    api = FailingWindowsProcessApi()

    with pytest.raises(OSError, match="GetProcessTimes failed"):
        ffmpeg_backend._WindowsProcessTree(  # type: ignore[attr-defined]
            100,
            api=api,
        )

    assert sorted(api.closed) == ["handle-100", "handle-200"]


def _windows_process_is_active(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
            exit_code.value == still_active
        )
    finally:
        close_handle(handle)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_ffmpeg_backend_job_kills_descendant_that_ignores_break(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_script = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
        "open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_script = (
        "import os, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGBREAK, lambda *_args: os._exit(0)); "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}, sys.argv[1]]); "
        "time.sleep(1)"
    )

    def reject_when_child_is_ready() -> None:
        if child_pid_path.exists():
            raise OutputLimitExceeded("stop the process tree")

    backend = FfmpegBackend(
        output_guard=reject_when_child_is_ready,
        poll_interval=0.05,
    )
    child_pid: int | None = None
    try:
        with pytest.raises(OutputLimitExceeded, match="stop the process tree"):
            backend.run([sys.executable, "-c", parent_script, str(child_pid_path)])
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 2
        while _windows_process_is_active(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _windows_process_is_active(child_pid)
    finally:
        if child_pid is not None and _windows_process_is_active(child_pid):
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows process handles")
def test_ffmpeg_backend_fallback_kills_child_after_root_exits_during_taskkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "fallback-child.pid"
    child_script = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
        "open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_script = (
        "import os, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGBREAK, lambda *_args: os._exit(0)); "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}, sys.argv[1]]); "
        "time.sleep(60)"
    )
    taskkill_calls: list[tuple[int, float]] = []

    def timeout_after_break(pid: int, timeout_seconds: float) -> int:
        taskkill_calls.append((pid, timeout_seconds))
        if len(taskkill_calls) == 1:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 2
            while _windows_process_is_active(pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _windows_process_is_active(pid)
        raise subprocess.TimeoutExpired("taskkill", timeout_seconds)

    def reject_when_child_is_ready() -> None:
        if child_pid_path.exists():
            raise OutputLimitExceeded("fallback must retain child ownership")

    monkeypatch.setattr(ffmpeg_backend, "_create_windows_job", lambda _process: None)
    monkeypatch.setattr(
        ffmpeg_backend,
        "_run_windows_taskkill",
        timeout_after_break,
    )
    backend = FfmpegBackend(
        output_guard=reject_when_child_is_ready,
        poll_interval=0.05,
    )
    child_pid: int | None = None
    try:
        with pytest.raises(
            OutputLimitExceeded,
            match="fallback must retain child ownership",
        ):
            backend.run([sys.executable, "-c", parent_script, str(child_pid_path)])
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 2
        while _windows_process_is_active(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _windows_process_is_active(child_pid)
        root_pid = taskkill_calls[0][0]
        assert taskkill_calls == [(root_pid, 5.0), (root_pid, 10.0)]
    finally:
        if child_pid is not None and _windows_process_is_active(child_pid):
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def test_ffmpeg_backend_falls_back_to_monitor_when_prlimit_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "out.mp4"

    class GrowingOutputPopen(FakePopen):
        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            del timeout
            target.write_bytes(b"x" * 80)
            raise subprocess.TimeoutExpired("ffmpeg", 0.01)

    process = GrowingOutputPopen([])
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(ffmpeg_backend, "which", lambda _executable: None)
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, max_bytes=80, safety_bytes=1),
        poll_interval=0.01,
        popen=_popen_factory(process, calls),
        max_file_bytes=79,
    )

    with pytest.raises(OutputLimitExceeded, match="80-byte limit"):
        backend.run(["ffmpeg", str(target)])

    assert calls[0][0] == ["ffmpeg", str(target)]
    assert "preexec_fn" not in calls[0][1]
    assert process.terminate_called


def test_ffmpeg_backend_keeps_windows_command_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakePopen([("", "")])
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(
        ffmpeg_backend,
        "which",
        lambda _executable: (_ for _ in ()).throw(
            AssertionError("Windows must not search for prlimit")
        ),
    )
    backend = FfmpegBackend(
        popen=_popen_factory(process, calls),
        max_file_bytes=123_456,
    )

    result = backend.run(["ffmpeg", "out.mp4"])

    assert calls[0][0] == ["ffmpeg", "out.mp4"]
    assert "preexec_fn" not in calls[0][1]
    assert calls[0][1]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    assert result.returncode == 0
