from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from queue import Empty
from stat import S_ISREG

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from PIL import Image

from designtoolkit.chroma_keyer.core import key_rgb
from designtoolkit.chroma_keyer.image_io import (
    make_preview_png,
    read_rgb_image,
    read_rgb_image_bytes,
    resize_preview_rgb,
)
from designtoolkit.chroma_keyer.models import KeyerError, KeyerParams
from designtoolkit.chroma_keyer.sampling import (
    map_preview_point,
    screen_model_from_samples,
)
from designtoolkit.chroma_keyer.video_io import ImageioVideoDecoder
from designtoolkit.chroma_keyer.video_models import (
    VideoDecoder,
    VideoMetadata,
    clamp_timeline_timestamp,
    validate_timestamp,
)
from designtoolkit.media_resize import MediaResizeError
from designtoolkit.video_frames import VideoFrameError
from designtoolkit.web.chroma_previews import ChromaPreviewEntry, ChromaPreviewStore
from designtoolkit.web.local_paths import resolve_dropped_file_paths
from designtoolkit.web.native_picker import (
    LocalPathPicker,
    LocalPathPickerError,
    TkinterLocalPathPicker,
)
from designtoolkit.web.schemas import (
    ChromaImageInspectRequest,
    ChromaImageInspectResponse,
    ChromaKeyerTaskRequest,
    ChromaPreviewRequest,
    ChromaPreviewResponse,
    ChromaSampleRequest,
    ChromaSampleResponse,
    ChromaVideoFrameRequest,
    ChromaVideoFrameResponse,
    ChromaVideoInspectRequest,
    ChromaVideoInspectResponse,
    ChromaVideoPreviewRequest,
    ChromaVideoPreviewResponse,
    ChromaVideoSampleRequest,
    ChromaVideoSampleResponse,
    ChromaViewMode,
    DroppedFilePathResolveRequest,
    DroppedFilePathResolveResponse,
    ErrorResponse,
    LocalDirectoryPickRequest,
    LocalFilePickRequest,
    LocalPathPickResponse,
    LocalSaveFilePickRequest,
    LogEventResponse,
    LogPhase,
    MediaResizeTaskRequest,
    TaskCreateResponse,
    TaskResponse,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskConflictError, TaskManager

_INVALID_REQUEST_ERRORS = (ValueError, VideoFrameError, MediaResizeError)
_TERMINAL_PHASES = (LogPhase.COMPLETED, LogPhase.FAILED)
_CHROMA_SNAPSHOT_READ_ATTEMPTS = 3
_CHROMA_VIDEO_HASH_CHUNK_SIZE = 1024 * 1024
_KEYER_ERROR_CODES = {
    "Input image could not be read": "invalid_image",
    "samples must not be empty": "missing_samples",
    "请选择具有明显主色的纯色幕背景": "invalid_screen_color",
    "Output path already exists": "output_exists",
}


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


def _chroma_keyer_error_response(exc: KeyerError) -> ApiError:
    code = _KEYER_ERROR_CODES.get(str(exc), "invalid_request")
    return ApiError(
        code,
        str(exc),
        409 if code == "output_exists" else 400,
    )


def _chroma_video_error_response(exc: KeyerError) -> ApiError:
    if str(exc).startswith("timestamp"):
        return ApiError("invalid_timestamp", str(exc), 400)
    mapped_error = _chroma_keyer_error_response(exc)
    if mapped_error.code == "invalid_request":
        mapped_error.code = "invalid_video"
    return mapped_error


def _chroma_preview_url(preview_id: str) -> str:
    return f"/api/chroma-keyer/previews/{preview_id}"


def _source_stat(
    input_path: Path,
    *,
    read_error_message: str = "Input image could not be read",
):
    try:
        source_stat = input_path.stat()
    except FileNotFoundError as exc:
        raise KeyerError("Input path does not exist") from exc
    except OSError as exc:
        raise KeyerError(read_error_message) from exc
    if not S_ISREG(source_stat.st_mode):
        raise KeyerError("Input path must be a file")
    return source_stat


def _source_version(source_stat) -> tuple[int, int]:
    return source_stat.st_mtime_ns, source_stat.st_size


def _read_rgb_snapshot(input_path: Path) -> tuple[np.ndarray, dict[str, str | int]]:
    """Read RGB pixels and a fingerprint from one immutable source-byte snapshot."""
    try:
        resolved_path = input_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise KeyerError("Input path does not exist") from exc
    except OSError as exc:
        raise KeyerError("Input image could not be read") from exc

    for _ in range(_CHROMA_SNAPSHOT_READ_ATTEMPTS):
        before_decode = _source_stat(resolved_path)
        try:
            try:
                source_bytes = resolved_path.read_bytes()
            except FileNotFoundError as exc:
                raise KeyerError("Input path does not exist") from exc
            except OSError as exc:
                raise KeyerError("Input image could not be read") from exc
            rgb = read_rgb_image_bytes(source_bytes, resolved_path.suffix)
        except KeyerError:
            if _source_version(before_decode) != _source_version(
                _source_stat(resolved_path)
            ):
                continue
            raise
        after_decode = _source_stat(resolved_path)
        if _source_version(before_decode) == _source_version(after_decode):
            return rgb, {
                "path": str(resolved_path),
                "mtime_ns": after_decode.st_mtime_ns,
                "size": len(source_bytes),
                "content_sha256": sha256(source_bytes).hexdigest(),
            }
    raise KeyerError("Input image changed while it was being read")


def _hash_file_chunks(input_path: Path) -> tuple[int, str]:
    content_hash = sha256()
    size = 0
    with input_path.open("rb") as source_file:
        while chunk := source_file.read(_CHROMA_VIDEO_HASH_CHUNK_SIZE):
            content_hash.update(chunk)
            size += len(chunk)
    return size, content_hash.hexdigest()


def _video_source_fingerprint(input_path: Path) -> dict[str, str | int]:
    try:
        resolved_path = input_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise KeyerError("Input path does not exist") from exc
    except OSError as exc:
        raise KeyerError("Video could not be opened") from exc

    for _ in range(_CHROMA_SNAPSHOT_READ_ATTEMPTS):
        before_read = _source_stat(
            resolved_path,
            read_error_message="Video could not be opened",
        )
        try:
            size, content_sha256 = _hash_file_chunks(resolved_path)
        except FileNotFoundError as exc:
            raise KeyerError("Input path does not exist") from exc
        except OSError as exc:
            raise KeyerError("Video could not be opened") from exc
        after_read = _source_stat(
            resolved_path,
            read_error_message="Video could not be opened",
        )
        if _source_version(before_read) == _source_version(after_read):
            return {
                "path": str(resolved_path),
                "mtime_ns": after_read.st_mtime_ns,
                "size": size,
                "content_sha256": content_sha256,
            }
    raise KeyerError("Input video changed while it was being read")


def _chroma_preview_cache_key(
    preview_kind: str,
    source_fingerprint: dict[str, str | int],
    *,
    samples: list[list[int]] | None = None,
    params: dict[str, float | int] | None = None,
    view_mode: ChromaViewMode | None = None,
    timestamp_seconds: float | None = None,
) -> str:
    request_state: dict[str, object] = {
        "preview_kind": preview_kind,
        "source": source_fingerprint,
    }
    if samples is not None:
        request_state["samples"] = samples
    if params is not None:
        request_state["params"] = params
    if view_mode is not None:
        request_state["view_mode"] = view_mode.value
    if timestamp_seconds is not None:
        request_state["timestamp_seconds"] = timestamp_seconds
    serialized_state = json.dumps(
        request_state,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(serialized_state.encode("utf-8")).hexdigest()


class _VideoSourceChangedError(Exception):
    pass


def _resolve_video_timestamp(
    requested_timestamp: float,
    metadata: VideoMetadata,
) -> float:
    return clamp_timeline_timestamp(
        validate_timestamp(requested_timestamp, metadata),
        metadata,
    )


def _read_stable_video_frame(
    input_path: Path,
    decoder: VideoDecoder,
    requested_timestamp: float,
    read_frame: Callable[[Path, float], np.ndarray],
) -> tuple[VideoMetadata, float, np.ndarray]:
    for _ in range(_CHROMA_SNAPSHOT_READ_ATTEMPTS):
        source_fingerprint = _video_source_fingerprint(input_path)
        try:
            metadata = decoder.probe(input_path)
            timestamp_seconds = _resolve_video_timestamp(
                requested_timestamp,
                metadata,
            )
            rgb = read_frame(input_path, timestamp_seconds)
        except KeyerError:
            if _video_source_fingerprint(input_path) != source_fingerprint:
                continue
            raise
        if _video_source_fingerprint(input_path) == source_fingerprint:
            return metadata, timestamp_seconds, rgb
    raise KeyerError("Input video changed while it was being read")


def _get_stable_video_preview_entry(
    store: ChromaPreviewStore,
    input_path: Path,
    preview_kind: str,
    decoder: VideoDecoder,
    requested_timestamp: float,
    read_frame: Callable[[Path, float], np.ndarray],
    create_png_bytes: Callable[[np.ndarray], bytes],
    *,
    samples: list[list[int]] | None = None,
    params: dict[str, float | int] | None = None,
    view_mode: ChromaViewMode | None = None,
) -> tuple[VideoMetadata, float, ChromaPreviewEntry]:
    for _ in range(_CHROMA_SNAPSHOT_READ_ATTEMPTS):
        source_fingerprint = _video_source_fingerprint(input_path)
        try:
            metadata = decoder.probe(input_path)
            timestamp_seconds = _resolve_video_timestamp(
                requested_timestamp,
                metadata,
            )
            cache_key = _chroma_preview_cache_key(
                preview_kind,
                source_fingerprint,
                samples=samples,
                params=params,
                view_mode=view_mode,
                timestamp_seconds=timestamp_seconds,
            )
            generated = False

            def create_stable_preview(
                expected_timestamp: float = timestamp_seconds,
                expected_fingerprint: dict[str, str | int] = source_fingerprint,
            ) -> bytes:
                nonlocal generated
                generated = True
                rgb = read_frame(input_path, expected_timestamp)
                png_bytes = create_png_bytes(rgb)
                if _video_source_fingerprint(input_path) != expected_fingerprint:
                    raise _VideoSourceChangedError
                return png_bytes

            preview_entry = store.get_or_put_entry(
                cache_key,
                create_stable_preview,
            )
        except _VideoSourceChangedError:
            continue
        except KeyerError:
            if _video_source_fingerprint(input_path) != source_fingerprint:
                continue
            raise
        if (
            generated
            or _video_source_fingerprint(input_path) == source_fingerprint
        ):
            return metadata, timestamp_seconds, preview_entry
    raise KeyerError("Input video changed while it was being read")


def _sse_line(event) -> str:
    payload = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"data: {payload}\n\n"


def _is_terminal_event(event: LogEventResponse) -> bool:
    return event.phase in _TERMINAL_PHASES


def create_router(
    task_manager: TaskManager,
    *,
    local_path_roots: list[Path] | None = None,
    local_path_picker: LocalPathPicker | None = None,
    video_decoder: VideoDecoder | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    picker = local_path_picker or TkinterLocalPathPicker()
    decoder = video_decoder if video_decoder is not None else ImageioVideoDecoder()
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

    @router.post("/tasks/chroma-keyer", response_model=TaskCreateResponse)
    def create_chroma_keyer(request: ChromaKeyerTaskRequest) -> TaskCreateResponse:
        try:
            task = task_manager.create_chroma_keyer_task(request)
        except TaskConflictError as exc:
            raise ApiError("task_conflict", str(exc), 409) from exc
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc
        return TaskCreateResponse(task_id=task.task_id, status=task.status)

    @router.post(
        "/chroma-keyer/images/inspect",
        response_model=ChromaImageInspectResponse,
    )
    def inspect_chroma_image(
        request: ChromaImageInspectRequest,
    ) -> ChromaImageInspectResponse:
        try:
            input_path = Path(request.input_path)
            rgb, source_fingerprint = _read_rgb_snapshot(input_path)
            cache_key = _chroma_preview_cache_key(
                "inspect",
                source_fingerprint,
            )
            preview_entry = chroma_preview_store.get_or_put_entry(
                cache_key,
                lambda: make_preview_png(rgb),
            )
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc

        height, width = rgb.shape[:2]
        with Image.open(BytesIO(preview_entry.png_bytes)) as preview_image:
            preview_width, preview_height = preview_image.size
        return ChromaImageInspectResponse(
            input_path=request.input_path,
            width=int(width),
            height=int(height),
            preview_width=int(preview_width),
            preview_height=int(preview_height),
            preview_url=_chroma_preview_url(preview_entry.preview_id),
        )

    @router.post(
        "/chroma-keyer/videos/inspect",
        response_model=ChromaVideoInspectResponse,
    )
    def inspect_chroma_video(
        request: ChromaVideoInspectRequest,
    ) -> ChromaVideoInspectResponse:
        try:
            input_path = Path(request.input_path)
            metadata, timestamp_seconds, preview_entry = (
                _get_stable_video_preview_entry(
                    chroma_preview_store,
                    input_path,
                    "video_inspect",
                    decoder,
                    0.0,
                    decoder.read_preview_frame,
                    make_preview_png,
                )
            )
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc

        with Image.open(BytesIO(preview_entry.png_bytes)) as preview_image:
            preview_width, preview_height = preview_image.size
        return ChromaVideoInspectResponse(
            input_path=request.input_path,
            width=metadata.width,
            height=metadata.height,
            preview_width=preview_width,
            preview_height=preview_height,
            preview_url=_chroma_preview_url(preview_entry.preview_id),
            duration_seconds=metadata.duration_seconds,
            fps=metadata.fps,
            estimated_frame_count=metadata.estimated_frame_count,
            timestamp_seconds=timestamp_seconds,
        )

    @router.post(
        "/chroma-keyer/videos/frame",
        response_model=ChromaVideoFrameResponse,
    )
    def create_chroma_video_frame(
        request: ChromaVideoFrameRequest,
    ) -> ChromaVideoFrameResponse:
        try:
            input_path = Path(request.input_path)
            _metadata, timestamp_seconds, preview_entry = (
                _get_stable_video_preview_entry(
                    chroma_preview_store,
                    input_path,
                    "video_frame",
                    decoder,
                    request.timestamp_seconds,
                    decoder.read_preview_frame,
                    make_preview_png,
                )
            )
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc

        return ChromaVideoFrameResponse(
            preview_url=_chroma_preview_url(preview_entry.preview_id),
            timestamp_seconds=timestamp_seconds,
        )

    @router.post(
        "/chroma-keyer/videos/sample",
        response_model=ChromaVideoSampleResponse,
    )
    def sample_chroma_video(
        request: ChromaVideoSampleRequest,
    ) -> ChromaVideoSampleResponse:
        try:
            input_path = Path(request.input_path)
            metadata, timestamp_seconds, rgb = _read_stable_video_frame(
                input_path,
                decoder,
                request.timestamp_seconds,
                decoder.read_frame,
            )
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc

        try:
            source_x, source_y = map_preview_point(
                request.x,
                request.y,
                request.preview_width,
                request.preview_height,
                metadata.width,
                metadata.height,
            )
            frame_height, frame_width = rgb.shape[:2]
            frame_x = min(source_x * frame_width // metadata.width, frame_width - 1)
            frame_y = min(source_y * frame_height // metadata.height, frame_height - 1)
        except (KeyerError, ValueError) as exc:
            raise ApiError("invalid_sample_coordinates", str(exc), 400) from exc

        sampled_rgb = [int(channel) for channel in rgb[frame_y, frame_x]]
        try:
            screen_model_from_samples([sampled_rgb])
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc
        return ChromaVideoSampleResponse(
            rgb=sampled_rgb,
            hex=f"#{sampled_rgb[0]:02X}{sampled_rgb[1]:02X}{sampled_rgb[2]:02X}",
            source_x=source_x,
            source_y=source_y,
            timestamp_seconds=timestamp_seconds,
        )

    @router.post(
        "/chroma-keyer/videos/preview",
        response_model=ChromaVideoPreviewResponse,
    )
    def create_chroma_video_preview(
        request: ChromaVideoPreviewRequest,
    ) -> ChromaVideoPreviewResponse:
        try:
            input_path = Path(request.input_path)
            model = screen_model_from_samples(request.samples)
            params_data = request.params.model_dump(mode="json")

            def create_preview_png(rgb: np.ndarray) -> bytes:
                params = KeyerParams(**params_data)
                preview_rgb, alpha = key_rgb(rgb, model, params)
                if request.view_mode is ChromaViewMode.FINAL:
                    return make_preview_png(preview_rgb, alpha=alpha)
                alpha_rgb = np.repeat(alpha[..., np.newaxis], 3, axis=2)
                return make_preview_png(alpha_rgb)

            _metadata, timestamp_seconds, preview_entry = (
                _get_stable_video_preview_entry(
                    chroma_preview_store,
                    input_path,
                    "video_generated",
                    decoder,
                    request.timestamp_seconds,
                    decoder.read_preview_frame,
                    create_preview_png,
                    samples=request.samples,
                    params=params_data,
                    view_mode=request.view_mode,
                )
            )
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc

        return ChromaVideoPreviewResponse(
            preview_url=_chroma_preview_url(preview_entry.preview_id),
            timestamp_seconds=timestamp_seconds,
        )

    @router.post(
        "/chroma-keyer/images/sample",
        response_model=ChromaSampleResponse,
    )
    def sample_chroma_image(request: ChromaSampleRequest) -> ChromaSampleResponse:
        try:
            rgb = read_rgb_image(Path(request.input_path))
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc

        try:
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
            raise ApiError("invalid_sample_coordinates", str(exc), 400) from exc

        sampled_rgb = [int(channel) for channel in rgb[source_y, source_x]]
        try:
            screen_model_from_samples([sampled_rgb])
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc
        return ChromaSampleResponse(
            rgb=sampled_rgb,
            hex=f"#{sampled_rgb[0]:02X}{sampled_rgb[1]:02X}{sampled_rgb[2]:02X}",
            source_x=source_x,
            source_y=source_y,
        )

    @router.post("/chroma-keyer/preview", response_model=ChromaPreviewResponse)
    @router.post("/chroma-keyer/previews", response_model=ChromaPreviewResponse)
    def create_chroma_preview(
        request: ChromaPreviewRequest,
    ) -> ChromaPreviewResponse:
        try:
            model = screen_model_from_samples(request.samples)
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc

        try:
            input_path = Path(request.input_path)
            rgb, source_fingerprint = _read_rgb_snapshot(input_path)
            params_data = request.params.model_dump(mode="json")
            cache_key = _chroma_preview_cache_key(
                "generated",
                source_fingerprint,
                samples=request.samples,
                params=params_data,
                view_mode=request.view_mode,
            )

            def create_preview_png() -> bytes:
                params = KeyerParams(**params_data)
                preview_rgb = resize_preview_rgb(rgb)
                preview_rgb, alpha = key_rgb(preview_rgb, model, params)
                if request.view_mode is ChromaViewMode.FINAL:
                    return make_preview_png(preview_rgb, alpha=alpha)
                else:
                    preview_rgb = np.repeat(alpha[..., np.newaxis], 3, axis=2)
                    return make_preview_png(preview_rgb)

            preview_id = chroma_preview_store.get_or_put(cache_key, create_preview_png)
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc

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
                terminal_event_seen = False
                for event in history:
                    yield _sse_line(event)
                    terminal_event_seen = _is_terminal_event(event)
                    if terminal_event_seen:
                        break

                while not terminal_event_seen:
                    try:
                        event = subscriber.get(timeout=0.25)
                    except Empty:
                        continue

                    yield _sse_line(event)
                    terminal_event_seen = _is_terminal_event(event)
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
