from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock, Thread
from time import perf_counter
from uuid import uuid4

from designtoolkit.media_resize import (
    MediaResizeOptions,
    MediaResizeResult,
    ResizeBackend,
    resize_media,
)
from designtoolkit.video_frames import (
    ExportResult,
    FrameExportBackend,
    VideoFrameOptions,
    export_video_frames,
)
from designtoolkit.web.adapters import (
    media_resize_options_list_from_request,
    video_frame_options_list_from_request,
)
from designtoolkit.web.logs import LogStore
from designtoolkit.web.schemas import (
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskKind,
    TaskResponse,
    TaskStatus,
    VideoFramesTaskRequest,
)

Exporter = Callable[[VideoFrameOptions, FrameExportBackend], ExportResult]
MediaResizer = Callable[[MediaResizeOptions, ResizeBackend], MediaResizeResult]
BackendFactory = Callable[[], FrameExportBackend | ResizeBackend]
Clock = Callable[[], float]


class TaskConflictError(RuntimeError):
    pass


@dataclass
class TaskRecord:
    task_id: str
    kind: TaskKind
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    input_path: str
    output_path: str | None
    error: str | None

    def to_response(self) -> TaskResponse:
        return TaskResponse(
            task_id=self.task_id,
            kind=self.kind,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            input_path=self.input_path,
            output_path=self.output_path,
            error=self.error,
        )


class TaskManager:
    def __init__(
        self,
        *,
        log_store: LogStore,
        exporter: Exporter | None = export_video_frames,
        media_resizer: MediaResizer | None = resize_media,
        backend_factory: BackendFactory | None = None,
        clock: Clock = perf_counter,
        run_async: bool = True,
    ) -> None:
        self.log_store = log_store
        self._exporter = exporter
        self._media_resizer = media_resizer
        self._backend_factory = backend_factory or self._default_backend
        self._clock = clock
        self._run_async = run_async
        self._records: dict[str, TaskRecord] = {}
        self._active_task_id: str | None = None
        self._lock = Lock()

    def reserve_task(
        self,
        *,
        kind: TaskKind,
        input_path: str,
        output_path: str | None,
    ) -> TaskRecord:
        with self._lock:
            if self._active_task_id is not None:
                raise TaskConflictError("A task is already running")
            task = TaskRecord(
                task_id=str(uuid4()),
                kind=kind,
                status=TaskStatus.QUEUED,
                created_at=datetime.now(UTC),
                started_at=None,
                finished_at=None,
                input_path=input_path,
                output_path=output_path,
                error=None,
            )
            self._records[task.task_id] = task
            self._active_task_id = task.task_id
        self.log_store.append(
            task_id=task.task_id,
            level=LogLevel.INFO,
            phase=LogPhase.CREATED,
            message=f"Created {kind.value} task",
            details={"input_path": input_path, "output_path": output_path},
        )
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._records.get(task_id)

    def mark_finished(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None = None,
        error: str | None = None,
    ) -> TaskRecord:
        with self._lock:
            task = self._records[task_id]
            task.status = status
            task.output_path = (
                output_path if output_path is not None else task.output_path
            )
            task.error = error
            task.finished_at = datetime.now(UTC)
            if self._active_task_id == task_id:
                self._active_task_id = None
            return task

    def create_video_frames_task(
        self,
        request: VideoFramesTaskRequest,
    ) -> TaskRecord:
        options_list = video_frame_options_list_from_request(request)
        task = self.reserve_task(
            kind=TaskKind.VIDEO_FRAMES,
            input_path="\n".join(str(options.input_path) for options in options_list),
            output_path=request.output_dir,
        )
        try:
            self._start(task, lambda: self._run_video_frames(task, options_list))
        except Exception as exc:
            self._mark_failed(task.task_id, str(exc))
        return self.get_task(task.task_id) or task

    def create_media_resize_task(
        self,
        request: MediaResizeTaskRequest,
    ) -> TaskRecord:
        options_list = media_resize_options_list_from_request(request)
        task = self.reserve_task(
            kind=TaskKind.MEDIA_RESIZE,
            input_path="\n".join(str(options.input_path) for options in options_list),
            output_path=request.output_path,
        )
        try:
            self._start(task, lambda: self._run_media_resize(task, options_list))
        except Exception as exc:
            self._mark_failed(task.task_id, str(exc))
        return self.get_task(task.task_id) or task

    def _start(self, task: TaskRecord, runner: Callable[[], None]) -> None:
        if self._run_async:
            Thread(target=runner, daemon=True, name=f"dt-web-{task.task_id}").start()
        else:
            runner()

    def _run_video_frames(
        self,
        task: TaskRecord,
        options_list: list[VideoFrameOptions],
    ) -> None:
        self._mark_running(task.task_id)
        task_started_at = self._clock()
        try:
            if self._exporter is None:
                raise RuntimeError("video frame exporter is not configured")
            results: list[ExportResult] = []
            for index, options in enumerate(options_list, start=1):
                if len(options_list) > 1:
                    self.log_store.append(
                        task_id=task.task_id,
                        level=LogLevel.PROGRESS,
                        phase=LogPhase.RUNNING,
                        message=(
                            f"Processing video {index}/{len(options_list)}: "
                            f"{options.input_path}"
                        ),
                        details={
                            "index": index,
                            "total": len(options_list),
                            "input_path": str(options.input_path),
                        },
                    )
                material_started_at = self._clock()
                result = self._exporter(options, self._backend_factory())
                material_elapsed = self._elapsed_seconds(material_started_at)
                results.append(result)
                self.log_store.append(
                    task_id=task.task_id,
                    level=LogLevel.SUCCESS,
                    phase=LogPhase.RUNNING,
                    message=(
                        f"Completed video {index}/{len(options_list)}: "
                        f"{options.input_path}"
                    ),
                    details={
                        "index": index,
                        "total": len(options_list),
                        "input_path": str(options.input_path),
                        "output_path": str(result.output_dir),
                        "elapsed_seconds": material_elapsed,
                    },
                )
        except Exception as exc:
            self._mark_failed(task.task_id, str(exc))
            return
        self._mark_succeeded(
            task.task_id,
            "; ".join(str(result.output_dir) for result in results),
            elapsed_seconds=self._elapsed_seconds(task_started_at),
        )

    def _run_media_resize(
        self,
        task: TaskRecord,
        options_list: list[MediaResizeOptions],
    ) -> None:
        self._mark_running(task.task_id)
        task_started_at = self._clock()
        try:
            if self._media_resizer is None:
                raise RuntimeError("media resizer is not configured")
            results: list[MediaResizeResult] = []
            for index, options in enumerate(options_list, start=1):
                if len(options_list) > 1:
                    self.log_store.append(
                        task_id=task.task_id,
                        level=LogLevel.PROGRESS,
                        phase=LogPhase.RUNNING,
                        message=(
                            f"Processing media {index}/{len(options_list)}: "
                            f"{options.input_path}"
                        ),
                        details={
                            "index": index,
                            "total": len(options_list),
                            "input_path": str(options.input_path),
                        },
                    )
                material_started_at = self._clock()
                result = self._media_resizer(options, self._backend_factory())
                material_elapsed = self._elapsed_seconds(material_started_at)
                results.append(result)
                message = (
                    f"Completed media resize {index}/{len(options_list)}: "
                    f"{options.input_path}"
                    if len(options_list) > 1
                    else f"Completed media resize: {options.input_path}"
                )
                self.log_store.append(
                    task_id=task.task_id,
                    level=LogLevel.SUCCESS,
                    phase=LogPhase.RUNNING,
                    message=message,
                    details={
                        "index": index,
                        "total": len(options_list),
                        "input_path": str(options.input_path),
                        "output_path": str(result.output_path),
                        "elapsed_seconds": material_elapsed,
                    },
                )
        except Exception as exc:
            self._mark_failed(task.task_id, str(exc))
            return
        self._mark_succeeded(
            task.task_id,
            "; ".join(str(result.output_path) for result in results),
            elapsed_seconds=self._elapsed_seconds(task_started_at),
        )

    def _mark_running(self, task_id: str) -> None:
        with self._lock:
            task = self._records[task_id]
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(UTC)
        self.log_store.append(
            task_id=task_id,
            level=LogLevel.PROGRESS,
            phase=LogPhase.RUNNING,
            message="Task is running",
        )

    def _mark_succeeded(
        self,
        task_id: str,
        output_path: str,
        *,
        elapsed_seconds: float | None = None,
    ) -> None:
        details: dict[str, object] = {"output_path": output_path}
        if elapsed_seconds is not None:
            details["elapsed_seconds"] = elapsed_seconds
        self.log_store.append(
            task_id=task_id,
            level=LogLevel.SUCCESS,
            phase=LogPhase.COMPLETED,
            message=f"Task completed: {output_path}",
            details=details,
        )
        self.mark_finished(
            task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=output_path,
        )

    def _elapsed_seconds(self, started_at: float) -> float:
        return round(self._clock() - started_at, 3)

    def _mark_failed(self, task_id: str, message: str) -> None:
        self.log_store.append(
            task_id=task_id,
            level=LogLevel.ERROR,
            phase=LogPhase.FAILED,
            message=message,
        )
        self.mark_finished(
            task_id,
            status=TaskStatus.FAILED,
            error=message,
        )

    @staticmethod
    def _default_backend() -> FrameExportBackend | ResizeBackend:
        from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

        return FfmpegBackend()
