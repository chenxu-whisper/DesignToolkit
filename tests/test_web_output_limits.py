from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from designtoolkit.video_frames.ffmpeg_backend import (
    FfmpegBackend,
    _file_size_limit_preexec,
)
from designtoolkit.web.output_limits import OutputBudgetGuard, OutputLimitExceeded


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
