from __future__ import annotations

import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from inspect import Parameter, signature
from io import BytesIO
from os import stat_result
from pathlib import Path
from queue import Empty
from shutil import disk_usage
from stat import S_ISREG
from threading import BoundedSemaphore, Event, Lock, Thread
from urllib.parse import unquote

import numpy as np
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from PIL import Image
from redis.exceptions import RedisError

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
from designtoolkit.web.heartbeat import (
    DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
    HeartbeatStopTimeoutError,
    stop_heartbeat_thread,
)
from designtoolkit.web.hosted import (
    AssetDeletionStateError,
    HostedSettings,
    HostedStorage,
    UploadRejectedError,
    _asset_id,
    _safe_filename,
)
from designtoolkit.web.local_paths import resolve_dropped_file_paths
from designtoolkit.web.media_limits import (
    HostedMediaLimits,
    HostedMediaValidator,
    MediaLimitError,
)
from designtoolkit.web.native_picker import (
    LocalPathPicker,
    LocalPathPickerError,
    TkinterLocalPathPicker,
)
from designtoolkit.web.redis_errors import (
    is_redis_unavailable,
    storage_unavailable_payload,
)
from designtoolkit.web.redis_tasks import RedisAssetReadPin, RedisTaskManager
from designtoolkit.web.resources import (
    AssetReleaseMismatchError,
    RedisResourceCoordinator,
    RedisSessionLease,
    ResourceUnavailableError,
)
from designtoolkit.web.schemas import (
    AppConfigResponse,
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
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskCreateResponse,
    TaskResponse,
    TaskStatus,
    UploadResponse,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskConflictError, TaskManager, TaskRecord

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
_LOGGER = logging.getLogger(__name__)


class _SessionLeaseHeartbeat:
    def __init__(self, lease: RedisSessionLease, interval: float) -> None:
        self._lease = lease
        self._interval = interval
        self._stop_timeout = getattr(
            getattr(lease, "coordinator", None),
            "heartbeat_stop_timeout_seconds",
            DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
        )
        self._stopped = Event()
        self._failure: BaseException | None = None
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            stop_heartbeat_thread(
                self._stopped,
                self._thread,
                timeout=self._stop_timeout,
                heartbeat_name="Session lease heartbeat",
            )
        except HeartbeatStopTimeoutError as exc:
            self._failure = exc
            raise

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    @property
    def quiesced(self) -> bool:
        return not self._thread.is_alive()

    def commit[T](self, action: Callable[[], T]) -> T:
        # Quiescing removes all in-flight refreshes from the irreversible
        # boundary. Each later commit gets a fresh full lease TTL before action.
        self.stop()
        self.raise_if_failed()
        self._lease.refresh()
        return action()

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            if self._stopped.is_set():
                return
            try:
                self._lease.refresh()
            except BaseException as exc:
                self._failure = exc
                return


class _AssetReadPinHeartbeat:
    def __init__(
        self,
        pin: RedisAssetReadPin,
        interval: float,
        stop_timeout: float,
    ) -> None:
        self._pin = pin
        self._interval = interval
        self._stop_timeout = stop_timeout
        self._stopped = Event()
        self._failure: BaseException | None = None
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            stop_heartbeat_thread(
                self._stopped,
                self._thread,
                timeout=self._stop_timeout,
                heartbeat_name="Asset read pin heartbeat",
            )
        except HeartbeatStopTimeoutError as exc:
            self._failure = exc
            raise

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    @property
    def quiesced(self) -> bool:
        return not self._thread.is_alive()

    def commit[T](self, action: Callable[[], T]) -> T:
        self.stop()
        self.raise_if_failed()
        self._pin.refresh()
        return action()

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            if self._stopped.is_set():
                return
            try:
                self._pin.refresh()
            except BaseException as exc:
                self._failure = exc
                return


@dataclass
class _HostedSessionOperation:
    lease: RedisSessionLease | None = None
    heartbeat: _SessionLeaseHeartbeat | None = None
    committed: bool = False

    def commit[T](self, action: Callable[[], T]) -> T:
        def commit_action() -> T:
            result = action()
            self.mark_committed()
            return result

        if self.heartbeat is None:
            return commit_action()
        return self.heartbeat.commit(commit_action)

    def mark_committed(self) -> None:
        self.committed = True


@dataclass(frozen=True)
class _HostedTaskAssets:
    asset_ids: tuple[str, ...]
    pin: RedisAssetReadPin | None = None
    heartbeat: _AssetReadPinHeartbeat | None = None

    def commit[T](self, action: Callable[[], T]) -> T:
        if self.heartbeat is None:
            return action()
        return self.heartbeat.commit(action)


class _LeaseProtectedFileResponse(FileResponse):
    def __init__(
        self,
        *args: object,
        lease: RedisSessionLease,
        heartbeat: _SessionLeaseHeartbeat,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lease = lease
        self._heartbeat = heartbeat

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        async def send_while_leased(message) -> None:  # noqa: ANN001
            self._heartbeat.raise_if_failed()
            await send(message)

        try:
            await super().__call__(scope, receive, send_while_leased)
        except BaseException:
            _stop_session_lease_heartbeat(
                self._heartbeat,
                failure_context=(
                    "download session lease heartbeat failed during response "
                    "send error"
                ),
            )
            raise
        else:
            _stop_session_lease_heartbeat(self._heartbeat)
        finally:
            if self._heartbeat.quiesced:
                _release_redis_resource_best_effort(
                    self._lease.release,
                    "download session lease",
                )


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


def _media_limit_error_response(exc: MediaLimitError) -> ApiError:
    return ApiError("media_limit_exceeded", str(exc), 422)


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


def _sse_storage_unavailable() -> str:
    payload = json.dumps(
        storage_unavailable_payload(
            "Task event storage is temporarily unavailable",
        ),
        separators=(",", ":"),
    )
    return f"event: error\ndata: {payload}\n\n"


def _is_terminal_event(event: LogEventResponse) -> bool:
    return event.phase in _TERMINAL_PHASES


def _terminal_event_from_task(task: TaskRecord | None) -> LogEventResponse | None:
    if task is None or task.finished_at is None:
        return None
    if task.status is TaskStatus.SUCCEEDED:
        level = LogLevel.SUCCESS
        phase = LogPhase.COMPLETED
        message = "Task completed"
    elif task.status is TaskStatus.FAILED:
        level = LogLevel.ERROR
        phase = LogPhase.FAILED
        message = task.error or "Task failed"
    else:
        return None
    return LogEventResponse(
        task_id=task.task_id,
        timestamp=task.finished_at,
        level=level,
        phase=phase,
        message=message,
        details={"source": "task_state", "status": task.status.value},
    )


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
    hosted_settings: HostedSettings | None = None,
    hosted_storage: HostedStorage | None = None,
    resource_coordinator: RedisResourceCoordinator | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    picker = local_path_picker or TkinterLocalPathPicker()
    picker_accepts_allowed_suffixes = _picker_accepts_allowed_suffixes(picker)
    decoder = video_decoder if video_decoder is not None else ImageioVideoDecoder()
    settings = hosted_settings or HostedSettings.from_env({})
    storage = hosted_storage
    resources = resource_coordinator
    preview_semaphore = BoundedSemaphore(settings.max_preview_concurrency)
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
    chroma_preview_owners: OrderedDict[str, str] = OrderedDict()
    chroma_preview_owners_lock = Lock()
    media_validator = (
        HostedMediaValidator(
            HostedMediaLimits(
                max_image_pixels=settings.max_image_pixels,
                max_video_width=settings.max_video_width,
                max_video_height=settings.max_video_height,
                max_video_duration_seconds=settings.max_video_duration_seconds,
            ),
            video_probe=decoder.probe,
        )
        if storage is not None
        else None
    )
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
        if media_validator is not None:
            try:
                return media_validator.validate_video(input_path)
            except MediaLimitError as exc:
                raise _media_limit_error_response(exc) from exc
        return get_cached_video_metadata(
            input_path,
            tuple(sorted(source_snapshot.fingerprint.items())),
            source_snapshot.version,
        )

    def resolve_hosted_asset(http_request: Request, reference: str) -> Path:
        if storage is None:
            return Path(reference)
        try:
            return storage.resolve(http_request.state.session_id, reference)
        except UploadRejectedError as exc:
            raise ApiError("asset_not_found", str(exc), 404) from exc

    def validate_hosted_paths(
        http_request: Request,
        paths: list[Path],
        *,
        allowed_media_types: frozenset[str] | None = None,
    ) -> None:
        del http_request
        if media_validator is None:
            return
        try:
            validated = media_validator.validate_media_many(paths)
        except MediaLimitError as exc:
            raise _media_limit_error_response(exc) from exc
        if allowed_media_types is None:
            return
        invalid = next(
            (
                media.media_type
                for media in validated
                if media.media_type not in allowed_media_types
            ),
            None,
        )
        if invalid is not None:
            expected = " or ".join(sorted(allowed_media_types))
            raise ApiError(
                "invalid_request",
                f"Task input must be {expected}, not {invalid}",
                422,
            )

    def resolve_input_path(http_request: Request, reference: str) -> Path:
        path = resolve_hosted_asset(http_request, reference)
        validate_hosted_paths(http_request, [path])
        return path

    def owned_preview_url(http_request: Request, preview_id: str) -> str:
        if storage is not None:
            with chroma_preview_owners_lock:
                chroma_preview_owners[preview_id] = http_request.state.session_id
                chroma_preview_owners.move_to_end(preview_id)
                if len(chroma_preview_owners) > 32:
                    chroma_preview_owners.popitem(last=False)
        return _chroma_preview_url(preview_id)

    def preview_capacity() -> Iterator[bool]:
        if storage is None:
            yield True
            return
        if not preview_semaphore.acquire(blocking=False):
            raise ApiError(
                "preview_busy",
                "Preview capacity is busy; retry shortly",
                429,
            )
        try:
            yield True
        finally:
            preview_semaphore.release()

    def hosted_session_operation(
        http_request: Request,
    ) -> Iterator[_HostedSessionOperation]:
        if storage is None or resources is None:
            operation = _HostedSessionOperation()
            http_request.state.hosted_session_operation = operation
            yield operation
            return
        try:
            lease = resources.acquire_session(http_request.state.session_id)
        except ResourceUnavailableError as exc:
            raise ApiError("storage_unavailable", str(exc), 503) from exc
        heartbeat = _SessionLeaseHeartbeat(
            lease,
            getattr(resources, "session_lease_heartbeat_interval", 5.0),
        )
        heartbeat.start()
        operation = _HostedSessionOperation(lease=lease, heartbeat=heartbeat)
        http_request.state.hosted_session_operation = operation
        try:
            yield operation
        except BaseException:
            _stop_session_lease_heartbeat(
                heartbeat,
                failure_context=(
                    "hosted session lease heartbeat failed during endpoint error"
                ),
            )
            raise
        else:
            if operation.committed:
                _stop_session_lease_heartbeat(
                    heartbeat,
                    failure_context=(
                        "hosted session lease heartbeat failed after task enqueue"
                    ),
                )
            else:
                try:
                    _stop_session_lease_heartbeat(heartbeat)
                except ResourceUnavailableError as exc:
                    raise ApiError("storage_unavailable", str(exc), 503) from exc
        finally:
            if heartbeat.quiesced:
                _release_redis_resource_best_effort(
                    lease.release,
                    "hosted session lease",
                )

    def create_task_with_session_commit(
        http_request: Request,
        create_task: Callable[..., TaskRecord],
        payload: object,
        *,
        task_assets: _HostedTaskAssets,
    ) -> TaskRecord:
        operation = http_request.state.hosted_session_operation
        owner_id = getattr(http_request.state, "session_id", None)
        if isinstance(task_manager, RedisTaskManager):
            task = task_assets.commit(
                lambda: create_task(
                    payload,
                    owner_id=owner_id,
                    commit=operation.commit,
                    asset_ids=task_assets.asset_ids,
                    asset_read_pin_token=(
                        task_assets.pin.token
                        if task_assets.pin is not None
                        else None
                    ),
                )
            )
            operation.mark_committed()
            return task
        return operation.commit(
            lambda: create_task(payload, owner_id=owner_id)
        )

    def hosted_task_asset_ids(payload: object) -> tuple[str, ...]:
        if storage is None:
            return ()
        input_path = getattr(payload, "input_path", None)
        input_paths = getattr(payload, "input_paths", None)
        references = [input_path] if input_path is not None else list(input_paths or [])
        try:
            return tuple(
                dict.fromkeys(_asset_id(reference) for reference in references)
            )
        except UploadRejectedError as exc:
            raise ApiError("asset_not_found", str(exc), 404) from exc

    @contextmanager
    def pin_hosted_task_assets(
        http_request: Request,
        payload: object,
    ) -> Iterator[_HostedTaskAssets]:
        asset_ids = hosted_task_asset_ids(payload)
        if (
            storage is None
            or not asset_ids
            or not isinstance(task_manager, RedisTaskManager)
        ):
            yield _HostedTaskAssets(asset_ids)
            return
        try:
            pin = task_manager.acquire_asset_read_pin(
                http_request.state.session_id,
                asset_ids,
            )
        except TaskConflictError as exc:
            raise ApiError("task_conflict", str(exc), 409) from exc
        heartbeat = _AssetReadPinHeartbeat(
            pin,
            task_manager.asset_read_pin_heartbeat_interval,
            task_manager.heartbeat_stop_timeout_seconds,
        )
        heartbeat.start()
        task_assets = _HostedTaskAssets(
            asset_ids=asset_ids,
            pin=pin,
            heartbeat=heartbeat,
        )
        try:
            yield task_assets
        except BaseException:
            _stop_asset_read_pin_heartbeat(
                heartbeat,
                failure_context=(
                    "asset read pin heartbeat failed during endpoint error"
                ),
            )
            raise
        else:
            try:
                _stop_asset_read_pin_heartbeat(heartbeat)
            except ResourceUnavailableError as exc:
                raise ApiError("storage_unavailable", str(exc), 503) from exc
        finally:
            if heartbeat.quiesced:
                _release_redis_resource_best_effort(
                    pin.release,
                    "asset read pin",
                )

    def resolve_input_references(
        http_request: Request,
        references: list[str],
        *,
        allowed_media_types: frozenset[str] | None = None,
    ) -> list[str]:
        paths = [
            resolve_hosted_asset(http_request, reference)
            for reference in references
        ]
        validate_hosted_paths(
            http_request,
            paths,
            allowed_media_types=allowed_media_types,
        )
        return [str(path) for path in paths]

    def hosted_video_frames_request(
        http_request: Request,
        payload: VideoFramesTaskRequest,
    ) -> VideoFramesTaskRequest:
        if storage is None:
            return payload
        references = (
            [payload.input_path]
            if payload.input_path is not None
            else list(payload.input_paths or [])
        )
        paths = resolve_input_references(
            http_request,
            references,
            allowed_media_types=frozenset({"video"}),
        )
        output_dir = storage.create_task_dir(http_request.state.session_id) / "frames"
        return payload.model_copy(
            update={
                "input_path": paths[0] if payload.input_path is not None else None,
                "input_paths": paths if payload.input_paths is not None else None,
                "output_dir": str(output_dir),
                "overwrite": False,
            }
        )

    def hosted_media_resize_request(
        http_request: Request,
        payload: MediaResizeTaskRequest,
    ) -> MediaResizeTaskRequest:
        if storage is None:
            return payload
        references = (
            [payload.input_path]
            if payload.input_path is not None
            else list(payload.input_paths or [])
        )
        paths = resolve_input_references(http_request, references)
        task_dir = storage.create_task_dir(http_request.state.session_id)
        output_path = None
        if payload.input_path is not None:
            input_path = Path(paths[0])
            output_path = str(
                task_dir / f"{input_path.stem}_resized{input_path.suffix}"
            )
        return payload.model_copy(
            update={
                "input_path": paths[0] if payload.input_path is not None else None,
                "input_paths": paths if payload.input_paths is not None else None,
                "output_path": output_path,
                "output_dir": str(task_dir)
                if payload.input_paths is not None
                else None,
            }
        )

    def hosted_chroma_keyer_request(
        http_request: Request,
        payload: ChromaKeyerTaskRequest,
    ) -> ChromaKeyerTaskRequest:
        if storage is None:
            return payload
        input_path = resolve_hosted_asset(http_request, payload.input_path)
        validate_hosted_paths(
            http_request,
            [input_path],
            allowed_media_types=frozenset({"image"}),
        )
        task_dir = storage.create_task_dir(http_request.state.session_id)
        return payload.model_copy(
            update={
                "input_path": str(input_path),
                "output_path": str(task_dir / f"{input_path.stem}_keyed.png"),
            }
        )

    def hosted_chroma_video_request(
        http_request: Request,
        payload: ChromaVideoKeyerTaskRequest,
    ) -> ChromaVideoKeyerTaskRequest:
        if storage is None:
            return payload
        input_path = resolve_hosted_asset(http_request, payload.input_path)
        validate_hosted_paths(
            http_request,
            [input_path],
            allowed_media_types=frozenset({"video"}),
        )
        task_dir = storage.create_task_dir(http_request.state.session_id)
        return payload.model_copy(
            update={
                "input_path": str(input_path),
                "output_root": str(task_dir),
            }
        )

    def discard_failed_hosted_task_dir(
        http_request: Request,
        payload: object,
    ) -> None:
        if storage is None:
            return
        for field in ("output_dir", "output_path", "output_root"):
            storage.discard_empty_task_dir(
                http_request.state.session_id,
                getattr(payload, field, None),
            )

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/config", response_model=AppConfigResponse)
    def app_config() -> AppConfigResponse:
        return AppConfigResponse(
            hosted=settings.enabled,
            max_upload_bytes=settings.max_upload_bytes,
        )

    @router.post("/uploads", response_model=UploadResponse, status_code=201)
    async def upload_asset(http_request: Request) -> UploadResponse:
        if storage is None or resources is None:
            raise ApiError("hosted_mode_disabled", "Uploads are unavailable", 404)
        content_length = _upload_content_length(
            http_request.headers.get("content-length")
        )
        encoded_filename = http_request.headers.get("x-upload-filename")
        filename = _upload_filename(encoded_filename)
        if content_length > settings.max_upload_bytes:
            raise ApiError(
                "upload_too_large",
                "File exceeds the configured file limit",
                413,
            )

        reservation = None
        lease = None
        release_resources = True
        try:
            reservation = resources.reserve_upload(
                http_request.state.session_id,
                content_length,
                free_bytes=disk_usage(settings.data_dir).free,
            )
            lease = resources.acquire_session(http_request.state.session_id)
            asset = await storage.store_stream(
                http_request.state.session_id,
                filename=filename,
                content_length=content_length,
                chunks=http_request.stream(),
                reservation=reservation,
                lease=lease,
            )
        except UploadRejectedError as exc:
            code = "upload_too_large" if "file limit" in str(exc) else "invalid_upload"
            status_code = 413 if code == "upload_too_large" else 400
            raise ApiError(code, str(exc), status_code) from exc
        except HeartbeatStopTimeoutError as exc:
            release_resources = False
            raise ApiError("storage_unavailable", str(exc), 503) from exc
        except ResourceUnavailableError as exc:
            message = str(exc)
            if "concurrency" in message:
                raise ApiError("upload_concurrency", message, 429) from exc
            if "session byte" in message:
                raise ApiError("upload_too_large", message, 413) from exc
            raise ApiError("storage_unavailable", message, 503) from exc
        except OSError as exc:
            raise ApiError(
                "storage_unavailable",
                "Server storage is temporarily unavailable",
                503,
            ) from exc
        finally:
            if release_resources and lease is not None:
                _release_redis_resource_best_effort(
                    lease.release,
                    "upload session lease",
                )
            if release_resources and reservation is not None:
                _release_redis_resource_best_effort(
                    reservation.release,
                    "upload reservation",
                )
        return UploadResponse(
            reference=asset.reference,
            filename=asset.filename,
            size=asset.size,
        )

    @router.delete(
        "/uploads/{asset_id}",
        status_code=204,
        dependencies=[Depends(hosted_session_operation, scope="function")],
    )
    def delete_upload(http_request: Request, asset_id: str) -> Response:
        if storage is None or resources is None:
            raise ApiError("hosted_mode_disabled", "Uploads are unavailable", 404)
        operation = http_request.state.hosted_session_operation
        session_id = http_request.state.session_id
        deletion_claim: str | None = None
        try:
            if isinstance(task_manager, RedisTaskManager):
                deletion_claim = operation.commit(
                    lambda: task_manager.claim_asset_deletion(
                        session_id,
                        asset_id,
                    )
                )
            try:
                deletion = operation.commit(
                    lambda: storage.delete_asset(
                        session_id,
                        f"asset:{asset_id}",
                    )
                )
            except AssetDeletionStateError as exc:
                if not resources.is_committed_asset_released(
                    session_id,
                    exc.asset_id,
                ):
                    raise
                deletion = None
            if deletion is not None:
                operation.commit(
                    lambda: resources.release_committed_asset(
                        session_id,
                        deletion.asset_id,
                        deletion.size,
                    )
                )
            asset_was_absent = deletion is not None and deletion.size == 0
            operation.commit(
                lambda: storage.finalize_asset_deletion(
                    session_id,
                    asset_id,
                )
            )
            if asset_was_absent:
                try:
                    operation.commit(
                        lambda: resources.finalize_asset_release(
                            session_id,
                            asset_id,
                        )
                    )
                except RedisError:
                    _LOGGER.warning(
                        "Failed to finalize release marker for absent asset",
                        exc_info=True,
                    )
            else:
                operation.commit(
                    lambda: resources.finalize_asset_release(
                        session_id,
                        asset_id,
                    )
                )
        except UploadRejectedError as exc:
            raise ApiError("asset_not_found", str(exc), 404) from exc
        except AssetReleaseMismatchError as exc:
            raise ApiError(
                "asset_not_found",
                "Uploaded asset is not available",
                404,
            ) from exc
        except TaskConflictError as exc:
            raise ApiError("asset_in_use", str(exc), 409) from exc
        except ResourceUnavailableError as exc:
            raise ApiError("storage_unavailable", str(exc), 503) from exc
        except OSError as exc:
            raise ApiError(
                "storage_unavailable",
                "Server storage is temporarily unavailable",
                503,
            ) from exc
        finally:
            if deletion_claim is not None and isinstance(
                task_manager,
                RedisTaskManager,
            ):
                try:
                    task_manager.release_asset_deletion(
                        session_id,
                        asset_id,
                        deletion_claim,
                    )
                except RedisError:
                    _LOGGER.warning(
                        "Failed to release asset deletion claim; TTL will expire it",
                        exc_info=True,
                    )
        return Response(status_code=204)

    @router.post(
        "/local-paths/resolve-dropped-files",
        response_model=DroppedFilePathResolveResponse,
    )
    def resolve_dropped_files(
        request: DroppedFilePathResolveRequest,
    ) -> DroppedFilePathResolveResponse:
        if settings.enabled:
            raise ApiError("api_not_found", "API route not found", 404)
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
        if settings.enabled:
            raise ApiError("api_not_found", "API route not found", 404)
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
        if settings.enabled:
            raise ApiError("api_not_found", "API route not found", 404)
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
        if settings.enabled:
            raise ApiError("api_not_found", "API route not found", 404)
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

    @router.post(
        "/tasks/video-frames",
        status_code=202,
        response_model=TaskCreateResponse,
        dependencies=[Depends(hosted_session_operation, scope="function")],
    )
    def create_video_frames(
        payload: VideoFramesTaskRequest,
        http_request: Request,
    ) -> TaskCreateResponse:
        with pin_hosted_task_assets(http_request, payload) as task_assets:
            hosted_payload = hosted_video_frames_request(http_request, payload)
            try:
                task = create_task_with_session_commit(
                    http_request,
                    task_manager.create_video_frames_task,
                    hosted_payload,
                    task_assets=task_assets,
                )
            except Exception as exc:
                discard_failed_hosted_task_dir(http_request, hosted_payload)
                if isinstance(exc, TaskConflictError):
                    raise ApiError("task_conflict", str(exc), 409) from exc
                if isinstance(exc, (RedisError, ResourceUnavailableError)):
                    raise ApiError("storage_unavailable", str(exc), 503) from exc
                if isinstance(exc, _INVALID_REQUEST_ERRORS):
                    raise ApiError("invalid_request", str(exc), 400) from exc
                raise
        return TaskCreateResponse(task_id=task.task_id, status=task.status)

    @router.post(
        "/tasks/media-resize",
        status_code=202,
        response_model=TaskCreateResponse,
        dependencies=[Depends(hosted_session_operation, scope="function")],
    )
    def create_media_resize(
        payload: MediaResizeTaskRequest,
        http_request: Request,
    ) -> TaskCreateResponse:
        with pin_hosted_task_assets(http_request, payload) as task_assets:
            hosted_payload = hosted_media_resize_request(http_request, payload)
            try:
                task = create_task_with_session_commit(
                    http_request,
                    task_manager.create_media_resize_task,
                    hosted_payload,
                    task_assets=task_assets,
                )
            except Exception as exc:
                discard_failed_hosted_task_dir(http_request, hosted_payload)
                if isinstance(exc, TaskConflictError):
                    raise ApiError("task_conflict", str(exc), 409) from exc
                if isinstance(exc, (RedisError, ResourceUnavailableError)):
                    raise ApiError("storage_unavailable", str(exc), 503) from exc
                if isinstance(exc, _INVALID_REQUEST_ERRORS):
                    raise ApiError("invalid_request", str(exc), 400) from exc
                raise
        return TaskCreateResponse(task_id=task.task_id, status=task.status)

    @router.post(
        "/tasks/chroma-keyer",
        status_code=202,
        response_model=TaskCreateResponse,
        dependencies=[Depends(hosted_session_operation, scope="function")],
    )
    def create_chroma_keyer(
        payload: ChromaKeyerTaskRequest,
        http_request: Request,
    ) -> TaskCreateResponse:
        with pin_hosted_task_assets(http_request, payload) as task_assets:
            hosted_payload = hosted_chroma_keyer_request(http_request, payload)
            try:
                task = create_task_with_session_commit(
                    http_request,
                    task_manager.create_chroma_keyer_task,
                    hosted_payload,
                    task_assets=task_assets,
                )
            except Exception as exc:
                discard_failed_hosted_task_dir(http_request, hosted_payload)
                if isinstance(exc, TaskConflictError):
                    raise ApiError("task_conflict", str(exc), 409) from exc
                if isinstance(exc, (RedisError, ResourceUnavailableError)):
                    raise ApiError("storage_unavailable", str(exc), 503) from exc
                if isinstance(exc, KeyerError):
                    raise _chroma_keyer_error_response(exc) from exc
                raise
        return TaskCreateResponse(task_id=task.task_id, status=task.status)

    @router.post(
        "/tasks/chroma-keyer-video",
        status_code=202,
        response_model=TaskCreateResponse,
        dependencies=[Depends(hosted_session_operation, scope="function")],
    )
    def create_chroma_video_keyer(
        payload: ChromaVideoKeyerTaskRequest,
        http_request: Request,
    ) -> TaskCreateResponse:
        with pin_hosted_task_assets(http_request, payload) as task_assets:
            hosted_payload = hosted_chroma_video_request(http_request, payload)
            try:
                task = create_task_with_session_commit(
                    http_request,
                    task_manager.create_chroma_video_keyer_task,
                    hosted_payload,
                    task_assets=task_assets,
                )
            except Exception as exc:
                discard_failed_hosted_task_dir(http_request, hosted_payload)
                if isinstance(exc, TaskConflictError):
                    raise ApiError("task_conflict", str(exc), 409) from exc
                if isinstance(exc, (RedisError, ResourceUnavailableError)):
                    raise ApiError("storage_unavailable", str(exc), 503) from exc
                if isinstance(exc, KeyerError):
                    raise _chroma_video_error_response(exc) from exc
                raise
        return TaskCreateResponse(task_id=task.task_id, status=task.status)

    @router.post(
        "/chroma-keyer/images/inspect",
        response_model=ChromaImageInspectResponse,
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    def inspect_chroma_image(
        payload: ChromaImageInspectRequest,
        http_request: Request,
    ) -> ChromaImageInspectResponse:
        try:
            input_path = resolve_input_path(http_request, payload.input_path)
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
            input_path=payload.input_path,
            width=int(width),
            height=int(height),
            preview_width=int(preview_width),
            preview_height=int(preview_height),
            preview_url=owned_preview_url(http_request, preview_entry.preview_id),
        )

    @router.post(
        "/chroma-keyer/videos/inspect",
        response_model=ChromaVideoInspectResponse,
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    def inspect_chroma_video(
        payload: ChromaVideoInspectRequest,
        http_request: Request,
    ) -> ChromaVideoInspectResponse:
        try:
            input_path = resolve_input_path(http_request, payload.input_path)
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
            input_path=payload.input_path,
            width=metadata.width,
            height=metadata.height,
            preview_width=preview_width,
            preview_height=preview_height,
            preview_url=owned_preview_url(http_request, preview_entry.preview_id),
            duration_seconds=metadata.duration_seconds,
            fps=metadata.fps,
            estimated_frame_count=metadata.estimated_frame_count,
            timestamp_seconds=timestamp_seconds,
        )

    @router.post(
        "/chroma-keyer/videos/frame",
        response_model=ChromaVideoFrameResponse,
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    def create_chroma_video_frame(
        payload: ChromaVideoFrameRequest,
        http_request: Request,
    ) -> ChromaVideoFrameResponse:
        try:
            input_path = resolve_input_path(http_request, payload.input_path)
            _metadata, timestamp_seconds, preview_entry = (
                _get_stable_video_preview_entry(
                    chroma_preview_store,
                    input_path,
                    "video_frame",
                    payload.timestamp_seconds,
                    get_video_metadata,
                    read_video_preview_frame,
                    make_preview_png,
                )
            )
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc

        return ChromaVideoFrameResponse(
            preview_url=owned_preview_url(http_request, preview_entry.preview_id),
            timestamp_seconds=timestamp_seconds,
        )

    @router.post(
        "/chroma-keyer/videos/sample",
        response_model=ChromaVideoSampleResponse,
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    def sample_chroma_video(
        payload: ChromaVideoSampleRequest,
        http_request: Request,
    ) -> ChromaVideoSampleResponse:
        try:
            input_path = resolve_input_path(http_request, payload.input_path)
            metadata, timestamp_seconds, rgb = _read_stable_video_frame(
                input_path,
                payload.timestamp_seconds,
                get_video_metadata,
                read_video_frame,
            )
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc

        try:
            source_x, source_y = map_preview_point(
                payload.x,
                payload.y,
                payload.preview_width,
                payload.preview_height,
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
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    def create_chroma_video_preview(
        payload: ChromaVideoPreviewRequest,
        http_request: Request,
    ) -> ChromaVideoPreviewResponse:
        try:
            input_path = resolve_input_path(http_request, payload.input_path)
            model = screen_model_from_samples(payload.samples)
            params_data = payload.params.model_dump(mode="json")

            def create_preview_png(rgb: np.ndarray) -> bytes:
                params = KeyerParams(**params_data)
                preview_rgb, alpha = key_rgb(rgb, model, params)
                if payload.view_mode is ChromaViewMode.FINAL:
                    return make_preview_png(preview_rgb, alpha=alpha)
                alpha_rgb = np.repeat(alpha[..., np.newaxis], 3, axis=2)
                return make_preview_png(alpha_rgb)

            _metadata, timestamp_seconds, preview_entry = (
                _get_stable_video_preview_entry(
                    chroma_preview_store,
                    input_path,
                    "video_generated",
                    payload.timestamp_seconds,
                    get_video_metadata,
                    read_video_preview_frame,
                    create_preview_png,
                    samples=payload.samples,
                    params=params_data,
                    view_mode=payload.view_mode,
                )
            )
        except KeyerError as exc:
            raise _chroma_video_error_response(exc) from exc

        return ChromaVideoPreviewResponse(
            preview_url=owned_preview_url(http_request, preview_entry.preview_id),
            timestamp_seconds=timestamp_seconds,
        )

    @router.post(
        "/chroma-keyer/images/sample",
        response_model=ChromaSampleResponse,
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    def sample_chroma_image(
        payload: ChromaSampleRequest,
        http_request: Request,
    ) -> ChromaSampleResponse:
        try:
            rgb = read_rgb_image(
                resolve_input_path(http_request, payload.input_path)
            )
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc

        try:
            height, width = rgb.shape[:2]
            source_x, source_y = map_preview_point(
                payload.x,
                payload.y,
                payload.preview_width,
                payload.preview_height,
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

    @router.post(
        "/chroma-keyer/preview",
        response_model=ChromaPreviewResponse,
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    @router.post(
        "/chroma-keyer/previews",
        response_model=ChromaPreviewResponse,
        dependencies=[
            Depends(preview_capacity),
            Depends(hosted_session_operation, scope="function"),
        ],
    )
    def create_chroma_preview(
        payload: ChromaPreviewRequest,
        http_request: Request,
    ) -> ChromaPreviewResponse:
        try:
            model = screen_model_from_samples(payload.samples)
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc

        try:
            input_path = resolve_input_path(http_request, payload.input_path)
            rgb, source_fingerprint = _read_rgb_snapshot(input_path)
            params_data = payload.params.model_dump(mode="json")
            cache_key = _chroma_preview_cache_key(
                "generated",
                source_fingerprint,
                samples=payload.samples,
                params=params_data,
                view_mode=payload.view_mode,
            )

            def create_preview_png() -> bytes:
                params = KeyerParams(**params_data)
                preview_rgb = resize_preview_rgb(rgb)
                preview_rgb, alpha = key_rgb(preview_rgb, model, params)
                if payload.view_mode is ChromaViewMode.FINAL:
                    return make_preview_png(preview_rgb, alpha=alpha)
                else:
                    preview_rgb = np.repeat(alpha[..., np.newaxis], 3, axis=2)
                    return make_preview_png(preview_rgb)

            preview_id = chroma_preview_store.get_or_put(cache_key, create_preview_png)
        except KeyerError as exc:
            raise _chroma_keyer_error_response(exc) from exc

        return ChromaPreviewResponse(
            preview_url=owned_preview_url(http_request, preview_id)
        )

    @router.get("/chroma-keyer/previews/{preview_id}")
    def get_chroma_preview(preview_id: str, http_request: Request) -> Response:
        if storage is not None:
            with chroma_preview_owners_lock:
                owner_id = chroma_preview_owners.get(preview_id)
            if owner_id != http_request.state.session_id:
                raise ApiError("preview_not_found", "Preview not found", 404)
        preview_png = chroma_preview_store.get(preview_id)
        if preview_png is None:
            raise ApiError("preview_not_found", "Preview not found", 404)
        return Response(content=preview_png, media_type="image/png")

    @router.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str, http_request: Request) -> TaskResponse:
        task = task_manager.get_task(
            task_id,
            owner_id=getattr(http_request.state, "session_id", None),
        )
        if task is None:
            raise ApiError("task_not_found", f"Task not found: {task_id}", 404)
        return task.to_response()

    @router.get("/tasks/{task_id}/download")
    def download_task_result(
        task_id: str,
        http_request: Request,
    ) -> FileResponse:
        if storage is None or resources is None:
            raise ApiError("hosted_mode_disabled", "Downloads are unavailable", 404)
        task = task_manager.get_task(
            task_id,
            owner_id=getattr(http_request.state, "session_id", None),
        )
        if task is None or task.status.value != "succeeded":
            raise ApiError("task_not_found", f"Task not found: {task_id}", 404)
        try:
            lease = resources.acquire_session(http_request.state.session_id)
        except ResourceUnavailableError as exc:
            raise ApiError("storage_unavailable", str(exc), 503) from exc
        heartbeat = _SessionLeaseHeartbeat(
            lease,
            getattr(resources, "session_lease_heartbeat_interval", 5.0),
        )
        heartbeat.start()
        try:
            result_path = storage.validated_result_path(task.result_path)
        except UploadRejectedError as exc:
            _stop_session_lease_heartbeat(
                heartbeat,
                failure_context=(
                    "download session lease heartbeat failed during result "
                    "validation error"
                ),
            )
            if heartbeat.quiesced:
                _release_redis_resource_best_effort(
                    lease.release,
                    "download session lease",
                )
            raise ApiError("result_not_found", str(exc), 404) from exc
        except BaseException:
            _stop_session_lease_heartbeat(
                heartbeat,
                failure_context=(
                    "download session lease heartbeat failed during result "
                    "validation error"
                ),
            )
            if heartbeat.quiesced:
                _release_redis_resource_best_effort(
                    lease.release,
                    "download session lease",
                )
            raise
        return _LeaseProtectedFileResponse(
            result_path,
            filename=result_path.name,
            media_type="application/octet-stream",
            lease=lease,
            heartbeat=heartbeat,
        )

    @router.get("/tasks/{task_id}/events")
    def stream_task_events(
        task_id: str,
        http_request: Request,
    ) -> StreamingResponse:
        owner_id = getattr(http_request.state, "session_id", None)
        task = task_manager.get_task(
            task_id,
            owner_id=owner_id,
        )
        if task is None:
            raise ApiError("task_not_found", f"Task not found: {task_id}", 404)
        history, subscriber = task_manager.log_store.history_and_subscribe(task_id)

        def event_stream() -> Iterator[str]:
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
                        terminal_event = _terminal_event_from_task(
                            task_manager.get_task(task_id, owner_id=owner_id)
                        )
                        if terminal_event is not None:
                            yield _sse_line(terminal_event)
                            terminal_event_seen = True
                        continue

                    yield _sse_line(event)
                    terminal_event_seen = _is_terminal_event(event)
            except RedisError as exc:
                if not is_redis_unavailable(exc):
                    raise
                yield _sse_storage_unavailable()
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


def _release_redis_resource_best_effort(
    release: Callable[[], None],
    resource_name: str,
) -> None:
    try:
        release()
    except RedisError:
        _LOGGER.warning(
            "Failed to release %s; expiry will reconcile it",
            resource_name,
            exc_info=True,
        )


def _stop_session_lease_heartbeat(
    heartbeat: _SessionLeaseHeartbeat,
    *,
    failure_context: str | None = None,
) -> None:
    heartbeat.stop()
    try:
        heartbeat.raise_if_failed()
    except BaseException:
        if failure_context is None:
            raise
        _LOGGER.error(
            "storage_unavailable: %s",
            failure_context,
            exc_info=True,
        )


def _stop_asset_read_pin_heartbeat(
    heartbeat: _AssetReadPinHeartbeat,
    *,
    failure_context: str | None = None,
) -> None:
    heartbeat.stop()
    try:
        heartbeat.raise_if_failed()
    except BaseException:
        if failure_context is None:
            raise
        _LOGGER.error(
            "storage_unavailable: %s",
            failure_context,
            exc_info=True,
        )


def _upload_content_length(value: str | None) -> int:
    try:
        content_length = int(value) if value is not None else 0
    except ValueError as exc:
        raise ApiError(
            "length_required",
            "A positive Content-Length header is required",
            411,
        ) from exc
    if content_length <= 0:
        raise ApiError(
            "length_required",
            "A positive Content-Length header is required",
            411,
        )
    return content_length


def _upload_filename(value: str | None) -> str:
    if value is None:
        raise ApiError(
            "invalid_upload",
            "X-Upload-Filename header is required",
            400,
        )
    try:
        value.encode("ascii")
        if re.search(r"%(?![0-9A-Fa-f]{2})", value):
            raise ValueError
        filename = unquote(value, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise ApiError(
            "invalid_upload",
            "X-Upload-Filename must contain URI-encoded UTF-8",
            400,
        ) from exc
    try:
        return _safe_filename(filename)
    except UploadRejectedError as exc:
        raise ApiError("invalid_upload", str(exc), 400) from exc
