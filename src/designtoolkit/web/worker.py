from __future__ import annotations

from pathlib import Path
from shutil import disk_usage, rmtree

from redis import Redis
from rq import Queue

from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    prepare_download_artifact,
)
from designtoolkit.web.output_limits import OutputBudgetGuard, OutputLimitExceeded
from designtoolkit.web.redis_tasks import RedisTaskManager
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaVideoKeyerTaskRequest,
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskStatus,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskManager, TaskRecord


class RedisTaskExecutor(TaskManager):
    def __init__(
        self,
        state: RedisTaskManager,
        *,
        max_output_bytes: int = 5 * 1024 * 1024 * 1024,
    ) -> None:
        super().__init__(log_store=state.log_store, run_async=False)
        self._state = state
        self._max_output_bytes = max_output_bytes
        self._output_guard: OutputBudgetGuard | None = None
        self._output_root: Path | None = None
        self._backend_factory = self._guarded_backend

    def execute_reserved_task(
        self,
        task: TaskRecord,
        request: (
            VideoFramesTaskRequest
            | MediaResizeTaskRequest
            | ChromaKeyerTaskRequest
            | ChromaVideoKeyerTaskRequest
        ),
    ) -> None:
        output_root = _request_output_root(request)
        self._output_root = output_root.resolve() if output_root is not None else None
        self._output_guard = (
            OutputBudgetGuard(self._output_root, self._max_output_bytes)
            if self._output_root is not None
            else None
        )
        super().execute_reserved_task(task, request)

    def get_task(
        self,
        task_id: str,
        *,
        owner_id: str | None = None,
    ) -> TaskRecord | None:
        return self._state.get_task(task_id, owner_id=owner_id)

    def mark_finished(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None = None,
        error: str | None = None,
        result_path: str | None = None,
    ) -> TaskRecord:
        return self._state.mark_finished(
            task_id,
            status=status,
            output_path=output_path,
            error=error,
            result_path=result_path,
        )

    def _mark_running(self, task_id: str) -> None:
        self._state.mark_running(task_id)

    def _account_output(self, path: Path) -> None:
        if self._output_guard is not None:
            self._output_guard.account(path)

    def _check_output_tree(self) -> None:
        if self._output_guard is not None:
            self._output_guard.check_tree()

    def _handle_task_failure(self, task_id: str, exc: Exception) -> None:
        output_limit_error = _find_output_limit_error(exc)
        if output_limit_error is not None:
            self._discard_partial_output()
            self._mark_failed(task_id, str(output_limit_error))
            return
        super()._handle_task_failure(task_id, exc)

    def _mark_succeeded(
        self,
        task_id: str,
        output_path: str,
        *,
        elapsed_seconds: float | None = None,
        frame_count: int | None = None,
    ) -> None:
        try:
            self._check_output_tree()
            artifact = prepare_download_artifact(
                task_id,
                output_path,
                max_output_bytes=self._max_output_bytes,
            )
        except OutputLimitExceeded as exc:
            self._discard_partial_output()
            self._mark_failed(task_id, str(exc))
            return
        download_url = f"/api/tasks/{task_id}/download"
        task = self._state.get_task(task_id)
        if task is not None and task.owner_id is not None:
            HostedStorage(
                Path(self._state.data_dir),
                max_upload_bytes=1,
            ).touch_session(task.owner_id)
        details: dict[str, object] = {"output_path": download_url}
        if elapsed_seconds is not None:
            details["elapsed_seconds"] = elapsed_seconds
        if frame_count is not None:
            details["frame_count"] = frame_count
        self._state.mark_finished(
            task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=download_url,
            result_path=str(artifact),
        )
        self.log_store.append(
            task_id=task_id,
            level=LogLevel.SUCCESS,
            phase=LogPhase.COMPLETED,
            message="Task completed",
            details=details,
        )

    def _guarded_backend(self):
        from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

        if self._output_guard is None:
            return FfmpegBackend()
        return FfmpegBackend(
            output_guard=self._output_guard.check_tree,
            max_file_bytes=self._output_guard.remaining_bytes,
        )

    def _discard_partial_output(self) -> None:
        if self._output_root is None:
            return
        if self._output_root.is_dir():
            rmtree(self._output_root, ignore_errors=True)
        else:
            self._output_root.unlink(missing_ok=True)


def run_task(task_id: str, job_kind: str, payload: dict[str, object]) -> None:
    settings = HostedSettings.from_env()
    redis_connection = Redis.from_url(settings.redis_url)
    state = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=settings.data_dir,
        max_queued_tasks=settings.max_queued_tasks,
        retention_seconds=settings.retention_seconds,
    )
    task = state.get_task(task_id)
    if task is None:
        return
    executor = RedisTaskExecutor(
        state,
        max_output_bytes=settings.max_output_bytes,
    )
    try:
        state.mark_running(task_id)
        required_free_bytes = (
            settings.min_free_bytes + 2 * settings.max_output_bytes
        )
        if disk_usage(settings.data_dir).free < required_free_bytes:
            raise RuntimeError("Server storage capacity is temporarily unavailable")
        request = _task_request(job_kind, payload)
        executor.execute_reserved_task(task, request)
    except Exception as exc:
        state.mark_finished(
            task_id,
            status=TaskStatus.FAILED,
            error=str(exc),
        )
        state.log_store.append(
            task_id=task_id,
            level=LogLevel.ERROR,
            phase=LogPhase.FAILED,
            message=str(exc),
        )


def _task_request(
    job_kind: str,
    payload: dict[str, object],
) -> (
    VideoFramesTaskRequest
    | MediaResizeTaskRequest
    | ChromaKeyerTaskRequest
    | ChromaVideoKeyerTaskRequest
):
    models = {
        "video_frames": VideoFramesTaskRequest,
        "media_resize": MediaResizeTaskRequest,
        "chroma_keyer": ChromaKeyerTaskRequest,
        "chroma_video_keyer": ChromaVideoKeyerTaskRequest,
    }
    try:
        model = models[job_kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported queued task kind: {job_kind}") from exc
    return model.model_validate(payload)


def _request_output_root(
    request: (
        VideoFramesTaskRequest
        | MediaResizeTaskRequest
        | ChromaKeyerTaskRequest
        | ChromaVideoKeyerTaskRequest
    ),
) -> Path | None:
    if isinstance(request, VideoFramesTaskRequest):
        return Path(request.output_dir) if request.output_dir is not None else None
    if isinstance(request, MediaResizeTaskRequest):
        if request.output_dir is not None:
            return Path(request.output_dir)
        if request.output_path is not None:
            return Path(request.output_path).parent
        return None
    if isinstance(request, ChromaVideoKeyerTaskRequest):
        return Path(request.output_root) if request.output_root is not None else None
    return (
        Path(request.output_path).parent
        if request.output_path is not None
        else None
    )


def _find_output_limit_error(exc: BaseException) -> OutputLimitExceeded | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, OutputLimitExceeded):
            return current
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return None
