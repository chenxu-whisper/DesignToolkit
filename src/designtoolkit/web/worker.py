from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from shutil import disk_usage, rmtree
from threading import Event, Lock, Thread
from time import monotonic

from redis import Redis
from rq import Queue

from designtoolkit.web.heartbeat import (
    DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
    HeartbeatStopTimeoutError,
    stop_heartbeat_thread,
)
from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    decode_output_paths,
    prepare_download_artifact,
    redis_connection_options,
)
from designtoolkit.web.output_limits import OutputBudgetGuard, OutputLimitExceeded
from designtoolkit.web.redis_tasks import (
    RedisTaskManager,
    RunningTransitionResult,
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
        resource_health_check: Callable[[], None] | None = None,
        before_success_publish: Callable[[], None] | None = None,
        task_already_running: bool = False,
    ) -> None:
        super().__init__(log_store=state.log_store, run_async=False)
        self._state = state
        self._max_output_bytes = max_output_bytes
        self._output_guard: OutputBudgetGuard | None = None
        self._output_root: Path | None = None
        self._input_paths: set[Path] = set()
        self._resource_heartbeat = resource_heartbeat
        self._resource_health_check = resource_health_check
        self._before_success_publish = before_success_publish
        self._task_already_running = task_already_running
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
                refresh=self._heartbeat_resources,
                raise_if_failed=self._raise_if_resource_failed,
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
        if self._task_already_running:
            return
        _task, transition = self._state.mark_running(task_id)
        if transition is not RunningTransitionResult.APPLIED:
            raise RuntimeError("Task is no longer queued")

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
        output_paths: list[Path] | str,
        *,
        elapsed_seconds: float | None = None,
        frame_count: int | None = None,
    ) -> None:
        artifact: Path | None = None
        paths = decode_output_paths(output_paths)
        archive_candidate = (
            paths[0].parent / f"{task_id}.zip" if paths else None
        )
        try:
            self._check_output_tree()
            artifact = prepare_download_artifact(
                task_id,
                paths,
                max_output_bytes=self._max_output_bytes,
            )
            self._validate_artifact(artifact)
            self._heartbeat_resources()
            download_url = f"/api/tasks/{task_id}/download"
            task = self._state.get_task(task_id)
            if task is not None and task.owner_id is not None:
                HostedStorage(
                    Path(self._state.data_dir),
                    max_upload_bytes=1,
                ).touch_session(task.owner_id)
            self._heartbeat_resources()
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
            if self._before_success_publish is not None:
                self._before_success_publish()
        except Exception as exc:
            if artifact is not None:
                artifact.unlink(missing_ok=True)
            if archive_candidate is not None:
                archive_candidate.unlink(missing_ok=True)
            self._discard_partial_output()
            output_limit_error = _find_output_limit_error(exc)
            self._mark_failed(
                task_id,
                str(output_limit_error or exc),
            )
            return
        try:
            completed, transition = self._state._transition_finished(
                task_id,
                status=TaskStatus.SUCCEEDED,
                output_path=download_url,
                result_path=str(artifact),
                terminal_event=terminal_event,
            )
        except Exception as exc:
            authoritative = self._state.get_task(task_id)
            if (
                authoritative is not None
                and authoritative.status is TaskStatus.SUCCEEDED
            ):
                if authoritative.result_path != str(artifact):
                    artifact.unlink(missing_ok=True)
                return
            artifact.unlink(missing_ok=True)
            if archive_candidate is not None:
                archive_candidate.unlink(missing_ok=True)
            self._discard_partial_output()
            self._mark_failed(task_id, str(exc))
            return
        if transition is not TerminalTransitionResult.APPLIED:
            if completed.result_path != str(artifact):
                artifact.unlink(missing_ok=True)
            return

    def _heartbeat_resources(self) -> None:
        self._raise_if_resource_failed()
        if self._resource_heartbeat is not None:
            self._resource_heartbeat()
        self._raise_if_resource_failed()

    def _raise_if_resource_failed(self) -> None:
        if self._resource_health_check is not None:
            self._resource_health_check()

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
        refresh: Callable[[], None],
        raise_if_failed: Callable[[], None],
        refresh_interval: float = 0.25,
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be positive")
        super().__init__(root, max_bytes)
        self._refresh = refresh
        self._raise_if_failed = raise_if_failed
        self._refresh_interval = refresh_interval
        self._last_refresh_at = float("-inf")
        self._refresh_lock = Lock()

    def check_health(self) -> None:
        self._raise_if_failed()

    def refresh_health(self, *, force: bool = False) -> None:
        self._raise_if_failed()
        with self._refresh_lock:
            now = monotonic()
            if not force and now - self._last_refresh_at < self._refresh_interval:
                return
            self._last_refresh_at = now
            self._refresh()
        self._raise_if_failed()


class _PeriodicResourceHeartbeat:
    def __init__(
        self,
        heartbeat: Callable[[], None],
        interval: float,
        stop_timeout: float = DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._heartbeat = heartbeat
        self._interval = interval
        self._stop_timeout = stop_timeout
        self._stopped = Event()
        self._failure: BaseException | None = None
        self._thread = Thread(target=self._run, daemon=True)
        self._started = False

    def start(self) -> None:
        self._heartbeat()
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        if self._started:
            try:
                stop_heartbeat_thread(
                    self._stopped,
                    self._thread,
                    timeout=self._stop_timeout,
                    heartbeat_name="Worker resource heartbeat",
                )
            except HeartbeatStopTimeoutError as exc:
                self._failure = exc
                raise
        else:
            self._stopped.set()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                self._heartbeat()
            except BaseException as exc:
                self._failure = exc
                return


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
        heartbeat_stop_timeout_seconds=settings.heartbeat_stop_timeout_seconds,
        release_marker_ttl=max(
            settings.retention_seconds * 2,
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
    redis_connection = Redis.from_url(
        settings.redis_url,
        **redis_connection_options(settings),
    )
    resources = _build_resource_coordinator(settings, redis_connection)
    pin_released = False

    def record_pin_release(released_task_id: str) -> None:
        nonlocal pin_released
        if released_task_id == task_id:
            pin_released = True

    state = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=settings.data_dir,
        max_queued_tasks=settings.max_queued_tasks,
        retention_seconds=settings.retention_seconds,
        task_timeout_seconds=settings.task_timeout_seconds,
        resource_coordinator=resources,
        heartbeat_stop_timeout_seconds=settings.heartbeat_stop_timeout_seconds,
        on_task_pin_released=record_pin_release,
    )
    task = state.get_task(task_id)
    if task is None:
        return
    if task.owner_id is None:
        state._finalize_external_failure(task_id, "Task has no session owner")
        return
    _running, transition = state.mark_running(task_id)
    if transition is not RunningTransitionResult.APPLIED:
        return
    lease: RedisSessionLease | None = None
    output: RedisReservation | None = None
    periodic_heartbeat: _PeriodicResourceHeartbeat | None = None
    heartbeat_stopped = False

    def heartbeat() -> None:
        if output is not None:
            output.refresh()
        if lease is not None:
            lease.refresh()
        state.record_heartbeat(task_id)

    def raise_if_heartbeat_failed() -> None:
        if periodic_heartbeat is not None:
            periodic_heartbeat.raise_if_failed()

    def checked_heartbeat() -> None:
        raise_if_heartbeat_failed()
        heartbeat()
        raise_if_heartbeat_failed()

    def stop_heartbeat_before_success() -> None:
        nonlocal heartbeat_stopped
        if periodic_heartbeat is None:
            return
        if not heartbeat_stopped:
            periodic_heartbeat.stop()
            heartbeat_stopped = True
        periodic_heartbeat.raise_if_failed()

    executor = RedisTaskExecutor(
        state,
        max_output_bytes=settings.max_output_bytes,
        resource_heartbeat=heartbeat,
        resource_health_check=raise_if_heartbeat_failed,
        before_success_publish=stop_heartbeat_before_success,
        task_already_running=True,
    )
    try:
        lease = resources.acquire_session(task.owner_id, task_id=task_id)
        periodic_heartbeat = _PeriodicResourceHeartbeat(
            heartbeat,
            min(
                resources.session_lease_heartbeat_interval,
                getattr(
                    resources,
                    "reservation_heartbeat_interval",
                    resources.session_lease_heartbeat_interval,
                ),
            ),
            getattr(
                resources,
                "heartbeat_stop_timeout_seconds",
                DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
            ),
        )
        periodic_heartbeat.start()
        output = resources.reserve_output(
            task_id,
            task.owner_id,
            settings.max_output_bytes,
            free_bytes=disk_usage(settings.data_dir).free,
            min_free_bytes=settings.min_free_bytes,
        )
        checked_heartbeat()
        request = _task_request(job_kind, payload)
        executor.execute_reserved_task(task, request)
        periodic_heartbeat.raise_if_failed()
    except Exception as exc:
        authoritative = state.get_task(task_id)
        if authoritative is None or authoritative.status is not TaskStatus.SUCCEEDED:
            executor._discard_partial_output()
            state._finalize_external_failure(task_id, str(exc))
    finally:
        cleanup_steps: list[tuple[str, Callable[[], object]]] = []
        if periodic_heartbeat is not None and not heartbeat_stopped:
            cleanup_steps.append(("heartbeat stop", periodic_heartbeat.stop))
        if output is not None:
            cleanup_steps.append(("output release", output.release))
        if lease is not None:
            cleanup_steps.append(("lease release", lease.release))
        if not pin_released and task.has_canonical_asset_identities:
            cleanup_steps.append(
                (
                    "task pin release",
                    lambda: resources.release_task_pin(
                        task_id,
                        expected_session_id=task.owner_id,
                        expected_asset_identities=task.asset_identities,
                    ),
                )
            )
        elif not pin_released:
            cleanup_steps.append(
                ("legacy task pin release", lambda: state.release_task_pin(task_id))
            )
        _run_cleanup_steps(cleanup_steps)


def _run_cleanup_steps(
    steps: list[tuple[str, Callable[[], object]]],
) -> None:
    first_error: BaseException | None = None
    for name, cleanup in steps:
        try:
            cleanup()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            else:
                first_error.add_note(f"{name} also failed: {exc}")
    if first_error is not None:
        raise first_error


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
