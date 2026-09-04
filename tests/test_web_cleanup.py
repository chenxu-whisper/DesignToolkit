from pathlib import Path

from designtoolkit.web import cleanup as cleanup_module
from designtoolkit.web.hosted import HostedSettings


def test_cleanup_reconciles_stale_tasks_and_reservations_before_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[str] = []
    redis_connection = object()
    coordinator = object()

    class FakeRedis:
        @staticmethod
        def from_url(_url: str) -> object:
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

        def reconcile_tasks(self) -> int:
            events.append("tasks")
            return 1

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
        )
    )

    assert result == 2
    assert events == ["tasks", "reservations", "cleanup"]
