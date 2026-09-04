from __future__ import annotations

import os
import re
import shutil
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import time
from urllib.parse import quote
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from designtoolkit.web.resources import RedisReservation, RedisSessionLease

_DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
_DEFAULT_MAX_SESSION_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MAX_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_MAX_QUEUED_TASKS = 20
_DEFAULT_RETENTION_SECONDS = 2 * 60 * 60
_UPLOAD_CHUNK_BYTES = 1024 * 1024
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


class UploadRejectedError(ValueError):
    pass


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
            reservation.release()
            raise

        asset_id = uuid4().hex
        session_dir = self._session_dir(session_id)
        asset_dir = session_dir / "assets" / asset_id
        temporary_path = asset_dir / ".uploading"
        final_path = asset_dir / filename
        size = 0
        try:
            asset_dir.mkdir(parents=True, exist_ok=False)
            with temporary_path.open("xb") as destination:
                async for chunk in chunks:
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
                    reservation.refresh()
                    if lease is not None:
                        lease.refresh()
            if size != content_length:
                raise UploadRejectedError(
                    "Upload size does not match declared Content-Length"
                )
            temporary_path.replace(final_path)
            os.utime(self._session_dir(session_id), None)
            reservation.commit()
        except Exception:
            shutil.rmtree(asset_dir, ignore_errors=True)
            self._remove_empty_session(session_id)
            reservation.release()
            raise

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

    def create_task_dir(self, session_id: str) -> Path:
        session_id = _validate_session_id(session_id)
        task_dir = self._session_dir(session_id) / "tasks" / uuid4().hex
        task_dir.mkdir(parents=True, exist_ok=False)
        os.utime(self._session_dir(session_id), None)
        return task_dir

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
        protected_sessions: set[str] | None = None,
    ) -> int:
        sessions_dir = self.root / "sessions"
        if not sessions_dir.is_dir():
            return 0
        cutoff = time() - retention_seconds
        protected = protected_sessions or set()
        removed = 0
        for session_dir in sessions_dir.iterdir():
            if (
                session_dir.is_dir()
                and session_dir.name not in protected
                and _directory_modified_at(session_dir) < cutoff
            ):
                shutil.rmtree(session_dir, ignore_errors=True)
                if not session_dir.exists():
                    removed += 1
        return removed

    def _session_dir(self, session_id: str) -> Path:
        return self.root / "sessions" / session_id

    def _remove_empty_session(self, session_id: str) -> None:
        session_dir = self._session_dir(session_id)
        assets_dir = session_dir / "assets"
        if assets_dir.is_dir() and not any(assets_dir.iterdir()):
            assets_dir.rmdir()
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            session_dir.rmdir()
        sessions_dir = self.root / "sessions"
        if sessions_dir.is_dir() and not any(sessions_dir.iterdir()):
            sessions_dir.rmdir()


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
    output_size = sum(
        path.stat().st_size if path.is_file() else _directory_size(path)
        for path in paths
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
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_file():
                archive.write(path, arcname=path.name)
                path.unlink()
                continue
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    archive.write(
                        child,
                        arcname=str(Path(path.name) / child.relative_to(path)),
                    )
                    child.unlink()
            shutil.rmtree(path, ignore_errors=True)
    return archive_path


def is_valid_session_id(value: str | None) -> bool:
    return bool(value and _SESSION_PATTERN.fullmatch(value))


def _validate_session_id(value: str) -> str:
    if not is_valid_session_id(value):
        raise UploadRejectedError("Invalid upload session")
    return value


def _asset_id(reference: str) -> str:
    prefix, separator, remainder = reference.partition(":")
    if prefix != "asset" or separator != ":":
        raise UploadRejectedError("Uploaded asset is not available")
    value = remainder.partition(":")[0]
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise UploadRejectedError("Uploaded asset is not available") from exc
    if parsed.hex != value:
        raise UploadRejectedError("Uploaded asset is not available")
    return value


def _safe_filename(value: str | None) -> str:
    filename = Path((value or "").replace("\\", "/")).name.strip()
    if not filename or filename in {".", ".."}:
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


def _directory_modified_at(path: Path) -> float:
    return max(
        (entry.stat().st_mtime for entry in path.rglob("*")),
        default=path.stat().st_mtime,
    )


def _directory_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(
        entry.stat().st_size
        for entry in path.rglob("*")
        if entry.is_file()
    )


def _ensure_free_space(root: Path, minimum_free_bytes: int) -> None:
    if minimum_free_bytes <= 0:
        return
    existing = root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if shutil.disk_usage(existing).free < minimum_free_bytes:
        raise UploadRejectedError("Server storage is temporarily unavailable")
