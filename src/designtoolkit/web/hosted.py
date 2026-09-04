from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG
from threading import Event, RLock, Thread
from time import time
from unicodedata import category
from urllib.parse import quote
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from redis.exceptions import RedisError

from designtoolkit.web.media_limits import HostedMediaLimits
from designtoolkit.web.resources import (
    RedisReservation,
    RedisResourceCoordinator,
    RedisSessionLease,
    ReservationState,
    ResourceUnavailableError,
)

_DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
_DEFAULT_MAX_SESSION_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MAX_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_MAX_QUEUED_TASKS = 20
_DEFAULT_RETENTION_SECONDS = 2 * 60 * 60
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_RESOURCE_RECOVERY_ATTEMPTS = 3
_DELETION_METADATA_NAME = ".deletion.json"
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


class UploadRejectedError(ValueError):
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
        self._stopped = Event()
        self._failure: BaseException | None = None
        self._thread = Thread(target=self._run, daemon=True)

    def __enter__(self) -> _CleanupClaimHeartbeat:
        self._coordinator.refresh_cleanup_claim(self._session_id, self._token)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stopped.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
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
    ) -> None:
        self._reservation = reservation
        self._lease = lease
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
        exc_type: type[BaseException] | None,
        *_args: object,
    ) -> None:
        self._stopped.set()
        self._thread.join()
        if exc_type is None:
            self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _refresh(self) -> None:
        self._reservation.refresh()
        if self._lease is not None:
            self._lease.refresh()

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
    max_preview_concurrency: int = 2
    max_upload_concurrency: int = 4
    reservation_ttl_seconds: float = 60.0
    reservation_heartbeat_seconds: float = 5.0
    lease_ttl_seconds: float = 60.0
    lease_heartbeat_seconds: float = 5.0
    upload_requests_per_hour: int = 30
    api_requests_per_minute: int = 120
    retention_seconds: int = _DEFAULT_RETENTION_SECONDS
    secure_cookies: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HostedSettings:
        values = os.environ if env is None else env
        return cls(
            enabled=_read_bool(values, "DESIGNTOOLKIT_HOSTED", False),
            data_dir=Path(values.get("DESIGNTOOLKIT_DATA_DIR", "/data")),
            redis_url=values.get("REDIS_URL", "redis://redis:6379/0"),
            max_upload_bytes=_read_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_UPLOAD_BYTES",
                _DEFAULT_MAX_UPLOAD_BYTES,
            ),
            max_session_bytes=_read_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_SESSION_BYTES",
                _DEFAULT_MAX_SESSION_BYTES,
            ),
            max_output_bytes=_read_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_OUTPUT_BYTES",
                _DEFAULT_MAX_OUTPUT_BYTES,
            ),
            min_free_bytes=_read_non_negative_int(
                values,
                "DESIGNTOOLKIT_MIN_FREE_BYTES",
                _DEFAULT_MIN_FREE_BYTES,
            ),
            max_queued_tasks=_read_positive_int(
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
            max_preview_concurrency=_read_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_PREVIEW_CONCURRENCY",
                2,
            ),
            max_upload_concurrency=_read_positive_int(
                values,
                "DESIGNTOOLKIT_MAX_UPLOAD_CONCURRENCY",
                4,
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
            secure_cookies=_read_bool(
                values,
                "DESIGNTOOLKIT_SECURE_COOKIES",
                True,
            ),
        )


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
        filename: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
        reservation: RedisReservation,
        lease: RedisSessionLease | None = None,
    ) -> UploadedAsset:
        """Store an upload with conservative Redis/filesystem recovery.

        Redis and the filesystem cannot be committed atomically. Unpublished
        data is therefore kept hidden whenever Redis state remains ambiguous,
        and published data is never removed because marker cleanup failed.
        Session expiry eventually removes quarantined hidden data.
        """
        try:
            session_id = _validate_session_id(session_id)
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

        asset_id = uuid4().hex
        session_dir = self._session_dir(session_id)
        asset_dir = session_dir / "assets" / asset_id
        temporary_path = asset_dir / ".uploading"
        final_path = asset_dir / filename
        size = 0
        committed = False
        try:
            with _UploadResourceHeartbeat(reservation, lease) as heartbeat:
                asset_dir.mkdir(parents=True, exist_ok=False)
                with temporary_path.open("xb") as destination:
                    async for chunk in chunks:
                        heartbeat.raise_if_failed()
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
                os.utime(self._session_dir(session_id), None)
                heartbeat.raise_if_failed()
                _commit_with_recovery(reservation)
                committed = True
                heartbeat.raise_if_failed()
                temporary_path.replace(final_path)
                heartbeat.raise_if_failed()
        except _AmbiguousCommitError:
            raise
        except Exception:
            if committed:
                if _rollback_with_recovery(reservation):
                    self._discard_asset(session_id, asset_dir)
            else:
                _release_best_effort(reservation)
                self._discard_asset(session_id, asset_dir)
            raise

        _finalize_best_effort(reservation)
        return UploadedAsset(
            reference=f"asset:{asset_id}:{quote(filename, safe='')}",
            filename=filename,
            size=size,
        )

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

    def delete_asset(self, session_id: str, reference: str) -> AssetDeletion:
        session_id = _validate_session_id(session_id)
        asset_id = _asset_id(reference)
        session_dir = self._session_dir(session_id)
        assets_dir = session_dir / "assets"
        asset_dir = assets_dir / asset_id
        tombstone = session_dir / ".trash" / asset_id
        if tombstone.is_dir():
            return _read_asset_deletion(tombstone, asset_id)
        if not asset_dir.is_dir():
            if self._asset_belongs_to_other_session(session_id, asset_id):
                raise UploadRejectedError("Uploaded asset is not available")
            return AssetDeletion(
                asset_id=asset_id,
                reference=f"asset:{asset_id}",
                size=0,
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

    def finalize_asset_deletion(self, session_id: str, asset_id: str) -> None:
        session_id = _validate_session_id(session_id)
        asset_id = _asset_id(f"asset:{asset_id}")
        tombstone = self._session_dir(session_id) / ".trash" / asset_id
        with self._deletion_lock:
            shutil.rmtree(tombstone, ignore_errors=True)
            if tombstone.exists():
                raise OSError("Asset tombstone removal failed")
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
        removed = self._cleanup_orphaned_quarantine(coordinator)
        sessions_dir = self.root / "sessions"
        if not sessions_dir.is_dir():
            return removed
        cutoff = time() - retention_seconds
        for session_dir in sessions_dir.iterdir():
            if (
                not session_dir.is_dir()
                or not _SESSION_PATTERN.fullmatch(session_dir.name)
                or _directory_modified_at(session_dir) >= cutoff
            ):
                continue
            session_id = session_dir.name
            token = coordinator.claim_session_cleanup(session_id)
            if token is None:
                continue
            try:
                with _CleanupClaimHeartbeat(
                    coordinator,
                    session_id,
                    token,
                ) as heartbeat:
                    if _directory_modified_at(session_dir) >= cutoff:
                        continue
                    heartbeat.raise_if_failed()
                    trash_dir = self.root / ".trash"
                    trash_dir.mkdir(parents=True, exist_ok=True)
                    quarantine = trash_dir / f"{session_id}-{token}"
                    heartbeat.raise_if_failed()
                    os.replace(session_dir, quarantine)
                    heartbeat.raise_if_failed()
                    coordinator.clear_session_usage(session_id)
                    heartbeat.raise_if_failed()
                    quarantine_removed = self._delete_quarantine(quarantine)
                    heartbeat.raise_if_failed()
                if quarantine_removed:
                    removed += 1
            finally:
                coordinator.release_cleanup_claim(session_id, token)
        self._remove_empty_trash()
        return removed

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
            coordinator.clear_session_usage(match.group("session_id"))
            if self._delete_quarantine(quarantine):
                removed += 1
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
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            session_dir.rmdir()
        sessions_dir = self.root / "sessions"
        if sessions_dir.is_dir() and not any(sessions_dir.iterdir()):
            sessions_dir.rmdir()

    def _discard_asset(self, session_id: str, asset_dir: Path) -> None:
        shutil.rmtree(asset_dir, ignore_errors=True)
        self._remove_empty_session(session_id)

    def _asset_belongs_to_other_session(
        self,
        session_id: str,
        asset_id: str,
    ) -> bool:
        sessions_dir = self.root / "sessions"
        if not sessions_dir.is_dir():
            return False
        return any(
            candidate.name != session_id
            and (
                (candidate / "assets" / asset_id).is_dir()
                or (candidate / ".trash" / asset_id).is_dir()
            )
            for candidate in sessions_dir.iterdir()
            if candidate.is_dir()
        )


def new_session_id() -> str:
    return uuid4().hex


def prepare_download_artifact(
    task_id: str,
    output_value: str,
    *,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> Path:
    paths = [Path(value) for value in output_value.split("; ") if value]
    if not paths or any(not path.exists() for path in paths):
        raise FileNotFoundError("Task output is not available")
    output_files = {path: _collect_download_files(path) for path in paths}
    output_size = sum(
        child.lstat().st_size for files in output_files.values() for child in files
    )
    if output_size > max_output_bytes:
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        raise UploadRejectedError("Task output exceeds the configured limit")
    if len(paths) == 1 and paths[0].is_file():
        return paths[0]

    archive_path = paths[0].parent / f"{task_id}.zip"
    try:
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            for path in paths:
                files = output_files[path]
                if S_ISREG(path.lstat().st_mode):
                    archive.write(path, arcname=path.name)
                    path.unlink()
                    continue
                for child in files:
                    archive.write(
                        child,
                        arcname=str(Path(path.name) / child.relative_to(path)),
                    )
                    child.unlink()
                shutil.rmtree(path, ignore_errors=True)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


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


def _write_asset_deletion(asset_dir: Path, deletion: AssetDeletion) -> None:
    metadata_path = asset_dir / _DELETION_METADATA_NAME
    temporary_path = asset_dir / f"{_DELETION_METADATA_NAME}.{uuid4().hex}.tmp"
    payload = {
        "asset_id": deletion.asset_id,
        "reference": deletion.reference,
        "bytes": deletion.size,
    }
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
        if (
            payload["asset_id"] != asset_id
            or reference != f"asset:{asset_id}"
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AssetDeletionStateError(asset_id) from exc
    return AssetDeletion(asset_id=asset_id, reference=reference, size=size)


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
    return min(_read_positive_int(env, key, ceiling), ceiling)


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
    return min(value, ceiling)


def _directory_modified_at(path: Path) -> float:
    return max(
        (entry.stat().st_mtime for entry in path.rglob("*")),
        default=path.stat().st_mtime,
    )


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
