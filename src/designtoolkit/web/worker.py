from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
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
from designtoolkit.web.redis_tasks import (
    RedisTaskManager,
    TerminalTransitionResult,
)
from designtoolkit.web.resources import (
    RedisReservation,
    RedisResourceCoordinator,
    RedisSessionLease,
)
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaVideoKeyerTaskRequest,
    LogEventResponse,
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
        resource_heartbeat: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(log_store=state.log_store, run_async=False)
        self._state = state
        self._max_output_bytes = max_output_bytes
        self._output_guard: OutputBudgetGuard | None = None
        self._output_root: Path | None = None
        self._input_paths: set[Path] = set()
        self._resource_heartbeat = resource_heartbeat
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
        self._input_paths = _request_input_paths(request)
        self._output_guard = (
            _HeartbeatOutputBudgetGuard(
                self._output_root,
                self._max_output_bytes,
                heartbeat=self._heartbeat_resources,
            )
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
        if self._output_guard is None:
            self._heartbeat_resources()
            return
        self._output_guard.account(path)

    def _check_output_tree(self) -> None:
        if self._output_guard is None:
            self._heartbeat_resources()
            return
        self._output_guard.check_tree()

    def _handle_task_failure(self, task_id: str, exc: Exception) -> None:
        self._discard_partial_output()
        output_limit_error = _find_output_limit_error(exc)
        if output_limit_error is not None:
            self._mark_failed(task_id, str(output_limit_error))
            return
        super()._handle_task_failure(task_id, exc)

    def _mark_failed(self, task_id: str, message: str) -> None:
        self._state._finalize_external_failure(task_id, message)

    def _mark_succeeded(
        self,
        task_id: str,
        output_path: str,
        *,
        elapsed_seconds: float | None = None,
        frame_count: int | None = None,
    ) -> None:
        artifact: Path | None = None
        archive_candidate = (
            Path(output_path.split("; ", 1)[0]).parent / f"{task_id}.zip"
        )
        try:
            self._check_output_tree()
            artifact = prepare_download_artifact(
                task_id,
                output_path,
                max_output_bytes=self._max_output_bytes,
            )
            self._validate_artifact(artifact)
        except Exception as exc:
            if artifact is not None:
                artifact.unlink(missing_ok=True)
            archive_candidate.unlink(missing_ok=True)
            self._discard_partial_output()
            output_limit_error = _find_output_limit_error(exc)
            self._mark_failed(
                task_id,
                str(output_limit_error or exc),
            )
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
        terminal_event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=LogLevel.SUCCESS,
            phase=LogPhase.COMPLETED,
            message="Task completed",
            details=details,
        )
        completed, transition = self._state._transition_finished(
            task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=download_url,
            result_path=str(artifact),
            terminal_event=terminal_event,
        )
        if transition is not TerminalTransitionResult.APPLIED:
            if completed.result_path != str(artifact):
                artifact.unlink(missing_ok=True)
            return

    def _heartbeat_resources(self) -> None:
        if self._resource_heartbeat is not None:
            self._resource_heartbeat()

    def _guarded_backend(self):
        from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

        if self._output_guard is None:
            return FfmpegBackend()
        return FfmpegBackend(
            output_guard=self._output_guard,
            max_file_bytes=self._output_guard.remaining_bytes,
        )

    def _validate_artifact(self, artifact: Path) -> None:
        artifact_guard = OutputBudgetGuard(
            artifact.parent,
            self._max_output_bytes,
            safety_bytes=0,
        )
        artifact_guard.account(artifact)

    def _discard_partial_output(self) -> None:
        if self._output_root is None:
            return
        protected = {
            path
            for path in self._input_paths
            if path == self._output_root or path.is_relative_to(self._output_root)
        }
        if not protected and self._output_root.is_dir():
            rmtree(self._output_root, ignore_errors=True)
            return
        if self._output_root.is_file():
            if self._output_root not in protected:
                self._output_root.unlink(missing_ok=True)
            return
        if not self._output_root.is_dir():
            return
        for path in sorted(
            self._output_root.rglob("*"),
            key=lambda candidate: len(candidate.parts),
            reverse=True,
        ):
            if path.is_file() and path.resolve() not in protected:
                path.unlink(missing_ok=True)
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        try:
            self._output_root.rmdir()
        except OSError:
            pass


def _request_input_paths(
    request: (
        VideoFramesTaskRequest
        | MediaResizeTaskRequest
        | ChromaKeyerTaskRequest
        | ChromaVideoKeyerTaskRequest
    ),
) -> set[Path]:
    input_paths = getattr(request, "input_paths", None)
    if input_paths is not None:
        return {Path(path).resolve() for path in input_paths}
    input_path = getattr(request, "input_path", None)
    if input_path is None:
        return set()
    return {Path(input_path).resolve()}


class _HeartbeatOutputBudgetGuard(OutputBudgetGuard):
    def __init__(
        self,
        root: Path,
        max_bytes: int,
        *,
        heartbeat: Callable[[], None],
    ) -> None:
        super().__init__(root, max_bytes)
        self._heartbeat = heartbeat

    def account(self, path: Path) -> None:
        self._heartbeat()
        super().account(path)

    def check_tree(self) -> None:
        self._heartbeat()
        super().check_tree()


def _build_resource_coordinator(
    settings: HostedSettings,
    redis_connection: Redis,
) -> RedisResourceCoordinator:
    return RedisResourceCoordinator(
        redis_connection=redis_connection,
        max_upload_concurrency=settings.max_upload_concurrency,
        max_session_bytes=settings.max_session_bytes,
        min_free_bytes=settings.min_free_bytes,
        reservation_ttl=settings.reservation_ttl_seconds,
        lease_ttl=settings.lease_ttl_seconds,
        reservation_heartbeat_interval=settings.reservation_heartbeat_seconds,
        lease_heartbeat_interval=settings.lease_heartbeat_seconds,
        release_marker_ttl=max(
            settings.retention_seconds,
            settings.reservation_ttl_seconds,
            settings.lease_ttl_seconds,
        ),
    )


def discard_failed_task_output(
    task_id: str,
    state: RedisTaskManager,
) -> None:
    work = state.task_work(task_id)
    if work is None:
        return
    job_kind, payload = work
    try:
        request = _task_request(job_kind, payload)
    except (TypeError, ValueError):
        return
    executor = RedisTaskExecutor(state)
    output_root = _request_output_root(request)
    executor._output_root = output_root.resolve() if output_root is not None else None
    executor._input_paths = _request_input_paths(request)
    executor._discard_partial_output()


def run_task(task_id: str, job_kind: str, payload: dict[str, object]) -> None:
    settings = HostedSettings.from_env()
    redis_connection = Redis.from_url(settings.redis_url)
    resources = _build_resource_coordinator(settings, redis_connection)
    state = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=settings.data_dir,
        max_queued_tasks=settings.max_queued_tasks,
        retention_seconds=settings.retention_seconds,
        resource_coordinator=resources,
    )
    task = state.get_task(task_id)
    if task is None:
        return
    if task.owner_id is None:
        state._finalize_external_failure(task_id, "Task has no session owner")
        return
    lease: RedisSessionLease | None = None
    output: RedisReservation | None = None

    def heartbeat() -> None:
        if output is not None:
            output.refresh()
        if lease is not None:
            lease.refresh()
        state.record_heartbeat(task_id)

    executor = RedisTaskExecutor(
        state,
        max_output_bytes=settings.max_output_bytes,
        resource_heartbeat=heartbeat,
    )
    try:
        lease = resources.acquire_session(task.owner_id, task_id=task_id)
        output = resources.reserve_output(
            task_id,
            task.owner_id,
            settings.max_output_bytes,
            free_bytes=disk_usage(settings.data_dir).free,
            min_free_bytes=settings.min_free_bytes,
        )
        state.mark_running(task_id)
        request = _task_request(job_kind, payload)
        executor.execute_reserved_task(task, request)
    except Exception as exc:
        executor._discard_partial_output()
        state._finalize_external_failure(task_id, str(exc))
    finally:
        if output is not None:
            output.release()
        if lease is not None:
            lease.release()


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
