from __future__ import annotations

import json
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from queue import Empty

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from PIL import Image

from designtoolkit.chroma_keyer.despill import despill
from designtoolkit.chroma_keyer.image_io import make_preview_png, read_rgb_image
from designtoolkit.chroma_keyer.matte import generate_alpha, refine_alpha
from designtoolkit.chroma_keyer.models import KeyerError, KeyerParams
from designtoolkit.chroma_keyer.sampling import (
    map_preview_point,
    screen_model_from_samples,
)
from designtoolkit.media_resize import MediaResizeError
from designtoolkit.video_frames import VideoFrameError
from designtoolkit.web.chroma_previews import ChromaPreviewStore
from designtoolkit.web.local_paths import resolve_dropped_file_paths
from designtoolkit.web.native_picker import (
    LocalPathPicker,
    LocalPathPickerError,
    TkinterLocalPathPicker,
)
from designtoolkit.web.schemas import (
    ChromaImageInspectRequest,
    ChromaImageInspectResponse,
    ChromaPreviewRequest,
    ChromaPreviewResponse,
    ChromaSampleRequest,
    ChromaSampleResponse,
    ChromaViewMode,
    DroppedFilePathResolveRequest,
    DroppedFilePathResolveResponse,
    ErrorResponse,
    LocalDirectoryPickRequest,
    LocalFilePickRequest,
    LocalPathPickResponse,
    LocalSaveFilePickRequest,
    LogEventResponse,
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskCreateResponse,
    TaskResponse,
    TaskStatus,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskConflictError, TaskManager

_INVALID_REQUEST_ERRORS = (ValueError, VideoFrameError, MediaResizeError)
_TERMINAL_STATUSES = (TaskStatus.SUCCEEDED, TaskStatus.FAILED)
_TERMINAL_LEVELS = (LogLevel.SUCCESS, LogLevel.ERROR)
_TERMINAL_PHASES = (LogPhase.COMPLETED, LogPhase.FAILED)


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error={"code": exc.code, "message": exc.message},
        ).model_dump(mode="json"),
    )


def _chroma_preview_url(preview_id: str) -> str:
    return f"/api/chroma-keyer/previews/{preview_id}"


def _sse_line(event) -> str:
    payload = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"data: {payload}\n\n"


def _is_terminal_event(event: LogEventResponse) -> bool:
    return event.level in _TERMINAL_LEVELS or event.phase in _TERMINAL_PHASES


def create_router(
    task_manager: TaskManager,
    *,
    local_path_roots: list[Path] | None = None,
    local_path_picker: LocalPathPicker | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    picker = local_path_picker or TkinterLocalPathPicker()
    chroma_preview_store = ChromaPreviewStore()

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.post(
        "/local-paths/resolve-dropped-files",
        response_model=DroppedFilePathResolveResponse,
    )
    def resolve_dropped_files(
        request: DroppedFilePathResolveRequest,
    ) -> DroppedFilePathResolveResponse:
        paths, unresolved = resolve_dropped_file_paths(
            request.names,
            roots=local_path_roots,
        )
        return DroppedFilePathResolveResponse(
            paths=[str(path) for path in paths],
            unresolved=unresolved,
        )

    @router.post("/local-paths/pick-files", response_model=LocalPathPickResponse)
    def pick_local_files(request: LocalFilePickRequest) -> LocalPathPickResponse:
        try:
            paths = picker.pick_files(
                title=request.title,
                multiple=request.multiple,
                file_kind=request.file_kind,
            )
        except LocalPathPickerError as exc:
            raise ApiError("local_picker_unavailable", str(exc), 503) from exc
        return LocalPathPickResponse(
            paths=[str(path) for path in paths],
            cancelled=len(paths) == 0,
        )

    @router.post(
        "/local-paths/pick-directory",
        response_model=LocalPathPickResponse,
    )
    def pick_local_directory(
        request: LocalDirectoryPickRequest,
    ) -> LocalPathPickResponse:
        try:
            paths = picker.pick_directory(title=request.title)
        except LocalPathPickerError as exc:
            raise ApiError("local_picker_unavailable", str(exc), 503) from exc
        return LocalPathPickResponse(
            paths=[str(path) for path in paths],
            cancelled=len(paths) == 0,
        )

    @router.post("/local-paths/pick-save-file", response_model=LocalPathPickResponse)
    def pick_local_save_file(
        request: LocalSaveFilePickRequest,
    ) -> LocalPathPickResponse:
        try:
            paths = picker.pick_save_file(
                title=request.title,
                file_kind=request.file_kind,
            )
        except LocalPathPickerError as exc:
            raise ApiError("local_picker_unavailable", str(exc), 503) from exc
        return LocalPathPickResponse(
            paths=[str(path) for path in paths],
            cancelled=len(paths) == 0,
        )

    @router.post("/tasks/video-frames", response_model=TaskCreateResponse)
    def create_video_frames(request: VideoFramesTaskRequest) -> TaskCreateResponse:
        try:
            task = task_manager.create_video_frames_task(request)
        except TaskConflictError as exc:
            raise ApiError("task_conflict", str(exc), 409) from exc
        except _INVALID_REQUEST_ERRORS as exc:
            raise ApiError("invalid_request", str(exc), 400) from exc
        return TaskCreateResponse(task_id=task.task_id, status=task.status)

    @router.post("/tasks/media-resize", response_model=TaskCreateResponse)
    def create_media_resize(request: MediaResizeTaskRequest) -> TaskCreateResponse:
        try:
            task = task_manager.create_media_resize_task(request)
        except TaskConflictError as exc:
            raise ApiError("task_conflict", str(exc), 409) from exc
        except _INVALID_REQUEST_ERRORS as exc:
            raise ApiError("invalid_request", str(exc), 400) from exc
        return TaskCreateResponse(task_id=task.task_id, status=task.status)

    @router.post(
        "/chroma-keyer/images/inspect",
        response_model=ChromaImageInspectResponse,
    )
    def inspect_chroma_image(
        request: ChromaImageInspectRequest,
    ) -> ChromaImageInspectResponse:
        try:
            rgb = read_rgb_image(Path(request.input_path))
            preview_png = make_preview_png(rgb)
        except KeyerError as exc:
            raise ApiError("invalid_request", str(exc), 400) from exc

        preview_id = chroma_preview_store.put(preview_png)
        height, width = rgb.shape[:2]
        with Image.open(BytesIO(preview_png)) as preview_image:
            preview_width, preview_height = preview_image.size
        return ChromaImageInspectResponse(
            input_path=request.input_path,
            width=int(width),
            height=int(height),
            preview_width=int(preview_width),
            preview_height=int(preview_height),
            preview_url=_chroma_preview_url(preview_id),
        )

    @router.post(
        "/chroma-keyer/images/sample",
        response_model=ChromaSampleResponse,
    )
    def sample_chroma_image(request: ChromaSampleRequest) -> ChromaSampleResponse:
        try:
            rgb = read_rgb_image(Path(request.input_path))
            height, width = rgb.shape[:2]
            source_x, source_y = map_preview_point(
                request.x,
                request.y,
                request.preview_width,
                request.preview_height,
                width,
                height,
            )
        except KeyerError as exc:
            raise ApiError("invalid_request", str(exc), 400) from exc

        return ChromaSampleResponse(
            rgb=[int(channel) for channel in rgb[source_y, source_x]],
            source_x=source_x,
            source_y=source_y,
        )

    @router.post("/chroma-keyer/previews", response_model=ChromaPreviewResponse)
    def create_chroma_preview(
        request: ChromaPreviewRequest,
    ) -> ChromaPreviewResponse:
        try:
            rgb = read_rgb_image(Path(request.input_path))
            model = screen_model_from_samples(request.samples)
            params = KeyerParams(**request.params.model_dump())
            alpha = refine_alpha(generate_alpha(rgb, model, params), params)
            if request.view_mode is ChromaViewMode.FINAL:
                preview_rgb = despill(rgb, alpha, model, params.despill_amount)
            else:
                preview_rgb = np.repeat(alpha[..., np.newaxis], 3, axis=2)
            preview_png = make_preview_png(preview_rgb)
        except KeyerError as exc:
            raise ApiError("invalid_request", str(exc), 400) from exc

        preview_id = chroma_preview_store.put(preview_png)
        return ChromaPreviewResponse(preview_url=_chroma_preview_url(preview_id))

    @router.get("/chroma-keyer/previews/{preview_id}")
    def get_chroma_preview(preview_id: str) -> Response:
        preview_png = chroma_preview_store.get(preview_id)
        if preview_png is None:
            raise ApiError("preview_not_found", "Preview not found", 404)
        return Response(content=preview_png, media_type="image/png")

    @router.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        task = task_manager.get_task(task_id)
        if task is None:
            raise ApiError("task_not_found", f"Task not found: {task_id}", 404)
        return task.to_response()

    @router.get("/tasks/{task_id}/events")
    def stream_task_events(task_id: str) -> StreamingResponse:
        task = task_manager.get_task(task_id)
        if task is None:
            raise ApiError("task_not_found", f"Task not found: {task_id}", 404)

        def event_stream() -> Iterator[str]:
            history, subscriber = task_manager.log_store.history_and_subscribe(task_id)
            try:
                for event in history:
                    yield _sse_line(event)
                    if _is_terminal_event(event):
                        return

                while True:
                    try:
                        event = subscriber.get(timeout=0.25)
                    except Empty:
                        latest_task = task_manager.get_task(task_id)
                        if (
                            latest_task is None
                            or latest_task.status in _TERMINAL_STATUSES
                        ):
                            return
                        continue

                    yield _sse_line(event)
                    if _is_terminal_event(event):
                        return
            finally:
                task_manager.log_store.unsubscribe(task_id, subscriber)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @router.get("/{api_path:path}")
    def api_not_found(request: Request, api_path: str) -> None:
        raise ApiError(
            "api_not_found",
            f"API route not found: {request.url.path}",
            404,
        )

    return router
