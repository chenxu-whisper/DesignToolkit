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
from threading import Event
from typing import Any

import pytest
from rq.timeouts import JobTimeoutException

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
        self.claim_calls = 0
        self.close_calls = 0
        self.closed = False

    def claim_and_release(self, process: FakePopen) -> None:
        del process
        self.claim_calls += 1

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


def test_windows_gate_setup_failure_closes_job_before_kill_even_if_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    operations: list[str] = []

    class JobOwningGate(FakeWindowsLaunchGate):
        has_job = True

        def claim_and_release(self, process: FakePopen) -> None:
            del process
            raise OSError("assign failed")

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
def test_windows_launcher_rebuilds_matching_gated_startup_error(
    startup_error: OSError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = FakeWindowsLaunchGate()
    stderr = _windows_startup_error_sentinel(
        gate.event_name,
        gate.startup_error_token,
        startup_error,
    )
    process = FakePopen([("", stderr)], returncode=126)
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


def test_windows_launcher_ignores_spoofed_startup_error_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = FakeWindowsLaunchGate()
    stderr = _windows_startup_error_sentinel(
        gate.event_name,
        "attacker-controlled-token",
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
    gate.claim_and_release(process)  # type: ignore[arg-type]

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
def test_windows_launcher_preserves_stderr_and_returncode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ffmpeg_backend, "os_name", "nt")
    backend = FfmpegBackend()

    result = backend.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('diagnostic'); raise SystemExit(23)",
        ]
    )

    assert result.returncode == 23
    assert result.stderr == "diagnostic"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
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


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
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
        "independent-secret",
        command,
    )

    assert launcher_command[:2] == [sys.executable, "-c"]
    assert command[2] not in launcher_command[2]
    assert launcher_command[3] == "Local\\DesignToolkit.FFmpeg.test"
    assert launcher_command[4] == "independent-secret"
    assert launcher_command[4] != launcher_command[3]
    assert json.loads(base64.b64decode(launcher_command[5]).decode("utf-8")) == command
    assert "independent-secret" not in launcher_command[5]
