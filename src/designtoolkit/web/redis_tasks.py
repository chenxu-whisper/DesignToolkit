from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from queue import Empty
from time import monotonic, sleep, time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from redis import Redis
from redis.exceptions import ResponseError, WatchError
from rq import Queue
from rq.job import Callback, Job, JobStatus
from rq.registry import FailedJobRegistry, StartedJobRegistry

from designtoolkit.chroma_keyer.image_io import (
    default_output_path,
    validate_output_path,
)
from designtoolkit.chroma_keyer.models import KeyerError
from designtoolkit.video_frames import VideoFrameOptions
from designtoolkit.web.adapters import (
    chroma_keyer_options_from_request,
    chroma_video_keyer_options_from_request,
    media_resize_options_list_from_request,
    video_frame_options_list_from_request,
)
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaVideoKeyerTaskRequest,
    LogEventResponse,
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskKind,
    TaskStatus,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskConflictError, TaskRecord

if TYPE_CHECKING:
    from designtoolkit.web.resources import RedisResourceCoordinator

_QUEUE_NAME = "designtoolkit"
_TERMINAL_TTL_SECONDS = 2 * 60 * 60
_UNCERTAIN_ENQUEUE_ERROR = "Task enqueue could not be confirmed"
_MARK_RUNNING_SCRIPT = """-- mark_task_running
local value = redis.call("GET", KEYS[1])
if not value then
    return {0, ""}
end

local task = cjson.decode(value)
if task.status ~= "queued" then
    return {2, value}
end

task.status = "running"
task.started_at = ARGV[2]
local encoded = cjson.encode(task)
redis.call("SET", KEYS[1], encoded, "EX", ARGV[3])
redis.call("LREM", KEYS[2], 0, ARGV[1])
redis.call("RPUSH", KEYS[3], ARGV[4])
redis.call("EXPIRE", KEYS[3], ARGV[3])
return {1, encoded}
"""
_TERMINAL_TRANSITION_SCRIPT = """-- transition_task_terminal
local value = redis.call("GET", KEYS[1])
if not value then
    return {0, ""}
end

local task = cjson.decode(value)
if task.status == "succeeded" or task.status == "failed" then
    return {2, value}
end

task.status = ARGV[1]
if ARGV[2] == "1" then
    task.output_path = ARGV[3]
end
if ARGV[4] == "1" then
    task.error = ARGV[5]
else
    task.error = cjson.null
end
if ARGV[6] == "1" then
    task.result_path = ARGV[7]
end
task.finished_at = ARGV[8]

local encoded = cjson.encode(task)
redis.call("SET", KEYS[1], encoded, "EX", ARGV[9])
if redis.call("EXISTS", KEYS[2]) == 1 then
    redis.call("EXPIRE", KEYS[2], ARGV[9])
end
if ARGV[10] == "1" then
    redis.call("RPUSH", KEYS[3], ARGV[11])
    redis.call("EXPIRE", KEYS[3], ARGV[9])
end
return {1, encoded}
"""


class RunningTransitionResult(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"


class TerminalTransitionResult(StrEnum):
    APPLIED = "applied"
    ALREADY_TERMINAL = "already_terminal"


class RedisEventSubscriber:
    def __init__(
        self,
        store: RedisLogStore,
        task_id: str,
        offset: int,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._offset = offset

    def get(self, timeout: float) -> LogEventResponse:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            events = self._store.history(self._task_id)
            if self._offset < len(events):
                event = events[self._offset]
                self._offset += 1
                return event
            sleep(min(0.05, max(0.0, deadline - monotonic())))
        raise Empty


class RedisLogStore:
    def __init__(
        self,
        redis_connection: Redis,
        *,
        retention_seconds: int,
    ) -> None:
        self._redis = redis_connection
        self._retention_seconds = retention_seconds

    def append(
        self,
        *,
        task_id: str,
        level: LogLevel,
        phase: LogPhase,
        message: str,
        details: dict[str, object] | None = None,
    ) -> LogEventResponse:
        event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=level,
            phase=phase,
            message=message,
            details=dict(details or {}),
        )
        key = _event_key(task_id)
        pipeline = self._redis.pipeline()
        pipeline.rpush(key, event.model_dump_json())
        pipeline.expire(key, self._retention_seconds)
        pipeline.execute()
        return event

    def history(self, task_id: str) -> list[LogEventResponse]:
        values = self._redis.lrange(_event_key(task_id), 0, -1)
        return [LogEventResponse.model_validate_json(value) for value in values]

    def history_and_subscribe(
        self,
        task_id: str,
    ) -> tuple[list[LogEventResponse], RedisEventSubscriber]:
        history = self.history(task_id)
        return history, RedisEventSubscriber(self, task_id, len(history))

    def unsubscribe(
        self,
        task_id: str,
        subscriber: RedisEventSubscriber,
    ) -> None:
        del task_id, subscriber


class RedisTaskManager:
    def __init__(
        self,
        *,
        redis_connection: Redis,
        queue: Queue | None = None,
        data_dir: Path,
        max_queued_tasks: int,
        retention_seconds: int,
        resource_coordinator: RedisResourceCoordinator | None = None,
    ) -> None:
        self._redis = redis_connection
        self.queue = queue or Queue(_QUEUE_NAME, connection=redis_connection)
        self.data_dir = data_dir
        self.max_queued_tasks = max_queued_tasks
        self.retention_seconds = retention_seconds
        self.resource_coordinator = resource_coordinator
        self.live_task_ttl = max(retention_seconds * 2, 24 * 60 * 60)
        self._mark_running_script = self._redis.register_script(
            _MARK_RUNNING_SCRIPT
        )
        self._terminal_transition_script = self._redis.register_script(
            _TERMINAL_TRANSITION_SCRIPT
        )
        self.log_store = RedisLogStore(
            redis_connection,
            retention_seconds=retention_seconds,
        )

    def create_video_frames_task(
        self,
        request: VideoFramesTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
    ) -> TaskRecord:
        options = video_frame_options_list_from_request(request)
        return self._enqueue(
            TaskKind.VIDEO_FRAMES,
            request,
            job_kind="video_frames",
            owner_id=owner_id,
            input_path=_joined_input_names(options),
            commit=commit,
        )

    def create_media_resize_task(
        self,
        request: MediaResizeTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
    ) -> TaskRecord:
        options = media_resize_options_list_from_request(request)
        return self._enqueue(
            TaskKind.MEDIA_RESIZE,
            request,
            job_kind="media_resize",
            owner_id=owner_id,
            input_path="\n".join(option.input_path.name for option in options),
            commit=commit,
        )

    def create_chroma_keyer_task(
        self,
        request: ChromaKeyerTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
    ) -> TaskRecord:
        options, _model, _params = chroma_keyer_options_from_request(request)
        output_path = options.output_path or default_output_path(options.input_path)
        validate_output_path(options.input_path, output_path)
        return self._enqueue(
            TaskKind.CHROMA_KEYER,
            request,
            job_kind="chroma_keyer",
            owner_id=owner_id,
            input_path=options.input_path.name,
            commit=commit,
        )

    def create_chroma_video_keyer_task(
        self,
        request: ChromaVideoKeyerTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
    ) -> TaskRecord:
        options, _model, _params = chroma_video_keyer_options_from_request(request)
        output_root = options.output_root or options.input_path.parent
        if not output_root.exists() or not output_root.is_dir():
            raise KeyerError("Output root must exist and be a directory")
        output_path = output_root / f"{options.input_path.stem}_keyed_frames"
        if output_path.exists():
            raise KeyerError("Final output directory already exists")
        return self._enqueue(
            TaskKind.CHROMA_KEYER,
            request,
            job_kind="chroma_video_keyer",
            owner_id=owner_id,
            input_path=options.input_path.name,
            commit=commit,
        )

    def get_task(
        self,
        task_id: str,
        *,
        owner_id: str | None = None,
    ) -> TaskRecord | None:
        value = self._redis.get(_task_key(task_id))
        if value is None:
            return None
        task = _decode_task(value)
        if owner_id is not None and task.owner_id != owner_id:
            return None
        return task

    def mark_running(
        self,
        task_id: str,
    ) -> tuple[TaskRecord, RunningTransitionResult]:
        started_at = datetime.now(UTC)
        event = LogEventResponse(
            task_id=task_id,
            timestamp=started_at,
            level=LogLevel.PROGRESS,
            phase=LogPhase.RUNNING,
            message="Task is running",
            details={},
        )
        try:
            result = self._mark_running_script(
                keys=[
                    _task_key(task_id),
                    _queue_reservations_key(),
                    _event_key(task_id),
                ],
                args=[
                    task_id,
                    started_at.isoformat(),
                    self.live_task_ttl,
                    event.model_dump_json(),
                ],
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            return self._mark_running_without_lua(
                task_id,
                started_at=started_at,
                event=event,
            )
        code = int(result[0])
        if code == 0:
            raise KeyError(task_id)
        transition = (
            RunningTransitionResult.APPLIED
            if code == 1
            else RunningTransitionResult.REJECTED
        )
        return _decode_task(result[1]), transition

    def mark_finished(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None = None,
        error: str | None = None,
        result_path: str | None = None,
    ) -> TaskRecord:
        task, _transitioned = self._transition_finished(
            task_id,
            status=status,
            output_path=output_path,
            error=error,
            result_path=result_path,
        )
        return task

    def record_heartbeat(self, task_id: str) -> None:
        key = _task_resources_key(task_id)
        pipeline = self._redis.pipeline()
        pipeline.hset(key, "heartbeat_at", time())
        pipeline.expire(key, self.live_task_ttl)
        pipeline.execute()

    def reconcile_tasks(self, *, grace_seconds: float = 60) -> int:
        now = datetime.now(UTC)
        started_ids = set(
            StartedJobRegistry(
                self.queue.name,
                connection=self._redis,
                queue=self.queue,
            ).get_job_ids()
        )
        failed_ids = set(
            FailedJobRegistry(
                self.queue.name,
                connection=self._redis,
                queue=self.queue,
            ).get_job_ids()
        )
        reconciled = 0
        for task in self._tasks_requiring_failure_cleanup():
            message = task.error or "Task failed"
            if self._finalize_external_failure(task.task_id, message):
                reconciled += 1
        for task_id in self._uncertain_enqueue_task_ids():
            if self._reconcile_uncertain_enqueue(task_id):
                reconciled += 1
        for task in self._active_tasks():
            job = self.queue.fetch_job(task.task_id)
            status = job.get_status(refresh=True) if job is not None else None
            if task.status is TaskStatus.QUEUED:
                stale = (now - task.created_at).total_seconds() > grace_seconds
                abandoned = job is None or status in {
                    JobStatus.FAILED,
                    JobStatus.STOPPED,
                    JobStatus.CANCELED,
                    JobStatus.FINISHED,
                }
                if not stale or not abandoned:
                    continue
                message = (
                    "Task finished without publishing a terminal result"
                    if status is JobStatus.FINISHED
                    else "Queued task was abandoned before execution"
                )
            else:
                if status is JobStatus.STARTED or task.task_id in started_ids:
                    continue
                if not self._heartbeat_is_stale(task, now, grace_seconds):
                    continue
                terminal_evidence = (
                    job is None
                    or task.task_id in failed_ids
                    or status
                    in {
                        JobStatus.FAILED,
                        JobStatus.STOPPED,
                        JobStatus.CANCELED,
                        JobStatus.FINISHED,
                    }
                )
                if not terminal_evidence:
                    continue
                message = (
                    "Task finished without publishing a terminal result"
                    if status is JobStatus.FINISHED
                    else "Running task was abandoned by its worker"
                )
            if self._finalize_external_failure(task.task_id, message):
                reconciled += 1
        return reconciled

    def active_owner_ids(self) -> set[str]:
        owners: set[str] = set()
        for key in self._redis.scan_iter(match="dt:task:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            if key_text.endswith(":events"):
                continue
            value = self._redis.get(key)
            if value is None:
                continue
            task = _decode_task(value)
            if (
                task.owner_id is not None
                and task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
            ):
                owners.add(task.owner_id)
        return owners

    def reconcile_queue_slots(self) -> int:
        for value in self._redis.lrange(_queue_reservations_key(), 0, -1):
            task_id = value.decode() if isinstance(value, bytes) else value
            task = self.get_task(task_id)
            stale_missing_job = (
                task is not None
                and task.status is TaskStatus.QUEUED
                and (datetime.now(UTC) - task.created_at).total_seconds() > 60
                and self.queue.fetch_job(task_id) is None
            )
            if (
                task is None
                or task.status is not TaskStatus.QUEUED
                or stale_missing_job
            ):
                self._release_queue_slot(task_id)
                if stale_missing_job:
                    self.mark_finished(
                        task_id,
                        status=TaskStatus.FAILED,
                        error="Queued task was abandoned before execution",
                    )
        return self._redis.llen(_queue_reservations_key())

    def allow_request(self, bucket: str, *, limit: int, window: int) -> bool:
        window_id = int(time() // window)
        key = f"dt:rate:{bucket}:{window_id}"
        count = self._redis.incr(key)
        if count == 1:
            self._redis.expire(key, window + 1)
        return count <= limit

    def _enqueue(
        self,
        kind: TaskKind,
        request,
        *,
        job_kind: str,
        owner_id: str | None,
        input_path: str,
        commit: Callable[[Callable[[], Job]], Job] | None,
    ) -> TaskRecord:
        task_id = str(uuid4())
        queued_count = self._redis.rpush(_queue_reservations_key(), task_id)
        if queued_count > self.max_queued_tasks:
            self._release_queue_slot(task_id)
            raise TaskConflictError("The task queue is full")
        task = TaskRecord(
            task_id=task_id,
            kind=kind,
            status=TaskStatus.QUEUED,
            created_at=datetime.now(UTC),
            started_at=None,
            finished_at=None,
            input_path=input_path,
            output_path=None,
            error=None,
            owner_id=owner_id,
        )
        self._save_task(task)
        self._save_work(task.task_id, job_kind, request.model_dump(mode="json"))
        self.log_store.append(
            task_id=task.task_id,
            level=LogLevel.INFO,
            phase=LogPhase.CREATED,
            message=f"Created {kind.value} task",
            details={"input_path": input_path},
        )
        def enqueue_job() -> Job:
            return self.queue.enqueue(
                "designtoolkit.web.worker.run_task",
                task.task_id,
                job_kind,
                request.model_dump(mode="json"),
                job_id=task.task_id,
                job_timeout=self.retention_seconds,
                result_ttl=self.retention_seconds,
                failure_ttl=self.retention_seconds,
                meta={"owner_id": owner_id},
                on_failure=Callback(handle_job_failure),
                on_stopped=Callback(handle_job_stopped),
            )

        try:
            if commit is None:
                enqueue_job()
            else:
                commit(enqueue_job)
        except Exception as enqueue_error:
            try:
                enqueue_committed = self._enqueue_has_authoritative_evidence(
                    task.task_id
                )
            except Exception as recovery_error:
                enqueue_error.add_note(
                    "Enqueue recovery could not confirm whether the RQ job committed"
                )
                enqueue_error.add_note(
                    f"Enqueue recovery failed with: {recovery_error}"
                )
                try:
                    self._mark_enqueue_uncertain(task.task_id)
                except Exception as intent_error:
                    enqueue_error.add_note(
                        f"Enqueue intent could not be persisted: {intent_error}"
                    )
                    raise enqueue_error from intent_error
                return task
            if enqueue_committed:
                return task
            self._redis.delete(
                _task_key(task.task_id),
                _event_key(task.task_id),
                _task_work_key(task.task_id),
            )
            self._release_queue_slot(task.task_id)
            raise
        return task

    def _enqueue_has_authoritative_evidence(self, task_id: str) -> bool:
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.exists(Job.key_for(task_id))
        pipeline.get(_task_key(task_id))
        job_exists, task_value = pipeline.execute()
        if job_exists:
            return True
        if task_value is None:
            return False
        return _decode_task(task_value).status is not TaskStatus.QUEUED

    def _mark_enqueue_uncertain(self, task_id: str) -> None:
        pipeline = self._redis.pipeline()
        pipeline.hset(
            _enqueue_intent_key(task_id),
            mapping={
                "task_id": task_id,
                "job_id": task_id,
                "slot_reserved": "1",
                "state": "uncertain",
            },
        )
        pipeline.expire(_enqueue_intent_key(task_id), self.live_task_ttl)
        pipeline.execute()

    def _uncertain_enqueue_task_ids(self) -> list[str]:
        task_ids: list[str] = []
        for key in self._redis.scan_iter(match="dt:enqueue-intent:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            task_id = key_text.removeprefix("dt:enqueue-intent:")
            metadata = self._redis.hgetall(key)
            state = metadata.get(b"state", metadata.get("state"))
            stored_task_id = metadata.get(b"task_id", metadata.get("task_id"))
            job_id = metadata.get(b"job_id", metadata.get("job_id"))
            if (
                _redis_text(state) == "uncertain"
                and _redis_text(stored_task_id) == task_id
                and _redis_text(job_id) == task_id
            ):
                task_ids.append(task_id)
        return task_ids

    def _reconcile_uncertain_enqueue(self, task_id: str) -> bool:
        task_key = _task_key(task_id)
        intent_key = _enqueue_intent_key(task_id)
        job_key = Job.key_for(task_id)
        event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=LogLevel.ERROR,
            phase=LogPhase.FAILED,
            message=_UNCERTAIN_ENQUEUE_ERROR,
            details={},
        )
        transitioned = False
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(task_key, intent_key, job_key)
                    if not pipeline.exists(intent_key):
                        pipeline.unwatch()
                        return False
                    if pipeline.exists(job_key):
                        pipeline.multi()
                        pipeline.delete(intent_key)
                        pipeline.execute()
                        return False
                    value = pipeline.get(task_key)
                    if value is None:
                        pipeline.multi()
                        pipeline.lrem(_queue_reservations_key(), 0, task_id)
                        pipeline.delete(intent_key)
                        pipeline.execute()
                        return False
                    task = _decode_task(value)
                    if task.status is not TaskStatus.QUEUED:
                        pipeline.multi()
                        pipeline.lrem(_queue_reservations_key(), 0, task_id)
                        pipeline.delete(intent_key)
                        pipeline.execute()
                        return False
                    task.status = TaskStatus.FAILED
                    task.error = _UNCERTAIN_ENQUEUE_ERROR
                    task.finished_at = event.timestamp
                    pipeline.multi()
                    pipeline.set(
                        task_key,
                        _encode_task(task),
                        ex=_TERMINAL_TTL_SECONDS,
                    )
                    pipeline.expire(
                        _task_work_key(task_id),
                        _TERMINAL_TTL_SECONDS,
                    )
                    pipeline.rpush(_event_key(task_id), event.model_dump_json())
                    pipeline.expire(
                        _event_key(task_id),
                        _TERMINAL_TTL_SECONDS,
                    )
                    pipeline.lrem(_queue_reservations_key(), 0, task_id)
                    pipeline.delete(intent_key)
                    pipeline.execute()
                    transitioned = True
                    break
                except WatchError:
                    continue
        cleaned = self._cleanup_failed_task(task_id)
        return transitioned or cleaned

    def _release_queue_slot(self, task_id: str) -> None:
        self._redis.lrem(_queue_reservations_key(), 0, task_id)

    def _required_task(self, task_id: str) -> TaskRecord:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _mark_running_without_lua(
        self,
        task_id: str,
        *,
        started_at: datetime,
        event: LogEventResponse,
    ) -> tuple[TaskRecord, RunningTransitionResult]:
        key = _task_key(task_id)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(key)
                    value = pipeline.get(key)
                    if value is None:
                        raise KeyError(task_id)
                    task = _decode_task(value)
                    if task.status is not TaskStatus.QUEUED:
                        pipeline.unwatch()
                        return task, RunningTransitionResult.REJECTED
                    task.status = TaskStatus.RUNNING
                    task.started_at = started_at
                    pipeline.multi()
                    pipeline.set(
                        key,
                        _encode_task(task),
                        ex=self.live_task_ttl,
                    )
                    pipeline.lrem(_queue_reservations_key(), 0, task_id)
                    pipeline.rpush(_event_key(task_id), event.model_dump_json())
                    pipeline.expire(_event_key(task_id), self.live_task_ttl)
                    pipeline.execute()
                    return task, RunningTransitionResult.APPLIED
                except WatchError:
                    continue

    def _transition_finished(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None = None,
        error: str | None = None,
        result_path: str | None = None,
        terminal_event: LogEventResponse | None = None,
    ) -> tuple[TaskRecord, TerminalTransitionResult]:
        finished_at = datetime.now(UTC)
        event_json = (
            terminal_event.model_dump_json() if terminal_event is not None else ""
        )
        try:
            result = self._terminal_transition_script(
                keys=[
                    _task_key(task_id),
                    _task_work_key(task_id),
                    _event_key(task_id),
                ],
                args=[
                    status.value,
                    "1" if output_path is not None else "0",
                    output_path or "",
                    "1" if error is not None else "0",
                    error or "",
                    "1" if result_path is not None else "0",
                    result_path or "",
                    finished_at.isoformat(),
                    _TERMINAL_TTL_SECONDS,
                    "1" if terminal_event is not None else "0",
                    event_json,
                ],
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            return self._transition_finished_without_lua(
                task_id,
                status=status,
                output_path=output_path,
                error=error,
                result_path=result_path,
                terminal_event=terminal_event,
                finished_at=finished_at,
            )
        code = int(result[0])
        if code == 0:
            raise KeyError(task_id)
        task = _decode_task(result[1])
        transition = (
            TerminalTransitionResult.APPLIED
            if code == 1
            else TerminalTransitionResult.ALREADY_TERMINAL
        )
        return task, transition

    def _transition_finished_without_lua(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None,
        error: str | None,
        result_path: str | None,
        terminal_event: LogEventResponse | None,
        finished_at: datetime,
    ) -> tuple[TaskRecord, TerminalTransitionResult]:
        key = _task_key(task_id)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(key)
                    value = pipeline.get(key)
                    if value is None:
                        raise KeyError(task_id)
                    task = _decode_task(value)
                    if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
                        pipeline.unwatch()
                        return task, TerminalTransitionResult.ALREADY_TERMINAL
                    task.status = status
                    if output_path is not None:
                        task.output_path = output_path
                    task.error = error
                    if result_path is not None:
                        task.result_path = result_path
                    task.finished_at = finished_at
                    pipeline.multi()
                    pipeline.set(
                        key,
                        _encode_task(task),
                        ex=_TERMINAL_TTL_SECONDS,
                    )
                    pipeline.expire(
                        _task_work_key(task_id),
                        _TERMINAL_TTL_SECONDS,
                    )
                    if terminal_event is not None:
                        pipeline.rpush(
                            _event_key(task_id),
                            terminal_event.model_dump_json(),
                        )
                        pipeline.expire(
                            _event_key(task_id),
                            _TERMINAL_TTL_SECONDS,
                        )
                    pipeline.execute()
                    return task, TerminalTransitionResult.APPLIED
                except WatchError:
                    continue

    def _finalize_external_failure(self, task_id: str, message: str) -> bool:
        terminal_event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=LogLevel.ERROR,
            phase=LogPhase.FAILED,
            message=message,
            details={},
        )
        try:
            task, transition = self._transition_finished(
                task_id,
                status=TaskStatus.FAILED,
                error=message,
                terminal_event=terminal_event,
            )
        except KeyError:
            return False
        if task.status is TaskStatus.SUCCEEDED:
            return False
        cleaned = self._cleanup_failed_task(task_id)
        return transition is TerminalTransitionResult.APPLIED or cleaned

    def _cleanup_failed_task(self, task_id: str) -> bool:
        cleanup_key = _failure_cleanup_key(task_id)
        if self._redis.exists(cleanup_key):
            return False
        self._release_queue_slot(task_id)
        from designtoolkit.web.worker import discard_failed_task_output

        try:
            discard_failed_task_output(task_id, self)
        finally:
            if self.resource_coordinator is not None:
                self.resource_coordinator.release_task_resources(task_id)
        self._redis.set(cleanup_key, "1", ex=_TERMINAL_TTL_SECONDS)
        return True

    def _tasks_requiring_failure_cleanup(self) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        for key in self._redis.scan_iter(match="dt:task:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            if key_text.endswith(":events"):
                continue
            value = self._redis.get(key)
            if value is None:
                continue
            task = _decode_task(value)
            if (
                task.status is TaskStatus.FAILED
                and not self._redis.exists(_failure_cleanup_key(task.task_id))
            ):
                tasks.append(task)
        return tasks

    def _active_tasks(self) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        for key in self._redis.scan_iter(match="dt:task:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            if key_text.endswith(":events"):
                continue
            value = self._redis.get(key)
            if value is None:
                continue
            task = _decode_task(value)
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                tasks.append(task)
        return tasks

    def _heartbeat_is_stale(
        self,
        task: TaskRecord,
        now: datetime,
        grace_seconds: float,
    ) -> bool:
        value = self._redis.hget(_task_resources_key(task.task_id), "heartbeat_at")
        heartbeat_at = float(value) if value is not None else None
        fallback = task.started_at or task.created_at
        last_seen = (
            datetime.fromtimestamp(heartbeat_at, UTC)
            if heartbeat_at is not None
            else fallback
        )
        return (now - last_seen).total_seconds() > grace_seconds

    def _save_task(self, task: TaskRecord) -> None:
        key = _task_key(task.task_id)
        value = _encode_task(task)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            self._redis.set(key, value, ex=self.retention_seconds)
        else:
            self._redis.set(key, value, ex=self.live_task_ttl)

    def _save_work(
        self,
        task_id: str,
        job_kind: str,
        payload: dict[str, object],
    ) -> None:
        value = json.dumps(
            {"job_kind": job_kind, "payload": payload},
            separators=(",", ":"),
        )
        self._redis.set(
            _task_work_key(task_id),
            value,
            ex=self.live_task_ttl,
        )

    def task_work(self, task_id: str) -> tuple[str, dict[str, object]] | None:
        value = self._redis.get(_task_work_key(task_id))
        if value is None:
            return None
        data = json.loads(value)
        return data["job_kind"], data["payload"]


def _joined_input_names(options: list[VideoFrameOptions]) -> str:
    return "\n".join(option.input_path.name for option in options)


def _task_key(task_id: str) -> str:
    return f"dt:task:{task_id}"


def _event_key(task_id: str) -> str:
    return f"dt:task:{task_id}:events"


def _queue_reservations_key() -> str:
    return "dt:queue:reservations"


def _enqueue_intent_key(task_id: str) -> str:
    return f"dt:enqueue-intent:{task_id}"


def _task_resources_key(task_id: str) -> str:
    return f"dt:resource:task:{task_id}"


def _task_work_key(task_id: str) -> str:
    return f"dt:task-work:{task_id}"


def _failure_cleanup_key(task_id: str) -> str:
    return f"dt:failure-cleanup:{task_id}"


def _redis_text(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode()
    return value


def handle_job_failure(
    job: Job,
    connection: Redis,
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: object,
) -> None:
    del exc_type, traceback
    _handle_external_job_failure(job, connection, str(exc_value))


def handle_job_stopped(job: Job, connection: Redis) -> None:
    _handle_external_job_failure(job, connection, "Task was stopped")


def _handle_external_job_failure(
    job: Job,
    connection: Redis,
    message: str,
) -> None:
    manager = _callback_manager(connection)
    manager._finalize_external_failure(job.id, message)


def _callback_manager(connection: Redis) -> RedisTaskManager:
    from designtoolkit.web.hosted import HostedSettings
    from designtoolkit.web.resources import RedisResourceCoordinator

    settings = HostedSettings.from_env()
    resources = RedisResourceCoordinator(
        redis_connection=connection,
        max_upload_concurrency=settings.max_upload_concurrency,
        max_session_bytes=settings.max_session_bytes,
        min_free_bytes=settings.min_free_bytes,
        reservation_ttl=settings.reservation_ttl_seconds,
        lease_ttl=settings.lease_ttl_seconds,
        reservation_heartbeat_interval=settings.reservation_heartbeat_seconds,
        lease_heartbeat_interval=settings.lease_heartbeat_seconds,
        release_marker_ttl=_TERMINAL_TTL_SECONDS,
    )
    return RedisTaskManager(
        redis_connection=connection,
        queue=Queue(_QUEUE_NAME, connection=connection),
        data_dir=settings.data_dir,
        max_queued_tasks=settings.max_queued_tasks,
        retention_seconds=_TERMINAL_TTL_SECONDS,
        resource_coordinator=resources,
    )


def _encode_task(task: TaskRecord) -> str:
    data: dict[str, Any] = {
        "task_id": task.task_id,
        "kind": task.kind.value,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "input_path": task.input_path,
        "output_path": task.output_path,
        "error": task.error,
        "owner_id": task.owner_id,
        "result_path": task.result_path,
    }
    return json.dumps(data, separators=(",", ":"))


def _decode_task(value: bytes | str) -> TaskRecord:
    data = json.loads(value)
    return TaskRecord(
        task_id=data["task_id"],
        kind=TaskKind(data["kind"]),
        status=TaskStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        started_at=(
            datetime.fromisoformat(data["started_at"])
            if data["started_at"]
            else None
        ),
        finished_at=(
            datetime.fromisoformat(data["finished_at"])
            if data["finished_at"]
            else None
        ),
        input_path=data["input_path"],
        output_path=data["output_path"],
        error=data["error"],
        owner_id=data["owner_id"],
        result_path=data["result_path"],
    )
