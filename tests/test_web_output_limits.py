from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Timer
from typing import Any

import pytest
from rq.timeouts import JobTimeoutException, TimerDeathPenalty

from designtoolkit.video_frames import ffmpeg_backend
from designtoolkit.video_frames.ffmpeg_backend import (
    BackendUnavailableError,
    FfmpegBackend,
)
from designtoolkit.web.output_limits import (
    OutputBudgetGuard,
    OutputLimitExceeded,
    OutputTargetMonitor,
)


@pytest.fixture(autouse=True)
def _default_ffmpeg_tests_to_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")


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


class FakeWindowsLaunchGate:
    def __init__(self) -> None:
        self.launcher_command = [sys.executable, "-c", "raise SystemExit(0)"]
        self.event_name = "Local\\DesignToolkit.FFmpeg.test"
        self.startup_error_token = "trusted-startup-error-token"
        self.startup_error: OSError | None = None
        self.start_calls = 0
        self.claim_calls = 0
        self.close_calls = 0
        self.closed = False
        self.has_job = False

    def start_launcher(
        self,
        popen: Any,
        popen_kwargs: dict[str, object],
    ) -> FakePopen:
        self.start_calls += 1
        return popen(self.launcher_command, **popen_kwargs)

    def prepare(self, process: FakePopen) -> None:
        del process
        self.claim_calls += 1
        self.has_job = True

    def release(self) -> None:
        pass

    def read_startup_error(self, returncode: int | None) -> OSError | None:
        return self.startup_error if returncode == 126 else None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_calls += 1


def _windows_startup_error_sentinel(
    event_name: str,
    token: str,
    error: OSError,
) -> str:
    payload = {
        "event": event_name,
        "token": token,
        "type": type(error).__name__,
        "errno": error.errno,
        "winerror": getattr(error, "winerror", None),
        "filename": error.filename,
        "strerror": error.strerror,
        "message": str(error),
    }
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return f"DESIGNTOOLKIT_FFMPEG_STARTUP_ERROR:{encoded}\n"


class FakeWindowsKernelApi:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.closed: list[str] = []

    def _fail(self, operation: str) -> None:
        if self.failure == operation:
            raise OSError(f"{operation} failed")

    def create_event(self, name: str) -> str:
        assert name.startswith("Local\\DesignToolkit.FFmpeg.")
        self._fail("create_event")
        return "event"

    def create_job(self) -> str:
        self._fail("create_job")
        return "job"

    def set_job_kill_on_close(self, job_handle: object) -> None:
        assert job_handle == "job"
        self._fail("set_info")

    def assign_process(self, job_handle: object, process_handle: object) -> None:
        assert (job_handle, process_handle) == ("job", 456)
        self._fail("assign")

    def set_event(self, event_handle: object) -> None:
        assert event_handle == "event"
        self._fail("set_event")

    def close_handle(self, handle: object) -> None:
        self.closed.append(str(handle))


@pytest.mark.parametrize(
    ("failure", "expected_closed", "starts_launcher", "terminates_launcher"),
    [
        ("create_event", [], False, False),
        ("create_job", ["event"], True, True),
        ("set_info", ["event", "job"], True, False),
        ("assign", ["event", "job"], True, False),
        ("set_event", ["event", "job"], True, False),
    ],
)
def test_windows_gate_setup_failure_is_fail_closed_without_handle_leaks(
    failure: str,
    expected_closed: list[str],
    starts_launcher: bool,
    terminates_launcher: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakePopen([])
    process._handle = 456  # type: ignore[attr-defined]
    calls: list[tuple[list[str], dict[str, Any]]] = []
    api = FakeWindowsKernelApi(failure)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(
        popen=_popen_factory(process, calls),
        windows_launch_gate_factory=lambda command: (
            ffmpeg_backend._WindowsLaunchGate.create(command, api=api)
        ),
    )

    with pytest.raises(
        BackendUnavailableError,
        match="Windows FFmpeg launch containment",
    ):
        backend.run(["ffmpeg", "out.mp4"])

    assert bool(calls) is starts_launcher
    assert process.terminate_called is terminates_launcher
    assert sorted(api.closed) == sorted(expected_closed)


def test_windows_gate_create_job_failure_reaps_launcher_when_terminate_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class TerminateDeniedPopen(FakePopen):
        def terminate(self) -> None:
            super().terminate()
            raise PermissionError("terminate denied")

    process = TerminateDeniedPopen([], wait_times_out=True)
    process._handle = 456  # type: ignore[attr-defined]
    api = FakeWindowsKernelApi("create_job")
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(
        popen=_popen_factory(process, []),
        windows_launch_gate_factory=lambda command: (
            ffmpeg_backend._WindowsLaunchGate.create(command, api=api)
        ),
    )
    caplog.set_level("ERROR", logger="designtoolkit.video_frames.ffmpeg_backend")

    with pytest.raises(BackendUnavailableError) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "create_job failed"
    assert process.terminate_called
    assert process.kill_called
    assert process.wait_calls == [
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ]
    assert api.closed == ["event"]
    assert "terminate Windows FFmpeg launcher" in caplog.text
    assert "wait for Windows FFmpeg launcher" in caplog.text


@pytest.mark.parametrize(
    "interrupted_stage",
    [
        "factory",
        "popen",
        "create_job",
        "set_info",
        "assign",
        "set_event",
    ],
)
def test_windows_launch_transaction_cancels_each_blocked_startup_stage(
    interrupted_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = JobTimeoutException("worker timed out during Windows launch")
    stage_entered = Event()
    release_stage = Event()
    transactions: list[Any] = []

    class BlockingGate(FakeWindowsLaunchGate):
        def __init__(self) -> None:
            super().__init__()
            self.has_job = False
            self.target_alive = False
            self.target_was_released = False

        def prepare(self, process: FakePopen) -> None:
            del process
            self.claim_calls += 1
            for operation in ("create_job", "set_info", "assign"):
                if operation == "create_job":
                    self.has_job = True
                if interrupted_stage == operation:
                    stage_entered.set()
                    assert release_stage.wait(2)

        def release(self) -> None:
            self.target_alive = True
            self.target_was_released = True
            if interrupted_stage == "set_event":
                stage_entered.set()
                assert release_stage.wait(2)

        def close(self) -> None:
            self.target_alive = False
            super().close()

    gate = BlockingGate()
    process = FakePopen([], wait_times_out=True)
    process._handle = 456  # type: ignore[attr-defined]
    real_transaction = ffmpeg_backend._WindowsLaunchTransaction

    def create_transaction(*args: object, **kwargs: object) -> Any:
        transaction = real_transaction(*args, **kwargs)
        original_cancel = transaction.cancel

        def interrupting_wait() -> Any:
            assert stage_entered.wait(2)
            raise interruption

        def cancel() -> None:
            if interrupted_stage == "set_event":
                release_stage.set()
                original_cancel()
            else:
                original_cancel()
                release_stage.set()

        transaction.wait = interrupting_wait
        transaction.cancel = cancel
        transactions.append(transaction)
        return transaction

    def create_gate(command: list[str]) -> BlockingGate:
        del command
        if interrupted_stage == "factory":
            stage_entered.set()
            assert release_stage.wait(2)
        return gate

    def popen(command: list[str], **kwargs: object) -> FakePopen:
        del command, kwargs
        if interrupted_stage == "popen":
            stage_entered.set()
            assert release_stage.wait(2)
        return process

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(
        ffmpeg_backend,
        "_WindowsLaunchTransaction",
        create_transaction,
    )
    backend = FfmpegBackend(
        popen=popen,
        windows_launch_gate_factory=create_gate,
    )

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is interruption
    assert gate.closed
    assert not gate.target_alive
    assert gate.target_was_released is (interrupted_stage == "set_event")
    if interrupted_stage == "factory":
        assert gate.start_calls == 0
    else:
        assert process.kill_called
        assert process.returncode is not None
    assert not transactions[0]._thread.is_alive()


def test_windows_launch_transaction_eventually_cleans_up_after_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = JobTimeoutException("worker timed out during Windows launch")
    stage_entered = Event()
    release_stage = Event()
    gate = FakeWindowsLaunchGate()
    transactions: list[Any] = []
    real_transaction = ffmpeg_backend._WindowsLaunchTransaction

    def create_transaction(*args: object, **kwargs: object) -> Any:
        transaction = real_transaction(*args, **kwargs)

        def interrupting_wait() -> Any:
            assert stage_entered.wait(2)
            raise interruption

        transaction.wait = interrupting_wait
        transactions.append(transaction)
        return transaction

    def create_gate(command: list[str]) -> FakeWindowsLaunchGate:
        del command
        stage_entered.set()
        assert release_stage.wait(2)
        return gate

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(
        ffmpeg_backend,
        "_WINDOWS_LAUNCH_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        ffmpeg_backend,
        "_WindowsLaunchTransaction",
        create_transaction,
    )
    backend = FfmpegBackend(windows_launch_gate_factory=create_gate)

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is interruption
    transaction = transactions[0]
    assert transaction._thread.daemon
    assert transaction._thread.is_alive()

    release_stage.set()
    assert transaction._done.wait(2)
    transaction._thread.join(timeout=2)
    assert not transaction._thread.is_alive()
    assert gate.closed


def test_windows_launch_transaction_preserves_gate_factory_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = JobTimeoutException("factory timed out")

    def create_gate(_command: list[str]) -> FakeWindowsLaunchGate:
        raise interruption

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(windows_launch_gate_factory=create_gate)

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is interruption


def test_windows_launch_wait_delivers_timer_timeout_before_prepare_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeTimeout(JobTimeoutException):
        pass

    marker = tmp_path / "target-started.txt"
    prepare_started = Event()
    cleanup_finished = Event()
    transactions: list[Any] = []

    class DelayedGate(FakeWindowsLaunchGate):
        def prepare(self, process: FakePopen) -> None:
            del process
            self.has_job = True
            prepare_started.set()
            time.sleep(0.2)

        def release(self) -> None:
            marker.touch()

        def close(self) -> None:
            super().close()
            cleanup_finished.set()

    gate = DelayedGate()
    process = FakePopen([], wait_times_out=True)
    real_transaction = ffmpeg_backend._WindowsLaunchTransaction

    def create_transaction(*args: object, **kwargs: object) -> Any:
        transaction = real_transaction(*args, **kwargs)
        transactions.append(transaction)
        return transaction

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(
        ffmpeg_backend,
        "_WINDOWS_LAUNCH_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        ffmpeg_backend,
        "_WindowsLaunchTransaction",
        create_transaction,
    )
    backend = FfmpegBackend(
        popen=_popen_factory(process, []),
        windows_launch_gate_factory=lambda _command: gate,
    )

    started_at = time.monotonic()
    with pytest.raises(ProbeTimeout):
        with TimerDeathPenalty(0.05, exception=ProbeTimeout):
            backend.run(["ffmpeg", "out.mp4"])
    elapsed = time.monotonic() - started_at

    transaction = transactions[0]
    assert prepare_started.is_set()
    assert elapsed < 0.15
    assert not marker.exists()
    assert transaction._thread.daemon
    assert transaction._thread.is_alive()

    assert cleanup_finished.wait(1)
    transaction._thread.join(timeout=1)
    assert not transaction._thread.is_alive()
    assert gate.closed
    assert process.kill_called
    assert not marker.exists()


def test_windows_launch_wait_delivers_timer_timeout_when_worker_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeTimeout(JobTimeoutException):
        pass

    prepare_started = Event()
    release_prepare = Event()
    cleanup_finished = Event()
    transactions: list[Any] = []

    class BlockingGate(FakeWindowsLaunchGate):
        def prepare(self, process: FakePopen) -> None:
            del process
            self.has_job = True
            prepare_started.set()
            assert release_prepare.wait(2)

        def close(self) -> None:
            super().close()
            cleanup_finished.set()

    gate = BlockingGate()
    process = FakePopen([], wait_times_out=True)
    real_transaction = ffmpeg_backend._WindowsLaunchTransaction

    def create_transaction(*args: object, **kwargs: object) -> Any:
        transaction = real_transaction(*args, **kwargs)
        transactions.append(transaction)
        return transaction

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(
        ffmpeg_backend,
        "_WINDOWS_LAUNCH_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        ffmpeg_backend,
        "_WindowsLaunchTransaction",
        create_transaction,
    )
    backend = FfmpegBackend(
        popen=_popen_factory(process, []),
        windows_launch_gate_factory=lambda _command: gate,
    )

    deadlock_guard = Timer(0.3, release_prepare.set)
    deadlock_guard.start()
    started_at = time.monotonic()
    try:
        with pytest.raises(ProbeTimeout):
            with TimerDeathPenalty(0.05, exception=ProbeTimeout):
                backend.run(["ffmpeg", "out.mp4"])
        elapsed = time.monotonic() - started_at

        transaction = transactions[0]
        assert prepare_started.is_set()
        assert elapsed < 0.15
        assert transaction._thread.daemon
        assert transaction._thread.is_alive()
    finally:
        deadlock_guard.cancel()
        release_prepare.set()

    assert cleanup_finished.wait(1)
    transaction._thread.join(timeout=1)
    assert not transaction._thread.is_alive()
    assert gate.closed
    assert process.kill_called


def test_windows_launch_transaction_cancel_wins_before_commit(tmp_path: Path) -> None:
    prepared = Event()
    continue_commit = Event()
    operations: list[str] = []
    marker = tmp_path / "target-started.txt"

    class PreparedGate(FakeWindowsLaunchGate):
        has_job = True

        def prepare(self, process: FakePopen) -> None:
            del process
            operations.append("prepare")
            self.has_job = True
            prepared.set()
            assert continue_commit.wait(2)

        def release(self) -> None:
            operations.append("set_event")
            marker.touch()

        def close(self) -> None:
            operations.append("close")
            super().close()

    process = FakePopen([], wait_times_out=True)
    gate = PreparedGate()
    transaction = ffmpeg_backend._WindowsLaunchTransaction(
        ["ffmpeg", "out.mp4"],
        lambda _command: gate,  # type: ignore[arg-type]
        _popen_factory(process, []),  # type: ignore[arg-type]
        {},
        FfmpegBackend._abort_windows_launch,
    )

    transaction.start()
    assert prepared.wait(2)
    transaction.cancel()
    continue_commit.set()
    transaction.wait_for_cleanup()

    assert "set_event" not in operations
    assert not marker.exists()
    assert gate.closed
    assert process.kill_called
    assert not transaction._thread.is_alive()


def test_windows_launch_transaction_commit_wins_and_cancel_aborts_immediately() -> None:
    worker_waiting = Event()
    release_worker = Event()
    operations: list[str] = []

    class BlockingDecision:
        def set(self) -> None:
            pass

        def wait(self) -> None:
            worker_waiting.set()
            assert release_worker.wait(2)

    class PreparedGate(FakeWindowsLaunchGate):
        has_job = True
        target_alive = False

        def prepare(self, process: FakePopen) -> None:
            del process
            operations.append("prepare")
            self.has_job = True

        def release(self) -> None:
            operations.append("set_event")
            self.target_alive = True

        def close(self) -> None:
            operations.append("close")
            self.target_alive = False
            super().close()

    process = FakePopen([], wait_times_out=True)
    gate = PreparedGate()
    transaction = ffmpeg_backend._WindowsLaunchTransaction(
        ["ffmpeg", "out.mp4"],
        lambda _command: gate,  # type: ignore[arg-type]
        _popen_factory(process, []),  # type: ignore[arg-type]
        {},
        FfmpegBackend._abort_windows_launch,
    )
    transaction._decision = BlockingDecision()  # type: ignore[assignment]

    transaction.start()
    assert transaction.wait().process is process
    assert worker_waiting.wait(2)
    transaction.cancel()

    assert operations == ["prepare", "set_event", "close"]
    assert not gate.target_alive
    assert process.kill_called

    release_worker.set()
    transaction.wait_for_cleanup()
    assert not transaction._thread.is_alive()


def test_windows_launcher_popen_failure_preserves_original_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CleanupFailure(BaseException):
        pass

    gate = FakeWindowsLaunchGate()
    original_error = FileNotFoundError("launcher is missing")

    def fail_to_launch(
        _command: list[str],
        **_kwargs: Any,
    ) -> FakePopen:
        raise original_error

    def fail_to_close() -> None:
        gate.close_calls += 1
        raise CleanupFailure("close failed")

    def fail_to_log(*_args: object, **_kwargs: object) -> None:
        raise CleanupFailure("logging failed")

    gate.close = fail_to_close  # type: ignore[method-assign]
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(ffmpeg_backend._LOGGER, "exception", fail_to_log)
    backend = FfmpegBackend(
        popen=fail_to_launch,
        windows_launch_gate_factory=lambda _command: gate,
    )

    with pytest.raises(FileNotFoundError) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is original_error
    assert gate.close_calls == 1


def test_windows_gate_setup_failure_closes_job_before_kill_even_if_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    operations: list[str] = []

    class JobOwningGate(FakeWindowsLaunchGate):
        has_job = True

        def prepare(self, process: FakePopen) -> None:
            del process
            self.has_job = True
            raise ffmpeg_backend._WindowsContainmentError(OSError("assign failed"))

        def close(self) -> None:
            operations.append("close")
            raise OSError("close failed")

    class ReapTrackingPopen(FakePopen):
        def terminate(self) -> None:
            operations.append("terminate")
            super().terminate()

        def kill(self) -> None:
            operations.append("kill")
            super().kill()

        def wait(self, timeout: float | None = None) -> int:
            operations.append("wait")
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            self.returncode = 1
            return 1

    process = ReapTrackingPopen([])
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(
        popen=_popen_factory(process, []),
        windows_launch_gate_factory=lambda _command: JobOwningGate(),
    )
    caplog.set_level("ERROR", logger="designtoolkit.video_frames.ffmpeg_backend")

    with pytest.raises(BackendUnavailableError) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "assign failed"
    assert operations == ["close", "wait", "kill", "wait"]
    assert process.wait_calls == [
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
        ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ]
    assert "close Windows FFmpeg launch gate" in caplog.text


@pytest.mark.parametrize(
    "startup_error",
    [
        FileNotFoundError(2, "missing executable", "missing.exe", 2),
        PermissionError(13, "permission denied", "denied.exe", 5),
        OSError(123, "operating system failure", "broken.exe", 456),
    ],
    ids=["file-not-found", "permission", "os-error"],
)
def test_windows_launcher_rebuilds_status_pipe_startup_error(
    startup_error: OSError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = FakeWindowsLaunchGate()
    gate.startup_error = startup_error
    process = FakePopen([("", "launcher diagnostic")], returncode=126)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(
        popen=_popen_factory(process, []),
        windows_launch_gate_factory=lambda _command: gate,
    )

    with pytest.raises(type(startup_error)) as exc_info:
        backend.run(["missing.exe"])

    rebuilt = exc_info.value
    assert rebuilt.errno == startup_error.errno
    assert getattr(rebuilt, "winerror", None) == getattr(
        startup_error,
        "winerror",
        None,
    )
    assert rebuilt.filename == startup_error.filename
    assert rebuilt.strerror == startup_error.strerror
    assert str(rebuilt) == str(startup_error)


def test_windows_launcher_does_not_treat_stderr_as_startup_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = FakeWindowsLaunchGate()
    stderr = _windows_startup_error_sentinel(
        gate.event_name,
        gate.startup_error_token,
        FileNotFoundError(2, "spoofed missing executable", "spoof.exe", 2),
    )
    process = FakePopen([("", stderr)], returncode=126)
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(
        popen=_popen_factory(process, []),
        windows_launch_gate_factory=lambda _command: gate,
    )

    result = backend.run(["real-target.exe"])

    assert result.returncode == 126
    assert result.stderr == stderr


@pytest.mark.skipif(os.name != "nt", reason="requires Windows handle lists")
def test_windows_gate_scopes_status_writer_to_injected_launcher_popen() -> None:
    process = FakePopen([])
    observed: dict[str, object] = {}
    gate = ffmpeg_backend._WindowsLaunchGate.create(
        ["ffmpeg"],
        api=FakeWindowsKernelApi(""),
    )

    def popen(command: list[str], **kwargs: object) -> FakePopen:
        observed["command"] = command
        observed["kwargs"] = kwargs
        handle = int(command[4])
        observed["inheritable"] = os.get_handle_inheritable(handle)
        return process

    try:
        assert gate.start_launcher(popen, {"stderr": subprocess.PIPE}) is process
    finally:
        gate.close()

    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    startupinfo = kwargs["startupinfo"]
    assert kwargs["close_fds"] is True
    assert startupinfo.lpAttributeList == {  # type: ignore[union-attr]
        "handle_list": [int(gate.launcher_command[4])]
    }
    assert observed["inheritable"] is True


def test_windows_gate_close_attempts_event_after_job_close_failure() -> None:
    class CloseFailingKernelApi(FakeWindowsKernelApi):
        def close_handle(self, handle: object) -> None:
            super().close_handle(handle)
            if handle == "job":
                raise PermissionError("job close denied")

    api = CloseFailingKernelApi("")
    gate = ffmpeg_backend._WindowsLaunchGate.create(["ffmpeg"], api=api)
    process = FakePopen([])
    process._handle = 456  # type: ignore[attr-defined]
    gate.prepare(process)  # type: ignore[arg-type]
    gate.release()

    with pytest.raises(PermissionError, match="job close denied"):
        gate.close()

    assert api.closed == ["job", "event"]


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


def test_ffmpeg_backend_releases_windows_gate_once_and_preserves_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakePopen([("ignored stdout", "diagnostic")], returncode=23)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    gate = FakeWindowsLaunchGate()
    gated_commands: list[list[str]] = []
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")

    def create_gate(command: list[str]) -> FakeWindowsLaunchGate:
        gated_commands.append(command)
        return gate

    backend = FfmpegBackend(
        popen=_popen_factory(process, calls),
        windows_launch_gate_factory=create_gate,
    )

    result = backend.run(["ffmpeg", "out.mp4"])

    assert result.returncode == 23
    assert result.stderr == "diagnostic"
    assert gated_commands == [["ffmpeg", "out.mp4"]]
    assert calls[0][0] == gate.launcher_command
    assert gate.claim_calls == 1
    assert gate.close_calls == 1


def test_ffmpeg_backend_closes_windows_job_before_reaping_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")
    process = FakePopen([timeout])
    gate = FakeWindowsLaunchGate()
    close_before_wait: list[bool] = []
    original_wait = process.wait

    def wait(timeout: float | None = None) -> int:
        close_before_wait.append(gate.closed)
        return original_wait(timeout)

    process.wait = wait  # type: ignore[method-assign]
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(
        popen=_popen_factory(process, []),
        windows_launch_gate_factory=lambda _command: gate,
    )

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert process.wait_calls == [ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS]
    assert close_before_wait == [True]
    assert gate.close_calls == 1


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

    process = GroupedPopen([timeout])
    operations: list[tuple[str, int]] = []
    original_wait = process.wait

    def wait(timeout: float | None = None) -> int:
        operations.append(("wait", process.pid))
        return original_wait(timeout)

    process.wait = wait  # type: ignore[method-assign]
    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sent_signal: operations.append(
            ("signal", sent_signal)
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
    assert operations == [
        ("signal", signal.SIGTERM),
        ("signal", 9),
        ("wait", process.pid),
    ]
    assert process.wait_calls == [ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS]
    assert not process.terminate_called
    assert not process.kill_called


def test_ffmpeg_backend_does_not_signal_reused_posix_group_after_term_esrch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out")

    class GroupedPopen(FakePopen):
        pid = 4321

    process = GroupedPopen([timeout])
    signals: list[tuple[int, int]] = []

    def killpg(process_group: int, sent_signal: int) -> None:
        signals.append((process_group, sent_signal))
        raise ProcessLookupError

    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(
        os,
        "killpg",
        killpg,
        raising=False,
    )
    backend = FfmpegBackend(popen=_popen_factory(process, []))

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", "out.mp4"])

    assert exc_info.value is timeout
    assert signals == [(process.pid, signal.SIGTERM)]
    assert process.wait_calls == [ffmpeg_backend._PROCESS_CLEANUP_TIMEOUT_SECONDS]


def test_ffmpeg_backend_does_not_signal_reaped_posix_group_after_finish_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out after communicate")

    class GroupedPopen(FakePopen):
        pid = 4321

    process = GroupedPopen([("", "")])
    signals: list[tuple[int, int]] = []

    def fail_finish(_monitor: OutputTargetMonitor) -> None:
        raise timeout

    monkeypatch.setattr(ffmpeg_backend, "os_name", "posix")
    monkeypatch.setattr(ffmpeg_backend.OutputTargetMonitor, "finish", fail_finish)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, sent_signal: signals.append(
            (process_group, sent_signal)
        ),
        raising=False,
    )
    backend = FfmpegBackend(
        output_guard=OutputBudgetGuard(tmp_path, max_bytes=80, safety_bytes=1),
        popen=_popen_factory(process, []),
    )

    with pytest.raises(JobTimeoutException) as exc_info:
        backend.run(["ffmpeg", str(tmp_path / "out.mp4")])

    assert exc_info.value is timeout
    assert process.returncode == 0
    assert signals == []
    assert process.wait_calls == []


def test_ffmpeg_backend_does_not_signal_posix_group_reaped_by_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = JobTimeoutException("worker timed out before communicate reaped")

    class GroupedPopen(FakePopen):
        pid = 4321

        def __init__(self) -> None:
            super().__init__([timeout])
            self.poll_calls = 0

        def poll(self) -> int:
            self.poll_calls += 1
            self.returncode = 0
            return 0

    process = GroupedPopen()
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
    assert process.poll_calls == 1
    assert signals == []
    assert process.wait_calls == []


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


def _terminate_windows_process(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = open_process(0x0001, False, pid)
    if not handle:
        return
    try:
        terminate_process(handle, 1)
    finally:
        close_handle(handle)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows named events")
@pytest.mark.timeout(15)
def test_windows_launcher_open_event_failure_never_spawns_command(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist.txt"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
        str(marker),
    ]

    result = subprocess.run(
        ffmpeg_backend._windows_launcher_command(
            f"Local\\DesignToolkit.FFmpeg.missing-{time.monotonic_ns()}",
            "standalone-launcher-secret",
            command,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.timeout(15)
@pytest.mark.parametrize(
    "failing_method",
    ["set_job_kill_on_close", "assign_process", "set_event"],
    ids=["set-info", "assignment", "set-event"],
)
def test_windows_job_setup_failure_never_releases_marker_command(
    failing_method: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / f"{failing_method}.txt"
    api = ffmpeg_backend._WindowsKernelApi()

    def fail(*_args: object) -> None:
        raise OSError(f"{failing_method} failed")

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    monkeypatch.setattr(api, failing_method, fail)
    backend = FfmpegBackend(
        windows_launch_gate_factory=lambda command: (
            ffmpeg_backend._WindowsLaunchGate.create(command, api=api)
        ),
    )

    with pytest.raises(
        BackendUnavailableError,
        match="Windows FFmpeg launch containment",
    ):
        backend.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                str(marker),
            ]
        )

    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.timeout(15)
def test_windows_launcher_preserves_stderr_and_returncode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_stdout: list[str] = []

    class RecordingPopen:
        def __init__(self, process: subprocess.Popen[str]) -> None:
            self._process = process

        def __getattr__(self, name: str) -> Any:
            return getattr(self._process, name)

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            stdout, stderr = self._process.communicate(timeout=timeout)
            captured_stdout.append(stdout)
            return stdout, stderr

    def popen(command: list[str], **kwargs: Any) -> RecordingPopen:
        return RecordingPopen(subprocess.Popen(command, **kwargs))

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(popen=popen)
    arguments = ["space value", 'embedded"quote', "Unicode-\u89c6\u9891"]

    result = backend.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "sys.stdout.write(json.dumps(sys.argv[1:], ensure_ascii=False)); "
                "sys.stderr.write('diagnostic'); "
                "raise SystemExit(23)"
            ),
            *arguments,
        ]
    )

    assert result.returncode == 23
    assert result.stderr == "diagnostic"
    assert json.loads(captured_stdout[0]) == arguments


@pytest.mark.skipif(os.name != "nt", reason="requires Windows process inspection")
@pytest.mark.timeout(15)
def test_windows_target_cannot_forge_startup_error_from_launcher_commandline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_script = r"""
import ctypes
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes

query = (
    f"$currentPid = {os.getpid()}; $items = @(); "
    "for ($i = 0; $i -lt 8 -and $currentPid; $i++) { "
    "$p = Get-CimInstance Win32_Process "
    "-Filter ('ProcessId = ' + $currentPid); "
    "$items += [pscustomobject]@{"
    "ProcessId=$p.ProcessId; CommandLine=$p.CommandLine}; "
    "$currentPid = $p.ParentProcessId }; "
    "$items | ConvertTo-Json -Compress"
)
ancestor_json = subprocess.check_output(
    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
    text=True,
)
ancestors = json.loads(ancestor_json)
if isinstance(ancestors, dict):
    ancestors = [ancestors]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
launcher_matches = [
    (item, match)
    for item in ancestors
    if (
        match := re.search(
            r"\s(Local\\DesignToolkit\.FFmpeg\.[0-9a-f]+)\s+"
            r"(\d+)\s+[A-Za-z0-9+/=]+\s*$",
            item.get("CommandLine") or "",
        )
    )
]
if not launcher_matches:
    raise RuntimeError("launcher status capability was not readable")
error = FileNotFoundError(2, "spoofed missing executable", "spoof.exe", 2)
status_payload = {
    "type": type(error).__name__,
    "errno": error.errno,
    "winerror": getattr(error, "winerror", None),
    "filename": error.filename,
    "strerror": error.strerror,
    "message": str(error),
}
encoded_status = json.dumps(status_payload).encode("utf-8")
open_process = kernel32.OpenProcess
open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
open_process.restype = wintypes.HANDLE
duplicate_handle = kernel32.DuplicateHandle
duplicate_handle.argtypes = [
    wintypes.HANDLE,
    wintypes.HANDLE,
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
duplicate_handle.restype = wintypes.BOOL
get_current_process = kernel32.GetCurrentProcess
get_current_process.restype = wintypes.HANDLE
write_file = kernel32.WriteFile
write_file.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
write_file.restype = wintypes.BOOL
close_handle = kernel32.CloseHandle
close_handle.argtypes = [wintypes.HANDLE]
close_handle.restype = wintypes.BOOL
for launcher, match in launcher_matches:
    _event_name, exposed_value = match.groups()
    launcher_handle = open_process(0x0040, False, launcher["ProcessId"])
    if not launcher_handle:
        continue
    try:
        duplicate = wintypes.HANDLE()
        if not duplicate_handle(
            launcher_handle,
            wintypes.HANDLE(int(exposed_value)),
            get_current_process(),
            ctypes.byref(duplicate),
            0,
            False,
            0x00000002,
        ):
            continue
        try:
            written = wintypes.DWORD()
            write_file(
                duplicate,
                encoded_status,
                len(encoded_status),
                ctypes.byref(written),
                None,
            )
        finally:
            close_handle(duplicate)
    finally:
        close_handle(launcher_handle)
sys.stderr.write("target diagnostic")
raise SystemExit(23)
"""
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    deadline = time.monotonic() + 10

    def fail_if_backend_hangs() -> None:
        if time.monotonic() >= deadline:
            raise AssertionError("Windows launcher status writer remained open")

    result = FfmpegBackend(
        output_guard=fail_if_backend_hangs,
        poll_interval=0.05,
    ).run([sys.executable, "-c", target_script])

    assert result.returncode == 23
    assert result.stderr == "target diagnostic"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.timeout(15)
def test_windows_launcher_preserves_missing_executable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_executable = tmp_path / "does-not-exist.exe"
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")

    with pytest.raises(FileNotFoundError) as exc_info:
        FfmpegBackend().run([str(missing_executable)])

    error = exc_info.value
    assert error.errno == 2
    assert error.winerror == 2
    assert error.filename is None
    assert error.strerror
    assert str(error)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file descriptors")
@pytest.mark.timeout(15)
def test_windows_status_pipe_file_descriptors_do_not_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def open_file_descriptors() -> set[int]:
        descriptors: set[int] = set()
        for descriptor in range(2048):
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            descriptors.add(descriptor)
        return descriptors

    missing_executable = tmp_path / "does-not-exist.exe"
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend()
    before = open_file_descriptors()

    for _ in range(20):
        with pytest.raises(FileNotFoundError):
            backend.run([str(missing_executable)])

    assert open_file_descriptors() == before


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.timeout(15)
def test_windows_timer_death_penalty_during_popen_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeTimeout(JobTimeoutException):
        pass

    marker = tmp_path / "target-started.txt"
    launcher_created = Event()
    launchers: list[subprocess.Popen[str]] = []

    def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen(command, **kwargs)
        launchers.append(process)
        launcher_created.set()
        time.sleep(0.25)
        return process

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(popen=popen)

    with pytest.raises(ProbeTimeout):
        with TimerDeathPenalty(0.05, exception=ProbeTimeout):
            backend.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                ]
            )

    assert launcher_created.is_set()
    assert launchers
    assert launchers[0].returncode is not None
    assert not _windows_process_is_active(launchers[0].pid)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.timeout(15)
def test_ffmpeg_backend_job_kills_descendant_that_ignores_break(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
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
            _terminate_windows_process(child_pid)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
@pytest.mark.timeout(15)
def test_windows_timeout_closes_job_after_launcher_and_root_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "detached-child.pid"
    child_script = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
        "open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}, sys.argv[1]], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL); "
        "path = pathlib.Path(sys.argv[1]); "
        "deadline = time.monotonic() + 5; "
        "\nwhile not path.exists() and time.monotonic() < deadline: time.sleep(0.01)"
    )

    class TimeoutAfterLauncherExit:
        def __init__(self, process: subprocess.Popen[str]) -> None:
            self._process = process

        def __getattr__(self, name: str) -> object:
            return getattr(self._process, name)

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self._process.communicate(timeout=timeout)
            assert self._process.returncode == 0
            raise JobTimeoutException("worker timed out after launcher exit")

    def popen(command: list[str], **kwargs: Any) -> TimeoutAfterLauncherExit:
        return TimeoutAfterLauncherExit(subprocess.Popen(command, **kwargs))

    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend(
        poll_interval=0.05,
        popen=popen,
    )
    child_pid: int | None = None
    try:
        with pytest.raises(JobTimeoutException, match="after launcher exit"):
            backend.run([sys.executable, "-c", parent_script, str(child_pid_path)])
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 2
        while _windows_process_is_active(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _windows_process_is_active(child_pid)
    finally:
        if child_pid is not None and _windows_process_is_active(child_pid):
            _terminate_windows_process(child_pid)


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


def test_windows_launcher_passes_command_as_base64_json_arguments() -> None:
    command = [
        "ffmpeg",
        "-metadata",
        "comment='; raise RuntimeError(\"not code\")",
        "out.mp4",
    ]

    launcher_command = ffmpeg_backend._windows_launcher_command(
        "Local\\DesignToolkit.FFmpeg.test",
        123_456,
        command,
    )

    assert launcher_command[:2] == [
        ffmpeg_backend._WINDOWS_LAUNCHER_EXECUTABLE,
        "-c",
    ]
    assert command[2] not in launcher_command[2]
    assert launcher_command[3] == "Local\\DesignToolkit.FFmpeg.test"
    assert launcher_command[4] == "123456"
    assert json.loads(base64.b64decode(launcher_command[5]).decode("utf-8")) == command
