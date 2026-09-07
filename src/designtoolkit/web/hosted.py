from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG
from threading import Event, RLock, Thread
from time import time
from typing import BinaryIO
from unicodedata import category
from urllib.parse import quote
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from redis.exceptions import RedisError

from designtoolkit.web.heartbeat import (
    DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
    HeartbeatStopTimeoutError,
    stop_heartbeat_thread,
)
from designtoolkit.web.media_limits import HostedMediaLimits
from designtoolkit.web.output_limits import OutputLimitExceeded
from designtoolkit.web.resources import (
    CleanupMarker,
    RedisReservation,
    RedisResourceCoordinator,
    RedisSessionLease,
    RedisUploadRegistration,
    ReservationState,
    ResourceUnavailableError,
    SessionUsageGeneration,
)

_DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
_DEFAULT_MAX_SESSION_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MAX_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_MAX_QUEUED_TASKS = 20
_DEFAULT_MAX_UPLOAD_CONCURRENCY = 4
_DEFAULT_MAX_PREVIEW_CONCURRENCY = 2
_DEFAULT_RETENTION_SECONDS = 2 * 60 * 60
_DEFAULT_TASK_TIMEOUT_SECONDS = 2 * 60 * 60
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_RESOURCE_RECOVERY_ATTEMPTS = 3
_DELETION_METADATA_NAME = ".deletion.json"
_UPLOAD_CANCELLATIONS_NAME = ".upload-cancellations"
_UPLOAD_GENERATIONS_NAME = ".upload-generations"
_CLEANUP_GENERATION_NAME = ".cleanup-generation.json"
_DEFAULT_MEDIA_LIMITS = HostedMediaLimits()
_ALLOWED_SUFFIXES = frozenset(
    {
        ".avi",
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".png",
        ".webm",
        ".webp",
    }
)
_SESSION_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_QUARANTINE_PATTERN = re.compile(
    r"^(?P<session_id>[a-f0-9]{32})-(?P<token>[a-f0-9]{32})$"
)
_ASSET_REFERENCE_PATTERN = re.compile(
    r"^asset:(?P<asset_id>[a-f0-9]{32})(?::[^:]*)?$"
)
_LOGGER = logging.getLogger(__name__)


class UploadRejectedError(ValueError):
    pass


class UploadConflictError(UploadRejectedError):
    pass


class AssetDeletionStateError(OSError):
    def __init__(self, asset_id: str) -> None:
        super().__init__("Asset deletion state is invalid")
        self.asset_id = asset_id


class _CleanupClaimHeartbeat:
    def __init__(
        self,
        coordinator: RedisResourceCoordinator,
        session_id: str,
        token: str,
    ) -> None:
        self._coordinator = coordinator
        self._session_id = session_id
        self._token = token
        self._interval = coordinator.cleanup_claim_heartbeat_interval
        self._stop_timeout = getattr(
            coordinator,
            "heartbeat_stop_timeout_seconds",
            DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
        )
        self._stopped = Event()
        self._failure: BaseException | None = None
        self._committed = False
        self._thread = Thread(target=self._run, daemon=True)

    def __enter__(self) -> _CleanupClaimHeartbeat:
        self._coordinator.refresh_cleanup_claim(self._session_id, self._token)
        self._thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            self.stop()
            self.raise_if_failed()
        except BaseException as heartbeat_error:
            if exc_value is None and self._committed:
                _LOGGER.warning(
                    "Cleanup claim heartbeat failed after committed cleanup",
                    exc_info=(
                        type(heartbeat_error),
                        heartbeat_error,
                        heartbeat_error.__traceback__,
                    ),
                )
                return
            if exc_value is None:
                raise
            exc_value.add_note(
                f"Cleanup claim heartbeat also failed: {heartbeat_error}"
            )
            _LOGGER.error(
                "Cleanup claim heartbeat failed during cleanup error",
                exc_info=(
                    type(heartbeat_error),
                    heartbeat_error,
                    heartbeat_error.__traceback__,
                ),
            )

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    @property
    def quiesced(self) -> bool:
        return not self._thread.is_alive()

    def stop(self) -> None:
        try:
            stop_heartbeat_thread(
                self._stopped,
                self._thread,
                timeout=self._stop_timeout,
                heartbeat_name="Cleanup claim heartbeat",
            )
        except HeartbeatStopTimeoutError as exc:
            self._failure = exc
            raise

    def commit[T](self, action: Callable[[], T]) -> T:
        # Rename starts only after every periodic refresh has completed. The
        # synchronous refresh gives the short rename boundary a full claim TTL.
        self.stop()
        self.raise_if_failed()
        self._coordinator.refresh_cleanup_claim(
            self._session_id,
            self._token,
        )
        result = action()
        self._committed = True
        self._stopped = Event()
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        return result

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            if self._stopped.is_set():
                return
            try:
                self._coordinator.refresh_cleanup_claim(
                    self._session_id,
                    self._token,
                )
            except BaseException as exc:
                self._failure = exc
                return


class _UploadResourceHeartbeat:
    def __init__(
        self,
        reservation: RedisReservation,
        lease: RedisSessionLease | None,
        registration: RedisUploadRegistration | None,
        refresh_generation: Callable[[], None] | None,
    ) -> None:
        self._reservation = reservation
        self._lease = lease
        self._registration = registration
        self._refresh_generation = refresh_generation
        intervals = [
            getattr(
                getattr(reservation, "coordinator", None),
                "reservation_heartbeat_interval",
                5.0,
            )
        ]
        if lease is not None:
            intervals.append(
                getattr(
                    getattr(lease, "coordinator", None),
                    "session_lease_heartbeat_interval",
                    5.0,
                )
            )
        coordinators = [
            getattr(reservation, "coordinator", None),
            getattr(lease, "coordinator", None),
            getattr(registration, "coordinator", None),
        ]
        self._stop_timeout = max(
            (
                getattr(
                    coordinator,
                    "heartbeat_stop_timeout_seconds",
                    DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
                )
                for coordinator in coordinators
                if coordinator is not None
            ),
            default=DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
        )
        self._interval = min(intervals)
        self._stopped = Event()
        self._failure: BaseException | None = None
        self._thread = Thread(target=self._run, daemon=True)

    def __enter__(self) -> _UploadResourceHeartbeat:
        self._refresh()
        self._thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        *_args: object,
    ) -> None:
        self.seal()

    def seal(self) -> None:
        try:
            stop_heartbeat_thread(
                self._stopped,
                self._thread,
                timeout=self._stop_timeout,
                heartbeat_name="Upload resource heartbeat",
            )
        except HeartbeatStopTimeoutError as exc:
            self._failure = exc
            raise
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _refresh(self) -> None:
        self._reservation.refresh()
        if self._lease is not None:
            self._lease.refresh()
        if (
            self._registration is not None
            and self._registration.refresh()
        ):
            raise UploadConflictError("Upload was cancelled")
        if self._refresh_generation is not None:
            self._refresh_generation()

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                self._refresh()
            except BaseException as exc:
                self._failure = exc
                return


@dataclass(frozen=True)
class HostedSettings:
    enabled: bool
    data_dir: Path
    redis_url: str
    redis_socket_connect_timeout_seconds: float = 5.0
    redis_socket_timeout_seconds: float = 5.0
    max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES
    max_session_bytes: int = _DEFAULT_MAX_SESSION_BYTES
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES
    min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES
    max_queued_tasks: int = _DEFAULT_MAX_QUEUED_TASKS
    max_image_pixels: int = _DEFAULT_MEDIA_LIMITS.max_image_pixels
    max_video_width: int = _DEFAULT_MEDIA_LIMITS.max_video_width
    max_video_height: int = _DEFAULT_MEDIA_LIMITS.max_video_height
    max_video_duration_seconds: float = (
        _DEFAULT_MEDIA_LIMITS.max_video_duration_seconds
    )
    max_preview_concurrency: int = _DEFAULT_MAX_PREVIEW_CONCURRENCY
    max_upload_concurrency: int = _DEFAULT_MAX_UPLOAD_CONCURRENCY
    max_stream_concurrency: int = 16
    reservation_ttl_seconds: float = 60.0
    reservation_heartbeat_seconds: float = 5.0
    lease_ttl_seconds: float = 60.0
    lease_heartbeat_seconds: float = 5.0
    upload_requests_per_hour: int = 30
    api_requests_per_minute: int = 120
    retention_seconds: int = _DEFAULT_RETENTION_SECONDS
    task_timeout_seconds: int = _DEFAULT_TASK_TIMEOUT_SECONDS
    secure_cookies: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HostedSettings:
        values = os.environ if env is None else env
        settings = cls(
            enabled=_read_bool(values, "DESIGNTOOLKIT_HOSTED", False),
            data_dir=Path(values.get("DESIGNTOOLKIT_DATA_DIR", "/data")),
            redis_url=values.get("REDIS_URL", "redis://redis:6379/0"),
            redis_socket_connect_timeout_seconds=_read_positive_float(
                values,
                "DESIGNTOOLKIT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS",
                5.0,
            ),
            redis_socket_timeout_seconds=_read_positive_float(
                values,
                "DESIGNTOOLKIT_REDIS_SOCKET_TIMEOUT_SECONDS",
                5.0,
            ),
            max_upload_bytes=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_UPLOAD_BYTES",
                _DEFAULT_MAX_UPLOAD_BYTES,
            ),
            max_session_bytes=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_SESSION_BYTES",
                _DEFAULT_MAX_SESSION_BYTES,
            ),
            max_output_bytes=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_OUTPUT_BYTES",
                _DEFAULT_MAX_OUTPUT_BYTES,
            ),
            min_free_bytes=_read_floored_non_negative_int(
                values,
                "DESIGNTOOLKIT_MIN_FREE_BYTES",
                _DEFAULT_MIN_FREE_BYTES,
            ),
            max_queued_tasks=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_QUEUED_TASKS",
                _DEFAULT_MAX_QUEUED_TASKS,
            ),
            max_image_pixels=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_IMAGE_PIXELS",
                _DEFAULT_MEDIA_LIMITS.max_image_pixels,
            ),
            max_video_width=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_VIDEO_WIDTH",
                _DEFAULT_MEDIA_LIMITS.max_video_width,
            ),
            max_video_height=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_VIDEO_HEIGHT",
                _DEFAULT_MEDIA_LIMITS.max_video_height,
            ),
            max_video_duration_seconds=_read_clamped_positive_float(
                values,
                "DESIGNTOOLKIT_MAX_VIDEO_DURATION_SECONDS",
                _DEFAULT_MEDIA_LIMITS.max_video_duration_seconds,
            ),
            max_preview_concurrency=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_PREVIEW_CONCURRENCY",
                _DEFAULT_MAX_PREVIEW_CONCURRENCY,
            ),
            max_upload_concurrency=_read_clamped_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_UPLOAD_CONCURRENCY",
                _DEFAULT_MAX_UPLOAD_CONCURRENCY,
            ),
            max_stream_concurrency=_read_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_STREAM_CONCURRENCY",
                16,
            ),
            reservation_ttl_seconds=_read_positive_float(
                values,
                "DESIGNTOOLKIT_RESERVATION_TTL_SECONDS",
                60.0,
            ),
            reservation_heartbeat_seconds=_read_positive_float(
                values,
                "DESIGNTOOLKIT_RESERVATION_HEARTBEAT_SECONDS",
                5.0,
            ),
            lease_ttl_seconds=_read_positive_float(
                values,
                "DESIGNTOOLKIT_LEASE_TTL_SECONDS",
                60.0,
            ),
            lease_heartbeat_seconds=_read_positive_float(
                values,
                "DESIGNTOOLKIT_LEASE_HEARTBEAT_SECONDS",
                5.0,
            ),
            upload_requests_per_hour=_read_positive_int(
                values,
                "DESIGNTOOLKIT_UPLOAD_REQUESTS_PER_HOUR",
                30,
            ),
            api_requests_per_minute=_read_positive_int(
                values,
                "DESIGNTOOLKIT_API_REQUESTS_PER_MINUTE",
                120,
            ),
            retention_seconds=_read_positive_int(
                values,
                "DESIGNTOOLKIT_RETENTION_SECONDS",
                _DEFAULT_RETENTION_SECONDS,
            ),
            task_timeout_seconds=_read_positive_int(
                values,
                "DESIGNTOOLKIT_TASK_TIMEOUT_SECONDS",
                _DEFAULT_TASK_TIMEOUT_SECONDS,
            ),
            secure_cookies=_read_bool(
                values,
                "DESIGNTOOLKIT_SECURE_COOKIES",
                True,
            ),
        )
        if (
            settings.reservation_ttl_seconds
            <= settings.reservation_heartbeat_seconds
        ):
            raise ValueError(
                "Reservation TTL must be greater than the reservation heartbeat"
            )
        if (
            settings.reservation_ttl_seconds
            <= settings.redis_socket_timeout_seconds
        ):
            raise ValueError(
                "Reservation TTL must be greater than the Redis socket timeout"
            )
        return settings

    @property
    def heartbeat_stop_timeout_seconds(self) -> float:
        return (
            self.redis_socket_connect_timeout_seconds
            + self.redis_socket_timeout_seconds
            + 1.0
        )


def redis_connection_options(settings: HostedSettings) -> dict[str, float]:
    return {
        "socket_connect_timeout": settings.redis_socket_connect_timeout_seconds,
        "socket_timeout": settings.redis_socket_timeout_seconds,
    }


@dataclass(frozen=True)
class UploadedAsset:
    reference: str
    filename: str
    size: int


@dataclass(frozen=True)
class AssetDeletion:
    asset_id: str
    reference: str
    size: int
    pending: bool = False
    expected_remaining_bytes: int | None = None


class HostedStorage:
    def __init__(
        self,
        root: Path,
        *,
        max_upload_bytes: int,
        max_session_bytes: int = _DEFAULT_MAX_SESSION_BYTES,
        min_free_bytes: int = 0,
    ) -> None:
        self.root = root.resolve()
        self.max_upload_bytes = max_upload_bytes
        self.max_session_bytes = max_session_bytes
        self.min_free_bytes = min_free_bytes
        self._deletion_lock = RLock()

    async def store_stream(
        self,
        session_id: str,
        *,
        asset_id: str | None = None,
        filename: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
        reservation: RedisReservation,
        lease: RedisSessionLease | None = None,
        registration: RedisUploadRegistration | None = None,
    ) -> UploadedAsset:
        """Store an upload with conservative Redis/filesystem recovery.

        Redis and the filesystem cannot be committed atomically. Unpublished
        data is therefore kept hidden whenever Redis state remains ambiguous,
        and published data is never removed because marker cleanup failed.
        Session expiry eventually removes quarantined hidden data.
        """
        try:
            session_id = _validate_session_id(session_id)
            asset_id = (
                uuid4().hex if asset_id is None else _asset_id(f"asset:{asset_id}")
            )
            filename = _safe_filename(filename)
            suffix = Path(filename).suffix.lower()
            if suffix not in _ALLOWED_SUFFIXES:
                raise UploadRejectedError("File type is unsupported")
            if (
                isinstance(content_length, bool)
                or not isinstance(content_length, int)
                or content_length <= 0
            ):
                raise UploadRejectedError("Content-Length must be a positive integer")
            if content_length > self.max_upload_bytes:
                raise UploadRejectedError("File exceeds the configured file limit")
        except Exception:
            _release_best_effort(reservation)
            raise

        session_dir = self._session_dir(session_id)
        asset_dir = session_dir / "assets" / asset_id
        temporary_path = asset_dir / ".uploading"
        final_path = asset_dir / filename
        size = 0
        committed = False
        owns_asset_dir = False
        try:
            def refresh_generation() -> None:
                if registration is None:
                    return
                self._refresh_upload_generation(
                    session_id,
                    asset_id,
                    registration.token,
                )

            with _UploadResourceHeartbeat(
                reservation,
                lease,
                registration,
                refresh_generation,
            ) as heartbeat:
                with self._deletion_lock:
                    self._raise_if_upload_cancelled(
                        session_id,
                        asset_id,
                        registration,
                    )
                    try:
                        asset_dir.mkdir(parents=True, exist_ok=False)
                    except FileExistsError as exc:
                        raise UploadConflictError("Upload ID already exists") from exc
                    owns_asset_dir = True
                with temporary_path.open("xb") as destination:
                    async for chunk in chunks:
                        heartbeat.raise_if_failed()
                        self._raise_if_upload_cancelled(
                            session_id,
                            asset_id,
                            registration,
                        )
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > content_length:
                            raise UploadRejectedError(
                                "Upload exceeds declared Content-Length"
                            )
                        if size > self.max_upload_bytes:
                            raise UploadRejectedError(
                                "File exceeds the configured file limit"
                            )
                        destination.write(chunk)
                heartbeat.raise_if_failed()
                if size != content_length:
                    raise UploadRejectedError(
                        "Upload size does not match declared Content-Length"
                    )
                self._raise_if_upload_cancelled(
                    session_id,
                    asset_id,
                    registration,
                )
                os.utime(self._session_dir(session_id), None)
                heartbeat.seal()
                self._raise_if_upload_cancelled(
                    session_id,
                    asset_id,
                    registration,
                )
                _commit_with_recovery(reservation)
                committed = True
                with self._deletion_lock:
                    self._raise_if_upload_cancelled(
                        session_id,
                        asset_id,
                        registration,
                    )
                    temporary_path.replace(final_path)
                    self._raise_if_upload_cancelled(
                        session_id,
                        asset_id,
                        registration,
                    )
        except HeartbeatStopTimeoutError:
            if owns_asset_dir:
                self._discard_asset(session_id, asset_dir)
            raise
        except _AmbiguousCommitError:
            raise
        except Exception:
            if committed:
                if _rollback_with_recovery(reservation):
                    if owns_asset_dir:
                        self._discard_asset(session_id, asset_dir)
            else:
                _release_best_effort(reservation)
                if owns_asset_dir:
                    self._discard_asset(session_id, asset_dir)
            raise

        _finalize_best_effort(reservation)
        return UploadedAsset(
            reference=f"asset:{asset_id}:{quote(filename, safe='')}",
            filename=filename,
            size=size,
        )

    def claim_upload_generation(
        self,
        session_id: str,
        asset_id: str,
        *,
        token: str,
    ) -> None:
        session_id = _validate_session_id(session_id)
        asset_id = _asset_id(f"asset:{asset_id}")
        if not _SESSION_PATTERN.fullmatch(token):
            raise UploadRejectedError("Upload generation token is invalid")
        session_dir = self._session_dir(session_id)
        generation = session_dir / _UPLOAD_GENERATIONS_NAME / asset_id
        cancellation = session_dir / _UPLOAD_CANCELLATIONS_NAME / asset_id
        with self._deletion_lock:
            if (session_dir / "assets" / asset_id).exists() or (
                session_dir / ".trash" / asset_id
            ).exists():
                raise UploadConflictError("Upload ID already exists")
            if generation.exists() or cancellation.exists():
                # The successful Redis registration fences every older token.
                self._clear_orphaned_upload_state(
                    session_id,
                    asset_id,
                )
            generation.parent.mkdir(parents=True, exist_ok=True)
            try:
                with generation.open("x", encoding="ascii") as marker:
                    marker.write(token)
            except FileExistsError as exc:
                raise UploadConflictError("Upload ID already exists") from exc

    def resolve(self, session_id: str, reference: str) -> Path:
        session_id = _validate_session_id(session_id)
        asset_id = _asset_id(reference)
        asset_dir = self._session_dir(session_id) / "assets" / asset_id
        matches = [
            path
            for path in asset_dir.iterdir()
            if path.is_file() and not path.name.startswith(".")
        ] if asset_dir.is_dir() else []
        if len(matches) != 1:
            raise UploadRejectedError("Uploaded asset is not available")
        os.utime(self._session_dir(session_id), None)
        return matches[0]

    def delete_asset(
        self,
        session_id: str,
        reference: str,
        *,
        cancel_inflight: bool = False,
        orphan_stale_after_seconds: float | None = None,
    ) -> AssetDeletion | None:
        session_id = _validate_session_id(session_id)
        asset_id = _asset_id(reference)
        session_dir = self._session_dir(session_id)
        assets_dir = session_dir / "assets"
        asset_dir = assets_dir / asset_id
        tombstone = session_dir / ".trash" / asset_id
        generation = session_dir / _UPLOAD_GENERATIONS_NAME / asset_id
        cancellation = session_dir / _UPLOAD_CANCELLATIONS_NAME / asset_id
        with self._deletion_lock:
            if tombstone.is_dir():
                self._mark_upload_cancelled(session_id, asset_id)
                return _read_asset_deletion(tombstone, asset_id)
            if not asset_dir.is_dir():
                if not (
                    cancel_inflight
                    or generation.is_file()
                    or cancellation.is_file()
                ):
                    return None
                if (
                    not cancel_inflight
                    and orphan_stale_after_seconds is not None
                    and self._clear_orphaned_upload_state(
                        session_id,
                        asset_id,
                        stale_after_seconds=orphan_stale_after_seconds,
                    )
                ):
                    return None
                self._mark_upload_cancelled(session_id, asset_id)
                return AssetDeletion(
                    asset_id=asset_id,
                    reference=f"asset:{asset_id}",
                    size=0,
                    pending=True,
                )

            if (
                not cancel_inflight
                and orphan_stale_after_seconds is not None
                and self._upload_state_is_stale(
                    session_id,
                    asset_id,
                    stale_after_seconds=orphan_stale_after_seconds,
                )
            ):
                orphan_size = _unpublished_upload_size(asset_dir)
                expected_remaining_bytes = _auditable_session_upload_bytes(
                    session_dir,
                    excluded_asset_id=asset_id,
                )
                if (
                    orphan_size is not None
                    and expected_remaining_bytes is not None
                ):
                    tombstone.parent.mkdir(parents=True, exist_ok=True)
                    deletion = AssetDeletion(
                        asset_id=asset_id,
                        reference=f"asset:{asset_id}",
                        size=orphan_size,
                        expected_remaining_bytes=expected_remaining_bytes,
                    )
                    try:
                        _write_asset_deletion(asset_dir, deletion)
                        asset_dir.replace(tombstone)
                    except OSError:
                        if tombstone.is_dir():
                            return _read_asset_deletion(tombstone, asset_id)
                        raise
                    self._clear_orphaned_upload_state(session_id, asset_id)
                    os.utime(session_dir, None)
                    return deletion

            self._mark_upload_cancelled(session_id, asset_id)
            if (asset_dir / ".uploading").is_file():
                return AssetDeletion(
                    asset_id=asset_id,
                    reference=f"asset:{asset_id}",
                    size=0,
                    pending=True,
                )

            tombstone.parent.mkdir(parents=True, exist_ok=True)
            deletion = AssetDeletion(
                asset_id=asset_id,
                reference=f"asset:{asset_id}",
                size=_asset_payload_size(asset_dir),
            )
            try:
                _write_asset_deletion(asset_dir, deletion)
                asset_dir.replace(tombstone)
            except OSError:
                if tombstone.is_dir():
                    return _read_asset_deletion(tombstone, asset_id)
                raise

        os.utime(session_dir, None)
        return deletion

    def has_upload_state(self, session_id: str, asset_id: str) -> bool:
        session_id = _validate_session_id(session_id)
        asset_id = _asset_id(f"asset:{asset_id}")
        session_dir = self._session_dir(session_id)
        with self._deletion_lock:
            return any(
                (
                    (session_dir / "assets" / asset_id).is_dir(),
                    (session_dir / ".trash" / asset_id).is_dir(),
                    (
                        session_dir / _UPLOAD_CANCELLATIONS_NAME / asset_id
                    ).is_file(),
                    (session_dir / _UPLOAD_GENERATIONS_NAME / asset_id).is_file(),
                )
            )

    def finalize_asset_deletion(self, session_id: str, asset_id: str) -> None:
        session_id = _validate_session_id(session_id)
        asset_id = _asset_id(f"asset:{asset_id}")
        session_dir = self._session_dir(session_id)
        tombstone = session_dir / ".trash" / asset_id
        with self._deletion_lock:
            shutil.rmtree(tombstone, ignore_errors=True)
            if tombstone.exists():
                raise OSError("Asset tombstone removal failed")
            (
                session_dir / _UPLOAD_CANCELLATIONS_NAME / asset_id
            ).unlink(missing_ok=True)
            (
                session_dir / _UPLOAD_GENERATIONS_NAME / asset_id
            ).unlink(missing_ok=True)
            self._remove_empty_session(session_id)

    def create_task_dir(self, session_id: str) -> Path:
        session_id = _validate_session_id(session_id)
        task_dir = self._session_dir(session_id) / "tasks" / uuid4().hex
        task_dir.mkdir(parents=True, exist_ok=False)
        os.utime(self._session_dir(session_id), None)
        return task_dir

    def discard_empty_task_dir(
        self,
        session_id: str,
        candidate: str | Path | None,
    ) -> None:
        if candidate is None:
            return
        session_id = _validate_session_id(session_id)
        tasks_dir = self._session_dir(session_id) / "tasks"
        try:
            relative = Path(candidate).resolve().relative_to(tasks_dir.resolve())
        except ValueError:
            return
        if not relative.parts:
            return
        task_id = relative.parts[0]
        if not _SESSION_PATTERN.fullmatch(task_id):
            return
        task_dir = tasks_dir / task_id
        if not task_dir.is_dir() or any(path.is_file() for path in task_dir.rglob("*")):
            return
        shutil.rmtree(task_dir)
        if tasks_dir.is_dir() and not any(tasks_dir.iterdir()):
            tasks_dir.rmdir()
        self._remove_empty_session(session_id)

    def touch_session(self, session_id: str) -> None:
        session_dir = self._session_dir(_validate_session_id(session_id))
        if session_dir.is_dir():
            os.utime(session_dir, None)

    def validated_result_path(self, value: str | None) -> Path:
        if value is None:
            raise UploadRejectedError("Task result is not available")
        path = Path(value).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise UploadRejectedError("Task result is not available")
        return path

    def cleanup_expired(
        self,
        *,
        retention_seconds: int,
        coordinator: RedisResourceCoordinator,
    ) -> int:
        self._finalize_orphaned_cleanup_markers(coordinator)
        removed = self._cleanup_orphaned_quarantine(coordinator)
        sessions_dir = self.root / "sessions"
        if not sessions_dir.is_dir():
            return removed
        cutoff = time() - retention_seconds
        try:
            session_dirs = tuple(sessions_dir.iterdir())
        except OSError:
            _LOGGER.warning(
                "Failed to scan hosted sessions",
                extra={"sessions_dir": str(sessions_dir)},
                exc_info=True,
            )
            return removed
        for session_dir in session_dirs:
            try:
                expired = (
                    session_dir.is_dir()
                    and _SESSION_PATTERN.fullmatch(session_dir.name) is not None
                    and _directory_modified_at(session_dir) < cutoff
                )
            except OSError:
                self._log_session_scan_failure(session_dir)
                continue
            if expired:
                try:
                    removed += self._cleanup_expired_session(
                        session_dir,
                        cutoff=cutoff,
                        coordinator=coordinator,
                    )
                except Exception:
                    self._log_session_scan_failure(session_dir)
        self._remove_empty_trash()
        return removed

    def _cleanup_expired_session(
        self,
        session_dir: Path,
        *,
        cutoff: float,
        coordinator: RedisResourceCoordinator,
    ) -> int:
        session_id = session_dir.name
        token = coordinator.claim_session_cleanup(session_id)
        if token is None:
            return 0
        heartbeat = _CleanupClaimHeartbeat(
            coordinator,
            session_id,
            token,
        )
        try:
            with heartbeat:
                try:
                    modified_at = _directory_modified_at(session_dir)
                except OSError:
                    self._log_session_scan_failure(session_dir)
                    return 0
                if modified_at >= cutoff:
                    return 0
                heartbeat.raise_if_failed()
                trash_dir = self.root / ".trash"
                trash_dir.mkdir(parents=True, exist_ok=True)
                quarantine = trash_dir / f"{session_id}-{token}"
                generation = coordinator.capture_session_usage(
                    session_id,
                    token,
                )
                _write_session_usage_generation(session_dir, generation)
                heartbeat.commit(
                    lambda source=session_dir, destination=quarantine: os.replace(
                        source,
                        destination,
                    )
                )
                heartbeat.raise_if_failed()
                coordinator.clear_session_usage_generation(
                    session_id,
                    token,
                    generation,
                )
                heartbeat.raise_if_failed()
                quarantine_removed = self._delete_quarantine(quarantine)
                heartbeat.raise_if_failed()
            if quarantine_removed:
                coordinator.finalize_session_usage_generation(
                    generation.generation_id
                )
                return 1
            return 0
        finally:
            if heartbeat.quiesced:
                coordinator.release_cleanup_claim(session_id, token)

    @staticmethod
    def _log_session_scan_failure(session_dir: Path) -> None:
        _LOGGER.warning(
            "Failed to inspect or clean hosted session",
            extra={
                "session_dir": str(session_dir),
                "session_id": session_dir.name,
            },
            exc_info=True,
        )

    def _finalize_orphaned_cleanup_markers(
        self,
        coordinator: RedisResourceCoordinator,
    ) -> None:
        for marker in coordinator.pending_cleanup_markers():
            quarantine = (
                self.root
                / ".trash"
                / f"{marker.session_id}-{marker.generation_id}"
            )
            if (
                quarantine.exists()
                or quarantine.is_symlink()
                or _session_has_cleanup_generation(self.root, marker)
            ):
                continue
            if marker.legacy:
                coordinator.finalize_legacy_session_usage(marker.generation_id)
            else:
                coordinator.finalize_session_usage_generation(marker.generation_id)

    def _cleanup_orphaned_quarantine(
        self,
        coordinator: RedisResourceCoordinator,
    ) -> int:
        trash_dir = self.root / ".trash"
        if not trash_dir.is_dir():
            return 0
        removed = 0
        for quarantine in trash_dir.iterdir():
            match = _QUARANTINE_PATTERN.fullmatch(quarantine.name)
            if not quarantine.is_dir() or match is None:
                continue
            session_id = match.group("session_id")
            generation_id = match.group("token")
            claim_token = coordinator.claim_session_cleanup(session_id)
            if claim_token is None:
                continue
            heartbeat = _CleanupClaimHeartbeat(
                coordinator,
                session_id,
                claim_token,
            )
            try:
                try:
                    with heartbeat:
                        generation = _read_session_usage_generation(
                            quarantine,
                            expected_generation_id=generation_id,
                        )
                        if generation is None:
                            current_bytes = _auditable_session_upload_bytes(
                                self._session_dir(session_id)
                            )
                            legacy_physical_bytes = _auditable_session_upload_bytes(
                                quarantine
                            )
                            if (
                                current_bytes is None
                                or legacy_physical_bytes is None
                            ):
                                raise ResourceUnavailableError(
                                    "Legacy session usage cannot be audited safely"
                                )
                            coordinator.reconcile_legacy_session_usage(
                                session_id,
                                claim_token,
                                generation_id,
                                current_committed_bytes=current_bytes,
                                legacy_physical_bytes=legacy_physical_bytes,
                            )
                        else:
                            coordinator.clear_session_usage_generation(
                                session_id,
                                claim_token,
                                generation,
                            )
                        heartbeat.raise_if_failed()
                        quarantine_removed = self._delete_quarantine(quarantine)
                        heartbeat.raise_if_failed()
                    if quarantine_removed:
                        if generation is not None:
                            coordinator.finalize_session_usage_generation(
                                generation.generation_id
                            )
                        else:
                            coordinator.finalize_legacy_session_usage(generation_id)
                        removed += 1
                finally:
                    if heartbeat.quiesced:
                        coordinator.release_cleanup_claim(session_id, claim_token)
            except Exception:
                _LOGGER.warning(
                    "Failed to recover quarantined session",
                    extra={
                        "generation_id": generation_id,
                        "quarantine": str(quarantine),
                        "session_id": session_id,
                    },
                    exc_info=True,
                )
        self._remove_empty_trash()
        return removed

    @staticmethod
    def _delete_quarantine(quarantine: Path) -> bool:
        try:
            shutil.rmtree(quarantine)
        except OSError:
            return False
        return not quarantine.exists()

    def _remove_empty_trash(self) -> None:
        trash_dir = self.root / ".trash"
        if trash_dir.is_dir() and not any(trash_dir.iterdir()):
            trash_dir.rmdir()

    def _session_dir(self, session_id: str) -> Path:
        return self.root / "sessions" / session_id

    def _remove_empty_session(self, session_id: str) -> None:
        session_dir = self._session_dir(session_id)
        assets_dir = session_dir / "assets"
        if assets_dir.is_dir() and not any(assets_dir.iterdir()):
            assets_dir.rmdir()
        trash_dir = session_dir / ".trash"
        if trash_dir.is_dir() and not any(trash_dir.iterdir()):
            trash_dir.rmdir()
        cancellations_dir = session_dir / _UPLOAD_CANCELLATIONS_NAME
        if cancellations_dir.is_dir() and not any(cancellations_dir.iterdir()):
            cancellations_dir.rmdir()
        generations_dir = session_dir / _UPLOAD_GENERATIONS_NAME
        if generations_dir.is_dir() and not any(generations_dir.iterdir()):
            generations_dir.rmdir()
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            session_dir.rmdir()
        sessions_dir = self.root / "sessions"
        if sessions_dir.is_dir() and not any(sessions_dir.iterdir()):
            sessions_dir.rmdir()

    def _discard_asset(self, session_id: str, asset_dir: Path) -> None:
        shutil.rmtree(asset_dir, ignore_errors=True)
        self._remove_empty_session(session_id)

    def _mark_upload_cancelled(self, session_id: str, asset_id: str) -> None:
        marker = self._session_dir(session_id) / _UPLOAD_CANCELLATIONS_NAME / asset_id
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.is_file():
            marker.touch(exist_ok=False)

    def _clear_orphaned_upload_state(
        self,
        session_id: str,
        asset_id: str,
        *,
        stale_after_seconds: float | None = None,
    ) -> bool:
        session_dir = self._session_dir(session_id)
        generation = session_dir / _UPLOAD_GENERATIONS_NAME / asset_id
        cancellation = session_dir / _UPLOAD_CANCELLATIONS_NAME / asset_id
        markers = tuple(
            marker for marker in (generation, cancellation) if marker.is_file()
        )
        if not markers:
            return False
        if stale_after_seconds is not None and not self._upload_state_is_stale(
            session_id,
            asset_id,
            stale_after_seconds=stale_after_seconds,
        ):
            return False
        generation.unlink(missing_ok=True)
        cancellation.unlink(missing_ok=True)
        self._remove_empty_session(session_id)
        return True

    def _upload_state_is_stale(
        self,
        session_id: str,
        asset_id: str,
        *,
        stale_after_seconds: float,
    ) -> bool:
        session_dir = self._session_dir(session_id)
        markers = tuple(
            marker
            for marker in (
                session_dir / _UPLOAD_GENERATIONS_NAME / asset_id,
                session_dir / _UPLOAD_CANCELLATIONS_NAME / asset_id,
            )
            if marker.is_file()
        )
        if not markers:
            return False
        cutoff = time() - stale_after_seconds
        return all(marker.stat().st_mtime <= cutoff for marker in markers)

    def _refresh_upload_generation(
        self,
        session_id: str,
        asset_id: str,
        token: str,
    ) -> None:
        generation = (
            self._session_dir(session_id) / _UPLOAD_GENERATIONS_NAME / asset_id
        )
        with self._deletion_lock:
            try:
                owner = generation.read_text(encoding="ascii")
            except (FileNotFoundError, OSError, UnicodeError) as exc:
                raise ResourceUnavailableError(
                    "Upload generation expired"
                ) from exc
            if owner != token:
                raise ResourceUnavailableError("Upload generation expired")
            generation.touch()

    def _raise_if_upload_cancelled(
        self,
        session_id: str,
        asset_id: str,
        registration: RedisUploadRegistration | None = None,
    ) -> None:
        marker = self._session_dir(session_id) / _UPLOAD_CANCELLATIONS_NAME / asset_id
        if marker.is_file() or (
            registration is not None and registration.refresh()
        ):
            raise UploadConflictError("Upload was cancelled")


def _write_session_usage_generation(
    session_dir: Path,
    generation: SessionUsageGeneration,
) -> None:
    payload = {
        "generation_id": generation.generation_id,
        "committed_bytes": generation.committed_bytes,
        "released_asset_ids": list(generation.released_asset_ids),
    }
    (session_dir / _CLEANUP_GENERATION_NAME).write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


def _read_session_usage_generation(
    quarantine: Path,
    *,
    expected_generation_id: str,
) -> SessionUsageGeneration | None:
    try:
        payload = json.loads(
            (quarantine / _CLEANUP_GENERATION_NAME).read_text(encoding="utf-8")
        )
        generation_id = payload["generation_id"]
        committed_bytes = payload["committed_bytes"]
        released_asset_ids = payload["released_asset_ids"]
        if (
            generation_id != expected_generation_id
            or not _SESSION_PATTERN.fullmatch(generation_id)
            or isinstance(committed_bytes, bool)
            or not isinstance(committed_bytes, int)
            or committed_bytes < 0
            or not isinstance(released_asset_ids, list)
            or any(
                not isinstance(asset_id, str)
                or not _SESSION_PATTERN.fullmatch(asset_id)
                for asset_id in released_asset_ids
            )
        ):
            return None
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return SessionUsageGeneration(
        generation_id=generation_id,
        committed_bytes=committed_bytes,
        released_asset_ids=tuple(sorted(set(released_asset_ids))),
    )


def _session_has_cleanup_generation(root: Path, marker: CleanupMarker) -> bool:
    metadata_path = (
        root / "sessions" / marker.session_id / _CLEANUP_GENERATION_NAME
    )
    if not metadata_path.exists():
        return False
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        generation_id = payload["generation_id"]
    except (OSError, KeyError, TypeError, ValueError):
        return True
    return (
        not isinstance(generation_id, str)
        or not _SESSION_PATTERN.fullmatch(generation_id)
        or generation_id == marker.generation_id
    )


def new_session_id() -> str:
    return uuid4().hex


def prepare_download_artifact(
    task_id: str,
    output_value: Sequence[Path] | str,
    *,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> Path:
    paths = decode_output_paths(output_value)
    if not paths or any(not path.exists() for path in paths):
        raise FileNotFoundError("Task output is not available")
    output_files = {path: _collect_download_files(path) for path in paths}
    output_size = sum(
        child.lstat().st_size for files in output_files.values() for child in files
    )
    if output_size > max_output_bytes:
        _remove_output_paths(paths)
        raise UploadRejectedError("Task output exceeds the configured limit")
    if len(paths) == 1 and paths[0].is_file():
        return paths[0]

    archive_path = paths[0].parent / f"{task_id}.zip"
    try:
        with archive_path.open("w+b") as archive_file:
            budgeted_archive = _BudgetedArchiveFile(
                archive_file,
                source_bytes=output_size,
                max_bytes=max_output_bytes,
            )
            with ZipFile(
                budgeted_archive,
                "w",
                compression=ZIP_DEFLATED,
            ) as archive:
                for path in paths:
                    files = output_files[path]
                    if S_ISREG(path.lstat().st_mode):
                        _archive_file(
                            archive,
                            path,
                            path.name,
                            budgeted_archive,
                        )
                        continue
                    for child in files:
                        _archive_file(
                            archive,
                            child,
                            str(Path(path.name) / child.relative_to(path)),
                            budgeted_archive,
                        )
                    shutil.rmtree(path)
    except Exception:
        archive_path.unlink(missing_ok=True)
        _remove_output_paths(paths)
        raise
    return archive_path


def decode_output_paths(output_value: Sequence[Path] | str) -> list[Path]:
    if isinstance(output_value, str):
        return [Path(value) for value in output_value.split("; ") if value]
    return list(output_value)


class _BudgetedArchiveFile:
    def __init__(
        self,
        file: BinaryIO,
        *,
        source_bytes: int,
        max_bytes: int,
    ) -> None:
        self._file = file
        self._source_bytes = source_bytes
        self._archive_bytes = 0
        self._max_bytes = max_bytes

    def write(self, data: bytes) -> int:
        projected_size = max(
            self._archive_bytes,
            self._file.tell() + len(data),
        )
        if self._source_bytes + projected_size > self._max_bytes:
            raise OutputLimitExceeded(
                "Task output exceeds the configured "
                f"{self._max_bytes}-byte limit"
            )
        written = self._file.write(data)
        self._archive_bytes = max(self._archive_bytes, self._file.tell())
        return written

    def release_source(self, size: int) -> None:
        self._source_bytes -= size

    def tell(self) -> int:
        return self._file.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def flush(self) -> None:
        self._file.flush()


def _archive_file(
    archive: ZipFile,
    source: Path,
    archive_name: str,
    budgeted_archive: _BudgetedArchiveFile,
) -> None:
    source_size = source.lstat().st_size
    with source.open("rb") as input_file, archive.open(archive_name, "w") as output:
        shutil.copyfileobj(input_file, output, length=_UPLOAD_CHUNK_BYTES)
    source.unlink()
    budgeted_archive.release_source(source_size)


def _remove_output_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _collect_download_files(path: Path) -> tuple[Path, ...]:
    root_stat = path.lstat()
    if _is_link_or_junction(path, root_stat.st_mode):
        raise UploadRejectedError("Task output contains a link or junction")
    if S_ISREG(root_stat.st_mode):
        return (path,)
    if not S_ISDIR(root_stat.st_mode):
        return ()

    root = path.resolve(strict=True)
    files: list[Path] = []
    pending = [path]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            child_stat = child.lstat()
            if _is_link_or_junction(child, child_stat.st_mode):
                raise UploadRejectedError("Task output contains a link or junction")
            try:
                child.resolve(strict=True).relative_to(root)
            except ValueError as exc:
                raise UploadRejectedError(
                    "Task output contains a path outside its output directory"
                ) from exc
            if S_ISREG(child_stat.st_mode):
                files.append(child)
            elif S_ISDIR(child_stat.st_mode):
                pending.append(child)
    return tuple(sorted(files))


def _is_link_or_junction(path: Path, mode: int) -> bool:
    return S_ISLNK(mode) or path.is_junction()


def is_valid_session_id(value: str | None) -> bool:
    return bool(value and _SESSION_PATTERN.fullmatch(value))


def _validate_session_id(value: str) -> str:
    if not is_valid_session_id(value):
        raise UploadRejectedError("Invalid upload session")
    return value


def _asset_id(reference: str) -> str:
    match = _ASSET_REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        raise UploadRejectedError("Uploaded asset is not available")
    value = match.group("asset_id")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise UploadRejectedError("Uploaded asset is not available") from exc
    if parsed.hex != value:
        raise UploadRejectedError("Uploaded asset is not available")
    return value


def _asset_payload_size(asset_dir: Path) -> int:
    return sum(
        path.stat().st_size
        for path in asset_dir.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(asset_dir).parts)
    )


def _auditable_session_upload_bytes(
    session_dir: Path,
    *,
    excluded_asset_id: str | None = None,
) -> int | None:
    assets_dir = session_dir / "assets"
    if not assets_dir.exists():
        return 0
    if not assets_dir.is_dir():
        return None

    total = 0
    for asset_dir in assets_dir.iterdir():
        if asset_dir.name == excluded_asset_id:
            continue
        if (
            not asset_dir.is_dir()
            or not _SESSION_PATTERN.fullmatch(asset_dir.name)
        ):
            return None
        files = [path for path in asset_dir.rglob("*") if path.is_file()]
        visible_files = [
            path
            for path in files
            if not any(
                part.startswith(".")
                for part in path.relative_to(asset_dir).parts
            )
        ]
        if not visible_files:
            entries = list(asset_dir.iterdir())
            if (
                len(entries) == 1
                and entries[0].name == ".uploading"
                and S_ISREG(entries[0].lstat().st_mode)
            ):
                continue
            return None
        if len(files) != 1 or len(visible_files) != 1:
            return None
        total += visible_files[0].stat().st_size
    return total


def _unpublished_upload_size(asset_dir: Path) -> int | None:
    uploading_size: int | None = None
    for path in asset_dir.iterdir():
        path_stat = path.lstat()
        if not path.name.startswith(".") or not S_ISREG(path_stat.st_mode):
            return None
        if path.name == ".uploading":
            uploading_size = path_stat.st_size
    return uploading_size


def _write_asset_deletion(asset_dir: Path, deletion: AssetDeletion) -> None:
    metadata_path = asset_dir / _DELETION_METADATA_NAME
    temporary_path = asset_dir / f"{_DELETION_METADATA_NAME}.{uuid4().hex}.tmp"
    payload = {
        "asset_id": deletion.asset_id,
        "reference": deletion.reference,
        "bytes": deletion.size,
    }
    if deletion.expected_remaining_bytes is not None:
        payload["expected_remaining_bytes"] = deletion.expected_remaining_bytes
    try:
        with temporary_path.open("x", encoding="utf-8") as metadata:
            json.dump(payload, metadata, separators=(",", ":"), sort_keys=True)
            metadata.flush()
            os.fsync(metadata.fileno())
        temporary_path.replace(metadata_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_asset_deletion(tombstone: Path, asset_id: str) -> AssetDeletion:
    try:
        payload = json.loads(
            (tombstone / _DELETION_METADATA_NAME).read_text(encoding="utf-8")
        )
        reference = payload["reference"]
        size = payload["bytes"]
        expected_remaining_bytes = payload.get("expected_remaining_bytes")
        if (
            payload["asset_id"] != asset_id
            or reference != f"asset:{asset_id}"
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or (
                expected_remaining_bytes is not None
                and (
                    isinstance(expected_remaining_bytes, bool)
                    or not isinstance(expected_remaining_bytes, int)
                    or expected_remaining_bytes < 0
                )
            )
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AssetDeletionStateError(asset_id) from exc
    return AssetDeletion(
        asset_id=asset_id,
        reference=reference,
        size=size,
        expected_remaining_bytes=expected_remaining_bytes,
    )


def _safe_filename(value: str | None) -> str:
    raw_filename = value or ""
    if any(category(character) == "Cc" for character in raw_filename):
        raise UploadRejectedError("File name is invalid")
    filename = Path(raw_filename.replace("\\", "/")).name.strip()
    if not filename or filename.startswith("."):
        raise UploadRejectedError("File name is invalid")
    return filename


def _read_bool(
    env: Mapping[str, str],
    key: str,
    default: bool,
) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _read_positive_int(
    env: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _read_clamped_positive_int(
    env: Mapping[str, str],
    key: str,
    ceiling: int,
) -> int:
    value = _read_positive_int(env, key, ceiling)
    if value > ceiling:
        _LOGGER.warning(
            "%s=%s exceeds hosted safety ceiling %s; clamping to %s",
            key,
            value,
            ceiling,
            ceiling,
        )
        return ceiling
    return value


def _read_non_negative_int(
    env: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{key} must not be negative")
    return value


def _read_floored_non_negative_int(
    env: Mapping[str, str],
    key: str,
    floor: int,
) -> int:
    value = _read_non_negative_int(env, key, floor)
    if value < floor:
        _LOGGER.warning(
            "%s=%s is below hosted safety floor %s; clamping to %s",
            key,
            value,
            floor,
            floor,
        )
        return floor
    return value


def _read_positive_float(
    env: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    raw = env.get(key)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _read_clamped_positive_float(
    env: Mapping[str, str],
    key: str,
    ceiling: float,
) -> float:
    value = _read_positive_float(env, key, ceiling)
    if not isfinite(value):
        raise ValueError(f"{key} must be positive")
    if value > ceiling:
        _LOGGER.warning(
            "%s=%s exceeds hosted safety ceiling %s; clamping to %s",
            key,
            value,
            ceiling,
            ceiling,
        )
        return ceiling
    return value


def _directory_modified_at(path: Path) -> float:
    root_modified_at = path.stat().st_mtime
    descendant_modified_at = max(
        (entry.stat().st_mtime for entry in path.rglob("*")),
        default=root_modified_at,
    )
    return max(root_modified_at, descendant_modified_at)


class _AmbiguousCommitError(ResourceUnavailableError):
    pass


def _commit_with_recovery(reservation: RedisReservation) -> None:
    last_error: RedisError | None = None
    for _attempt in range(_RESOURCE_RECOVERY_ATTEMPTS):
        try:
            reservation.commit()
        except RedisError as exc:
            last_error = exc
            continue
        except ResourceUnavailableError:
            if last_error is None:
                raise
            break
        return

    for _attempt in range(_RESOURCE_RECOVERY_ATTEMPTS):
        try:
            state = reservation.state()
        except RedisError as exc:
            last_error = exc
            continue
        if state is ReservationState.COMMITTED:
            return
        if state is ReservationState.RESERVED:
            raise ResourceUnavailableError(
                "Upload reservation was not committed"
            ) from last_error
        break

    raise _AmbiguousCommitError(
        "Upload commit state could not be confirmed; hidden data was quarantined"
    ) from last_error


def _rollback_with_recovery(reservation: RedisReservation) -> bool:
    for _attempt in range(_RESOURCE_RECOVERY_ATTEMPTS):
        try:
            return reservation.rollback()
        except RedisError:
            continue
    return False


def _finalize_best_effort(reservation: RedisReservation) -> None:
    for _attempt in range(_RESOURCE_RECOVERY_ATTEMPTS):
        try:
            reservation.finalize()
        except RedisError:
            continue
        return


def _release_best_effort(reservation: RedisReservation) -> None:
    try:
        reservation.release()
    except RedisError:
        pass


def _ensure_free_space(root: Path, minimum_free_bytes: int) -> None:
    if minimum_free_bytes <= 0:
        return
    existing = root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if shutil.disk_usage(existing).free < minimum_free_bytes:
        raise UploadRejectedError("Server storage is temporarily unavailable")
