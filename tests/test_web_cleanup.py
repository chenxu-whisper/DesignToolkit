from pathlib import Path

import pytest

from designtoolkit.web import cleanup as cleanup_module
from designtoolkit.web.hosted import HostedSettings


def test_cleanup_reconciles_stale_tasks_and_reservations_before_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    redis_options: dict[str, object] = {}
    redis_connection = object()
    coordinator = object()

    class FakeRedis:
        @staticmethod
        def from_url(_url: str, **kwargs: object) -> object:
            redis_options.update(kwargs)
            return redis_connection

    class FakeQueue:
        def __init__(self, _name: str, *, connection: object) -> None:
            assert connection is redis_connection

    class FakeCoordinator:
        def __new__(cls, **_kwargs):
            return coordinator

    class FakeManager:
        def __init__(self, **kwargs) -> None:
            assert kwargs["redis_connection"] is redis_connection
            assert kwargs["resource_coordinator"] is coordinator
            assert kwargs["task_timeout_seconds"] == 9000
            assert kwargs["retention_seconds"] == 60

        def reconcile_tasks(self) -> int:
            events.append("tasks")
            return 1

        def reconcile_queue_slots(self) -> int:
            events.append("queue-slots")
            return 0

    class FakeStorage:
        def __init__(self, root: Path, *, max_upload_bytes: int) -> None:
            assert root == tmp_path
            assert max_upload_bytes == 1024

        def cleanup_expired(
            self,
            *,
            retention_seconds: int,
            coordinator: object,
        ) -> int:
            assert retention_seconds == 60
            assert coordinator is coordinator_instance
            events.append("cleanup")
            return 2

    class RecordingCoordinator:
        def reconcile_reservations(self) -> int:
            events.append("reservations")
            return 1

    coordinator_instance = RecordingCoordinator()
    coordinator = coordinator_instance
    monkeypatch.setattr(cleanup_module, "Redis", FakeRedis)
    monkeypatch.setattr(cleanup_module, "Queue", FakeQueue)
    monkeypatch.setattr(
        cleanup_module,
        "RedisResourceCoordinator",
        FakeCoordinator,
        raising=False,
    )
    monkeypatch.setattr(cleanup_module, "RedisTaskManager", FakeManager)
    monkeypatch.setattr(cleanup_module, "HostedStorage", FakeStorage)

    result = cleanup_module.cleanup_once(
        HostedSettings(
            enabled=True,
            data_dir=tmp_path,
            redis_url="redis://unused",
            max_upload_bytes=1024,
            retention_seconds=60,
            task_timeout_seconds=9000,
        )
    )

    assert result == 2
    assert events == ["tasks", "queue-slots", "reservations", "cleanup"]
    assert redis_options == {
        "socket_connect_timeout": 5.0,
        "socket_timeout": 5.0,
    }


def test_cleanup_loop_logs_failure_waits_and_runs_next_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = HostedSettings(
        enabled=True,
        data_dir=tmp_path,
        redis_url="redis://unused",
    )
    calls = 0
    waits: list[int] = []

    def flaky_cleanup(_settings: HostedSettings) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("persistent storage unavailable")
        return 0

    def stop_after_second_wait(delay: int) -> None:
        waits.append(delay)
        if len(waits) == 2:
            raise SystemExit(0)

    monkeypatch.setattr(cleanup_module, "cleanup_once", flaky_cleanup)
    monkeypatch.setattr(cleanup_module, "sleep", stop_after_second_wait)
    caplog.set_level("ERROR", logger="designtoolkit.web.cleanup")

    with pytest.raises(SystemExit):
        cleanup_module.run_cleanup_loop(settings, interval=0)

    assert calls == 2
    assert waits == [1, 1]
    assert "Hosted cleanup iteration failed" in caplog.text


@pytest.mark.parametrize("fatal_error", [KeyboardInterrupt, SystemExit])
def test_cleanup_loop_does_not_swallow_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal_error: type[BaseException],
) -> None:
    settings = HostedSettings(
        enabled=True,
        data_dir=tmp_path,
        redis_url="redis://unused",
    )

    def stop_cleanup(_settings: HostedSettings) -> int:
        raise fatal_error

    monkeypatch.setattr(cleanup_module, "cleanup_once", stop_cleanup)

    with pytest.raises(fatal_error):
        cleanup_module.run_cleanup_loop(settings, interval=60)
