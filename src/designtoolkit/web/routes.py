from __future__ import annotations

import json
import os
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from hashlib import sha256
from inspect import Parameter, signature
from io import BytesIO
from os import stat_result
from pathlib import Path
from queue import Empty
from stat import S_ISREG
from threading import Lock

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
    ChromaVideoKeyerTaskRequest,
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
_CHROMA_VIDEO_HASH_CACHE_SIZE = 32
_CHROMA_VIDEO_METADATA_CACHE_SIZE = 32
_KEYER_ERROR_CODES = {
    "Input image could not be read": "invalid_image",
    "samples must not be empty": "missing_samples",
    "请选择具有明显主色的纯色幕背景": "invalid_screen_color",
    "Output path already exists": "output_exists",
    "Final output directory already exists": "output_exists",
}


class _BoundedSingleFlightCache[CacheKey: Hashable, CacheValue]:
    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._values: OrderedDict[CacheKey, CacheValue] = OrderedDict()
        self._in_flight: dict[CacheKey, Future[CacheValue]] = {}
        self._lock = Lock()

    def get_or_compute(
        self,
        key: CacheKey,
        compute: Callable[[], CacheValue],
    ) -> CacheValue:
        with self._lock:
            try:
                value = self._values.pop(key)
            except KeyError:
                pass
            else:
                self._values[key] = value
                return value

            future = self._in_flight.get(key)
            if future is None:
                future = Future()
                self._in_flight[key] = future
                owns_computation = True
            else:
                owns_computation = False

        if not owns_computation:
            return future.result()

        try:
            value = compute()
        except BaseException as exc:
            with self._lock:
                if self._in_flight.get(key) is future:
                    del self._in_flight[key]
            future.set_exception(exc)
            raise

        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self._maxsize:
                self._values.popitem(last=False)
            if self._in_flight.get(key) is future:
                del self._in_flight[key]
        future.set_result(value)
        return value


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


def _windows_change_time_ns(input_path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("file_attributes", wintypes.DWORD),
        ]

    get_file_information = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).GetFileInformationByHandleEx
    get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_file_information.restype = wintypes.BOOL

    file_descriptor = os.open(input_path, os.O_RDONLY | os.O_BINARY)
    try:
        file_info = FileBasicInfo()
        if not get_file_information(
            msvcrt.get_osfhandle(file_descriptor),
            0,
            ctypes.byref(file_info),
            ctypes.sizeof(file_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(file_info.change_time) * 100
    finally:
        os.close(file_descriptor)


def _source_change_time_ns(
    input_path: Path,
    source_stat: stat_result,
) -> int:
    if os.name == "nt":
        return _windows_change_time_ns(input_path)
    return source_stat.st_ctime_ns


def _video_source_version(
    input_path: Path,
    source_stat: stat_result,
) -> tuple[int, int, int, int, int, int]:
    try:
        change_time_ns = _source_change_time_ns(input_path, source_stat)
    except FileNotFoundError as exc:
        raise KeyerError("Input path does not exist") from exc
    except OSError as exc:
        raise KeyerError("Video could not be opened") from exc
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_ctime_ns,
        change_time_ns,
        source_stat.st_mtime_ns,
        source_stat.st_size,
    )


_video_content_hash_cache = _BoundedSingleFlightCache[
    tuple[Path, tuple[int, int, int, int, int, int]],
    tuple[int, str],
](_CHROMA_VIDEO_HASH_CACHE_SIZE)


def _cached_video_content_hash(
    resolved_path: Path,
    source_version: tuple[int, int, int, int, int, int],
) -> tuple[int, str]:
    return _video_content_hash_cache.get_or_compute(
        (resolved_path, source_version),
        lambda: _hash_file_chunks(resolved_path),
    )


@dataclass(frozen=True)
class _VideoSourceSnapshot:
    fingerprint: dict[str, str | int]
    version: tuple[int, int, int, int, int, int]


def _video_source_snapshot(input_path: Path) -> _VideoSourceSnapshot:
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
        source_version = _video_source_version(resolved_path, before_read)
        try:
            size, content_sha256 = _cached_video_content_hash(
                resolved_path,
                source_version,
            )
        except FileNotFoundError as exc:
            raise KeyerError("Input path does not exist") from exc
        except OSError as exc:
            raise KeyerError("Video could not be opened") from exc
        after_read = _source_stat(
            resolved_path,
            read_error_message="Video could not be opened",
        )
        after_version = _video_source_version(resolved_path, after_read)
        if source_version == after_version:
            return _VideoSourceSnapshot(
                fingerprint={
                    "path": str(resolved_path),
                    "mtime_ns": after_read.st_mtime_ns,
                    "size": size,
                    "content_sha256": content_sha256,
                },
                version=after_version,
            )
    raise KeyerError("Input video changed while it was being read")


def _video_source_fingerprint(input_path: Path) -> dict[str, str | int]:
    return _video_source_snapshot(input_path).fingerprint


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


_PreprobedFrameReader = Callable[[Path, float, VideoMetadata], np.ndarray]


def _select_video_frame_reader(
    decoder: VideoDecoder,
    capability_name: str,
    legacy_reader: Callable[[Path, float], np.ndarray],
) -> _PreprobedFrameReader:
    capability_reader = getattr(decoder, capability_name, None)
    if callable(capability_reader):

        def read_with_metadata(
            path: Path,
            timestamp_seconds: float,
            metadata: VideoMetadata,
        ) -> np.ndarray:
            return capability_reader(
                path,
                timestamp_seconds,
                metadata=metadata,
            )

        return read_with_metadata

    def read_legacy(
        path: Path,
        timestamp_seconds: float,
        _metadata: VideoMetadata,
    ) -> np.ndarray:
        return legacy_reader(path, timestamp_seconds)

    return read_legacy


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
    requested_timestamp: float,
    get_metadata: Callable[
        [Path, _VideoSourceSnapshot],
        VideoMetadata,
    ],
    read_frame: _PreprobedFrameReader,
) -> tuple[VideoMetadata, float, np.ndarray]:
    for _ in range(_CHROMA_SNAPSHOT_READ_ATTEMPTS):
        source_snapshot = _video_source_snapshot(input_path)
        try:
            metadata = get_metadata(input_path, source_snapshot)
            timestamp_seconds = _resolve_video_timestamp(
                requested_timestamp,
                metadata,
            )
            rgb = read_frame(
                input_path,
                timestamp_seconds,
                metadata,
            )
        except KeyerError:
            if _video_source_snapshot(input_path) != source_snapshot:
                continue
            raise
        if _video_source_snapshot(input_path) == source_snapshot:
            return metadata, timestamp_seconds, rgb
    raise KeyerError("Input video changed while it was being read")


def _get_stable_video_preview_entry(
    store: ChromaPreviewStore,
    input_path: Path,
    preview_kind: str,
    requested_timestamp: float,
    get_metadata: Callable[
        [Path, _VideoSourceSnapshot],
        VideoMetadata,
    ],
    read_frame: _PreprobedFrameReader,
    create_png_bytes: Callable[[np.ndarray], bytes],
    *,
    samples: list[list[int]] | None = None,
    params: dict[str, float | int] | None = None,
    view_mode: ChromaViewMode | None = None,
) -> tuple[VideoMetadata, float, ChromaPreviewEntry]:
    for _ in range(_CHROMA_SNAPSHOT_READ_ATTEMPTS):
        source_snapshot = _video_source_snapshot(input_path)
        try:
            metadata = get_metadata(input_path, source_snapshot)
            timestamp_seconds = _resolve_video_timestamp(
                requested_timestamp,
                metadata,
            )
            cache_key = _chroma_preview_cache_key(
                preview_kind,
                source_snapshot.fingerprint,
                samples=samples,
                params=params,
                view_mode=view_mode,
                timestamp_seconds=timestamp_seconds,
            )
            generated = False

            def create_stable_preview(
                expected_timestamp: float = timestamp_seconds,
                expected_snapshot: _VideoSourceSnapshot = source_snapshot,
                expected_metadata: VideoMetadata = metadata,
            ) -> bytes:
                nonlocal generated
                generated = True
                rgb = read_frame(
                    input_path,
                    expected_timestamp,
                    expected_metadata,
                )
                png_bytes = create_png_bytes(rgb)
                if _video_source_snapshot(input_path) != expected_snapshot:
                    raise _VideoSourceChangedError
                return png_bytes

            preview_entry = store.get_or_put_entry(
                cache_key,
                create_stable_preview,
            )
        except _VideoSourceChangedError:
            continue
        except KeyerError:
            if _video_source_snapshot(input_path) != source_snapshot:
                continue
            raise
        if generated or _video_source_snapshot(input_path) == source_snapshot:
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


def _picker_accepts_allowed_suffixes(picker: LocalPathPicker) -> bool:
    pick_files = getattr(picker, "pick_files", None)
    if pick_files is None:
        return False
    try:
        parameters = signature(pick_files).parameters
    except (TypeError, ValueError):
        return False

    allowed_suffixes = parameters.get("allowed_suffixes")
    if allowed_suffixes is not None and allowed_suffixes.kind in {
        Parameter.POSITIONAL_OR_KEYWORD,
        Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def create_router(
    task_manager: TaskManager,
    *,
    local_path_roots: list[Path] | None = None,
    local_path_picker: LocalPathPicker | None = None,
    video_decoder: VideoDecoder | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    picker = local_path_picker or TkinterLocalPathPicker()
    picker_accepts_allowed_suffixes = _picker_accepts_allowed_suffixes(picker)
    decoder = video_decoder if video_decoder is not None else ImageioVideoDecoder()
    read_video_frame = _select_video_frame_reader(
        decoder,
        "read_frame_with_metadata",
        decoder.read_frame,
    )
    read_video_preview_frame = _select_video_frame_reader(
        decoder,
        "read_preview_frame_with_metadata",
        decoder.read_preview_frame,
    )
    chroma_preview_store = ChromaPreviewStore()
    video_metadata_cache = _BoundedSingleFlightCache[
        tuple[
            Path,
            tuple[tuple[str, str | int], ...],
            tuple[int, int, int, int, int, int],
        ],
        VideoMetadata,
    ](_CHROMA_VIDEO_METADATA_CACHE_SIZE)

    def get_cached_video_metadata(
        input_path: Path,
        fingerprint_items: tuple[tuple[str, str | int], ...],
        source_version: tuple[int, int, int, int, int, int],
    ) -> VideoMetadata:
        return video_metadata_cache.get_or_compute(
            (input_path, fingerprint_items, source_version),
            lambda: decoder.probe(input_path),
        )

    def get_video_metadata(
        input_path: Path,
        source_snapshot: _VideoSourceSnapshot,
    ) -> VideoMetadata:
        return get_cached_video_metadata(
            input_path,
            tuple(sorted(source_snapshot.fingerprint.items())),
            source_snapshot.version,
        )

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
            if request.allowed_suffixes is None or not picker_accepts_allowed_suffixes:
                paths = picker.pick_files(
                    title=request.title,
                    multiple=request.multiple,
                    file_kind=request.file_kind,
                )
            else:
                paths = picker.pick_files(
                    title=request.title,
                    multiple=request.multiple,
                    file_kind=request.file_kind,
                    allowed_suffixes=request.allowed_suffixes,
                )
        except LocalPathPickerError as exc:
            raise ApiError("local_picker_unavailable", str(exc), 503) from exc
        if request.allowed_suffixes is not None:
            allowed_suffixes = frozenset(request.allowed_suffixes)
            paths = [path for path in paths if path.suffix.lower() in allowed_suffixes]
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

    @router.post("/tasks/chroma-keyer-video", response_model=TaskCreateResponse)
    def create_chroma_video_keyer(
        request: ChromaVideoKeyerTaskRequest,
    ) -> TaskCreateResponse:
        try:
            task = task_manager.create_chroma_video_keyer_task(request)
        except TaskConflictError as exc:
            raise ApiError("task_conflict", str(exc), 409) from exc
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc
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
                    0.0,
                    get_video_metadata,
                    read_video_preview_frame,
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
                    request.timestamp_seconds,
                    get_video_metadata,
                    read_video_preview_frame,
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
                request.timestamp_seconds,
                get_video_metadata,
                read_video_frame,
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
                    request.timestamp_seconds,
                    get_video_metadata,
                    read_video_preview_frame,
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
