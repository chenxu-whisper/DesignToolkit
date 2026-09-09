from __future__ import annotations

import inspect
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty
from threading import Barrier, Event, Lock
from zipfile import ZipFile

import fakeredis
import pytest
from pydantic import ValidationError
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, ResponseError
from rq import Queue
from rq.job import Job, JobStatus
from rq.registry import FailedJobRegistry, StartedJobRegistry

from designtoolkit.chroma_keyer.models import KeyerError, KeyImageResult
from designtoolkit.media_resize import (
    MediaResizeResult,
    ResizeBackendResult,
    resize_media,
)
from designtoolkit.video_frames import ExportResult
from designtoolkit.web import cleanup as cleanup_module
from designtoolkit.web import redis_errors as redis_errors_module
from designtoolkit.web import redis_tasks as redis_tasks_module
from designtoolkit.web import worker as worker_module
from designtoolkit.web.adapters import (
    media_resize_options_list_from_request,
    video_frame_options_list_from_request,
)
from designtoolkit.web.hosted import HostedSettings, HostedStorage
from designtoolkit.web.redis_tasks import (
    RedisLogStore,
    RedisTaskManager,
    RunningTransitionResult,
    TerminalTransitionResult,
    handle_job_failure,
    handle_job_stopped,
)
from designtoolkit.web.resources import (
    CorruptTaskPinError,
    RedisResourceCoordinator,
    ResourceUnavailableError,
    SessionDeletingError,
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

_PRE_GENERATION_RELEASE_TASK_PIN_SCRIPT = """-- release_task_pin
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
if redis.call("HGET", KEYS[1], "session_id") ~= ARGV[1] then
    return 2
end
if redis.call("HGET", KEYS[1], "task_id") ~= ARGV[2] then
    return 2
end
local asset_ids_json = redis.call("HGET", KEYS[1], "asset_ids")
if asset_ids_json then
    local asset_ids = cjson.decode(asset_ids_json)
    for _, asset_id in ipairs(asset_ids) do
        local asset_tasks_key = ARGV[3] .. ARGV[1] .. ":" .. asset_id
        redis.call("SREM", asset_tasks_key, ARGV[2])
        if redis.call("SCARD", asset_tasks_key) == 0 then
            redis.call("DEL", asset_tasks_key)
        end
    end
end
redis.call("DEL", KEYS[1])
redis.call("SREM", KEYS[2], ARGV[2])
if redis.call("SCARD", KEYS[2]) == 0 then
    redis.call("DEL", KEYS[2])
end
return 1
"""


class StubTaskResources:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release_task_runtime_resources(self, task_id: str) -> None:
        self.released.append(task_id)

    def release_task_resources(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
        expected_asset_identities: (
            tuple[tuple[str, str | None], ...] | None
        ) = None,
    ) -> None:
        assert expected_session_id is not None
        assert expected_asset_identities is not None
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
        self.task_pin_releases: list[
            tuple[str, str | None, tuple[tuple[str, str | None], ...] | None]
        ] = []
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

    def release_task_resources(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
        expected_asset_identities: (
            tuple[tuple[str, str | None], ...] | None
        ) = None,
    ) -> None:
        assert expected_session_id is not None
        assert expected_asset_identities is not None
        self.events.append(f"release-task:{task_id}")

    def release_task_runtime_resources(self, task_id: str) -> None:
        self.events.append(f"release-task:{task_id}")

    def release_task_pin(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
        expected_asset_identities: (
            tuple[tuple[str, str | None], ...] | None
        ) = None,
    ) -> None:
        assert expected_session_id is not None
        assert expected_asset_identities is not None
        self.task_pin_releases.append(
            (task_id, expected_session_id, expected_asset_identities)
        )


class LegacyTrackingTaskResources(TrackingTaskResources):
    def release_task_resources(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
        expected_asset_identities: (
            tuple[tuple[str, str | None], ...] | None
        ) = None,
    ) -> None:
        assert expected_session_id is not None
        assert expected_asset_identities is None
        self.events.append(f"release-task:{task_id}")


class ReleaseShapeResourceCoordinatorSpy(RedisResourceCoordinator):
    def __init__(self, redis_connection) -> None:
        self._redis = redis_connection
        self.events: list[str] = []
        self.runtime_releases: list[str] = []
        self.pin_releases: list[str] = []
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

    def release_task_runtime_resources(self, task_id: str) -> None:
        self.runtime_releases.append(task_id)

    def reconcile_reservations(self) -> int:
        return 0

    def release_task_pin(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
        expected_asset_identities: (
            tuple[tuple[str, str | None], ...] | None
        ) = None,
    ) -> bool:
        assert expected_session_id is not None
        assert expected_asset_identities is not None
        self.pin_releases.append(task_id)
        return False


def _manager(
    redis_connection,
    tmp_path: Path,
    *,
    max_queued_tasks: int = 20,
    retention_seconds: int = 7200,
    resource_coordinator=None,
    enqueue_intent_ttl: float = 60,
    enqueue_intent_heartbeat_interval: float = 10,
) -> RedisTaskManager:
    return RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=max_queued_tasks,
        retention_seconds=retention_seconds,
        resource_coordinator=resource_coordinator,
        enqueue_intent_ttl=enqueue_intent_ttl,
        enqueue_intent_heartbeat_interval=enqueue_intent_heartbeat_interval,
    )


def _hosted_settings(
    tmp_path: Path,
    *,
    task_timeout_seconds: int = 7200,
    retention_seconds: int = 7200,
) -> HostedSettings:
    return HostedSettings(
        enabled=True,
        data_dir=tmp_path,
        redis_url="redis://unused/0",
        max_output_bytes=10,
        min_free_bytes=5,
        task_timeout_seconds=task_timeout_seconds,
        retention_seconds=retention_seconds,
    )


def _patch_worker_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: HostedSettings,
    redis_connection,
    resources,
) -> None:
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url, **_kwargs: redis_connection,
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


def _resources(redis_connection: Redis) -> RedisResourceCoordinator:
    return RedisResourceCoordinator(
        redis_connection=redis_connection,
        max_upload_concurrency=4,
        max_session_bytes=2 * 1024 * 1024 * 1024,
        min_free_bytes=0,
        reservation_ttl=60,
        lease_ttl=60,
    )


def _assert_ttl_matches(
    redis_connection: Redis,
    key: str,
    retention_seconds: int,
) -> None:
    assert retention_seconds - 2 <= redis_connection.ttl(key) <= retention_seconds


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


def test_real_redis_task_record_persists_internal_asset_identities(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    identities = (("1" * 32, "2" * 32),)

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_identities=identities,
    )

    stored = json.loads(
        real_redis_connection.get(f"dt:task:{task.task_id}")
    )
    assert task.asset_identities == identities
    assert stored["asset_identities"] == [
        list(identity) for identity in identities
    ]
    response = task.to_response().model_dump()
    assert "asset_identities" not in response
    assert "has_canonical_asset_identities" not in response


def test_rq_job_timeout_is_independent_from_artifact_retention(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=300,
        task_timeout_seconds=9000,
    )

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    assert job.timeout == 9000
    assert job.result_ttl == 300
    assert job.failure_ttl == 300


def test_callback_manager_uses_configured_task_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(
        tmp_path,
        task_timeout_seconds=9000,
        retention_seconds=300,
    )
    monkeypatch.setattr(HostedSettings, "from_env", lambda: settings)

    manager = redis_tasks_module._callback_manager(redis_connection)

    assert manager.task_timeout_seconds == 9000
    assert manager.retention_seconds == 300


@pytest.mark.parametrize("retention_seconds", [300, 10_800])
def test_real_redis_terminal_keys_use_configured_retention(
    real_redis_connection: Redis,
    tmp_path: Path,
    retention_seconds: int,
) -> None:
    manager = _manager(
        real_redis_connection,
        tmp_path,
        retention_seconds=retention_seconds,
        resource_coordinator=_resources(real_redis_connection),
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_ids=("1" * 32,),
    )

    assert manager._finalize_external_failure(task.task_id, "worker failed")

    for key in (
        f"dt:task:{task.task_id}",
        f"dt:task-work:{task.task_id}",
        f"dt:task:{task.task_id}:events",
        f"dt:task:{task.task_id}:event-index",
        f"dt:event-generation:{task.task_id}",
        f"dt:failure-cleanup:{task.task_id}",
    ):
        _assert_ttl_matches(real_redis_connection, key, retention_seconds)
    assert not real_redis_connection.exists(
        f"dt:resource:task-pin:{task.task_id}"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{'a' * 32}:{'1' * 32}"
    )


def test_real_redis_callback_uses_configured_retention(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retention_seconds = 300
    settings = _hosted_settings(
        tmp_path,
        retention_seconds=retention_seconds,
    )
    manager = _manager(
        real_redis_connection,
        tmp_path,
        retention_seconds=retention_seconds,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    monkeypatch.setattr(HostedSettings, "from_env", lambda: settings)

    handle_job_failure(
        job,
        real_redis_connection,
        RuntimeError,
        RuntimeError("worker failed"),
        None,
    )

    for key in (
        f"dt:task:{task.task_id}",
        f"dt:task-work:{task.task_id}",
        f"dt:task:{task.task_id}:events",
        f"dt:task:{task.task_id}:event-index",
        f"dt:event-generation:{task.task_id}",
        f"dt:failure-cleanup:{task.task_id}",
    ):
        _assert_ttl_matches(real_redis_connection, key, retention_seconds)


def test_real_redis_terminal_transition_refreshes_existing_event_retention(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    retention_seconds = 300
    manager = _manager(
        real_redis_connection,
        tmp_path,
        retention_seconds=retention_seconds,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    event_key = f"dt:task:{task.task_id}:events"
    real_redis_connection.expire(event_key, 30)

    manager.mark_finished(task.task_id, status=TaskStatus.SUCCEEDED)

    assert real_redis_connection.llen(event_key) == 1
    _assert_ttl_matches(real_redis_connection, event_key, retention_seconds)
    _assert_ttl_matches(
        real_redis_connection,
        f"dt:event-generation:{task.task_id}",
        retention_seconds,
    )
    _assert_ttl_matches(
        real_redis_connection,
        f"dt:task:{task.task_id}:event-index",
        retention_seconds,
    )


def test_real_redis_terminal_event_is_idempotent_after_acknowledgement_loss(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(real_redis_connection, tmp_path, retention_seconds=60)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    transition = manager._terminal_transition_script

    def transition_then_lose_ack(*args, **kwargs):
        transition(*args, **kwargs)
        raise RedisConnectionError("terminal acknowledgement lost")

    monkeypatch.setattr(
        manager,
        "_terminal_transition_script",
        transition_then_lose_ack,
    )
    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        manager._finalize_external_failure(task.task_id, "worker failed")

    monkeypatch.setattr(manager, "_terminal_transition_script", transition)
    assert manager._finalize_external_failure(task.task_id, "worker failed")

    terminal_events = [
        event
        for event in manager.log_store.history(task.task_id)
        if event.phase is LogPhase.FAILED
    ]
    assert len(terminal_events) == 1
    event_key = f"dt:task:{task.task_id}:events"
    event_index_key = f"dt:task:{task.task_id}:event-index"
    generation_key = f"dt:event-generation:{task.task_id}"
    assert abs(
        real_redis_connection.pttl(event_key)
        - real_redis_connection.pttl(generation_key)
    ) < 100
    assert abs(
        real_redis_connection.pttl(event_key)
        - real_redis_connection.pttl(event_index_key)
    ) < 100


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


def test_enqueue_failure_discards_staged_rq_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    original_enqueue_job = manager.queue.enqueue_job

    def stage_job_then_fail(*args, **kwargs):
        original_enqueue_job(*args, **kwargs)
        raise RedisConnectionError("enqueue failed before transaction commit")

    monkeypatch.setattr(manager.queue, "enqueue_job", stage_job_then_fail)

    with pytest.raises(RedisConnectionError, match="before transaction commit"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id="a" * 32,
        )

    assert manager.queue.count == 0
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert list(redis_connection.scan_iter(match="rq:job:*")) == []
    assert list(redis_connection.scan_iter(match="dt:task:*")) == []
    assert list(redis_connection.scan_iter(match="dt:task-work:*")) == []
    assert list(redis_connection.scan_iter(match="dt:enqueue-intent:*")) == []
    assert list(redis_connection.scan_iter(match="dt:enqueue-owner:*")) == []


def test_enqueue_failure_releases_asset_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    asset_id = "1" * 32

    def fail_create_job(*_args, **_kwargs):
        raise RuntimeError("job creation failed")

    monkeypatch.setattr(manager.queue, "create_job", fail_create_job)

    with pytest.raises(RuntimeError, match="job creation failed"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id="a" * 32,
            asset_ids=(asset_id,),
        )

    claim = manager.claim_asset_deletion("a" * 32, asset_id)
    manager.release_asset_deletion("a" * 32, asset_id, claim)
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-tasks:*")
    ) == []


def test_real_redis_asset_deletion_claim_blocks_enqueue(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    claim = manager.claim_asset_deletion(session_id, asset_id)

    with pytest.raises(TaskConflictError, match="deletion is already in progress"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
            asset_ids=(asset_id,),
        )

    assert manager.queue.count == 0
    assert real_redis_connection.llen("dt:queue:reservations") == 0
    manager.release_asset_deletion(session_id, asset_id, claim)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_ids=(asset_id,),
    )
    assert task.status is TaskStatus.QUEUED


def test_real_redis_asset_read_pin_handoff_is_multi_asset_and_generation_safe(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_ids = ("1" * 32, "2" * 32)
    deletion_claim = manager.claim_asset_deletion(session_id, asset_ids[0])
    with pytest.raises(TaskConflictError, match="deletion is already in progress"):
        manager.acquire_asset_read_pin(session_id, asset_ids)
    manager.release_asset_deletion(session_id, asset_ids[0], deletion_claim)
    released_generation = manager.acquire_asset_read_pin(session_id, asset_ids)
    assert released_generation.release()
    active_generation = manager.acquire_asset_read_pin(session_id, asset_ids)

    assert not released_generation.release()
    for asset_id in asset_ids:
        with pytest.raises(
            TaskConflictError,
            match="referenced by an active task",
        ):
            manager.claim_asset_deletion(session_id, asset_id)

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_paths=[str(tmp_path / "clip.mp4")] * 2),
        owner_id=session_id,
        asset_ids=asset_ids,
        asset_read_pin_token=active_generation.token,
    )

    assert not active_generation.release()
    for asset_id in asset_ids:
        with pytest.raises(
            TaskConflictError,
            match="referenced by an active task",
        ):
            manager.claim_asset_deletion(session_id, asset_id)

    manager.mark_finished(task.task_id, status=TaskStatus.SUCCEEDED)
    for asset_id in asset_ids:
        claim = manager.claim_asset_deletion(session_id, asset_id)
        manager.release_asset_deletion(session_id, asset_id, claim)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
def test_real_redis_asset_identity_handoff_validates_and_persists_generations(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    identities = (
        ("1" * 32, "3" * 32),
        ("2" * 32, "4" * 32),
    )

    if not use_lua:
        def unknown_command(**_kwargs):
            raise ResponseError("unknown command 'evalsha'")

        monkeypatch.setattr(
            manager,
            "_acquire_asset_read_pin_script",
            unknown_command,
        )
        monkeypatch.setattr(
            manager,
            "_reserve_enqueue_script",
            unknown_command,
        )
        monkeypatch.setattr(
            manager,
            "_release_task_pin_script",
            unknown_command,
        )

    stale_pin = manager.acquire_asset_read_pin(session_id, identities)
    with pytest.raises(TaskConflictError, match="read pin expired"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_paths=[str(tmp_path / "clip.mp4")] * 2),
            owner_id=session_id,
            asset_identities=(
                (identities[0][0], "5" * 32),
                identities[1],
            ),
            asset_read_pin_token=stale_pin.token,
        )
    assert stale_pin.release()

    pin = manager.acquire_asset_read_pin(session_id, identities)
    pin_metadata = real_redis_connection.hgetall(
        f"dt:resource:asset-read-pin:{pin.token}"
    )
    assert json.loads(pin_metadata[b"asset_identities"]) == [
        list(identity) for identity in identities
    ]
    assert b"asset_ids" not in pin_metadata

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_paths=[str(tmp_path / "clip.mp4")] * 2),
        owner_id=session_id,
        asset_identities=identities,
        asset_read_pin_token=pin.token,
    )

    task_pin = real_redis_connection.hgetall(
        f"dt:resource:task-pin:{task.task_id}"
    )
    assert json.loads(task_pin[b"asset_identities"]) == [
        list(identity) for identity in identities
    ]
    assert json.loads(task_pin[b"asset_ids"]) == [
        asset_id for asset_id, _generation_id in identities
    ]
    for asset_id, _generation_id in identities:
        with pytest.raises(TaskConflictError, match="active task"):
            manager.claim_asset_deletion(session_id, asset_id)

    manager.mark_finished(task.task_id, status=TaskStatus.SUCCEEDED)
    for asset_id, _generation_id in identities:
        claim = manager.claim_asset_deletion(session_id, asset_id)
        manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_old_worker_releases_new_generation_task_pin(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    identities = (
        ("1" * 32, "3" * 32),
        ("2" * 32, "4" * 32),
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_paths=[str(tmp_path / "clip.mp4")] * 2),
        owner_id=session_id,
        asset_identities=identities,
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    session_pins_key = f"dt:resource:session:{session_id}:task-pins"
    old_release = real_redis_connection.register_script(
        _PRE_GENERATION_RELEASE_TASK_PIN_SCRIPT
    )

    assert json.loads(real_redis_connection.hget(pin_key, "asset_ids")) == [
        asset_id for asset_id, _generation_id in identities
    ]
    assert int(
        old_release(
            keys=[pin_key, session_pins_key],
            args=[session_id, task.task_id, "dt:resource:asset-tasks:"],
        )
    ) == 1

    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(session_pins_key)
    assert list(
        real_redis_connection.scan_iter(match="dt:resource:asset-tasks:*")
    ) == []
    for asset_id, _generation_id in identities:
        claim = manager.claim_asset_deletion(session_id, asset_id)
        manager.release_asset_deletion(session_id, asset_id, claim)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
@pytest.mark.parametrize(
    "corruption",
    [
        "asset-divergent",
        "asset-malformed",
        "task-mismatch",
        "session-mismatch",
        "session-malformed",
    ],
)
def test_real_redis_task_pin_release_repairs_untrusted_metadata(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
    corruption: str,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    shared_asset_id = "1" * 32
    exclusive_asset_id = "2" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_paths=[str(tmp_path / "clip.mp4")] * 2),
        owner_id=session_id,
        asset_identities=(
            (shared_asset_id, "3" * 32),
            (exclusive_asset_id, "4" * 32),
        ),
    )
    other_task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "other.mp4")),
        owner_id=session_id,
        asset_identities=((shared_asset_id, "5" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    session_pins_key = f"dt:resource:session:{session_id}:task-pins"
    if corruption == "asset-divergent":
        real_redis_connection.hset(
            pin_key,
            "asset_ids",
            json.dumps(["f" * 32]),
        )
    elif corruption == "asset-malformed":
        real_redis_connection.hset(pin_key, "asset_identities", "{")
    elif corruption == "task-mismatch":
        real_redis_connection.hset(
            pin_key,
            "task_id",
            "ffffffff-ffff-4fff-bfff-ffffffffffff",
        )
    elif corruption == "session-mismatch":
        real_redis_connection.hset(pin_key, "session_id", "b" * 32)
    else:
        real_redis_connection.hset(pin_key, "session_id", "not-a-session")
    if not use_lua:
        monkeypatch.setattr(
            manager,
            "_release_task_pin_script",
            lambda **_kwargs: (_ for _ in ()).throw(
                ResponseError("unknown command 'evalsha'")
            ),
        )

    assert manager.release_task_pin(task.task_id)

    assert not real_redis_connection.exists(pin_key)
    assert real_redis_connection.smembers(session_pins_key) == {
        other_task.task_id.encode()
    }
    assert real_redis_connection.smembers(
        f"dt:resource:asset-tasks:{session_id}:{shared_asset_id}"
    ) == {other_task.task_id.encode()}
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{exclusive_asset_id}"
    )


@pytest.mark.parametrize(
    "corruption",
    ["self-consistent-empty", "self-consistent-wrong"],
)
@pytest.mark.parametrize(
    "release_via_reconcile",
    [False, True],
    ids=["release", "reconcile"],
)
def test_real_redis_task_pin_uses_canonical_asset_identities(
    real_redis_connection: Redis,
    tmp_path: Path,
    corruption: str,
    release_via_reconcile: bool,
) -> None:
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=_resources(real_redis_connection),
    )
    session_id = "a" * 32
    shared_asset_id = "1" * 32
    exclusive_asset_id = "2" * 32
    wrong_asset_id = "3" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_paths=[str(tmp_path / "clip.mp4")] * 2),
        owner_id=session_id,
        asset_identities=(
            (shared_asset_id, "4" * 32),
            (exclusive_asset_id, "5" * 32),
        ),
    )
    other_task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "other.mp4")),
        owner_id=session_id,
        asset_identities=((shared_asset_id, "6" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    corrupted_identities = (
        []
        if corruption == "self-consistent-empty"
        else [[wrong_asset_id, "7" * 32]]
    )
    real_redis_connection.hset(
        pin_key,
        mapping={
            "asset_ids": json.dumps(
                [identity[0] for identity in corrupted_identities]
            ),
            "asset_identities": json.dumps(corrupted_identities),
        },
    )
    if corrupted_identities:
        real_redis_connection.sadd(
            f"dt:resource:asset-tasks:{session_id}:{wrong_asset_id}",
            task.task_id,
        )
    if release_via_reconcile:
        task.status = TaskStatus.SUCCEEDED
        manager._save_task(task)
        assert manager.reconcile_tasks(grace_seconds=60) >= 1
    else:
        assert manager.release_task_pin(task.task_id)

    assert not real_redis_connection.exists(pin_key)
    assert real_redis_connection.smembers(
        f"dt:resource:session:{session_id}:task-pins"
    ) == {other_task.task_id.encode()}
    assert real_redis_connection.smembers(
        f"dt:resource:asset-tasks:{session_id}:{shared_asset_id}"
    ) == {other_task.task_id.encode()}
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{exclusive_asset_id}"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{wrong_asset_id}"
    )


def test_real_redis_coordinator_rejects_task_pin_from_unexpected_session(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    resources = _resources(real_redis_connection)
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    real_redis_connection.hset(pin_key, "session_id", "b" * 32)

    with pytest.raises(CorruptTaskPinError, match="ownership is invalid"):
        resources.release_task_pin(
            task.task_id,
            expected_session_id=session_id,
            expected_asset_identities=task.asset_identities,
        )

    assert real_redis_connection.exists(pin_key)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
@pytest.mark.parametrize(
    "release_via_reconcile",
    [False, True],
    ids=["release", "reconcile"],
)
def test_real_redis_corrupt_task_pin_repairs_all_reverse_indexes(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
    release_via_reconcile: bool,
) -> None:
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=_resources(real_redis_connection),
    )
    session_id = "a" * 32
    shared_asset_id = "1" * 32
    exclusive_asset_id = "2" * 32
    orphan_asset_id = "3" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_paths=[str(tmp_path / "clip.mp4")] * 2),
        owner_id=session_id,
        asset_identities=(
            (shared_asset_id, "4" * 32),
            (exclusive_asset_id, "5" * 32),
        ),
    )
    other_task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "other.mp4")),
        owner_id=session_id,
        asset_identities=((shared_asset_id, "6" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    session_pins_key = f"dt:resource:session:{session_id}:task-pins"
    orphan_asset_tasks_key = (
        f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}"
    )
    malformed_asset_tasks_key = (
        f"dt:resource:asset-tasks:{session_id}:not-an-asset"
    )
    real_redis_connection.sadd(orphan_asset_tasks_key, task.task_id)
    real_redis_connection.sadd(malformed_asset_tasks_key, task.task_id)
    real_redis_connection.hdel(pin_key, "asset_ids", "asset_identities")
    if not use_lua:
        monkeypatch.setattr(
            manager,
            "_release_task_pin_script",
            lambda **_kwargs: (_ for _ in ()).throw(
                ResponseError("unknown command 'evalsha'")
            ),
        )
    if release_via_reconcile:
        task.status = TaskStatus.SUCCEEDED
        manager._save_task(task)
        assert manager._reconcile_stale_task_pins(set()) == 1
    else:
        assert manager.release_task_pin(task.task_id)

    assert not real_redis_connection.exists(pin_key)
    assert real_redis_connection.smembers(session_pins_key) == {
        other_task.task_id.encode()
    }
    assert real_redis_connection.smembers(
        f"dt:resource:asset-tasks:{session_id}:{shared_asset_id}"
    ) == {other_task.task_id.encode()}
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{exclusive_asset_id}"
    )
    assert not real_redis_connection.exists(orphan_asset_tasks_key)
    assert real_redis_connection.smembers(malformed_asset_tasks_key) == {
        task.task_id.encode()
    }
    for asset_id in (exclusive_asset_id, orphan_asset_id):
        claim = manager.claim_asset_deletion(session_id, asset_id)
        manager.release_asset_deletion(session_id, asset_id, claim)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
def test_real_redis_corrupt_task_pin_uses_task_owner_when_session_is_missing(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "2" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    real_redis_connection.hdel(
        pin_key,
        "session_id",
        "asset_ids",
        "asset_identities",
    )
    if not use_lua:
        monkeypatch.setattr(
            manager,
            "_release_task_pin_script",
            lambda **_kwargs: (_ for _ in ()).throw(
                ResponseError("unknown command 'evalsha'")
            ),
        )

    assert manager.release_task_pin(task.task_id)

    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    )
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_missing_task_pin_repairs_reverse_indexes(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "2" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    real_redis_connection.delete(pin_key)

    assert manager.release_task_pin(task.task_id)
    assert manager.release_task_pin(task.task_id)

    assert not real_redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    )


def test_real_redis_missing_task_record_repairs_indexes_by_task_id(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    orphan_asset_id = "2" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "3" * 32),),
    )
    orphan_asset_tasks_key = (
        f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}"
    )
    real_redis_connection.sadd(orphan_asset_tasks_key, task.task_id)
    real_redis_connection.delete(f"dt:task:{task.task_id}")

    assert manager.release_task_pin(task.task_id)
    assert not real_redis_connection.exists(
        f"dt:resource:task-pin:{task.task_id}"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    )
    assert not real_redis_connection.exists(orphan_asset_tasks_key)
    assert manager.release_task_pin(task.task_id)


def test_real_redis_corrupt_task_pin_scans_session_indexes_without_owner(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    task_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    asset_id = "1" * 32
    pin_key = f"dt:resource:task-pin:{task_id}"
    session_pins_key = f"dt:resource:session:{session_id}:task-pins"
    asset_tasks_key = f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    malformed_session_pins_key = "dt:resource:session:not-a-session:task-pins"
    real_redis_connection.hset(pin_key, mapping={"task_id": task_id})
    real_redis_connection.sadd(session_pins_key, task_id)
    real_redis_connection.sadd(malformed_session_pins_key, task_id)
    real_redis_connection.sadd(asset_tasks_key, task_id)

    assert manager.release_task_pin(task_id)

    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(session_pins_key)
    assert not real_redis_connection.exists(asset_tasks_key)
    assert real_redis_connection.smembers(malformed_session_pins_key) == {
        task_id.encode()
    }
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_corrupt_task_pin_repair_is_idempotent_after_ack_loss(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "2" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    real_redis_connection.hdel(pin_key, "asset_ids", "asset_identities")
    repair = manager._repair_task_reverse_indexes

    def repair_then_lose_ack(*args, **kwargs):
        repair(*args, **kwargs)
        raise RedisConnectionError("corrupt pin repair acknowledgement lost")

    monkeypatch.setattr(
        manager,
        "_repair_task_reverse_indexes",
        repair_then_lose_ack,
    )
    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        manager.release_task_pin(task.task_id)

    monkeypatch.setattr(manager, "_repair_task_reverse_indexes", repair)
    assert manager.release_task_pin(task.task_id)
    assert manager._repair_task_reverse_indexes(task.task_id)
    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    )
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
@pytest.mark.parametrize(
    "asset_identities",
    [(), (("1" * 32, "2" * 32),)],
    ids=["empty", "with-asset"],
)
def test_real_redis_task_pin_normal_release_does_not_scan(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
    asset_identities: tuple[tuple[str, str], ...],
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=asset_identities,
    )
    if not use_lua:
        monkeypatch.setattr(
            manager,
            "_release_task_pin_script",
            lambda **_kwargs: (_ for _ in ()).throw(
                ResponseError("unknown command 'evalsha'")
            ),
        )
    monkeypatch.setattr(
        real_redis_connection,
        "scan_iter",
        lambda **_kwargs: pytest.fail("normal task pin release must not scan"),
    )

    assert manager.release_task_pin(task.task_id)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
@pytest.mark.parametrize(
    "corruption",
    [
        "asset-missing",
        "asset-divergent",
        "asset-malformed",
        "asset-self-consistent-empty",
        "asset-self-consistent-wrong",
        "pin-missing",
        "task-mismatch",
        "session-mismatch",
        "session-malformed",
        "intent-session-mismatch",
    ],
)
def test_real_redis_enqueue_rollback_repairs_corrupt_task_pin_indexes(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
    corruption: str,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    shared_asset_id = "1" * 32
    exclusive_asset_id = "2" * 32
    orphan_asset_id = "3" * 32
    other_task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "other.mp4")),
        owner_id=session_id,
        asset_identities=((shared_asset_id, "4" * 32),),
    )
    pin_key: bytes | str | None = None
    task_id: str | None = None
    owner_token: str | None = None

    if not use_lua:
        monkeypatch.setattr(
            manager,
            "_rollback_enqueue_script",
            lambda **_kwargs: (_ for _ in ()).throw(
                ResponseError("unknown command 'evalsha'")
            ),
        )

    def corrupt_pin_then_reject(_enqueue) -> None:
        nonlocal owner_token, pin_key, task_id
        [pin_key] = [
            key
            for key in real_redis_connection.scan_iter(
                match="dt:resource:task-pin:*"
            )
            if not key.decode().endswith(other_task.task_id)
        ]
        task_id = real_redis_connection.hget(pin_key, "task_id").decode()
        owner_token = real_redis_connection.hget(
            f"dt:enqueue-intent:{task_id}",
            "owner_token",
        ).decode()
        real_redis_connection.sadd(
            f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}",
            task_id,
        )
        if corruption == "asset-missing":
            real_redis_connection.hdel(
                pin_key,
                "asset_ids",
                "asset_identities",
            )
        elif corruption == "asset-divergent":
            real_redis_connection.hset(
                pin_key,
                "asset_ids",
                json.dumps(["f" * 32]),
            )
        elif corruption == "asset-malformed":
            real_redis_connection.hset(pin_key, "asset_identities", "{")
        elif corruption == "asset-self-consistent-empty":
            real_redis_connection.hset(
                pin_key,
                mapping={
                    "asset_ids": "[]",
                    "asset_identities": "[]",
                },
            )
        elif corruption == "asset-self-consistent-wrong":
            real_redis_connection.hset(
                pin_key,
                mapping={
                    "asset_ids": json.dumps([orphan_asset_id]),
                    "asset_identities": json.dumps(
                        [[orphan_asset_id, "7" * 32]]
                    ),
                },
            )
        elif corruption == "pin-missing":
            real_redis_connection.delete(pin_key)
        elif corruption == "task-mismatch":
            real_redis_connection.hset(
                pin_key,
                "task_id",
                "ffffffff-ffff-4fff-bfff-ffffffffffff",
            )
        elif corruption == "session-mismatch":
            real_redis_connection.hset(pin_key, "session_id", "b" * 32)
        elif corruption == "session-malformed":
            real_redis_connection.hset(pin_key, "session_id", "not-a-session")
        else:
            real_redis_connection.hset(
                f"dt:enqueue-intent:{task_id}",
                "owner_id",
                "b" * 32,
            )
        raise TaskConflictError("enqueue rejected")

    with pytest.raises(TaskConflictError, match="enqueue rejected"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(
                input_paths=[str(tmp_path / "clip.mp4")] * 2
            ),
            owner_id=session_id,
            commit=corrupt_pin_then_reject,
            asset_identities=(
                (shared_asset_id, "5" * 32),
                (exclusive_asset_id, "6" * 32),
            ),
        )

    assert pin_key is not None
    assert task_id is not None
    assert owner_token is not None
    assert not real_redis_connection.exists(pin_key)
    assert real_redis_connection.smembers(
        f"dt:resource:session:{session_id}:task-pins"
    ) == {other_task.task_id.encode()}
    assert real_redis_connection.smembers(
        f"dt:resource:asset-tasks:{session_id}:{shared_asset_id}"
    ) == {other_task.task_id.encode()}
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{exclusive_asset_id}"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}"
    )
    assert manager._rollback_enqueue(
        task_id,
        owner_id=session_id,
        owner_token=owner_token,
    )
    for asset_id in (exclusive_asset_id, orphan_asset_id):
        claim = manager.claim_asset_deletion(session_id, asset_id)
        manager.release_asset_deletion(session_id, asset_id, claim)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
def test_real_redis_normal_enqueue_rollback_does_not_scan(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    if not use_lua:
        monkeypatch.setattr(
            manager,
            "_rollback_enqueue_script",
            lambda **_kwargs: (_ for _ in ()).throw(
                ResponseError("unknown command 'evalsha'")
            ),
        )

    def reject_enqueue(_enqueue) -> None:
        monkeypatch.setattr(
            real_redis_connection,
            "scan_iter",
            lambda **_kwargs: pytest.fail(
                "normal enqueue rollback must not scan"
            ),
        )
        raise TaskConflictError("enqueue rejected")

    with pytest.raises(TaskConflictError, match="enqueue rejected"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
            commit=reject_enqueue,
            asset_identities=((asset_id, "2" * 32),),
        )

    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
@pytest.mark.parametrize(
    "corruption",
    ["missing", "divergent"],
)
def test_real_redis_enqueue_rollback_repairs_corrupt_pin_after_ack_loss(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
    corruption: str,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    task_id: str | None = None
    owner_token: str | None = None
    pin_key: bytes | str | None = None
    rollback_script = manager._rollback_enqueue_script
    pipeline_type = type(real_redis_connection.pipeline())
    pipeline_execute = pipeline_type.execute
    acknowledgement_lost = False

    def corrupt_pin_then_reject(_enqueue) -> None:
        nonlocal acknowledgement_lost, owner_token, pin_key, task_id
        [pin_key] = list(
            real_redis_connection.scan_iter(match="dt:resource:task-pin:*")
        )
        task_id = real_redis_connection.hget(pin_key, "task_id").decode()
        intent_key = f"dt:enqueue-intent:{task_id}"
        owner_token = real_redis_connection.hget(
            intent_key,
            "owner_token",
        ).decode()
        if corruption == "missing":
            real_redis_connection.hdel(
                pin_key,
                "asset_ids",
                "asset_identities",
            )
        else:
            real_redis_connection.hset(
                pin_key,
                "asset_ids",
                json.dumps(["f" * 32]),
            )

        if use_lua:
            def rollback_then_lose_ack(*args, **kwargs):
                rollback_script(*args, **kwargs)
                raise RedisConnectionError("rollback acknowledgement lost")

            monkeypatch.setattr(
                manager,
                "_rollback_enqueue_script",
                rollback_then_lose_ack,
            )
        else:
            monkeypatch.setattr(
                manager,
                "_rollback_enqueue_script",
                lambda **_kwargs: (_ for _ in ()).throw(
                    ResponseError("unknown command 'evalsha'")
                ),
            )

            def execute_then_lose_ack(pipeline, *args, **kwargs):
                nonlocal acknowledgement_lost
                lose_ack = not acknowledgement_lost and any(
                    command[0][0] == "DEL"
                    and intent_key in command[0]
                    for command in pipeline.command_stack
                )
                result = pipeline_execute(pipeline, *args, **kwargs)
                if lose_ack:
                    acknowledgement_lost = True
                    raise RedisConnectionError(
                        "rollback acknowledgement lost"
                    )
                return result

            monkeypatch.setattr(
                pipeline_type,
                "execute",
                execute_then_lose_ack,
            )
        raise TaskConflictError("enqueue rejected")

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
            commit=corrupt_pin_then_reject,
            asset_identities=((asset_id, "2" * 32),),
        )

    assert task_id is not None
    assert owner_token is not None
    assert pin_key is not None
    assert not real_redis_connection.exists(f"dt:task:{task_id}")
    assert not real_redis_connection.exists(f"dt:enqueue-intent:{task_id}")
    assert real_redis_connection.exists(pin_key)
    if use_lua:
        monkeypatch.setattr(
            manager,
            "_rollback_enqueue_script",
            rollback_script,
        )

    assert manager._rollback_enqueue(
        task_id,
        owner_id=session_id,
        owner_token=owner_token,
    )
    assert manager._rollback_enqueue(
        task_id,
        owner_id=session_id,
        owner_token=owner_token,
    )
    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    )
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_task_pin_release_repairs_legacy_task_without_canonical_identities(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "legacy.mp4")),
        owner_id=session_id,
        asset_ids=(asset_id,),
    )
    task_data = json.loads(redis_connection.get(f"dt:task:{task.task_id}"))
    task_data.pop("asset_identities", None)
    redis_connection.set(
        f"dt:task:{task.task_id}",
        json.dumps(task_data),
    )
    orphan_asset_id = "2" * 32
    redis_connection.sadd(
        f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}",
        task.task_id,
    )

    assert manager.release_task_pin(task.task_id)
    assert not redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}"
    )
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_asset_read_pin_rejects_generation_mismatch_during_handoff(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    pin = manager.acquire_asset_read_pin(
        session_id,
        ((asset_id, "2" * 32),),
    )

    with pytest.raises(TaskConflictError, match="read pin expired"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
            asset_identities=((asset_id, "3" * 32),),
            asset_read_pin_token=pin.token,
        )

    assert pin.release()
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_multiple_asset_read_pins_handoff_independently(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    pins = [
        manager.acquire_asset_read_pin(session_id, (asset_id,))
        for _index in range(2)
    ]
    tasks = [
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
            asset_ids=(asset_id,),
            asset_read_pin_token=pin.token,
        )
        for pin in pins
    ]

    manager.mark_finished(tasks[0].task_id, status=TaskStatus.SUCCEEDED)
    with pytest.raises(
        TaskConflictError,
        match="referenced by an active task",
    ):
        manager.claim_asset_deletion(session_id, asset_id)

    manager.mark_finished(tasks[1].task_id, status=TaskStatus.FAILED, error="failed")
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_asset_read_pin_refresh_and_stale_reconcile(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    identities = ((asset_id, "2" * 32),)
    pin = manager.acquire_asset_read_pin(session_id, identities)
    pin_key = f"dt:resource:asset-read-pin:{pin.token}"
    readers_key = f"dt:resource:asset-readers:{session_id}:{asset_id}"
    real_redis_connection.pexpire(pin_key, 1_000)
    real_redis_connection.zadd(readers_key, {pin.token: 0})

    pin.refresh()

    assert real_redis_connection.pttl(pin_key) > 1_000
    assert real_redis_connection.zscore(readers_key, pin.token) > time.time() * 1000
    real_redis_connection.delete(pin_key)
    real_redis_connection.zadd(readers_key, {pin.token: 0})
    assert manager.reconcile_tasks(grace_seconds=60) == 1
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_asset_read_pin_release_recovers_acknowledgement_loss(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    pin = manager.acquire_asset_read_pin(session_id, (asset_id,))
    release = manager._release_asset_read_pin_script

    def release_then_lose_ack(*args, **kwargs):
        release(*args, **kwargs)
        raise RedisConnectionError("asset read pin release acknowledgement lost")

    monkeypatch.setattr(
        manager,
        "_release_asset_read_pin_script",
        release_then_lose_ack,
    )

    assert pin.release()
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_asset_read_pin_handoff_ack_loss_is_reconciled(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    identities = ((asset_id, "2" * 32),)
    pin = manager.acquire_asset_read_pin(session_id, identities)
    reserve = manager._reserve_enqueue_script

    def reserve_then_lose_ack(*args, **kwargs):
        reserve(*args, **kwargs)
        raise RedisConnectionError("asset read pin handoff acknowledgement lost")

    monkeypatch.setattr(manager, "_reserve_enqueue_script", reserve_then_lose_ack)

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
            asset_identities=identities,
            asset_read_pin_token=pin.token,
        )

    assert not pin.release()
    assert list(
        real_redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        real_redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []
    with pytest.raises(
        TaskConflictError,
        match="referenced by an active task",
    ):
        manager.claim_asset_deletion(session_id, asset_id)

    monkeypatch.setattr(manager, "_reserve_enqueue_script", reserve)
    for key in real_redis_connection.scan_iter(match="dt:enqueue-owner:*"):
        real_redis_connection.delete(key)
    assert manager.reconcile_tasks(grace_seconds=0) == 1
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_reconcile_releases_terminal_asset_pin_after_ack_loss(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    identities = ((asset_id, "2" * 32),)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=identities,
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    pin_metadata = real_redis_connection.hgetall(pin_key)
    assert json.loads(pin_metadata[b"asset_ids"]) == [asset_id]
    assert json.loads(pin_metadata[b"asset_identities"]) == [
        list(identity) for identity in identities
    ]
    release_task_pin = manager.release_task_pin

    def lose_release_acknowledgement(_task_id: str) -> bool:
        raise RedisConnectionError("task pin release acknowledgement lost")

    monkeypatch.setattr(manager, "release_task_pin", lose_release_acknowledgement)

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        manager.mark_finished(task.task_id, status=TaskStatus.SUCCEEDED)

    assert manager.get_task(task.task_id).status is TaskStatus.SUCCEEDED
    monkeypatch.setattr(manager, "release_task_pin", release_task_pin)
    assert manager.reconcile_tasks(grace_seconds=60) == 1
    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert list(
        real_redis_connection.scan_iter(match="dt:resource:asset-tasks:*")
    ) == []
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_reconcile_repairs_all_corrupt_coordinator_task_pins(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    session_id = "a" * 32
    asset_id = "1" * 32
    corrupt_tasks = [
        manager.create_video_frames_task(
            VideoFramesTaskRequest(
                input_path=str(tmp_path / f"corrupt-{index}.mp4")
            ),
            owner_id=session_id,
            asset_identities=((asset_id, f"{index + 2:032x}"),),
        )
        for index in range(3)
    ]
    other_task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "active.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "f" * 32),),
    )
    for task in corrupt_tasks:
        task.status = TaskStatus.SUCCEEDED
        manager._save_task(task)

    malformed, divergent, task_mismatch = corrupt_tasks
    real_redis_connection.hset(
        f"dt:resource:task-pin:{malformed.task_id}",
        "asset_identities",
        "{",
    )
    real_redis_connection.hset(
        f"dt:resource:task-pin:{divergent.task_id}",
        "asset_ids",
        json.dumps(["e" * 32]),
    )
    real_redis_connection.hset(
        f"dt:resource:task-pin:{task_mismatch.task_id}",
        "task_id",
        "ffffffff-ffff-4fff-bfff-ffffffffffff",
    )

    assert manager.reconcile_tasks(grace_seconds=60) == 3
    assert manager.reconcile_tasks(grace_seconds=60) == 0

    assert list(
        real_redis_connection.scan_iter(match="dt:resource:task-pin:*")
    ) == [f"dt:resource:task-pin:{other_task.task_id}".encode()]
    assert real_redis_connection.smembers(
        f"dt:resource:session:{session_id}:task-pins"
    ) == {other_task.task_id.encode()}
    assert real_redis_connection.smembers(
        f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    ) == {other_task.task_id.encode()}


def test_real_redis_reconcile_repairs_valid_pin_with_wrong_owner_identity(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    owner_id = "a" * 32
    fake_owner_id = "b" * 32
    asset_id = "1" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "terminal.mp4")),
        owner_id=owner_id,
        asset_identities=((asset_id, "2" * 32),),
    )
    fake_owner_task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "active.mp4")),
        owner_id=fake_owner_id,
        asset_identities=((asset_id, "3" * 32),),
    )
    task.status = TaskStatus.SUCCEEDED
    manager._save_task(task)

    pin_key = f"dt:resource:task-pin:{task.task_id}"
    fake_owner_pins_key = (
        f"dt:resource:session:{fake_owner_id}:task-pins"
    )
    fake_owner_asset_tasks_key = (
        f"dt:resource:asset-tasks:{fake_owner_id}:{asset_id}"
    )
    real_redis_connection.hset(pin_key, "session_id", fake_owner_id)
    real_redis_connection.sadd(fake_owner_pins_key, task.task_id)
    real_redis_connection.sadd(fake_owner_asset_tasks_key, task.task_id)

    assert manager.reconcile_tasks(grace_seconds=60) == 1
    assert manager.reconcile_tasks(grace_seconds=60) == 0

    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(
        f"dt:resource:session:{owner_id}:task-pins"
    )
    assert not real_redis_connection.exists(
        f"dt:resource:asset-tasks:{owner_id}:{asset_id}"
    )
    assert real_redis_connection.smembers(fake_owner_pins_key) == {
        fake_owner_task.task_id.encode()
    }
    assert real_redis_connection.smembers(fake_owner_asset_tasks_key) == {
        fake_owner_task.task_id.encode()
    }


def test_real_redis_reconcile_removes_malformed_pins_and_reverse_orphans(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    active = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "active.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "2" * 32),),
    )
    terminal = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "terminal.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "3" * 32),),
    )
    terminal.status = TaskStatus.SUCCEEDED
    manager._save_task(terminal)
    no_pin_terminal = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "no-pin.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "4" * 32),),
    )
    no_pin_terminal.status = TaskStatus.SUCCEEDED
    manager._save_task(no_pin_terminal)
    real_redis_connection.delete(
        f"dt:resource:task-pin:{no_pin_terminal.task_id}"
    )
    malformed_task_id = "not-a-task"
    missing_task_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    oversized_task_id = "x" * 129
    binary_task_id = b"\xff"
    malformed_pin_key = f"dt:resource:task-pin:{malformed_task_id}"
    oversized_pin_key = f"dt:resource:task-pin:{oversized_task_id}"
    binary_pin_key = b"dt:resource:task-pin:" + binary_task_id
    unrelated_key = f"dt:resource:not-task-pin:{malformed_task_id}"
    session_pins_key = f"dt:resource:session:{session_id}:task-pins"
    asset_tasks_key = f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    real_redis_connection.hset(
        malformed_pin_key,
        mapping={"task_id": malformed_task_id, "session_id": session_id},
    )
    real_redis_connection.hset(oversized_pin_key, "task_id", oversized_task_id)
    real_redis_connection.hset(binary_pin_key, b"task_id", binary_task_id)
    real_redis_connection.set(unrelated_key, "preserve")
    real_redis_connection.sadd(
        session_pins_key,
        malformed_task_id,
        missing_task_id,
        oversized_task_id,
        binary_task_id,
    )
    real_redis_connection.sadd(
        asset_tasks_key,
        malformed_task_id,
        missing_task_id,
        oversized_task_id,
        binary_task_id,
    )

    assert manager.reconcile_tasks(grace_seconds=60) >= 4
    assert manager.reconcile_tasks(grace_seconds=60) == 0

    assert not real_redis_connection.exists(malformed_pin_key)
    assert not real_redis_connection.exists(oversized_pin_key)
    assert not real_redis_connection.exists(binary_pin_key)
    assert real_redis_connection.get(unrelated_key) == b"preserve"
    assert not real_redis_connection.exists(
        f"dt:resource:task-pin:{terminal.task_id}"
    )
    assert real_redis_connection.smembers(session_pins_key) == {
        active.task_id.encode()
    }
    assert real_redis_connection.smembers(asset_tasks_key) == {
        active.task_id.encode()
    }


def test_real_redis_reconcile_propagates_reverse_scan_transport_errors(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    scan_iter = real_redis_connection.scan_iter

    def fail_resource_scan(*, match=None, **kwargs):
        if match == "dt:resource:*":
            raise RedisConnectionError("reverse index scan disconnected")
        return scan_iter(match=match, **kwargs)

    monkeypatch.setattr(real_redis_connection, "scan_iter", fail_resource_scan)

    with pytest.raises(
        RedisConnectionError,
        match="reverse index scan disconnected",
    ):
        manager.reconcile_tasks(grace_seconds=60)


def test_real_redis_reconcile_normal_coordinator_release_does_not_repair_scan(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=_resources(real_redis_connection),
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    task.status = TaskStatus.SUCCEEDED
    manager._save_task(task)
    monkeypatch.setattr(
        manager,
        "_repair_task_reverse_indexes",
        lambda _task_id: pytest.fail("normal reconcile must not repair-scan"),
    )

    assert manager.reconcile_tasks(grace_seconds=60) == 1
    assert manager.reconcile_tasks(grace_seconds=60) == 0


@pytest.mark.parametrize(
    "error",
    [
        RedisConnectionError("task pin cleanup disconnected"),
        ResourceUnavailableError("storage unavailable"),
    ],
    ids=["redis-transport", "storage-unavailable"],
)
def test_real_redis_reconcile_does_not_treat_availability_errors_as_corruption(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    task.status = TaskStatus.SUCCEEDED
    manager._save_task(task)

    def fail_release(
        _task_id: str,
        *,
        expected_session_id: str | None = None,
        expected_asset_identities: (
            tuple[tuple[str, str | None], ...] | None
        ) = None,
    ) -> None:
        assert expected_session_id == "a" * 32
        assert expected_asset_identities == (("1" * 32, "2" * 32),)
        raise error

    monkeypatch.setattr(resources, "release_task_resources", fail_release)

    with pytest.raises(type(error), match=str(error)):
        manager.reconcile_tasks(grace_seconds=60)


def test_real_redis_task_pin_release_ack_loss_leaves_no_asset_blocker(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "2" * 32),),
    )
    pin_key = f"dt:resource:task-pin:{task.task_id}"
    release = manager._release_task_pin_script

    def release_then_lose_ack(*args, **kwargs):
        release(*args, **kwargs)
        raise RedisConnectionError("task pin release acknowledgement lost")

    monkeypatch.setattr(
        manager,
        "_release_task_pin_script",
        release_then_lose_ack,
    )

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        manager.mark_finished(task.task_id, status=TaskStatus.SUCCEEDED)

    assert manager.get_task(task.task_id).status is TaskStatus.SUCCEEDED
    assert not real_redis_connection.exists(pin_key)
    assert not real_redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert list(
        real_redis_connection.scan_iter(match="dt:resource:asset-tasks:*")
    ) == []
    claim = manager.claim_asset_deletion(session_id, asset_id)
    manager.release_asset_deletion(session_id, asset_id, claim)


def test_real_redis_repeated_terminal_does_not_shorten_ttl_or_resurrect(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    retention_seconds = 300
    manager = _manager(
        real_redis_connection,
        tmp_path,
        retention_seconds=retention_seconds,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    assert manager._finalize_external_failure(task.task_id, "worker failed")
    keys = (
        f"dt:task:{task.task_id}",
        f"dt:task-work:{task.task_id}",
        f"dt:task:{task.task_id}:events",
        f"dt:task:{task.task_id}:event-index",
        f"dt:event-generation:{task.task_id}",
        f"dt:failure-cleanup:{task.task_id}",
    )
    for key in keys:
        real_redis_connection.expire(key, retention_seconds + 120)
    ttl_before_retry = {
        key: real_redis_connection.pttl(key) for key in keys
    }

    assert not manager._finalize_external_failure(
        task.task_id,
        "repeated worker failure",
    )

    for key in keys:
        ttl_after_retry = real_redis_connection.pttl(key)
        assert ttl_before_retry[key] - 2_000 <= ttl_after_retry
        assert ttl_after_retry <= ttl_before_retry[key]

    real_redis_connection.delete(f"dt:task:{task.task_id}")
    with pytest.raises(KeyError):
        manager.mark_finished(task.task_id, status=TaskStatus.FAILED)
    assert not real_redis_connection.exists(f"dt:task:{task.task_id}")


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


def test_real_redis_queue_reconcile_cannot_release_inflight_enqueue_slot(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        real_redis_connection,
        tmp_path,
        max_queued_tasks=20,
    )
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    save_entered = Event()
    continue_save = Event()
    selection_lock = Lock()
    first_save = True
    original_save = manager._save_task

    def block_first_save(task: TaskRecord) -> None:
        nonlocal first_save
        with selection_lock:
            should_block = first_save
            first_save = False
        if should_block:
            save_entered.set()
            assert continue_save.wait(timeout=5)
        original_save(task)

    def submit() -> bool:
        try:
            manager.create_video_frames_task(
                request,
                owner_id="a" * 32,
            )
        except TaskConflictError:
            return False
        return True

    monkeypatch.setattr(manager, "_save_task", block_first_save)
    with ThreadPoolExecutor(max_workers=21) as executor:
        first = executor.submit(submit)
        assert save_entered.wait(timeout=5)
        assert manager.reconcile_tasks() == 0
        manager.reconcile_queue_slots()
        remaining = [executor.submit(submit) for _index in range(20)]
        accepted = [future.result(timeout=5) for future in remaining]
        continue_save.set()
        accepted.append(first.result(timeout=5))

    job_ids = manager.queue.get_job_ids()
    slot_ids = [
        value.decode()
        for value in real_redis_connection.lrange(
            "dt:queue:reservations",
            0,
            -1,
        )
    ]
    assert sum(accepted) == len(job_ids)
    assert len(job_ids) <= 20
    assert len(slot_ids) <= 20
    assert set(slot_ids) == set(job_ids)


def test_real_redis_reconcile_preserves_heartbeat_active_enqueue_past_grace(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    session_id = "a" * 32
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
        enqueue_intent_ttl=0.5,
        enqueue_intent_heartbeat_interval=0.05,
    )
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    commit_entered = Event()
    continue_commit = Event()

    def block_before_commit(action):
        commit_entered.set()
        assert continue_commit.wait(timeout=5)
        return action()

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            manager.create_video_frames_task,
            request,
            owner_id=session_id,
            commit=block_before_commit,
        )
        try:
            assert commit_entered.wait(timeout=5)
            time.sleep(0.8)
            assert manager.reconcile_tasks(grace_seconds=0.02) == 0
            intent_keys = list(
                real_redis_connection.scan_iter(match="dt:enqueue-intent:*")
            )
            assert len(intent_keys) == 1
            task_id = intent_keys[0].decode().rsplit(":", 1)[-1]
            assert real_redis_connection.pttl(
                f"dt:enqueue-owner:{task_id}"
            ) > 0
            assert manager.get_task(task_id).status is TaskStatus.QUEUED
            assert real_redis_connection.exists(
                f"dt:resource:task-pin:{task_id}"
            )
        finally:
            continue_commit.set()
        task = pending.result(timeout=5)

    assert manager.queue.get_job_ids() == [task.task_id]
    assert real_redis_connection.llen("dt:queue:reservations") == 1
    assert not real_redis_connection.exists(
        f"dt:enqueue-intent:{task.task_id}"
    )
    assert not real_redis_connection.exists(
        f"dt:enqueue-owner:{task.task_id}"
    )


def test_real_redis_expired_enqueue_owner_is_fenced_after_reconcile(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "a" * 32
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        max_queued_tasks=20,
        resource_coordinator=resources,
        enqueue_intent_ttl=0.5,
        enqueue_intent_heartbeat_interval=0.05,
    )
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    commit_entered = Event()
    continue_commit = Event()
    original_commit = manager._commit_enqueue_job

    def block_before_commit(*args, **kwargs):
        commit_entered.set()
        assert continue_commit.wait(timeout=10)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(manager, "_commit_enqueue_job", block_before_commit)

    with ThreadPoolExecutor(max_workers=21) as executor:
        stale_owner = executor.submit(
            manager.create_video_frames_task,
            request,
            owner_id=session_id,
        )
        assert commit_entered.wait(timeout=5)
        monkeypatch.setattr(manager, "_commit_enqueue_job", original_commit)
        intent_keys = list(
            real_redis_connection.scan_iter(match="dt:enqueue-intent:*")
        )
        assert len(intent_keys) == 1
        stale_task_id = intent_keys[0].decode().rsplit(":", 1)[-1]

        owner_key = f"dt:enqueue-owner:{stale_task_id}"
        assert real_redis_connection.pttl(owner_key) > 0
        deadline = time.monotonic() + 2
        while (
            real_redis_connection.exists(owner_key)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert not real_redis_connection.exists(owner_key)
        assert manager.reconcile_tasks(grace_seconds=0) == 1

        replacements = [
            executor.submit(
                manager.create_video_frames_task,
                request,
                owner_id=session_id,
            )
            for _index in range(20)
        ]
        replacement_tasks = [
            future.result(timeout=10) for future in replacements
        ]
        continue_commit.set()
        with pytest.raises(TaskConflictError, match="reservation expired"):
            stale_owner.result(timeout=5)

    job_ids = manager.queue.get_job_ids()
    slot_ids = [
        value.decode()
        for value in real_redis_connection.lrange(
            "dt:queue:reservations",
            0,
            -1,
        )
    ]
    stale_task = manager.get_task(stale_task_id)
    assert stale_task is not None
    assert stale_task.status is TaskStatus.FAILED
    assert stale_task.error == "Task enqueue could not be confirmed"
    assert stale_task_id not in job_ids
    assert not real_redis_connection.exists(
        f"dt:resource:task-pin:{stale_task_id}"
    )
    assert len(replacement_tasks) == len(job_ids) == len(slot_ids) == 20
    assert set(slot_ids) == set(job_ids)


def test_real_redis_reconcile_claims_expired_intent_before_job_absence(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "a" * 32
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        max_queued_tasks=20,
        resource_coordinator=resources,
    )
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    original_commit = manager._commit_enqueue_job
    original_confirmation = manager._enqueue_has_authoritative_evidence
    staged_job: Job | None = None
    confirmation_calls = 0

    def disconnect_before_commit(job: Job, **_kwargs) -> Job:
        nonlocal staged_job
        staged_job = job
        raise RedisConnectionError("enqueue disconnected before commit")

    def confirmation_disconnect_once(task_id: str) -> bool:
        nonlocal confirmation_calls
        confirmation_calls += 1
        if confirmation_calls == 1:
            raise RedisConnectionError("enqueue confirmation unavailable")
        return original_confirmation(task_id)

    monkeypatch.setattr(manager, "_commit_enqueue_job", disconnect_before_commit)
    monkeypatch.setattr(
        manager,
        "_enqueue_has_authoritative_evidence",
        confirmation_disconnect_once,
    )
    task = manager.create_video_frames_task(request, owner_id=session_id)
    assert staged_job is not None
    intent_key = f"dt:enqueue-intent:{task.task_id}"
    original_token = real_redis_connection.hget(intent_key, "owner_token")
    assert original_token

    pipeline_type = type(real_redis_connection.pipeline())
    original_exists = pipeline_type.exists
    job_absent = Event()
    continue_reconcile = Event()

    def pause_after_job_absence(pipeline, *names):
        result = original_exists(pipeline, *names)
        if (
            names == (Job.key_for(task.task_id),)
            and not result
            and not job_absent.is_set()
        ):
            job_absent.set()
            assert continue_reconcile.wait(timeout=5)
        return result

    monkeypatch.setattr(pipeline_type, "exists", pause_after_job_absence)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending_reconcile = executor.submit(
            manager.reconcile_tasks,
            grace_seconds=0,
        )
        try:
            assert job_absent.wait(timeout=5)
            claimed = real_redis_connection.hgetall(intent_key)
            assert claimed[b"state"] == b"reconciling"
            assert claimed[b"owner_token"] != original_token
            assert (
                manager._claim_expired_enqueue_intent(
                    task.task_id,
                    "competing-reconciler",
                )
                == 1
            )
            assert (
                real_redis_connection.hget(intent_key, "owner_token")
                == claimed[b"owner_token"]
            )
            with pytest.raises(TaskConflictError, match="reservation expired"):
                original_commit(
                    staged_job,
                    task_id=task.task_id,
                    owner_token=original_token.decode(),
                )
        finally:
            continue_reconcile.set()
        assert pending_reconcile.result(timeout=5) == 1

    monkeypatch.setattr(manager, "_commit_enqueue_job", original_commit)

    def submit(_index: int) -> bool:
        try:
            manager.create_video_frames_task(request, owner_id=session_id)
        except TaskConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=20) as executor:
        accepted = list(executor.map(submit, range(20)))

    job_ids = manager.queue.get_job_ids()
    slot_ids = real_redis_connection.lrange(
        "dt:queue:reservations",
        0,
        -1,
    )
    assert sum(accepted) == len(job_ids) == len(slot_ids) == 20
    assert set(slot_ids) == {job_id.encode() for job_id in job_ids}
    assert manager.get_task(task.task_id).status is TaskStatus.FAILED


def test_real_redis_enqueue_commit_before_reconcile_preserves_queued_task(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        real_redis_connection,
        tmp_path,
        max_queued_tasks=20,
    )
    request = VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    commit_entered = Event()
    continue_commit = Event()
    claim_entered = Event()
    continue_claim = Event()
    original_claim = manager._claim_expired_enqueue_intent

    def block_before_commit(action):
        commit_entered.set()
        assert continue_commit.wait(timeout=5)
        return action()

    def block_before_claim(task_id: str, reconciler_token: str) -> int:
        claim_entered.set()
        assert continue_claim.wait(timeout=5)
        return original_claim(task_id, reconciler_token)

    monkeypatch.setattr(
        manager,
        "_claim_expired_enqueue_intent",
        block_before_claim,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending_enqueue = executor.submit(
            manager.create_video_frames_task,
            request,
            owner_id="a" * 32,
            commit=block_before_commit,
        )
        assert commit_entered.wait(timeout=5)
        pending_reconcile = executor.submit(
            manager.reconcile_tasks,
            grace_seconds=0,
        )
        try:
            assert claim_entered.wait(timeout=5)
            continue_commit.set()
            task = pending_enqueue.result(timeout=5)
        finally:
            continue_commit.set()
            continue_claim.set()
        assert pending_reconcile.result(timeout=5) == 0

    assert manager.get_task(task.task_id).status is TaskStatus.QUEUED
    monkeypatch.setattr(
        manager,
        "_claim_expired_enqueue_intent",
        original_claim,
    )

    def submit(_index: int) -> bool:
        try:
            manager.create_video_frames_task(
                request,
                owner_id="a" * 32,
            )
        except TaskConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=20) as executor:
        accepted = list(executor.map(submit, range(20)))

    job_ids = manager.queue.get_job_ids()
    slot_ids = real_redis_connection.lrange(
        "dt:queue:reservations",
        0,
        -1,
    )
    assert sum(accepted) == 19
    assert len(job_ids) == len(slot_ids) == 20
    assert set(slot_ids) == {job_id.encode() for job_id in job_ids}
    assert task.task_id in job_ids


def test_expired_queued_session_is_pinned_until_terminal_cleanup(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    input_path = session_dir / "clip.mp4"
    input_path.write_bytes(b"input")
    os.utime(input_path, (1, 1))
    os.utime(session_dir, (1, 1))
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    storage = HostedStorage(tmp_path, max_upload_bytes=1024)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(input_path)),
        owner_id=session_id,
    )

    assert storage.cleanup_expired(
        retention_seconds=1,
        coordinator=resources,
    ) == 0
    assert session_dir.is_dir()

    assert manager._finalize_external_failure(task.task_id, "worker stopped")
    assert storage.cleanup_expired(
        retention_seconds=1,
        coordinator=resources,
    ) == 1
    assert not session_dir.exists()


def test_cleanup_claim_blocks_atomic_task_pin_and_enqueue(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    session_id = "a" * 32
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    claim = resources.claim_session_cleanup(session_id)
    assert claim is not None

    with pytest.raises(SessionDeletingError):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
        )

    assert real_redis_connection.llen("dt:queue:reservations") == 0
    assert manager.queue.get_job_ids() == []
    resources.release_cleanup_claim(session_id, claim)


def test_reconcile_preserves_active_enqueue_pin_while_task_save_is_blocked(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "a" * 32
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
        enqueue_intent_ttl=0.5,
        enqueue_intent_heartbeat_interval=0.05,
    )
    save_entered = Event()
    continue_save = Event()
    original_save = manager._save_task

    def block_task_save(task: TaskRecord) -> None:
        save_entered.set()
        assert continue_save.wait(timeout=5)
        original_save(task)

    monkeypatch.setattr(manager, "_save_task", block_task_save)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            manager.create_video_frames_task,
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
        )
        try:
            assert save_entered.wait(timeout=5)
            time.sleep(0.8)
            intent_keys = list(
                real_redis_connection.scan_iter(match="dt:enqueue-intent:*")
            )
            assert len(intent_keys) == 1
            task_id = intent_keys[0].decode().rsplit(":", 1)[-1]
            assert manager.reconcile_tasks(grace_seconds=0) == 0
            assert real_redis_connection.exists(
                f"dt:resource:task-pin:{task_id}"
            )
            assert resources.claim_session_cleanup(session_id) is None
        finally:
            continue_save.set()
        task = pending.result(timeout=5)

    assert manager.get_task(task.task_id).status is TaskStatus.QUEUED
    assert real_redis_connection.exists(
        f"dt:resource:task-pin:{task.task_id}"
    )
    assert resources.claim_session_cleanup(session_id) is None


def test_reconcile_releases_task_pin_after_crash_during_enqueue(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "a" * 32
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
        enqueue_intent_ttl=0.2,
        enqueue_intent_heartbeat_interval=0.05,
    )

    class SimulatedProcessCrash(BaseException):
        pass

    def crash_before_task_save(_task: TaskRecord) -> None:
        raise SimulatedProcessCrash("injected process crash")

    monkeypatch.setattr(manager, "_save_task", crash_before_task_save)
    with pytest.raises(SimulatedProcessCrash, match="injected process crash"):
        manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=session_id,
        )

    assert resources.claim_session_cleanup(session_id) is None
    intent_keys = list(
        real_redis_connection.scan_iter(match="dt:enqueue-intent:*")
    )
    assert len(intent_keys) == 1
    task_id = intent_keys[0].decode().rsplit(":", 1)[-1]
    owner_key = f"dt:enqueue-owner:{task_id}"
    deadline = time.monotonic() + 2
    while real_redis_connection.exists(owner_key) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not real_redis_connection.exists(owner_key)
    assert manager.reconcile_tasks(grace_seconds=0) == 1
    claim = resources.claim_session_cleanup(session_id)
    assert claim is not None
    assert real_redis_connection.llen("dt:queue:reservations") == 0
    resources.release_cleanup_claim(session_id, claim)


def test_run_task_reserves_refreshes_and_releases_worker_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(
        tmp_path,
        task_timeout_seconds=9000,
        retention_seconds=300,
    )
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    resources = TrackingTaskResources()
    manager_options: dict[str, object] = {}
    manager_type = worker_module.RedisTaskManager

    def create_manager(**kwargs):
        manager_options.update(kwargs)
        return manager_type(**kwargs)

    _patch_worker_dependencies(
        monkeypatch,
        settings=settings,
        redis_connection=redis_connection,
        resources=resources,
    )
    monkeypatch.setattr(worker_module, "RedisTaskManager", create_manager)
    monkeypatch.setattr(
        redis_connection,
        "scan_iter",
        lambda **_kwargs: pytest.fail(
            "canonical worker pin cleanup must not scan"
        ),
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
    assert resources.task_pin_releases == []
    assert manager_options["task_timeout_seconds"] == 9000
    assert manager_options["retention_seconds"] == 300
    assert manager.get_task(task.task_id).status is TaskStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_error", "cleanup_error"),
    [
        ("success", TaskStatus.SUCCEEDED, None, None),
        ("failure", TaskStatus.FAILED, "worker failed", None),
        (
            "cleanup-failure",
            TaskStatus.SUCCEEDED,
            None,
            "lease release failed",
        ),
    ],
)
def test_run_task_releases_legacy_pin_once_after_real_terminal_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: TaskStatus,
    expected_error: str | None,
    cleanup_error: str | None,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    resources = LegacyTrackingTaskResources()
    manager = _manager(redis_connection, tmp_path)
    session_id = "a" * 32
    asset_id = "1" * 32
    orphan_asset_id = "2" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=((asset_id, "3" * 32),),
    )
    task_key = f"dt:task:{task.task_id}"
    task_data = json.loads(redis_connection.get(task_key))
    task_data.pop("asset_identities")
    redis_connection.set(task_key, json.dumps(task_data))
    redis_connection.sadd(
        f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}",
        task.task_id,
    )
    _patch_worker_dependencies(
        monkeypatch,
        settings=settings,
        redis_connection=redis_connection,
        resources=resources,
    )
    manager_release_calls: list[str] = []
    release_task_pin = RedisTaskManager.release_task_pin

    def track_manager_release(
        self: RedisTaskManager,
        released_task_id: str,
    ) -> bool:
        manager_release_calls.append(released_task_id)
        return release_task_pin(self, released_task_id)

    monkeypatch.setattr(
        RedisTaskManager,
        "release_task_pin",
        track_manager_release,
    )
    if cleanup_error is not None:
        release_lease = resources.lease.release

        def release_then_fail() -> None:
            release_lease()
            raise RedisConnectionError(cleanup_error)

        monkeypatch.setattr(resources.lease, "release", release_then_fail)

    output_file = tmp_path / "frame.png"

    def export(_options, _backend) -> ExportResult:
        if outcome == "failure":
            raise RuntimeError("worker failed")
        output_file.write_bytes(b"png")
        return ExportResult(
            output_dir=output_file,
            output_pattern=output_file,
            command=("ffmpeg",),
        )

    executor_init = worker_module.RedisTaskExecutor.__init__

    def initialize_executor(executor, *args, **kwargs) -> None:
        executor_init(executor, *args, **kwargs)
        executor._exporter = export

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "__init__",
        initialize_executor,
    )

    def run() -> None:
        worker_module.run_task(
            task.task_id,
            "video_frames",
            VideoFramesTaskRequest(
                input_path=str(tmp_path / "clip.mp4")
            ).model_dump(mode="json"),
        )

    if cleanup_error is None:
        run()
    else:
        with pytest.raises(RedisConnectionError, match=cleanup_error):
            run()

    assert manager_release_calls == [task.task_id]
    assert resources.task_pin_releases == []
    terminal = manager.get_task(task.task_id)
    assert terminal is not None
    assert terminal.status is expected_status
    assert terminal.error == expected_error
    assert not redis_connection.exists(f"dt:resource:task-pin:{task.task_id}")
    assert not redis_connection.exists(
        f"dt:resource:session:{session_id}:task-pins"
    )
    assert not redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{asset_id}"
    )
    assert not redis_connection.exists(
        f"dt:resource:asset-tasks:{session_id}:{orphan_asset_id}"
    )


@pytest.mark.parametrize("retry_succeeds", [True, False])
def test_run_task_retries_legacy_pin_release_when_terminal_release_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_succeeds: bool,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    resources = LegacyTrackingTaskResources()
    manager = _manager(redis_connection, tmp_path)
    session_id = "a" * 32
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id=session_id,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    task_key = f"dt:task:{task.task_id}"
    task_data = json.loads(redis_connection.get(task_key))
    task_data.pop("asset_identities")
    redis_connection.set(task_key, json.dumps(task_data))
    _patch_worker_dependencies(
        monkeypatch,
        settings=settings,
        redis_connection=redis_connection,
        resources=resources,
    )
    release_calls: list[str] = []
    release_task_pin = RedisTaskManager.release_task_pin

    def fail_first_release(
        self: RedisTaskManager,
        released_task_id: str,
    ) -> bool:
        release_calls.append(released_task_id)
        if len(release_calls) == 1 or not retry_succeeds:
            raise RedisConnectionError("task pin release acknowledgement lost")
        return release_task_pin(self, released_task_id)

    monkeypatch.setattr(
        RedisTaskManager,
        "release_task_pin",
        fail_first_release,
    )
    output_file = tmp_path / "frame.png"

    def export(_options, _backend) -> ExportResult:
        output_file.write_bytes(b"png")
        return ExportResult(
            output_dir=output_file,
            output_pattern=output_file,
            command=("ffmpeg",),
        )

    executor_init = worker_module.RedisTaskExecutor.__init__

    def initialize_executor(executor, *args, **kwargs) -> None:
        executor_init(executor, *args, **kwargs)
        executor._exporter = export

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "__init__",
        initialize_executor,
    )

    def run() -> None:
        worker_module.run_task(
            task.task_id,
            "video_frames",
            VideoFramesTaskRequest(
                input_path=str(tmp_path / "clip.mp4")
            ).model_dump(mode="json"),
        )

    if retry_succeeds:
        run()
    else:
        with pytest.raises(
            RedisConnectionError,
            match="task pin release acknowledgement lost",
        ):
            run()

    assert release_calls == [task.task_id, task.task_id]
    completed = manager.get_task(task.task_id)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert bool(
        redis_connection.exists(f"dt:resource:task-pin:{task.task_id}")
    ) is (not retry_succeeds)


@pytest.mark.parametrize("failure_stage", ["lease", "reservation", "heartbeat"])
def test_run_task_releases_legacy_pin_once_after_setup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    resources = LegacyTrackingTaskResources()
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    task_key = f"dt:task:{task.task_id}"
    task_data = json.loads(redis_connection.get(task_key))
    task_data.pop("asset_identities")
    redis_connection.set(task_key, json.dumps(task_data))
    _patch_worker_dependencies(
        monkeypatch,
        settings=settings,
        redis_connection=redis_connection,
        resources=resources,
    )
    release_calls: list[str] = []
    release_task_pin = RedisTaskManager.release_task_pin

    def track_release(
        self: RedisTaskManager,
        released_task_id: str,
    ) -> bool:
        release_calls.append(released_task_id)
        return release_task_pin(self, released_task_id)

    monkeypatch.setattr(RedisTaskManager, "release_task_pin", track_release)

    def fail(message: str):
        def raise_failure(*_args, **_kwargs):
            raise RuntimeError(message)

        return raise_failure

    expected_error = f"{failure_stage} setup failed"
    if failure_stage == "lease":
        monkeypatch.setattr(
            resources,
            "acquire_session",
            fail(expected_error),
        )
    elif failure_stage == "reservation":
        monkeypatch.setattr(
            resources,
            "reserve_output",
            fail(expected_error),
        )
    else:
        monkeypatch.setattr(
            resources.lease,
            "refresh",
            fail(expected_error),
        )
    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "execute_reserved_task",
        lambda *_args, **_kwargs: pytest.fail(
            "executor must not run after setup failure"
        ),
    )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    assert release_calls == [task.task_id]
    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == expected_error
    assert not redis_connection.exists(f"dt:resource:task-pin:{task.task_id}")


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
        lambda _url, **_kwargs: redis_connection,
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
    reservation_entered = Event()
    heartbeat_during_reservation = Event()

    class InterleavedLease(TrackingHandle):
        def refresh(self) -> None:
            super().refresh()
            if reservation_entered.is_set():
                heartbeat_during_reservation.set()

    class BlockingReservationResources(TrackingTaskResources):
        def __init__(self) -> None:
            super().__init__()
            self.lease = InterleavedLease("lease", self.events)
            self.session_lease_heartbeat_interval = 0.005
            self.reservation_heartbeat_interval = 0.005

        def reserve_output(self, *args, **kwargs):
            reservation_entered.set()
            assert heartbeat_during_reservation.wait(timeout=1)
            return super().reserve_output(*args, **kwargs)

    resources = BlockingReservationResources()
    monkeypatch.setattr(worker_module.HostedSettings, "from_env", lambda: settings)
    monkeypatch.setattr(
        worker_module.Redis,
        "from_url",
        lambda _url, **_kwargs: redis_connection,
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
    assert heartbeat_during_reservation.is_set()
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
        lambda _url, **_kwargs: redis_connection,
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
        lambda _url, **_kwargs: redis_connection,
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
        lambda _url, **_kwargs: redis_connection,
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
        lambda _url, **_kwargs: redis_connection,
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
        lambda _url, **_kwargs: redis_connection,
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


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("success", TaskStatus.SUCCEEDED),
        ("failure", TaskStatus.FAILED),
        ("setup-failure", TaskStatus.FAILED),
    ],
)
def test_run_task_terminal_outcome_logically_releases_pin_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: TaskStatus,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    settings = _hosted_settings(tmp_path)
    manager = _manager(redis_connection, tmp_path)
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    resources = ReleaseShapeResourceCoordinatorSpy(redis_connection)
    _patch_worker_dependencies(
        monkeypatch,
        settings=settings,
        redis_connection=redis_connection,
        resources=resources,
    )
    manager_pin_releases: list[str] = []
    release_task_pin = RedisTaskManager.release_task_pin

    def track_manager_pin_release(
        self: RedisTaskManager,
        released_task_id: str,
    ) -> bool:
        manager_pin_releases.append(released_task_id)
        return release_task_pin(self, released_task_id)

    monkeypatch.setattr(
        RedisTaskManager,
        "release_task_pin",
        track_manager_pin_release,
    )
    output_file = tmp_path / "frame.png"

    def export(_options, _backend) -> ExportResult:
        if outcome == "failure":
            raise RuntimeError("worker failed")
        output_file.write_bytes(b"png")
        return ExportResult(
            output_dir=output_file,
            output_pattern=output_file,
            command=("ffmpeg",),
        )

    executor_init = worker_module.RedisTaskExecutor.__init__

    def initialize_executor(executor, *args, **kwargs) -> None:
        executor_init(executor, *args, **kwargs)
        executor._exporter = export

    monkeypatch.setattr(
        worker_module.RedisTaskExecutor,
        "__init__",
        initialize_executor,
    )
    if outcome == "setup-failure":
        monkeypatch.setattr(
            resources,
            "acquire_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("setup failed")
            ),
        )

    worker_module.run_task(
        task.task_id,
        "video_frames",
        VideoFramesTaskRequest(
            input_path=str(tmp_path / "clip.mp4")
        ).model_dump(mode="json"),
    )

    terminal = manager.get_task(task.task_id)
    assert terminal is not None
    assert terminal.status is expected_status
    assert manager_pin_releases + resources.pin_releases == [task.task_id]
    assert resources.runtime_releases == (
        [] if outcome == "success" else [task.task_id]
    )


def test_reconcile_historical_failed_task_logically_releases_pin_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = ReleaseShapeResourceCoordinatorSpy(redis_connection)
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    task.status = TaskStatus.FAILED
    task.error = "historical failure"
    task.finished_at = datetime.now(UTC)
    manager._save_task(task)
    manager_pin_releases: list[str] = []
    release_task_pin = manager.release_task_pin

    def track_manager_pin_release(released_task_id: str) -> bool:
        manager_pin_releases.append(released_task_id)
        return release_task_pin(released_task_id)

    monkeypatch.setattr(manager, "release_task_pin", track_manager_pin_release)

    assert manager.reconcile_tasks(grace_seconds=60) == 1

    assert manager_pin_releases + resources.pin_releases == [task.task_id]
    assert resources.runtime_releases == [task.task_id]
    assert not redis_connection.exists(f"dt:resource:task-pin:{task.task_id}")


@pytest.mark.parametrize("failure_mode", ["transport", "ack-loss"])
def test_failed_cleanup_retries_uncertain_pin_release_without_coordinator_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    resources = ReleaseShapeResourceCoordinatorSpy(redis_connection)
    manager = _manager(
        redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_identities=(("1" * 32, "2" * 32),),
    )
    release_task_pin = manager.release_task_pin
    manager_pin_releases: list[str] = []

    def unreliable_pin_release(released_task_id: str) -> bool:
        manager_pin_releases.append(released_task_id)
        if len(manager_pin_releases) == 1:
            if failure_mode == "ack-loss":
                release_task_pin(released_task_id)
            raise RedisConnectionError(f"pin release {failure_mode}")
        return release_task_pin(released_task_id)

    monkeypatch.setattr(manager, "release_task_pin", unreliable_pin_release)

    with pytest.raises(RedisConnectionError, match=failure_mode):
        manager._finalize_external_failure(task.task_id, "worker failed")

    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert not redis_connection.exists(f"dt:failure-cleanup:{task.task_id}")

    assert manager._finalize_external_failure(task.task_id, "worker failed")

    assert manager_pin_releases == [task.task_id, task.task_id]
    assert resources.pin_releases == []
    assert resources.runtime_releases == [task.task_id]
    assert not redis_connection.exists(f"dt:resource:task-pin:{task.task_id}")


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


def test_redis_subscriber_polls_only_the_event_at_its_cursor() -> None:
    event = redis_tasks_module.LogEventResponse(
        task_id="task-1",
        timestamp=datetime.now(UTC),
        level=LogLevel.PROGRESS,
        phase=LogPhase.RUNNING,
        message="new event",
        details={},
    )

    class CursorOnlyStore:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None, int]] = []
            self.responses = [
                ("generation-a", None, 10_000),
                ("generation-a", event, 10_001),
            ]

        def _event_at_cursor(
            self,
            task_id: str,
            generation: str | None,
            next_sequence: int,
        ):
            self.calls.append((task_id, generation, next_sequence))
            return self.responses.pop(0)

        def _snapshot(self, task_id: str):
            raise AssertionError(f"subscriber took a full snapshot for {task_id}")

    store = CursorOnlyStore()
    subscriber = redis_tasks_module.RedisEventSubscriber(
        store,
        "task-1",
        "generation-a",
        10_000,
    )

    assert subscriber.get(timeout=0.2) == event
    assert store.calls == [
        ("task-1", "generation-a", 10_000),
        ("task-1", "generation-a", 10_000),
    ]


def test_redis_subscriber_fallback_reads_constant_data_after_large_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    task_id = "large-event-log"
    event = redis_tasks_module.LogEventResponse(
        task_id=task_id,
        timestamp=datetime.now(UTC),
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="historical",
        details={},
    )
    event_key = f"dt:task:{task_id}:events"
    event_index_key = f"dt:task:{task_id}:event-index"
    generation_key = f"dt:event-generation:{task_id}"
    with redis_connection.pipeline() as pipeline:
        for _ in range(10_000):
            pipeline.rpush(event_key, event.model_dump_json())
        pipeline.set(generation_key, "generation-a")
        pipeline.expire(event_key, 60)
        pipeline.expire(generation_key, 60)
        pipeline.execute()

    history, subscriber = store.history_and_subscribe(task_id)
    assert len(history) == 10_000

    def unknown_command(**_kwargs):
        raise ResponseError("unknown command 'evalsha'")

    store._read_event_at_cursor_script = unknown_command
    pipeline_type = type(redis_connection.pipeline())
    original_hget = pipeline_type.hget
    reads: list[tuple[str, str]] = []

    def tracked_hget(self, name: str, key: str):
        reads.append((name, key))
        return original_hget(self, name, key)

    def forbidden_list_read(*_args, **_kwargs):
        raise AssertionError("steady-state polling read the event list")

    monkeypatch.setattr(pipeline_type, "hget", tracked_hget)
    monkeypatch.setattr(pipeline_type, "lindex", forbidden_list_read)
    monkeypatch.setattr(pipeline_type, "lrange", forbidden_list_read)
    store.append(
        task_id=task_id,
        level=LogLevel.SUCCESS,
        phase=LogPhase.COMPLETED,
        message="completed",
    )

    assert subscriber.get(timeout=0.2).message == "completed"
    with pytest.raises(Empty):
        subscriber.get(timeout=0.05)
    assert (event_index_key, "10001") in reads
    assert all(name != event_key for name, _field in reads)


def test_redis_unknown_command_fallback_builds_and_reads_event_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    task_id = "fallback-events"

    def unknown_command(**_kwargs):
        raise ResponseError("unknown command 'evalsha'")

    monkeypatch.setattr(store, "_append_event_script", unknown_command)
    monkeypatch.setattr(store, "_read_event_log_script", unknown_command)
    monkeypatch.setattr(store, "_read_event_at_cursor_script", unknown_command)
    first = store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="first",
    )
    history, subscriber = store.history_and_subscribe(task_id)
    second = store.append(
        task_id=task_id,
        level=LogLevel.SUCCESS,
        phase=LogPhase.COMPLETED,
        message="second",
    )

    assert history == [first]
    assert subscriber.get(timeout=0.2) == second
    event_index_key = f"dt:task:{task_id}:event-index"
    assert redis_connection.hget(event_index_key, "next_sequence") == b"2"
    assert redis_connection.hget(event_index_key, "1") == (
        first.model_dump_json().encode()
    )
    assert redis_connection.hget(event_index_key, "2") == (
        second.model_dump_json().encode()
    )


def test_redis_fallback_resets_inconsistent_event_index_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    task_id = "inconsistent-fallback-index"
    first = store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="first",
    )
    event_index_key = f"dt:task:{task_id}:event-index"
    redis_connection.hset(event_index_key, "next_sequence", 99)

    def unknown_command(**_kwargs):
        raise ResponseError("unknown command 'evalsha'")

    monkeypatch.setattr(store, "_append_event_script", unknown_command)
    second = store.append(
        task_id=task_id,
        level=LogLevel.SUCCESS,
        phase=LogPhase.COMPLETED,
        message="second",
    )

    assert redis_connection.hget(event_index_key, "next_sequence") == b"2"
    assert store.history(task_id) == [first, second]


def test_redis_subscriber_rejects_missing_generation() -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    task_id = "legacy-live-events"
    store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="queued",
    )
    history, subscriber = store.history_and_subscribe(task_id)
    generation_key = f"dt:event-generation:{task_id}"
    redis_connection.delete(generation_key)

    assert [event.message for event in history] == ["queued"]
    with pytest.raises(RedisError, match="cursor state") as error:
        subscriber.get(timeout=0.2)
    assert type(error.value).__name__ == "EventCursorUnavailable"
    assert redis_errors_module.is_redis_unavailable(error.value)
    assert not redis_connection.exists(generation_key)


def test_redis_subscriber_rejects_event_log_recreation() -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    task_id = "recreated-events"
    store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="old",
    )
    _history, subscriber = store.history_and_subscribe(task_id)
    redis_connection.delete(
        f"dt:task:{task_id}:events",
        f"dt:event-generation:{task_id}",
    )
    store.append(
        task_id=task_id,
        level=LogLevel.ERROR,
        phase=LogPhase.FAILED,
        message="new terminal",
    )

    with pytest.raises(RedisError, match="cursor state") as error:
        subscriber.get(timeout=0.2)
    assert type(error.value).__name__ == "EventCursorUnavailable"


def test_redis_event_cursor_uses_lua_result_and_propagates_redis_errors() -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    event = redis_tasks_module.LogEventResponse(
        task_id="task-1",
        timestamp=datetime.now(UTC),
        level=LogLevel.SUCCESS,
        phase=LogPhase.COMPLETED,
        message="completed",
        details={},
    )
    calls: list[tuple[list[str], list[object]]] = []

    def read_script(*, keys: list[str], args: list[object]):
        calls.append((keys, args))
        return ["ok", "generation-b", event.model_dump_json(), "1"]

    store._read_event_at_cursor_script = read_script

    assert store._event_at_cursor("task-1", "generation-a", 10_000) == (
        "generation-b",
        event,
        1,
    )
    assert calls[0][0] == [
        "dt:task:task-1:events",
        "dt:event-generation:task-1",
        "dt:task:task-1:event-index",
    ]
    assert calls[0][1][:2] == ["generation-a", 10_000]

    def unavailable(**_kwargs):
        raise RedisConnectionError("event storage unavailable")

    store._read_event_at_cursor_script = unavailable
    with pytest.raises(RedisConnectionError, match="event storage unavailable"):
        store._event_at_cursor("task-1", "generation-b", 1)


@pytest.mark.parametrize("use_lua", [True, False], ids=["lua", "fallback"])
@pytest.mark.parametrize(
    ("payload", "cause_type"),
    [
        (b'{"task_id":', ValidationError),
        (b'{"task_id":"malformed-events"}', ValidationError),
        (b"\xff", UnicodeDecodeError),
    ],
    ids=["malformed-json", "wrong-schema", "invalid-utf8"],
)
def test_redis_event_cursor_reports_corrupt_indexed_event_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
    payload: bytes,
    cause_type: type[Exception],
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    task_id = "malformed-events"
    generation_key = f"dt:event-generation:{task_id}"
    event_index_key = f"dt:task:{task_id}:event-index"
    redis_connection.set(generation_key, "generation-a")
    redis_connection.hset(
        event_index_key,
        mapping={"next_sequence": 1, "1": payload},
    )
    if use_lua:
        monkeypatch.setattr(
            store,
            "_read_event_at_cursor_script",
            lambda **_kwargs: ["ok", "generation-a", payload, "2"],
        )
    else:

        def unknown_command(**_kwargs):
            raise ResponseError("unknown command 'evalsha'")

        monkeypatch.setattr(
            store,
            "_read_event_at_cursor_script",
            unknown_command,
        )
    pipeline_type = type(redis_connection.pipeline())

    def forbidden_full_scan(*_args, **_kwargs):
        raise AssertionError("cursor read performed a full event-log scan")

    monkeypatch.setattr(pipeline_type, "lrange", forbidden_full_scan)
    subscriber = redis_tasks_module.RedisEventSubscriber(
        store,
        task_id,
        "generation-a",
        1,
    )

    with pytest.raises(
        redis_errors_module.EventCursorUnavailable,
        match="cursor event",
    ) as error:
        subscriber.get(timeout=0.2)
    assert isinstance(error.value.__cause__, cause_type)


def test_redis_history_keeps_existing_malformed_event_error() -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    store = RedisLogStore(redis_connection, retention_seconds=60)
    task_id = "malformed-history"
    event_key = f"dt:task:{task_id}:events"
    generation_key = f"dt:event-generation:{task_id}"
    redis_connection.rpush(event_key, '{"not":"a log event"}')
    redis_connection.set(generation_key, "generation-a")

    with pytest.raises(ValidationError):
        store.history(task_id)


def test_real_redis_subscriber_rejects_event_log_generation_after_expiry(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = RedisTaskManager(
        redis_connection=real_redis_connection,
        queue=Queue("designtoolkit", connection=real_redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=1,
        task_timeout_seconds=60,
    )
    task = TaskRecord(
        task_id="expiring-event-log",
        kind=TaskKind.VIDEO_FRAMES,
        status=TaskStatus.RUNNING,
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        finished_at=None,
        input_path=str(tmp_path / "clip.mp4"),
        output_path=None,
        error=None,
        owner_id=None,
    )
    manager._save_task(task)
    manager.log_store.append(
        task_id=task.task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="created",
    )
    history, subscriber = manager.log_store.history_and_subscribe(task.task_id)
    assert len(history) == 1

    event_key = f"dt:task:{task.task_id}:events"
    event_index_key = f"dt:task:{task.task_id}:event-index"
    generation_key = f"dt:event-generation:{task.task_id}"
    real_redis_connection.pexpire(event_key, 1)
    real_redis_connection.pexpire(event_index_key, 1)
    real_redis_connection.pexpire(generation_key, 1)
    deadline = time.monotonic() + 1
    while real_redis_connection.exists(event_key) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not real_redis_connection.exists(event_key)

    terminal_event = redis_tasks_module.LogEventResponse(
        task_id=task.task_id,
        timestamp=datetime.now(UTC),
        level=LogLevel.ERROR,
        phase=LogPhase.FAILED,
        message="worker timed out",
        details={},
    )
    manager._transition_finished(
        task.task_id,
        status=TaskStatus.FAILED,
        error="worker timed out",
        terminal_event=terminal_event,
    )

    with pytest.raises(RedisError, match="cursor state") as error:
        subscriber.get(timeout=0.5)
    assert type(error.value).__name__ == "EventCursorUnavailable"
    assert manager.get_task(task.task_id).status is TaskStatus.FAILED


def test_real_redis_subscriber_rejects_missing_generation(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "legacy-live-events"
    store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="queued",
    )
    history, subscriber = store.history_and_subscribe(task_id)
    generation_key = f"dt:event-generation:{task_id}"
    real_redis_connection.delete(generation_key)

    assert [event.message for event in history] == ["queued"]
    with pytest.raises(RedisError, match="cursor state") as error:
        subscriber.get(timeout=0.2)
    assert type(error.value).__name__ == "EventCursorUnavailable"
    assert not real_redis_connection.exists(generation_key)


def test_real_redis_event_cursor_rejects_new_generation(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "recreated-events"
    store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="old",
    )
    history, subscriber = store.history_and_subscribe(task_id)
    assert [event.message for event in history] == ["old"]

    real_redis_connection.delete(
        f"dt:task:{task_id}:events",
        f"dt:task:{task_id}:event-index",
        f"dt:event-generation:{task_id}",
    )
    store.append(
        task_id=task_id,
        level=LogLevel.ERROR,
        phase=LogPhase.FAILED,
        message="new terminal",
    )

    with pytest.raises(RedisError, match="cursor state") as error:
        subscriber.get(timeout=0.2)
    assert type(error.value).__name__ == "EventCursorUnavailable"


def test_real_redis_subscriber_does_not_repeat_events_without_generation_change(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "stable-events"
    store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="queued",
    )
    history, subscriber = store.history_and_subscribe(task_id)
    store.append(
        task_id=task_id,
        level=LogLevel.PROGRESS,
        phase=LogPhase.RUNNING,
        message="first",
    )
    store.append(
        task_id=task_id,
        level=LogLevel.PROGRESS,
        phase=LogPhase.RUNNING,
        message="second",
    )

    assert [event.message for event in history] == ["queued"]
    assert subscriber.get(timeout=0.2).message == "first"
    assert subscriber.get(timeout=0.2).message == "second"
    with pytest.raises(Empty):
        subscriber.get(timeout=0.05)


def test_real_redis_event_generation_ttl_matches_event_list(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "event-ttl"

    store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="queued",
    )

    event_ttl, generation_ttl, event_index_ttl = real_redis_connection.eval(
        """
        return {
            redis.call("PTTL", KEYS[1]),
            redis.call("PTTL", KEYS[2]),
            redis.call("PTTL", KEYS[3])
        }
        """,
        3,
        f"dt:task:{task_id}:events",
        f"dt:event-generation:{task_id}",
        f"dt:task:{task_id}:event-index",
    )
    assert event_ttl == generation_ttl == event_index_ttl
    assert generation_ttl > 0


def test_real_redis_history_backfills_legacy_event_index(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "legacy-events"
    events = [
        redis_tasks_module.LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=LogLevel.INFO,
            phase=LogPhase.CREATED,
            message=f"legacy event {sequence}",
            details={},
        )
        for sequence in range(2)
    ]
    event_key = f"dt:task:{task_id}:events"
    event_index_key = f"dt:task:{task_id}:event-index"
    generation_key = f"dt:event-generation:{task_id}"
    real_redis_connection.rpush(
        event_key,
        *(event.model_dump_json() for event in events),
    )
    real_redis_connection.set(generation_key, "legacy-generation")
    real_redis_connection.expire(event_key, 60)
    real_redis_connection.expire(generation_key, 60)
    commandstats_before = real_redis_connection.info("commandstats")

    history, subscriber = store.history_and_subscribe(task_id)
    assert history == events
    commandstats_after_snapshot = real_redis_connection.info("commandstats")
    lrange_before = commandstats_before.get("cmdstat_lrange", {})
    lrange_after_snapshot = commandstats_after_snapshot.get(
        "cmdstat_lrange",
        {},
    )
    assert isinstance(lrange_before, dict)
    assert isinstance(lrange_after_snapshot, dict)
    assert int(lrange_after_snapshot.get("calls", 0)) == (
        int(lrange_before.get("calls", 0)) + 1
    )

    assert real_redis_connection.hgetall(event_index_key) == {
        b"next_sequence": b"2",
        b"1": events[0].model_dump_json().encode(),
        b"2": events[1].model_dump_json().encode(),
    }
    event_ttl = real_redis_connection.pttl(event_key)
    event_index_ttl = real_redis_connection.pttl(event_index_key)
    generation_ttl = real_redis_connection.pttl(generation_key)
    assert abs(event_ttl - event_index_ttl) < 100
    assert abs(event_ttl - generation_ttl) < 100
    commandstats_before_poll = real_redis_connection.info("commandstats")

    with pytest.raises(Empty):
        subscriber.get(timeout=0.05)

    commandstats_after = real_redis_connection.info("commandstats")
    for command in ("lindex", "lrange"):
        before = commandstats_before_poll.get(f"cmdstat_{command}", {})
        after = commandstats_after.get(f"cmdstat_{command}", {})
        assert isinstance(before, dict)
        assert isinstance(after, dict)
        assert int(after.get("calls", 0)) == int(before.get("calls", 0))


@pytest.mark.parametrize("use_lua", [True, False])
def test_real_redis_poll_rejects_missing_index_without_list_reads(
    real_redis_connection: Redis,
    monkeypatch: pytest.MonkeyPatch,
    use_lua: bool,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "legacy-poll-events"
    event = redis_tasks_module.LogEventResponse(
        task_id=task_id,
        timestamp=datetime.now(UTC),
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="legacy event",
        details={},
    )
    event_key = f"dt:task:{task_id}:events"
    event_index_key = f"dt:task:{task_id}:event-index"
    generation_key = f"dt:event-generation:{task_id}"
    real_redis_connection.rpush(event_key, event.model_dump_json())
    real_redis_connection.set(generation_key, "legacy-generation")
    real_redis_connection.expire(event_key, 60)
    real_redis_connection.expire(generation_key, 60)
    subscriber = redis_tasks_module.RedisEventSubscriber(
        store,
        task_id,
        "legacy-generation",
        1,
    )
    if not use_lua:
        def unknown_command(**_kwargs):
            raise ResponseError("unknown command 'evalsha'")

        monkeypatch.setattr(
            store,
            "_read_event_at_cursor_script",
            unknown_command,
        )
    commandstats_before = real_redis_connection.info("commandstats")

    with pytest.raises(RedisError, match="cursor state") as error:
        subscriber.get(timeout=0.2)

    commandstats_after = real_redis_connection.info("commandstats")
    assert type(error.value).__name__ == "EventCursorUnavailable"
    assert not real_redis_connection.exists(event_index_key)
    for command, expected_calls in (("get", 1), ("hget", 1)):
        before = commandstats_before.get(f"cmdstat_{command}", {})
        after = commandstats_after.get(f"cmdstat_{command}", {})
        assert isinstance(before, dict)
        assert isinstance(after, dict)
        assert int(after.get("calls", 0)) == (
            int(before.get("calls", 0)) + expected_calls
        )
    for command in ("lindex", "lrange"):
        before = commandstats_before.get(f"cmdstat_{command}", {})
        after = commandstats_after.get(f"cmdstat_{command}", {})
        assert isinstance(before, dict)
        assert isinstance(after, dict)
        assert int(after.get("calls", 0)) == int(before.get("calls", 0))


def test_real_redis_history_repairs_incorrect_index_payload(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "incorrect-history-index"
    expected = store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="expected",
    )
    event_index_key = f"dt:task:{task_id}:event-index"
    real_redis_connection.hset(event_index_key, "1", '{"message":"wrong"}')

    assert store.history(task_id) == [expected]
    assert real_redis_connection.hget(event_index_key, "1") == (
        expected.model_dump_json().encode()
    )


def test_real_redis_poll_rejects_missing_expected_sequence(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "missing-poll-sequence"
    store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="expected",
    )
    generation = real_redis_connection.get(
        f"dt:event-generation:{task_id}"
    ).decode()
    event_index_key = f"dt:task:{task_id}:event-index"
    real_redis_connection.hdel(event_index_key, "1")
    real_redis_connection.hset(event_index_key, "unexpected", "value")
    subscriber = redis_tasks_module.RedisEventSubscriber(
        store,
        task_id,
        generation,
        1,
    )

    with pytest.raises(RedisError, match="cursor state") as error:
        subscriber.get(timeout=0.2)
    assert type(error.value).__name__ == "EventCursorUnavailable"
    assert real_redis_connection.hgetall(event_index_key) == {
        b"next_sequence": b"1",
        b"unexpected": b"value",
    }


def test_real_redis_append_resets_stale_index_for_new_generation(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "expired-events"
    old = store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="old",
    )
    event_key = f"dt:task:{task_id}:events"
    event_index_key = f"dt:task:{task_id}:event-index"
    generation_key = f"dt:event-generation:{task_id}"
    old_generation = real_redis_connection.get(generation_key)
    assert real_redis_connection.hget(event_index_key, "1") == (
        old.model_dump_json().encode()
    )
    real_redis_connection.delete(event_key, generation_key)

    new = store.append(
        task_id=task_id,
        level=LogLevel.ERROR,
        phase=LogPhase.FAILED,
        message="new",
    )

    assert real_redis_connection.get(generation_key) != old_generation
    assert real_redis_connection.hgetall(event_index_key) == {
        b"next_sequence": b"1",
        b"1": new.model_dump_json().encode(),
    }
    assert store.history(task_id) == [new]


def test_real_redis_append_resets_inconsistent_event_index_counter(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "inconsistent-lua-index"
    first = store.append(
        task_id=task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="first",
    )
    event_index_key = f"dt:task:{task_id}:event-index"
    real_redis_connection.hset(event_index_key, "next_sequence", 99)

    second = store.append(
        task_id=task_id,
        level=LogLevel.SUCCESS,
        phase=LogPhase.COMPLETED,
        message="second",
    )

    assert real_redis_connection.hget(event_index_key, "next_sequence") == b"2"
    assert store.history(task_id) == [first, second]


def test_real_redis_commandstats_spy_proves_large_cursor_read_is_constant(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "slow-cursor"
    event = redis_tasks_module.LogEventResponse(
        task_id=task_id,
        timestamp=datetime.now(UTC),
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="historical",
        details={},
    )
    event_key = f"dt:task:{task_id}:events"
    generation_key = f"dt:event-generation:{task_id}"
    with real_redis_connection.pipeline() as pipeline:
        for _ in range(10_000):
            pipeline.rpush(event_key, event.model_dump_json())
        pipeline.set(generation_key, "generation-a")
        pipeline.expire(event_key, 60)
        pipeline.expire(generation_key, 60)
        pipeline.execute()
    history, subscriber = store.history_and_subscribe(task_id)
    appended = store.append(
        task_id=task_id,
        level=LogLevel.SUCCESS,
        phase=LogPhase.COMPLETED,
        message="completed",
    )
    commandstats_before = real_redis_connection.info("commandstats")

    assert len(history) == 10_000
    assert subscriber.get(timeout=0.2) == appended
    commandstats_after = real_redis_connection.info("commandstats")

    def calls(stats: dict[str, object], command: str) -> int:
        command_stats = stats.get(f"cmdstat_{command}", {})
        assert isinstance(command_stats, dict)
        return int(command_stats.get("calls", 0))

    assert calls(commandstats_after, "hget") - calls(
        commandstats_before,
        "hget",
    ) == 2
    assert calls(commandstats_after, "get") - calls(
        commandstats_before,
        "get",
    ) == 1
    for command in ("hlen", "lindex", "llen", "lrange"):
        assert calls(commandstats_after, command) == calls(
            commandstats_before,
            command,
        )


def test_real_redis_concurrent_event_read_has_no_gaps_or_duplicates(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "concurrent-event-reader"
    _history, subscriber = store.history_and_subscribe(task_id)
    expected = [f"event-{sequence}" for sequence in range(100)]

    def append_all() -> None:
        for message in expected:
            store.append(
                task_id=task_id,
                level=LogLevel.PROGRESS,
                phase=LogPhase.RUNNING,
                message=message,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(append_all)
        actual = [subscriber.get(timeout=2).message for _ in expected]
        writer.result(timeout=5)

    assert actual == expected


def test_real_redis_concurrent_event_appends_preserve_each_writer_order(
    real_redis_connection: Redis,
) -> None:
    store = RedisLogStore(real_redis_connection, retention_seconds=60)
    task_id = "concurrent-events"
    barrier = Barrier(8)

    def append_pair(worker: int) -> None:
        barrier.wait()
        for sequence in range(2):
            store.append(
                task_id=task_id,
                level=LogLevel.PROGRESS,
                phase=LogPhase.RUNNING,
                message=f"{worker}:{sequence}",
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(append_pair, worker) for worker in range(8)]
        for future in futures:
            future.result(timeout=5)

    messages = [event.message for event in store.history(task_id)]
    assert len(messages) == 16
    for worker in range(8):
        assert messages.index(f"{worker}:0") < messages.index(f"{worker}:1")
    assert real_redis_connection.exists(f"dt:event-generation:{task_id}")


def test_real_redis_all_transition_producers_update_event_index(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path, retention_seconds=60)
    task = TaskRecord(
        task_id="transition-producers",
        kind=TaskKind.VIDEO_FRAMES,
        status=TaskStatus.QUEUED,
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
        input_path=str(tmp_path / "clip.mp4"),
        output_path=None,
        error=None,
        owner_id=None,
    )
    manager._save_task(task)
    manager.log_store.append(
        task_id=task.task_id,
        level=LogLevel.INFO,
        phase=LogPhase.CREATED,
        message="created",
    )
    manager.mark_running(task.task_id)
    terminal_event = redis_tasks_module.LogEventResponse(
        task_id=task.task_id,
        timestamp=datetime.now(UTC),
        level=LogLevel.ERROR,
        phase=LogPhase.FAILED,
        message="worker failed",
        details={},
    )
    manager._transition_finished(
        task.task_id,
        status=TaskStatus.FAILED,
        error="worker failed",
        terminal_event=terminal_event,
    )

    event_key = f"dt:task:{task.task_id}:events"
    event_index_key = f"dt:task:{task.task_id}:event-index"
    events = real_redis_connection.lrange(event_key, 0, -1)
    assert real_redis_connection.hget(event_index_key, "next_sequence") == b"3"
    assert real_redis_connection.hlen(event_index_key) == len(events) + 1
    assert [
        real_redis_connection.hget(event_index_key, str(sequence))
        for sequence in range(1, 4)
    ] == events


def test_terminal_transition_without_events_leaves_no_event_metadata(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    task = TaskRecord(
        task_id="task-without-events",
        kind=TaskKind.VIDEO_FRAMES,
        status=TaskStatus.RUNNING,
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        finished_at=None,
        input_path="clip.mp4",
        output_path=None,
        error=None,
        owner_id=None,
    )
    manager._save_task(task)

    manager.mark_finished(task.task_id, status=TaskStatus.SUCCEEDED)

    assert not redis_connection.exists(f"dt:task:{task.task_id}:events")
    assert not redis_connection.exists(
        f"dt:task:{task.task_id}:event-index"
    )
    assert not redis_connection.exists(f"dt:event-generation:{task.task_id}")


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
        def release_task_runtime_resources(self, _task_id: str) -> None:
            events.append("release")

        def release_task_resources(
            self,
            _task_id: str,
            *,
            expected_session_id: str | None = None,
            expected_asset_identities: (
                tuple[tuple[str, str | None], ...] | None
            ) = None,
        ) -> None:
            assert expected_session_id is not None
            assert expected_asset_identities is not None
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


def test_real_redis_reconcile_uses_configured_retention(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    retention_seconds = 10_800
    manager = _manager(
        real_redis_connection,
        tmp_path,
        retention_seconds=retention_seconds,
    )
    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )
    task.created_at = datetime.now(UTC) - timedelta(minutes=5)
    manager._save_task(task)
    job = manager.queue.fetch_job(task.task_id)
    assert job is not None
    real_redis_connection.delete(job.key)

    assert manager.reconcile_tasks(grace_seconds=60) == 1

    for key in (
        f"dt:task:{task.task_id}",
        f"dt:task-work:{task.task_id}",
        f"dt:task:{task.task_id}:events",
        f"dt:task:{task.task_id}:event-index",
        f"dt:event-generation:{task.task_id}",
        f"dt:failure-cleanup:{task.task_id}",
    ):
        _assert_ttl_matches(real_redis_connection, key, retention_seconds)


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


def test_real_redis_recovers_committed_enqueue_after_acknowledgement_loss(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        resource_coordinator=resources,
    )
    original_commit = manager._commit_enqueue_job
    original_confirmation = manager._enqueue_has_authoritative_evidence
    confirmation_calls = 0

    def enqueue_then_lose_ack(*args, **kwargs):
        original_commit(*args, **kwargs)
        raise RedisConnectionError("enqueue acknowledgement lost")

    def confirmation_disconnect_once(task_id: str) -> bool:
        nonlocal confirmation_calls
        confirmation_calls += 1
        if confirmation_calls == 1:
            raise RedisConnectionError("enqueue confirmation unavailable")
        return original_confirmation(task_id)

    monkeypatch.setattr(manager, "_commit_enqueue_job", enqueue_then_lose_ack)
    monkeypatch.setattr(
        manager,
        "_enqueue_has_authoritative_evidence",
        confirmation_disconnect_once,
    )

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
    )

    assert not real_redis_connection.exists(
        f"dt:enqueue-intent:{task.task_id}"
    )
    assert not real_redis_connection.exists(
        f"dt:enqueue-owner:{task.task_id}"
    )
    assert manager.reconcile_tasks(grace_seconds=60) == 0
    assert manager.get_task(task.task_id) == task
    assert manager.task_work(task.task_id) is not None
    assert manager.queue.get_job_ids() == [task.task_id]
    assert Job.exists(task.task_id, real_redis_connection)
    assert real_redis_connection.exists(
        f"dt:enqueue-intent:{task.task_id}"
    ) == 0
    assert real_redis_connection.lrange("dt:queue:reservations", 0, -1) == [
        task.task_id.encode()
    ]
    assert resources.claim_session_cleanup("a" * 32) is None


def test_real_redis_reconcile_fails_uncommitted_ambiguous_enqueue(
    real_redis_connection: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retention_seconds = 300
    resources = _resources(real_redis_connection)
    manager = _manager(
        real_redis_connection,
        tmp_path,
        retention_seconds=retention_seconds,
        resource_coordinator=resources,
    )
    original_confirmation = manager._enqueue_has_authoritative_evidence
    confirmation_calls = 0
    asset_id = "1" * 32

    def enqueue_without_commit(*_args, **_kwargs):
        raise RedisConnectionError("enqueue disconnected before commit")

    def confirmation_disconnect_once(task_id: str) -> bool:
        nonlocal confirmation_calls
        confirmation_calls += 1
        if confirmation_calls == 1:
            raise RedisConnectionError("enqueue confirmation unavailable")
        return original_confirmation(task_id)

    monkeypatch.setattr(manager, "_commit_enqueue_job", enqueue_without_commit)
    monkeypatch.setattr(
        manager,
        "_enqueue_has_authoritative_evidence",
        confirmation_disconnect_once,
    )

    task = manager.create_video_frames_task(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
        owner_id="a" * 32,
        asset_ids=(asset_id,),
    )

    assert task.status is TaskStatus.QUEUED
    with pytest.raises(TaskConflictError, match="referenced by an active task"):
        manager.claim_asset_deletion("a" * 32, asset_id)
    intent = real_redis_connection.hgetall(
        f"dt:enqueue-intent:{task.task_id}"
    )
    assert intent == {
        b"task_id": task.task_id.encode(),
        b"job_id": task.task_id.encode(),
        b"slot_reserved": b"1",
        b"state": b"uncertain",
        b"owner_id": b"a" * 32,
        b"created_at": intent[b"created_at"],
        b"owner_token": intent[b"owner_token"],
    }
    assert float(intent[b"created_at"]) > 0
    assert intent[b"owner_token"]
    assert not real_redis_connection.exists(
        f"dt:enqueue-owner:{task.task_id}"
    )
    assert manager.reconcile_tasks(grace_seconds=60) == 1
    failed = manager.get_task(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert failed.error == "Task enqueue could not be confirmed"
    assert manager.queue.get_job_ids() == []
    assert real_redis_connection.llen("dt:queue:reservations") == 0
    assert real_redis_connection.exists(
        f"dt:enqueue-intent:{task.task_id}"
    ) == 0
    for key in (
        f"dt:task:{task.task_id}",
        f"dt:task-work:{task.task_id}",
        f"dt:task:{task.task_id}:events",
        f"dt:task:{task.task_id}:event-index",
        f"dt:event-generation:{task.task_id}",
        f"dt:failure-cleanup:{task.task_id}",
    ):
        _assert_ttl_matches(real_redis_connection, key, retention_seconds)
    event_key = f"dt:task:{task.task_id}:events"
    event_index_key = f"dt:task:{task.task_id}:event-index"
    assert real_redis_connection.hget(
        event_index_key,
        "next_sequence",
    ) == str(real_redis_connection.llen(event_key)).encode()
    assert real_redis_connection.hlen(event_index_key) == (
        real_redis_connection.llen(event_key) + 1
    )
    claim = resources.claim_session_cleanup("a" * 32)
    assert claim is not None
    resources.release_cleanup_claim("a" * 32, claim)
    asset_claim = manager.claim_asset_deletion("a" * 32, asset_id)
    manager.release_asset_deletion("a" * 32, asset_id, asset_claim)
    assert list(
        real_redis_connection.scan_iter(match="dt:resource:asset-tasks:*")
    ) == []


def test_cleanup_once_reclaims_orphaned_real_redis_queue_slot(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    redis_url = os.environ.get(
        "DESIGNTOOLKIT_TEST_REDIS_URL",
        "redis://localhost:6379/15",
    )
    real_redis_connection.rpush("dt:queue:reservations", "orphaned-task")

    cleanup_module.cleanup_once(
        HostedSettings(
            enabled=True,
            data_dir=tmp_path,
            redis_url=redis_url,
            max_upload_bytes=1024,
            retention_seconds=60,
        )
    )

    assert real_redis_connection.llen("dt:queue:reservations") == 0


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


def test_rate_limit_is_scoped_per_bucket(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)

    assert manager.allow_request("client:upload", limit=2, window=60)
    assert manager.allow_request("client:upload", limit=2, window=60)
    assert not manager.allow_request("client:upload", limit=2, window=60)
    assert manager.allow_request("other:upload", limit=2, window=60)


def test_rate_limit_counter_gets_ttl_atomically_with_increment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = _manager(redis_connection, tmp_path)
    monkeypatch.setattr(
        redis_connection,
        "expire",
        lambda *_args, **_kwargs: pytest.fail(
            "rate limiting must not issue a separate EXPIRE command"
        ),
    )

    assert manager.allow_request("client:api", limit=2, window=60)

    keys = list(redis_connection.scan_iter(match="dt:rate:client:api:*"))
    assert len(keys) == 1
    assert 0 < redis_connection.ttl(keys[0]) <= 61


def test_real_redis_rate_limit_has_ttl_and_exact_concurrent_limit(
    real_redis_connection: Redis,
    tmp_path: Path,
) -> None:
    manager = _manager(real_redis_connection, tmp_path)
    workers = 40
    limit = 17
    barrier = Barrier(workers)

    def allow_request() -> bool:
        barrier.wait()
        return manager.allow_request("concurrent:api", limit=limit, window=60)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        allowed = list(executor.map(lambda _index: allow_request(), range(workers)))

    keys = list(real_redis_connection.scan_iter(match="dt:rate:concurrent:api:*"))
    assert sum(allowed) == limit
    assert len(keys) == 1
    assert int(real_redis_connection.get(keys[0])) == workers
    assert 0 < real_redis_connection.ttl(keys[0]) <= 61


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


def test_batch_video_frames_disambiguates_duplicate_input_names(
    tmp_path: Path,
) -> None:
    asset_ids = ("1" * 32, "2" * 32)
    input_paths = [
        str(tmp_path / "assets" / asset_id / "clip.mp4")
        for asset_id in asset_ids
    ]
    request = VideoFramesTaskRequest(
        input_paths=input_paths,
        output_dir=str(tmp_path / "output"),
    )

    first = video_frame_options_list_from_request(request)
    second = video_frame_options_list_from_request(request)
    single = video_frame_options_list_from_request(
        VideoFramesTaskRequest(
            input_path=input_paths[0],
            output_dir=str(tmp_path / "single-output"),
        )
    )

    expected = [
        tmp_path / "output" / "001_clip_frames",
        tmp_path / "output" / "002_clip_frames",
    ]
    assert [option.output_dir for option in first] == expected
    assert [option.output_dir for option in second] == expected
    assert single[0].output_dir == tmp_path / "single-output"
    assert all(
        asset_id not in output_dir.name
        for output_dir in expected
        for asset_id in asset_ids
    )


def test_batch_video_frames_without_output_dir_disambiguates_matching_stems(
    tmp_path: Path,
) -> None:
    options = video_frame_options_list_from_request(
        VideoFramesTaskRequest(
            input_paths=[
                str(tmp_path / "clip.mp4"),
                str(tmp_path / "clip.mov"),
            ]
        )
    )

    assert [option.output_dir for option in options] == [
        tmp_path / "001_clip_frames",
        tmp_path / "002_clip_frames",
    ]


def test_batch_video_frames_without_output_dir_disambiguates_repeated_path(
    tmp_path: Path,
) -> None:
    input_path = str(tmp_path / "clip.mp4")

    options = video_frame_options_list_from_request(
        VideoFramesTaskRequest(input_paths=[input_path, input_path])
    )

    assert [option.output_dir for option in options] == [
        tmp_path / "001_clip_frames",
        tmp_path / "002_clip_frames",
    ]


def test_single_video_frames_without_output_dir_keeps_default_naming(
    tmp_path: Path,
) -> None:
    options = video_frame_options_list_from_request(
        VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4"))
    )

    assert options[0].output_dir is None


def test_worker_packages_duplicate_video_names_without_overwriting(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_paths = [
        str(tmp_path / "assets" / ("1" * 32) / "clip.mp4"),
        str(tmp_path / "assets" / ("2" * 32) / "clip.mp4"),
    ]
    output_root = tmp_path / "task-output"
    output_root.mkdir()
    request = VideoFramesTaskRequest(
        input_paths=input_paths,
        output_dir=str(output_root),
    )
    task = state.create_video_frames_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state)

    def export(options, _backend) -> ExportResult:
        assert options.output_dir is not None
        options.output_dir.mkdir()
        frame = options.output_dir / "clip_000001.png"
        frame.write_text(options.input_path.parent.name, encoding="utf-8")
        return ExportResult(
            output_dir=options.output_dir,
            output_pattern=options.output_dir / "clip_%06d.png",
            command=("ffmpeg",),
        )

    executor._exporter = export

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.output_path == f"/api/tasks/{task.task_id}/download"
    assert completed.result_path == str(output_root / f"{task.task_id}.zip")
    with ZipFile(completed.result_path) as archive:
        assert archive.namelist() == [
            "001_clip_frames/clip_000001.png",
            "002_clip_frames/clip_000001.png",
        ]
        assert archive.read("001_clip_frames/clip_000001.png") == b"1" * 32
        assert archive.read("002_clip_frames/clip_000001.png") == b"2" * 32


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


def test_worker_publishes_single_output_with_semicolon_in_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    state = _manager(redis_connection, tmp_path)
    input_path = tmp_path / "assets" / "photo.png"
    input_path.parent.mkdir()
    input_path.write_bytes(b"input")
    output_path = tmp_path / "task-output" / "a; b.png"
    output_path.parent.mkdir()
    request = ChromaKeyerTaskRequest(
        input_path=str(input_path),
        samples=[[0, 255, 0]],
        params={},
        output_path=str(output_path),
    )
    task = state.create_chroma_keyer_task(request, owner_id="a" * 32)
    executor = RedisTaskExecutor(state)
    received_outputs: list[object] = []
    original_prepare = worker_module.prepare_download_artifact

    def prepare(task_id, output_paths, **kwargs):
        received_outputs.append(output_paths)
        return original_prepare(task_id, output_paths, **kwargs)

    def key_image(options, _model, _params) -> KeyImageResult:
        assert options.output_path is not None
        options.output_path.write_bytes(b"png")
        return KeyImageResult(output_path=options.output_path)

    monkeypatch.setattr(worker_module, "prepare_download_artifact", prepare)
    executor._keyer = key_image

    executor.execute_reserved_task(task, request)

    completed = state.get_task(task.task_id, owner_id="a" * 32)
    assert completed is not None
    assert completed.status is TaskStatus.SUCCEEDED
    assert received_outputs == [[output_path]]
    assert completed.result_path == str(output_path)
    assert output_path.read_bytes() == b"png"


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
        lambda _url, **_kwargs: redis_connection,
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
