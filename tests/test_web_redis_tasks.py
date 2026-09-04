from __future__ import annotations

import inspect
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import fakeredis
import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from rq import Queue
from rq.job import JobStatus
from rq.registry import FailedJobRegistry, StartedJobRegistry

from designtoolkit.chroma_keyer.models import KeyerError, KeyImageResult
from designtoolkit.media_resize import (
    MediaResizeResult,
    ResizeBackendResult,
    resize_media,
)
from designtoolkit.video_frames import ExportResult
from designtoolkit.web import worker as worker_module
from designtoolkit.web.adapters import media_resize_options_list_from_request
from designtoolkit.web.hosted import HostedSettings, HostedStorage
from designtoolkit.web.redis_tasks import (
    RedisLogStore,
    RedisTaskManager,
    RunningTransitionResult,
    TerminalTransitionResult,
    handle_job_failure,
    handle_job_stopped,
)
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaVideoKeyerTaskRequest,
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskKind,
    TaskStatus,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskConflictError, TaskRecord
from designtoolkit.web.worker import RedisTaskExecutor


class StubTaskResources:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release_task_resources(self, task_id: str) -> None:
        self.released.append(task_id)


class TrackingHandle:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def refresh(self) -> None:
        self.events.append(f"refresh:{self.name}")

    def release(self) -> None:
        self.events.append(f"release:{self.name}")


class TrackingTaskResources:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.lease = TrackingHandle("lease", self.events)
        self.output = TrackingHandle("output", self.events)
        self.session_lease_heartbeat_interval = 5.0
        self.reservation_heartbeat_interval = 5.0

    def acquire_session(self, session_id: str, *, task_id: str):
        self.events.append(f"acquire:{task_id}:{session_id}")
        return self.lease

    def reserve_output(
        self,
        task_id: str,
        session_id: str,
        requested_bytes: int,
        *,
        free_bytes: int,
        min_free_bytes: int,
    ):
        self.events.append(
            f"reserve:{task_id}:{session_id}:{requested_bytes}:"
            f"{free_bytes}:{min_free_bytes}"
        )
        return self.output

    def release_task_resources(self, task_id: str) -> None:
        self.events.append(f"release-task:{task_id}")


def _manager(
    redis_connection,
    tmp_path: Path,
    *,
    max_queued_tasks: int = 20,
    resource_coordinator=None,
) -> RedisTaskManager:
    return RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=max_queued_tasks,
        retention_seconds=7200,
        resource_coordinator=resource_coordinator,
    )


def _hosted_settings(tmp_path: Path) -> HostedSettings:
    return HostedSettings(
        enabled=True,
        data_dir=tmp_path,
        redis_url="redis://unused/0",
        max_output_bytes=10,
        min_free_bytes=5,
    )


def _race_mark_running(
    manager: RedisTaskManager,
    task_id: str,
    barrier: Barrier,
) -> tuple[TaskRecord, RunningTransitionResult]:
    barrier.wait()
    return manager.mark_running(task_id)


def _race_mark_failed(
    manager: RedisTaskManager,
    task_id: str,
    barrier: Barrier,
) -> tuple[TaskRecord, TerminalTransitionResult]:
    barrier.wait()
    return manager._transition_finished(
        task_id,
        status=TaskStatus.FAILED,
        error="worker timeout",
    )


@pytest.fixture
def real_redis_connection() -> Redis:
    url = os.environ.get(
        "DESIGNTOOLKIT_TEST_REDIS_URL",
        "redis://localhost:6379/15",
    )
    connection = Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )
    try:
        connection.ping()
    except RedisError as error:
        pytest.skip(f"real Redis unavailable at {url}: {error}")
    connection.flushdb()
    try:
        yield connection
    finally:
        connection.flushdb()
        connection.close()


def test_redis_manager_enqueues_and_isolates_task_records(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    assert task.status is TaskStatus.QUEUED
    assert manager.get_task(task.task_id, owner_id="a" * 32) == task
    assert manager.get_task(task.task_id, owner_id="b" * 32) is None
    assert manager.queue.count == 1
    assert redis_connection.ttl(f"dt:task:{task.task_id}") >= 24 * 60 * 60 - 1
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    assert job.func_name == "designtoolkit.web.worker.run_task"


def test_enqueue_registers_importable_rq_failure_and_stopped_callbacks(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    assert job.failure_callback is not None
    assert job.failure_callback.__module__ == "designtoolkit.web.redis_tasks"
    assert job.failure_callback.__name__ == "handle_job_failure"
    assert job.stopped_callback is not None
    assert job.stopped_callback.__module__ == "designtoolkit.web.redis_tasks"
    assert job.stopped_callback.__name__ == "handle_job_stopped"
    assert list(inspect.signature(handle_job_failure).parameters) == [
        "job",
        "connection",
        "exc_type",
        "exc_value",
        "traceback",
    ]
    assert list(inspect.signature(handle_job_stopped).parameters) == [
        "job",
        "connection",
    ]


def test_redis_manager_rejects_when_waiting_queue_is_full(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path, max_queued_tasks=1)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    manager.create_video_frames_task(request, owner_id="a" * 32)

    with pytest.raises(TaskConflictError, match="queue is full"):
        manager.create_video_frames_task(request, owner_id="a" * 32)


def test_queue_cap_holds_under_concurrent_submissions(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path, max_queued_tasks=20)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))

    def submit(_index: int) -> bool:
        try:
            manager.create_video_frames_task(request, owner_id="a" * 32)
        except TaskConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=30) as executor:
        accepted = list(executor.map(submit, range(30)))

    assert sum(accepted) == 20
    assert manager.queue.count == 20


def test_run_task_reserves_refreshes_and_releases_worker_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    resources = TrackingTaskResources()
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )

    def execute(executor, reserved_task, _request) -> None:
        running = executor.get_task(reserved_task.task_id)
        assert running is not None
        assert running.status is TaskStatus.RUNNING
        executor._heartbeat_resources()
        executor.mark_finished(
            reserved_task.task_id,
            status=TaskStatus.SUCCEEDED,
        )

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        execute,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    assert resources.events[:3] == [
        f"acquire:{task.task_id}:{'a' * 32}",
        "refresh:lease",
        f"reserve:{task.task_id}:{'a' * 32}:10:20:5",
    ]
    assert resources.events.count("refresh:output") >= 2
    assert resources.events.count("refresh:lease") >= 3
    assert resources.events[-2:] == ["release:output", "release:lease"]
    assert manager.get_task(task.task_id).status is TaskStatus.SUCCEEDED


def test_run_task_heartbeats_resources_during_slow_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    resources = TrackingTaskResources()
    resources.session_lease_heartbeat_interval = 0.05
    resources.reservation_heartbeat_interval = 0.01
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )

    def execute(executor, reserved_task, _request) -> None:
        time.sleep(0.04)
        executor.mark_finished(
            reserved_task.task_id,
            status=TaskStatus.SUCCEEDED,
        )

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        execute,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    assert resources.events.count("refresh:lease") >= 2
    assert resources.events.count("refresh:output") >= 2
    assert resources.events[-2:] == ["release:output", "release:lease"]


def test_run_task_heartbeats_lease_while_output_reservation_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    class ExpiringLease(TrackingHandle):
        def __init__(self, events: list[str]) -> None:
            super().__init__("lease", events)
            self.expires_at = time.monotonic() + 0.03

        def refresh(self) -> None:
            if time.monotonic() >= self.expires_at:
                raise RuntimeError("lease expired during reservation")
            self.expires_at = time.monotonic() + 0.03
            super().refresh()

    class BlockingReservationResources(TrackingTaskResources):
        def __init__(self) -> None:
            super().__init__()
            self.lease = ExpiringLease(self.events)
            self.session_lease_heartbeat_interval = 0.005
            self.reservation_heartbeat_interval = 0.005

        def reserve_output(self, *args, **kwargs):
            time.sleep(0.08)
            if time.monotonic() >= self.lease.expires_at:
                raise RuntimeError("lease expired during reservation")
            return super().reserve_output(*args, **kwargs)

    resources = BlockingReservationResources()
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )

    def execute(executor, reserved_task, _request) -> None:
        executor.mark_finished(
            reserved_task.task_id,
            status=TaskStatus.SUCCEEDED,
        )

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        execute,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    completed = manager.get_task(task.task_id)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert resources.events.count("refresh:lease") >= 3
    assert resources.events[-2:] == ["release:output", "release:lease"]


def test_run_task_checks_heartbeat_failure_after_blocking_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    class FailingLease(TrackingHandle):
        def __init__(self, events: list[str]) -> None:
            super().__init__("lease", events)
            self.refresh_count = 0

        def refresh(self) -> None:
            self.refresh_count += 1
            if self.refresh_count >= 2:
                raise RuntimeError("lease failed during reservation")
            super().refresh()

    class BlockingReservationResources(TrackingTaskResources):
        def __init__(self) -> None:
            super().__init__()
            self.lease = FailingLease(self.events)
            self.session_lease_heartbeat_interval = 0.005
            self.reservation_heartbeat_interval = 0.005

        def reserve_output(self, *args, **kwargs):
            time.sleep(0.04)
            return super().reserve_output(*args, **kwargs)

    resources = BlockingReservationResources()
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )
    executed = False

    def unexpected_execute(*_args, **_kwargs) -> None:
        nonlocal executed
        executed = True

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        unexpected_execute,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "lease failed during reservation"
    assert executed is False
    assert resources.events[-2:] == ["release:output", "release:lease"]


def test_run_task_surfaces_background_heartbeat_failure_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    resources = TrackingTaskResources()
    resources.session_lease_heartbeat_interval = 0.01
    refresh_calls = 0

    def fail_once() -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        resources.events.append("refresh:output")
        if refresh_calls == 1:
            raise RuntimeError("output heartbeat failed")

    resources.output.refresh = fail_once
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )

    def execute(executor, reserved_task, _request) -> None:
        time.sleep(0.04)
        executor._heartbeat_resources()
        executor.mark_finished(
            reserved_task.task_id,
            status=TaskStatus.SUCCEEDED,
        )

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        execute,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "output heartbeat failed"
    assert resources.events[-2:] == ["release:output", "release:lease"]


def test_run_task_observes_heartbeat_failure_recorded_during_stop_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    resources = TrackingTaskResources()
    artifact = tmp_path / "result.png"
    artifact.write_bytes(b"result")
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )
    original_stop = worker_module._PeriodicResourceHeartbeat.stop
    stop_calls = 0

    def stop_after_recording_failure(
        heartbeat: worker_module._PeriodicResourceHeartbeat,
    ) -> None:
        nonlocal stop_calls
        stop_calls += 1
        heartbeat._failure = RuntimeError("lease heartbeat failed at stop")
        original_stop(heartbeat)

    monkeypatch.setattr(
        worker_module._PeriodicResourceHeartbeat,
        "stop",
        stop_after_recording_failure,
    )

    def execute(executor, reserved_task, _request) -> None:
        executor._mark_succeeded(reserved_task.task_id, str(artifact))

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        execute,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "lease heartbeat failed at stop"
    assert stop_calls == 1
    assert not artifact.exists()


def test_run_task_stops_before_resource_mutation_when_start_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    manager.mark_finished(
        task.task_id,
        status=TaskStatus.FAILED,
        error="worker timeout",
    )
    resources = TrackingTaskResources()
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )

    def unexpected_execution(*_args, **_kwargs) -> None:
        raise AssertionError("terminal task must not execute")

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        unexpected_execution,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    assert resources.events == []
    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.started_at is None


def test_run_task_failure_cleans_output_terminalizes_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    output_root = tmp_path / "output"
    request = VideoFramesTaskRequest(
        input_path=str(tmp_path / "clip.mp4"),
        output_dir=str(output_root),
    )
    task = manager.create_video_frames_task(request, owner_id="a" * 32)
    resources = TrackingTaskResources()
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )

    def fail(executor, _reserved_task, _request) -> None:
        executor._output_root = output_root
        output_root.mkdir()
        (output_root / "partial.bin").write_bytes(b"partial")
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        fail,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        request.model_dump(mode="json"),
    )

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "worker crashed"
    assert not output_root.exists()
    terminal_events = [
        event
        for event in manager.log_store.history(task.task_id)
        if event.phase is LogPhase.FAILED
    ]
    assert len(terminal_events) == 1
    assert resources.events[-2:] == ["release:output", "release:lease"]


def test_redis_log_store_persists_events_for_other_processes() -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    writer = RedisLogStore(redis_connection, retention_seconds=7200)
    reader = RedisLogStore(redis_connection, retention_seconds=7200)

    writer.append(
        task_id="task-1",
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="queued",
        details={"position": 1},
    )

    events = reader.history("task-1")
    assert len(events) == 1
    assert events[0].task_id == "task-1"
    assert events[0].details == {"position": 1}


def test_worker_status_updates_are_visible_to_api_manager(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    api_manager = _manager(redis_connection, tmp_path)
    worker_manager = _manager(redis_connection, tmp_path)
    task = api_manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    worker_manager.mark_running(task.task_id)
    worker_manager.mark_finished(
        task.task_id,
        status=TaskStatus.SUCCEEDED,
        output_path="/api/tasks/task/download",
        result_path=str(tmp_path / "result.zip"),
    )

    completed = api_manager.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.output_path == "/api/tasks/task/download"
    assert completed.result_path == str(tmp_path / "result.zip")
    assert completed.kind is TaskKind.VIDEO_FRAMES
    assert api_manager.active_owner_ids() == set()
    assert redis_connection.ttl(f"dt:task:{task.task_id}") > 0


def test_terminal_success_cannot_be_overwritten_by_late_failure(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    manager.mark_finished(
        task.task_id,
        status=TaskStatus.SUCCEEDED,
        output_path="/api/tasks/task/download",
    )

    result = manager.mark_finished(
        task.task_id,
        status=TaskStatus.FAILED,
        error="late timeout",
    )

    assert result.status is TaskStatus.SUCCEEDED
    assert result.error is None
    assert result.output_path == "/api/tasks/task/download"


def test_terminal_cas_returns_explicit_transition_result(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    _failed, first = manager._transition_finished(
        task.task_id,
        status=TaskStatus.FAILED,
        error="failed",
    )
    _unchanged, second = manager._transition_finished(
        task.task_id,
        status=TaskStatus.FAILED,
        error="repeated",
    )

    assert first is TerminalTransitionResult.APPLIED
    assert second is TerminalTransitionResult.ALREADY_TERMINAL


def test_mark_running_returns_explicit_transition_result(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    running, first = manager.mark_running(task.task_id)
    unchanged, second = manager.mark_running(task.task_id)

    assert running.status is TaskStatus.RUNNING
    assert running.started_at is not None
    assert first is RunningTransitionResult.APPLIED
    assert unchanged.status is TaskStatus.RUNNING
    assert second is RunningTransitionResult.REJECTED
    assert redis_connection.llen("dt:queue:reservations") == 0
    running_events = [
        event
        for event in manager.log_store.history(task.task_id)
        if event.phase is LogPhase.RUNNING
        and event.message == "Task is running"
    ]
    assert len(running_events) == 1


@pytest.mark.parametrize(
    ("callback", "expected_error"),
    [
        (handle_job_failure, "late worker timeout"),
        (handle_job_stopped, "Task was stopped"),
    ],
)
def test_late_callback_does_not_touch_successful_task_artifact_or_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callback,
    expected_error: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / "successful-output"
    output_root.mkdir()
    artifact = output_root / "result.zip"
    artifact.write_bytes(b"complete")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    manager.mark_finished(
        task.task_id,
        status=TaskStatus.SUCCEEDED,
        output_path=f"/api/tasks/{task.task_id}/download",
        result_path=str(artifact),
    )
    monkeypatch.setattr(
        "designtoolkit.web.redis_tasks._callback_manager",
        lambda _connection: manager,
    )

    if callback is handle_job_failure:
        callback(
            job,
            redis_connection,
            RuntimeError,
            RuntimeError(expected_error),
            None,
        )
    else:
        callback(job, redis_connection)

    completed = manager.get_task(task.task_id)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result_path == str(artifact)
    assert artifact.read_bytes() == b"complete"
    assert resources.released == []
    assert all(
        event.phase is not LogPhase.FAILED
        for event in manager.log_store.history(task.task_id)
    )


def test_callback_racing_success_only_winner_applies_terminal_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / "racing-output"
    output_root.mkdir()
    artifact = output_root / "result.zip"
    artifact.write_bytes(b"complete")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    monkeypatch.setattr(
        "designtoolkit.web.redis_tasks._callback_manager",
        lambda _connection: manager,
    )
    barrier = Barrier(2)

    def succeed() -> None:
        barrier.wait()
        manager.mark_finished(
            task.task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=f"/api/tasks/{task.task_id}/download",
            result_path=str(artifact),
        )

    def fail() -> None:
        barrier.wait()
        handle_job_failure(
            job,
            redis_connection,
            RuntimeError,
            RuntimeError("worker timeout"),
            None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(succeed), executor.submit(fail)]
        for future in futures:
            future.result()

    terminal = manager.get_task(task.task_id)
    assert terminal is not None
    failed_events = [
        event
        for event in manager.log_store.history(task.task_id)
        if event.phase is LogPhase.FAILED
    ]
    if terminal.status is TaskStatus.SUCCEEDED:
        assert artifact.read_bytes() == b"complete"
        assert resources.released == []
        assert failed_events == []
    else:
        assert terminal.status is TaskStatus.FAILED
        assert not output_root.exists()
        assert resources.released == [task.task_id]
        assert len(failed_events) == 1


@pytest.mark.parametrize(
    ("callback", "expected_error"),
    [
        (handle_job_failure, "worker timeout"),
        (handle_job_stopped, "Task was stopped"),
    ],
)
def test_external_rq_callback_is_idempotent_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callback,
    expected_error: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / "partial-output"
    output_root.mkdir()
    (output_root / "partial.bin").write_bytes(b"partial")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    monkeypatch.setattr(
        "designtoolkit.web.redis_tasks._callback_manager",
        lambda _connection: manager,
    )

    if callback is handle_job_failure:
        args = (job, redis_connection, RuntimeError, RuntimeError(expected_error), None)
    else:
        args = (job, redis_connection)
    callback(*args)
    callback(*args)

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == expected_error
    terminal_events = [
        event
        for event in manager.log_store.history(task.task_id)
        if event.phase is LogPhase.FAILED
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].message == expected_error
    assert resources.released == [task.task_id]
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert 7190 <= redis_connection.ttl(f"dt:task:{task.task_id}") <= 7200
    assert not output_root.exists()


def test_external_failure_retries_cleanup_after_crash_following_terminal_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / "interrupted-output"
    output_root.mkdir()
    (output_root / "partial.bin").write_bytes(b"partial")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    monkeypatch.setattr(
        "designtoolkit.web.redis_tasks._callback_manager",
        lambda _connection: manager,
    )
    original_release = manager._release_queue_slot
    calls = 0

    def crash_once(task_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected cleanup crash")
        original_release(task_id)

    monkeypatch.setattr(manager, "_release_queue_slot", crash_once)

    with pytest.raises(RuntimeError, match="injected cleanup crash"):
        handle_job_failure(
            job,
            redis_connection,
            RuntimeError,
            RuntimeError("worker timeout"),
            None,
        )

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert redis_connection.llen("dt:queue:reservations") == 1
    assert output_root.exists()
    assert resources.released == []

    handle_job_failure(
        job,
        redis_connection,
        RuntimeError,
        RuntimeError("worker timeout"),
        None,
    )

    terminal_events = [
        event
        for event in manager.log_store.history(task.task_id)
        if event.phase is LogPhase.FAILED
    ]
    assert len(terminal_events) == 1
    assert resources.released == [task.task_id]
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert not output_root.exists()


def test_external_failure_discards_output_before_releasing_task_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    events: list[str] = []

    class OrderedResources:
        def release_task_resources(self, _task_id: str) -> None:
            events.append("release")

    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=OrderedResources(),
    )
    output_root = tmp_path / "partial-output"
    output_root.mkdir()
    (output_root / "partial.bin").write_bytes(b"partial")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    original_discard = worker_module.discard_failed_task_output

    def record_discard(task_id: str, state: RedisTaskManager) -> None:
        events.append("discard")
        original_discard(task_id, state)

    monkeypatch.setattr(
        worker_module,
        "discard_failed_task_output",
        record_discard,
    )

    assert manager._finalize_external_failure(task.task_id, "worker failed")

    assert events == ["discard", "release"]
    assert not output_root.exists()


def test_reconciler_retries_cleanup_after_crash_following_terminal_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / "reconciled-output"
    output_root.mkdir()
    (output_root / "partial.bin").write_bytes(b"partial")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    original_release = manager._release_queue_slot

    def crash(_task_id: str) -> None:
        raise RuntimeError("injected cleanup crash")

    monkeypatch.setattr(manager, "_release_queue_slot", crash)
    with pytest.raises(RuntimeError, match="injected cleanup crash"):
        manager._finalize_external_failure(task.task_id, "worker timeout")
    monkeypatch.setattr(manager, "_release_queue_slot", original_release)

    assert manager.reconcile_tasks(grace_seconds=60) == 1
    assert manager.reconcile_tasks(grace_seconds=60) == 0
    assert resources.released == [task.task_id]
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert not output_root.exists()
    terminal_events = [
        event
        for event in manager.log_store.history(task.task_id)
        if event.phase is LogPhase.FAILED
    ]
    assert len(terminal_events) == 1


def test_active_owner_ids_include_only_queued_or_running_tasks(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    queued = manager.create_video_frames_task(request, owner_id="a" * 32)

    assert manager.active_owner_ids() == {"a" * 32}

    manager.mark_finished(queued.task_id, status=TaskStatus.FAILED, error="failed")
    assert manager.active_owner_ids() == set()


def test_reconcile_fails_old_queued_task_when_rq_job_is_missing(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / "abandoned-output"
    output_root.mkdir()
    (output_root / "partial.bin").write_bytes(b"partial")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    task.created_at = datetime.now(UTC) - timedelta(minutes=5)
    manager._save_task(task)
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    redis_connection.delete(job.key)

    assert manager.reconcile_tasks(grace_seconds=60) == 1

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert resources.released == [task.task_id]
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert not output_root.exists()


def test_reconcile_preserves_running_task_with_recent_heartbeat(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    manager.mark_running(task.task_id)
    running = manager.get_task(task.task_id)
    assert running is not None
    running.started_at = datetime.now(UTC) - timedelta(hours=3)
    manager._save_task(running)
    manager.record_heartbeat(task.task_id)
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    redis_connection.delete(job.key)

    assert manager.reconcile_tasks(grace_seconds=60) == 0
    assert manager.get_task(task.task_id).status is TaskStatus.RUNNING
    assert resources.released == []


@pytest.mark.parametrize("initial_status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
def test_reconcile_fails_stale_active_task_when_rq_job_is_finished(
    tmp_path: Path,
    initial_status: TaskStatus,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / f"finished-{initial_status.value}"
    output_root.mkdir()
    (output_root / "partial.bin").write_bytes(b"partial")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    if initial_status is TaskStatus.RUNNING:
        manager.mark_running(task.task_id)
    active = manager.get_task(task.task_id)
    assert active is not None
    active.created_at = datetime.now(UTC) - timedelta(minutes=5)
    active.started_at = datetime.now(UTC) - timedelta(minutes=5)
    manager._save_task(active)
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    job.set_status(JobStatus.FINISHED)
    history, subscriber = manager.log_store.history_and_subscribe(task.task_id)

    assert manager.reconcile_tasks(grace_seconds=60) == 1

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "Task finished without publishing a terminal result"
    assert resources.released == [task.task_id]
    assert not output_root.exists()
    terminal_event = subscriber.get(timeout=0.2)
    assert terminal_event.phase is LogPhase.FAILED
    assert terminal_event.message == failed.error
    assert len(manager.log_store.history(task.task_id)) == len(history) + 1
    assert manager.reconcile_tasks(grace_seconds=60) == 0
    assert resources.released == [task.task_id]


def test_reconcile_finished_rq_job_preserves_succeeded_task(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    output_root = tmp_path / "successful-finished"
    output_root.mkdir()
    artifact = output_root / "result.zip"
    artifact.write_bytes(b"complete")
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4"),
            output_dir=str(output_root),
        ),
        owner_id="a" * 32,
    )
    manager.mark_finished(
        task.task_id,
        status=TaskStatus.SUCCEEDED,
        output_path=f"/api/tasks/{task.task_id}/download",
        result_path=str(artifact),
    )
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    job.set_status(JobStatus.FINISHED)

    assert manager.reconcile_tasks(grace_seconds=0) == 0
    assert manager.get_task(task.task_id).status is TaskStatus.SUCCEEDED
    assert artifact.read_bytes() == b"complete"
    assert resources.released == []


def test_reconcile_preserves_long_running_job_with_started_registry_evidence(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    manager.mark_running(task.task_id)
    running = manager.get_task(task.task_id)
    assert running is not None
    running.started_at = datetime.now(UTC) - timedelta(hours=3)
    manager._save_task(running)
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    job.set_status(JobStatus.QUEUED)
    started_registry = StartedJobRegistry(
        "designtoolkit",
        connection=redis_connection,
    )
    redis_connection.zadd(
        started_registry.key,
        {f"{job.id}:execution-1": datetime.now(UTC).timestamp() + 3600},
    )

    assert manager.reconcile_tasks(grace_seconds=60) == 0
    assert manager.get_task(task.task_id).status is TaskStatus.RUNNING
    assert resources.released == []


@pytest.mark.parametrize("evidence", ["missing", "failed", "stopped"])
def test_reconcile_fails_stale_running_task_with_terminal_or_missing_rq_job(
    tmp_path: Path,
    evidence: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = StubTaskResources()
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    manager.mark_running(task.task_id)
    running = manager.get_task(task.task_id)
    assert running is not None
    running.started_at = datetime.now(UTC) - timedelta(minutes=5)
    manager._save_task(running)
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    if evidence == "missing":
        redis_connection.delete(job.key)
    else:
        status = JobStatus.FAILED if evidence == "failed" else JobStatus.STOPPED
        job.set_status(status)
        if evidence == "failed":
            FailedJobRegistry(
                "designtoolkit",
                connection=redis_connection,
            ).add(job, ttl=3600, exc_string="timeout")

    assert manager.reconcile_tasks(grace_seconds=60) == 1
    assert manager.get_task(task.task_id).status is TaskStatus.FAILED
    assert resources.released == [task.task_id]


def test_real_redis_terminal_cas_and_reconciliation(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = StubTaskResources()
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    success_root = tmp_path / "real-success"
    success_root.mkdir()
    artifact = success_root / "result.zip"
    artifact.write_bytes(b"complete")
    succeeded = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "success.mp4"),
            output_dir=str(success_root),
        ),
        owner_id="a" * 32,
    )
    success_job = manager.queue.fetch_job(succeeded.task_id)
    assert success_job is not None
    manager.mark_finished(
        succeeded.task_id,
        status=TaskStatus.SUCCEEDED,
        output_path=f"/api/tasks/{succeeded.task_id}/download",
        result_path=str(artifact),
    )
    monkeypatch.setattr(
        "designtoolkit.web.redis_tasks._callback_manager",
        lambda _connection: manager,
    )

    handle_job_failure(
        success_job,
        real_redis_connection,
        RuntimeError,
        RuntimeError("late timeout"),
        None,
    )

    assert manager.get_task(succeeded.task_id).status is TaskStatus.SUCCEEDED
    assert artifact.read_bytes() == b"complete"
    assert resources.released == []

    abandoned_root = tmp_path / "real-finished"
    abandoned_root.mkdir()
    (abandoned_root / "partial.bin").write_bytes(b"partial")
    abandoned = manager.create_video_frames_task(
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "abandoned.mp4"),
            output_dir=str(abandoned_root),
        ),
        owner_id="b" * 32,
    )
    active = manager.get_task(abandoned.task_id)
    assert active is not None
    active.created_at = datetime.now(UTC) - timedelta(minutes=5)
    manager._save_task(active)
    abandoned_job = manager.queue.fetch_job(abandoned.task_id)
    assert abandoned_job is not None
    abandoned_job.set_status(JobStatus.FINISHED)
    _history, subscriber = manager.log_store.history_and_subscribe(
        abandoned.task_id
    )

    assert manager.reconcile_tasks(grace_seconds=60) == 1
    assert manager.get_task(abandoned.task_id).status is TaskStatus.FAILED
    assert subscriber.get(timeout=0.2).message == (
        "Task finished without publishing a terminal result"
    )
    assert resources.released == [abandoned.task_id]
    assert not abandoned_root.exists()


def test_real_redis_failure_cas_races_mark_running_without_resurrection(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)

    for index in range(20):
        task = manager.create_video_frames_task(
            VideoFramesTaskRequest(
                input_path=str(tmp_path / f"race-{index}.mp4")
            ),
            owner_id="a" * 32,
        )
        barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            start_future = executor.submit(
                _race_mark_running,
                manager,
                task.task_id,
                barrier,
            )
            fail_future = executor.submit(
                _race_mark_failed,
                manager,
                task.task_id,
                barrier,
            )
            _started_task, start_result = start_future.result()
            failed_task, failure_result = fail_future.result()

        terminal = manager.get_task(task.task_id)
        assert terminal is not None
        assert terminal.status is TaskStatus.FAILED
        assert failed_task.status is TaskStatus.FAILED
        assert failure_result is TerminalTransitionResult.APPLIED
        assert start_result in {
            RunningTransitionResult.APPLIED,
            RunningTransitionResult.REJECTED,
        }
        if start_result is RunningTransitionResult.REJECTED:
            assert terminal.started_at is None
        else:
            assert terminal.started_at is not None
        _unchanged, retry_result = manager.mark_running(task.task_id)
        assert retry_result is RunningTransitionResult.REJECTED
        assert manager.get_task(task.task_id).status is TaskStatus.FAILED


def test_rate_limit_is_atomic_per_bucket(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)

    assert manager.allow_request("client:upload", limit=2, window=60)
    assert manager.allow_request("client:upload", limit=2, window=60)
    assert not manager.allow_request("client:upload", limit=2, window=60)
    assert manager.allow_request("other:upload", limit=2, window=60)


def test_batch_resize_disambiguates_duplicate_input_names(tmp_path: Path) -> None:
    options = media_resize_options_list_from_request(
        MediaResizeTaskRequest(
            input_paths=["/first/photo.png", "/second/photo.png"],
            output_dir=str(tmp_path),
            size="100x100",
        )
    )

    assert options[0].output_path == tmp_path / "001_photo_resized.png"
    assert options[1].output_path == tmp_path / "002_photo_resized.png"


def test_worker_executor_runs_reserved_task_and_persists_success(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    task = state.create_video_frames_task(request, owner_id="a" * 32)
    output_dir = tmp_path / "frames"
    executor = RedisTaskExecutor(state)

    def export(_options, _backend) -> ExportResult:
        output_dir.mkdir()
        (output_dir / "clip_000001.png").write_bytes(b"png")
        return ExportResult(
            output_dir=output_dir,
            output_pattern=output_dir / "clip_%06d.png",
            command=("ffmpeg",),
        )

    executor._exporter = export

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.output_path == f"/api/tasks/{task.task_id}/download"
    assert completed.result_path is not None
    assert Path(completed.result_path).is_file()


def test_worker_preserves_success_artifact_when_terminal_ack_is_lost(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    task = state.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    artifact = tmp_path / "result.png"
    artifact.write_bytes(b"result")
    original_transition = state._transition_finished
    raised = False

    def commit_then_raise(*args, **kwargs):
        nonlocal raised
        result = original_transition(*args, **kwargs)
        if not raised:
            raised = True
            raise RedisConnectionError("terminal transition acknowledgement lost")
        return result

    state._transition_finished = commit_then_raise
    executor = RedisTaskExecutor(state)

    executor._mark_succeeded(task.task_id, str(artifact))

    completed = state.get_task(task.task_id)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result_path == str(artifact)
    assert artifact.read_bytes() == b"result"


def test_run_task_releases_resources_after_success_ack_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    artifact = tmp_path / "result.png"
    artifact.write_bytes(b"result")
    resources = TrackingTaskResources()
    original_transition = RedisTaskManager._transition_finished
    acknowledgement_lost = False

    def commit_then_raise(self, *args, **kwargs):
        nonlocal acknowledgement_lost
        result = original_transition(self, *args, **kwargs)
        if (
            kwargs["status"] is TaskStatus.SUCCEEDED
            and not acknowledgement_lost
        ):
            acknowledgement_lost = True
            raise RedisConnectionError("terminal transition acknowledgement lost")
        return result

    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url: redis_connection,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_resource_coordinator",
        lambda _settings, _connection: resources,
    )
    monkeypatch.setattr(
        worker_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 20})(),
    )
    monkeypatch.setattr(
        RedisTaskManager,
        "_transition_finished",
        commit_then_raise,
    )

    def execute(executor, reserved_task, _request) -> None:
        executor._mark_succeeded(reserved_task.task_id, str(artifact))

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        execute,
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    completed = manager.get_task(task.task_id)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result_path == str(artifact)
    assert artifact.read_bytes() == b"result"
    assert resources.events[-2:] == ["release:output", "release:lease"]


def test_worker_heartbeat_failure_after_packaging_blocks_session_touch_and_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    task = state.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    artifact = tmp_path / "result.png"
    artifact.write_bytes(b"result")
    heartbeat_calls = 0
    touched_sessions: list[str] = []

    def heartbeat() -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 2:
            raise RuntimeError("session heartbeat failed")

    monkeypatch.setattr(
        HostedStorage,
        "touch_session",
        lambda _storage, session_id: touched_sessions.append(session_id),
    )
    executor = RedisTaskExecutor(state, resource_heartbeat=heartbeat)

    executor._mark_succeeded(task.task_id, str(artifact))

    failed = state.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "session heartbeat failed"
    assert touched_sessions == []
    assert not artifact.exists()


def test_worker_rejects_zip_larger_than_budget_after_packaging(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "clip.mp4"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_dir = tmp_path / "frames"
    request = VideoFramesTaskRequest(
        input_path=str(input_path),
        output_dir=str(output_dir),
    )
    task = state.create_video_frames_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=129)

    def export(_options, _backend) -> ExportResult:
        output_dir.mkdir()
        (output_dir / "frame.bin").write_bytes(bytes(range(128)))
        return ExportResult(
            output_dir=output_dir,
            output_pattern=output_dir / "clip_%06d.png",
            command=("ffmpeg",),
        )

    executor._exporter = export

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "129-byte limit" in completed.error
    assert not output_dir.exists()
    assert not (tmp_path / f"{task.task_id}.zip").exists()
    assert input_path.read_bytes() == b"input"


def test_worker_removes_zip_candidate_when_preparation_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "clip.mp4"
    input_path.write_bytes(b"input")
    output_dir = tmp_path / "frames"
    request = VideoFramesTaskRequest(
        input_path=str(input_path),
        output_dir=str(output_dir),
    )
    task = state.create_video_frames_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state)
    archive_path = tmp_path / f"{task.task_id}.zip"

    def export(_options, _backend) -> ExportResult:
        output_dir.mkdir()
        (output_dir / "frame.png").write_bytes(b"png")
        return ExportResult(
            output_dir=output_dir,
            output_pattern=output_dir / "clip_%06d.png",
            command=("ffmpeg",),
        )

    def fail_preparation(*_args, **_kwargs) -> Path:
        archive_path.write_bytes(b"partial zip")
        raise OSError("zip close failed")

    executor._exporter = export
    monkeypatch.setattr(
        "designtoolkit.web.worker.prepare_download_artifact",
        fail_preparation,
    )

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error == "zip close failed"
    assert not archive_path.exists()


@pytest.mark.parametrize("failure_mode", ["nonzero", "exception"])
def test_worker_removes_partial_ffmpeg_output_after_any_failure(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "clip.mp4"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    request = MediaResizeTaskRequest(
        input_paths=[str(input_path)],
        output_dir=str(output_root),
        size="100x100",
    )
    task = state.create_media_resize_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state)

    class PartialFailureBackend:
        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def run(self, command) -> ResizeBackendResult:
            Path(command[-1]).write_bytes(b"partial")
            if failure_mode == "exception":
                raise RuntimeError("FFmpeg process failed")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    executor._media_resizer = resize_media
    executor._backend_factory = PartialFailureBackend

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert not output_root.exists()
    assert input_path.read_bytes() == b"input"


def test_worker_failure_cleanup_preserves_input_inside_output_root(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    input_path = output_root / "clip.mp4"
    input_path.write_bytes(b"input")
    request = MediaResizeTaskRequest(
        input_paths=[str(input_path)],
        output_dir=str(output_root),
        size="100x100",
    )
    task = state.create_media_resize_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state)

    class PartialFailureBackend:
        def get_ffmpeg_exe(self) -> str:
            return "ffmpeg"

        def run(self, command) -> ResizeBackendResult:
            Path(command[-1]).write_bytes(b"partial")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    executor._media_resizer = resize_media
    executor._backend_factory = PartialFailureBackend

    executor.execute_reserved_task(task, request)

    assert input_path.read_bytes() == b"input"
    assert sorted(path.name for path in output_root.iterdir()) == ["clip.mp4"]


def test_worker_aborts_batch_when_aggregate_output_exceeds_budget(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    inputs = [tmp_path / "assets" / name for name in ("one.png", "two.png")]
    for input_path in inputs:
        input_path.parent.mkdir(exist_ok=True)
        input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    request = MediaResizeTaskRequest(
        input_paths=[str(path) for path in inputs],
        output_dir=str(output_root),
        size="100x100",
    )
    task = state.create_media_resize_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=8)
    backend_limits: list[int | None] = []
    backends: list[object] = []

    def resize(options, backend) -> MediaResizeResult:
        assert backend._output_guard is not None
        backends.append(backend)
        backend_limits.append(backend._max_file_bytes)
        assert options.output_path is not None
        options.output_path.write_bytes(b"1234")
        return MediaResizeResult(output_path=options.output_path, command=("ffmpeg",))

    executor._media_resizer = resize

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "8-byte limit" in completed.error
    assert not output_root.exists()
    assert all(path.read_bytes() == b"input" for path in inputs)
    assert backend_limits == [7, 3]
    assert backends[0] is not backends[1]
    assert backends[0]._output_guard is backends[1]._output_guard


def test_worker_aborts_chroma_video_from_progress_accounting(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "clip.mp4"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    request = ChromaVideoKeyerTaskRequest(
        input_path=str(input_path),
        samples=[[0, 255, 0]],
        params={},
        output_root=str(output_root),
    )
    task = state.create_chroma_video_keyer_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=6)

    def key_video(options, decoder, model, params, progress_callback):
        del options, decoder, model, params
        current_path = output_root / ".clip.tmp" / "clip_keyed_000001.png"
        current_path.parent.mkdir()
        current_path.write_bytes(b"123456")
        assert progress_callback is not None
        try:
            progress_callback(1, 2, current_path)
        except Exception as exc:
            raise KeyerError("Video PNG sequence export failed") from exc
        raise AssertionError("progress callback should abort processing")

    executor._video_keyer = key_video

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "6-byte limit" in completed.error
    assert not output_root.exists()
    assert input_path.read_bytes() == b"input"


def test_worker_final_tree_check_catches_unaccounted_output(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "photo.png"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    output_path = output_root / "photo_keyed.png"
    request = ChromaKeyerTaskRequest(
        input_path=str(input_path),
        samples=[[0, 255, 0]],
        params={},
        output_path=str(output_path),
    )
    task = state.create_chroma_keyer_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state, max_output_bytes=7)

    def key_image(options, model, params) -> KeyImageResult:
        del model, params
        assert options.output_path is not None
        options.output_path.write_bytes(b"12")
        (options.output_path.parent / "sidecar.bin").write_bytes(b"12345")
        return KeyImageResult(output_path=options.output_path)

    executor._keyer = key_image

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.FAILED
    assert completed.error is not None
    assert "7-byte limit" in completed.error
    assert not output_root.exists()
    assert input_path.read_bytes() == b"input"
