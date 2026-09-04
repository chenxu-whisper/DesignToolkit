from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from designtoolkit.video_frames.ffmpeg_backend import (
    FfmpegBackend,
    _file_size_limit_preexec,
)
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


def test_sequence_monitor_only_accounts_new_contiguous_frames(
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

    assert accounted == [first, second, third]
    assert guard.used_bytes == 6


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
        del timeout
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


def test_file_size_limit_preexec_is_disabled_on_windows() -> None:
    assert _file_size_limit_preexec(10, os_name="nt") is None


def test_file_size_limit_preexec_is_created_for_posix() -> None:
    assert callable(_file_size_limit_preexec(10, os_name="posix"))
