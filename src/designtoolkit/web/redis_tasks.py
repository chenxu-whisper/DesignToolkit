from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty
from time import monotonic, sleep, time
from typing import Any
from uuid import uuid4

from redis import Redis
from rq import Queue

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

_QUEUE_NAME = "designtoolkit"


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
    ) -> None:
        self._redis = redis_connection
        self.queue = queue or Queue(_QUEUE_NAME, connection=redis_connection)
        self.data_dir = data_dir
        self.max_queued_tasks = max_queued_tasks
        self.retention_seconds = retention_seconds
        self.live_task_ttl = max(retention_seconds * 2, 24 * 60 * 60)
        self.log_store = RedisLogStore(
            redis_connection,
            retention_seconds=retention_seconds,
        )

    def create_video_frames_task(
        self,
        request: VideoFramesTaskRequest,
        *,
        owner_id: str | None = None,
    ) -> TaskRecord:
        options = video_frame_options_list_from_request(request)
        return self._enqueue(
            TaskKind.VIDEO_FRAMES,
            request,
            job_kind="video_frames",
            owner_id=owner_id,
            input_path=_joined_input_names(options),
        )

    def create_media_resize_task(
        self,
        request: MediaResizeTaskRequest,
        *,
        owner_id: str | None = None,
    ) -> TaskRecord:
        options = media_resize_options_list_from_request(request)
        return self._enqueue(
            TaskKind.MEDIA_RESIZE,
            request,
            job_kind="media_resize",
            owner_id=owner_id,
            input_path="\n".join(option.input_path.name for option in options),
        )

    def create_chroma_keyer_task(
        self,
        request: ChromaKeyerTaskRequest,
        *,
        owner_id: str | None = None,
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
        )

    def create_chroma_video_keyer_task(
        self,
        request: ChromaVideoKeyerTaskRequest,
        *,
        owner_id: str | None = None,
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

    def mark_running(self, task_id: str) -> TaskRecord:
        task = self._required_task(task_id)
        if task.status is not TaskStatus.QUEUED:
            return task
        self._release_queue_slot(task_id)
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(UTC)
        self._save_task(task)
        self.log_store.append(
            task_id=task_id,
            level=LogLevel.PROGRESS,
            phase=LogPhase.RUNNING,
            message="Task is running",
        )
        return task

    def mark_finished(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None = None,
        error: str | None = None,
        result_path: str | None = None,
    ) -> TaskRecord:
        task = self._required_task(task_id)
        task.status = status
        task.output_path = output_path if output_path is not None else task.output_path
        task.error = error
        task.result_path = result_path if result_path is not None else task.result_path
        task.finished_at = datetime.now(UTC)
        self._save_task(task)
        return task

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
        self.log_store.append(
            task_id=task.task_id,
            level=LogLevel.INFO,
            phase=LogPhase.CREATED,
            message=f"Created {kind.value} task",
            details={"input_path": input_path},
        )
        try:
            self.queue.enqueue(
                "designtoolkit.web.worker.run_task",
                task.task_id,
                job_kind,
                request.model_dump(mode="json"),
                job_id=task.task_id,
                job_timeout=self.retention_seconds,
                result_ttl=self.retention_seconds,
                failure_ttl=self.retention_seconds,
            )
        except Exception:
            self._redis.delete(_task_key(task.task_id), _event_key(task.task_id))
            self._release_queue_slot(task.task_id)
            raise
        return task

    def _release_queue_slot(self, task_id: str) -> None:
        self._redis.lrem(_queue_reservations_key(), 0, task_id)

    def _required_task(self, task_id: str) -> TaskRecord:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _save_task(self, task: TaskRecord) -> None:
        key = _task_key(task.task_id)
        value = _encode_task(task)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            self._redis.set(key, value, ex=self.retention_seconds)
        else:
            self._redis.set(key, value, ex=self.live_task_ttl)


def _joined_input_names(options: list[VideoFrameOptions]) -> str:
    return "\n".join(option.input_path.name for option in options)


def _task_key(task_id: str) -> str:
    return f"dt:task:{task_id}"


def _event_key(task_id: str) -> str:
    return f"dt:task:{task_id}:events"


def _queue_reservations_key() -> str:
    return "dt:queue:reservations"


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
