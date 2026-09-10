from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from random import Random
from threading import Event, get_ident
from time import sleep
from types import SimpleNamespace
from urllib.parse import quote
from zipfile import ZipFile

import anyio
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import designtoolkit.web.hosted as hosted_module
from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    UploadConflictError,
    UploadDeletionState,
    UploadRejectedError,
    _CleanupClaimHeartbeat,
    parse_asset_reference,
    prepare_download_artifact,
)
from designtoolkit.web.output_limits import OutputLimitExceeded
from designtoolkit.web.resources import ReservationState, ResourceUnavailableError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingReservation:
    def __init__(
        self,
        *,
        root: Path | None = None,
        fail_commit: bool = False,
        commit_transport_errors: int = 0,
        rollback_transport_errors: int = 0,
        finalize_transport_errors: int = 0,
        state_transport_errors: int = 0,
        resolved_state: ReservationState | None = None,
        fail_refresh_after: int | None = None,
    ) -> None:
        self.coordinator = SimpleNamespace(reservation_heartbeat_interval=0.01)
        self.root = root
        self.committed = False
        self.rolled_back = False
        self.finalized = False
        self.released = False
        self.refresh_count = 0
        self.fail_commit = fail_commit
        self.commit_transport_errors = commit_transport_errors
        self.rollback_transport_errors = rollback_transport_errors
        self.finalize_transport_errors = finalize_transport_errors
        self.state_transport_errors = state_transport_errors
        self.resolved_state = resolved_state
        self.fail_refresh_after = fail_refresh_after
        self.charge_count = 0
        self.operations: list[str] = []
        self.hidden_at_commit: list[Path] = []
        self.visible_at_commit: list[Path] = []

    def refresh(self) -> None:
        self.refresh_count += 1
        if (
            self.fail_refresh_after is not None
            and self.refresh_count >= self.fail_refresh_after
        ):
            raise ResourceUnavailableError("Upload reservation expired")

    def commit(self) -> None:
        self.operations.append("commit")
        if self.root is not None:
            self.hidden_at_commit = list(self.root.rglob(".uploading"))
            self.visible_at_commit = [
                path
                for path in self.root.rglob("*")
                if path.is_file() and not path.name.startswith(".")
            ]
        if self.fail_commit:
            raise ResourceUnavailableError("Upload reservation expired")
        if not self.committed:
            self.committed = True
            self.charge_count += 1
        if self.commit_transport_errors:
            self.commit_transport_errors -= 1
            raise RedisConnectionError("commit acknowledgement lost")

    def rollback(self) -> bool:
        self.operations.append("rollback")
        if not self.rolled_back:
            self.rolled_back = True
            self.committed = False
        if self.rollback_transport_errors:
            self.rollback_transport_errors -= 1
            raise RedisConnectionError("rollback acknowledgement lost")
        return True

    def finalize(self) -> None:
        self.operations.append("finalize")
        self.finalized = True
        if self.finalize_transport_errors:
            self.finalize_transport_errors -= 1
            raise RedisConnectionError("finalize acknowledgement lost")

    def state(self) -> ReservationState:
        self.operations.append("state")
        if self.state_transport_errors:
            self.state_transport_errors -= 1
            raise RedisConnectionError("state unavailable")
        if self.resolved_state is not None:
            return self.resolved_state
        if self.committed:
            return ReservationState.COMMITTED
        if self.released:
            return ReservationState.FINALIZED_OR_ABSENT
        return ReservationState.RESERVED

    def release(self) -> None:
        self.operations.append("release")
        self.released = True


class SlowCommitReservation(RecordingReservation):
    def commit(self) -> None:
        sleep(0.04)
        super().commit()


class RecordingLease:
    def __init__(self) -> None:
        self.coordinator = SimpleNamespace(session_lease_heartbeat_interval=0.01)
        self.refresh_count = 0

    def refresh(self) -> None:
        self.refresh_count += 1


class RecordingUploadRegistration:
    def __init__(self) -> None:
        self.coordinator = SimpleNamespace(
            heartbeat_stop_timeout_seconds=1.0,
        )
        self.token = "2" * 32
        self.cancelled = False
        self.refresh_count = 0

    def refresh(self) -> bool:
        self.refresh_count += 1
        return self.cancelled


class RecordingCleanupCoordinator:
    def __init__(
        self,
        *,
        active_sessions: set[str] | None = None,
        committed_bytes: dict[str, int] | None = None,
        cleanup_markers: list[SimpleNamespace] | None = None,
    ) -> None:
        self.cleanup_claim_heartbeat_interval = 0.01
        self.active_sessions = active_sessions or set()
        self.committed_bytes = committed_bytes if committed_bytes is not None else {}
        self.cleanup_markers = cleanup_markers or []
        self.claims: dict[str, str] = {}
        self.claim_calls: list[str] = []
        self.refresh_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_generations: set[str] = set()
        self.reconciled_legacy: dict[str, int] = {}
        self.finalized_generations: list[str] = []
        self.finalized_legacy: list[str] = []

    def pending_cleanup_markers(self) -> tuple[SimpleNamespace, ...]:
        return tuple(self.cleanup_markers)

    def pending_asset_releases(self) -> tuple[SimpleNamespace, ...]:
        return ()

    def finalize_asset_release(
        self,
        _session_id: str,
        _asset_id: str,
        _generation_id: str,
    ) -> bool:
        return True

    def claim_session_cleanup(self, session_id: str) -> str | None:
        self.claim_calls.append(session_id)
        if session_id in self.active_sessions:
            return None
        token = f"{len(self.claims) + 1:032x}"
        self.claims[session_id] = token
        return token

    def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
        assert self.claims.get(session_id) == token
        self.refresh_calls.append((session_id, token))

    def release_cleanup_claim(self, session_id: str, token: str) -> None:
        self.release_calls.append((session_id, token))
        if self.claims.get(session_id) == token:
            del self.claims[session_id]

    def clear_session_usage(self, session_id: str) -> None:
        self.cleared_sessions.append(session_id)
        self.committed_bytes.pop(session_id, None)

    def capture_session_usage(
        self,
        session_id: str,
        claim_token: str,
    ) -> SimpleNamespace:
        assert self.claims.get(session_id) == claim_token
        return SimpleNamespace(
            generation_id=claim_token,
            committed_bytes=self.committed_bytes.get(session_id, 0),
            released_asset_ids=(),
        )

    def clear_session_usage_generation(
        self,
        session_id: str,
        claim_token: str,
        generation: SimpleNamespace,
    ) -> None:
        assert self.claims.get(session_id) == claim_token
        if generation.generation_id in self.cleared_generations:
            return
        self.cleared_generations.add(generation.generation_id)
        self.cleared_sessions.append(session_id)
        remaining = (
            self.committed_bytes.get(session_id, 0) - generation.committed_bytes
        )
        if remaining:
            self.committed_bytes[session_id] = remaining
        else:
            self.committed_bytes.pop(session_id, None)

    def finalize_session_usage_generation(self, generation_id: str) -> None:
        assert generation_id in self.cleared_generations
        self.finalized_generations.append(generation_id)

    def reconcile_legacy_session_usage(
        self,
        session_id: str,
        claim_token: str,
        generation_id: str,
        *,
        current_committed_bytes: int,
        legacy_physical_bytes: int,
    ) -> int:
        assert self.claims.get(session_id) == claim_token
        if generation_id in self.reconciled_legacy:
            return self.reconciled_legacy[generation_id]
        committed = self.committed_bytes.get(session_id, 0)
        legacy_bytes = committed - current_committed_bytes
        if legacy_bytes < 0 or legacy_bytes > legacy_physical_bytes:
            raise ResourceUnavailableError("Legacy session usage cannot be reconciled")
        if current_committed_bytes:
            self.committed_bytes[session_id] = current_committed_bytes
        else:
            self.committed_bytes.pop(session_id, None)
        self.reconciled_legacy[generation_id] = legacy_bytes
        self.cleared_sessions.append(session_id)
        return legacy_bytes

    def finalize_legacy_session_usage(self, generation_id: str) -> None:
        assert generation_id in self.reconciled_legacy
        self.finalized_legacy.append(generation_id)


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


async def _slow_chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value
        await asyncio.sleep(0.04)


async def _failing_chunks() -> AsyncIterator[bytes]:
    yield b"im"
    await asyncio.sleep(0.04)
    raise UploadRejectedError("upload stream failed")


def test_hosted_settings_use_public_deployment_defaults(tmp_path: Path) -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_HOSTED": "1",
            "DESIGNTOOLKIT_DATA_DIR": str(tmp_path),
            "REDIS_URL": "redis://redis:6379/0",
        }
    )

    assert settings.enabled is True
    assert settings.data_dir == tmp_path
    assert settings.max_upload_bytes == 500 * 1024 * 1024
    assert settings.max_upload_concurrency == 4
    assert settings.max_stream_concurrency == 16
    assert settings.reservation_ttl_seconds >= (
        2 * settings.reservation_heartbeat_seconds
    )
    assert settings.lease_ttl_seconds >= 2 * settings.lease_heartbeat_seconds
    assert settings.max_queued_tasks == 20
    assert settings.retention_seconds == 2 * 60 * 60
    assert settings.task_timeout_seconds == 2 * 60 * 60
    assert settings.redis_socket_connect_timeout_seconds == 5.0
    assert settings.redis_socket_timeout_seconds == 5.0


@pytest.mark.parametrize(
    ("env_name", "attribute", "ceiling"),
    [
        ("DESIGNTOOLKIT_MAX_UPLOAD_BYTES", "max_upload_bytes", 500 * 1024 * 1024),
        (
            "DESIGNTOOLKIT_MAX_SESSION_BYTES",
            "max_session_bytes",
            2 * 1024 * 1024 * 1024,
        ),
        (
            "DESIGNTOOLKIT_MAX_OUTPUT_BYTES",
            "max_output_bytes",
            5 * 1024 * 1024 * 1024,
        ),
        ("DESIGNTOOLKIT_MAX_QUEUED_TASKS", "max_queued_tasks", 20),
        ("DESIGNTOOLKIT_MAX_UPLOAD_CONCURRENCY", "max_upload_concurrency", 4),
        ("DESIGNTOOLKIT_MAX_PREVIEW_CONCURRENCY", "max_preview_concurrency", 2),
        ("DESIGNTOOLKIT_MAX_IMAGE_PIXELS", "max_image_pixels", 100_000_000),
        ("DESIGNTOOLKIT_MAX_VIDEO_WIDTH", "max_video_width", 3840),
        ("DESIGNTOOLKIT_MAX_VIDEO_HEIGHT", "max_video_height", 2160),
        (
            "DESIGNTOOLKIT_MAX_VIDEO_DURATION_SECONDS",
            "max_video_duration_seconds",
            1800.0,
        ),
    ],
)
def test_hosted_settings_clamp_public_safety_limits_to_hard_ceiling(
    env_name: str,
    attribute: str,
    ceiling: int | float,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="designtoolkit.web.hosted")

    settings = HostedSettings.from_env({env_name: str(ceiling + 1)})

    assert getattr(settings, attribute) == ceiling
    assert env_name in caplog.text
    assert str(ceiling) in caplog.text


def test_hosted_settings_clamp_minimum_free_space_to_hard_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="designtoolkit.web.hosted")

    settings = HostedSettings.from_env({"DESIGNTOOLKIT_MIN_FREE_BYTES": "1"})

    assert settings.min_free_bytes == 5 * 1024 * 1024 * 1024
    assert "DESIGNTOOLKIT_MIN_FREE_BYTES" in caplog.text
    assert str(5 * 1024 * 1024 * 1024) in caplog.text


def test_hosted_settings_allow_stricter_public_safety_limits() -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_MAX_UPLOAD_BYTES": str(250 * 1024 * 1024),
            "DESIGNTOOLKIT_MAX_SESSION_BYTES": str(1024 * 1024 * 1024),
            "DESIGNTOOLKIT_MAX_OUTPUT_BYTES": str(2 * 1024 * 1024 * 1024),
            "DESIGNTOOLKIT_MIN_FREE_BYTES": str(6 * 1024 * 1024 * 1024),
            "DESIGNTOOLKIT_MAX_QUEUED_TASKS": "10",
            "DESIGNTOOLKIT_MAX_UPLOAD_CONCURRENCY": "2",
            "DESIGNTOOLKIT_MAX_PREVIEW_CONCURRENCY": "1",
            "DESIGNTOOLKIT_MAX_IMAGE_PIXELS": "50000000",
            "DESIGNTOOLKIT_MAX_VIDEO_WIDTH": "1920",
            "DESIGNTOOLKIT_MAX_VIDEO_HEIGHT": "1080",
            "DESIGNTOOLKIT_MAX_VIDEO_DURATION_SECONDS": "900",
        }
    )

    assert settings.max_upload_bytes == 250 * 1024 * 1024
    assert settings.max_session_bytes == 1024 * 1024 * 1024
    assert settings.max_output_bytes == 2 * 1024 * 1024 * 1024
    assert settings.min_free_bytes == 6 * 1024 * 1024 * 1024
    assert settings.max_queued_tasks == 10
    assert settings.max_upload_concurrency == 2
    assert settings.max_preview_concurrency == 1
    assert settings.max_image_pixels == 50_000_000
    assert settings.max_video_width == 1920
    assert settings.max_video_height == 1080
    assert settings.max_video_duration_seconds == 900.0


def test_hosted_settings_do_not_clamp_operational_tuning() -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_MAX_STREAM_CONCURRENCY": "32",
            "DESIGNTOOLKIT_UPLOAD_REQUESTS_PER_HOUR": "60",
            "DESIGNTOOLKIT_API_REQUESTS_PER_MINUTE": "240",
            "DESIGNTOOLKIT_RETENTION_SECONDS": "14400",
            "DESIGNTOOLKIT_TASK_TIMEOUT_SECONDS": "14400",
            "DESIGNTOOLKIT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": "10",
            "DESIGNTOOLKIT_REDIS_SOCKET_TIMEOUT_SECONDS": "20",
            "DESIGNTOOLKIT_RESERVATION_TTL_SECONDS": "120",
            "DESIGNTOOLKIT_RESERVATION_HEARTBEAT_SECONDS": "10",
            "DESIGNTOOLKIT_LEASE_TTL_SECONDS": "120",
            "DESIGNTOOLKIT_LEASE_HEARTBEAT_SECONDS": "10",
        }
    )

    assert settings.max_stream_concurrency == 32
    assert settings.upload_requests_per_hour == 60
    assert settings.api_requests_per_minute == 240
    assert settings.retention_seconds == 14400
    assert settings.task_timeout_seconds == 14400
    assert settings.redis_socket_connect_timeout_seconds == 10.0
    assert settings.redis_socket_timeout_seconds == 20.0
    assert settings.reservation_ttl_seconds == 120.0
    assert settings.reservation_heartbeat_seconds == 10.0
    assert settings.lease_ttl_seconds == 120.0
    assert settings.lease_heartbeat_seconds == 10.0


def test_hosted_settings_keep_task_timeout_independent_from_retention(
    tmp_path: Path,
) -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_DATA_DIR": str(tmp_path),
            "DESIGNTOOLKIT_RETENTION_SECONDS": "300",
            "DESIGNTOOLKIT_TASK_TIMEOUT_SECONDS": "9000",
        }
    )

    assert settings.retention_seconds == 300
    assert settings.task_timeout_seconds == 9000


def test_hosted_settings_read_redis_socket_timeouts(tmp_path: Path) -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_DATA_DIR": str(tmp_path),
            "DESIGNTOOLKIT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": "1.5",
            "DESIGNTOOLKIT_REDIS_SOCKET_TIMEOUT_SECONDS": "2.5",
        }
    )

    assert settings.redis_socket_connect_timeout_seconds == 1.5
    assert settings.redis_socket_timeout_seconds == 2.5


@pytest.mark.parametrize(
    "env",
    [
        {
            "DESIGNTOOLKIT_RESERVATION_TTL_SECONDS": "5",
            "DESIGNTOOLKIT_RESERVATION_HEARTBEAT_SECONDS": "5",
        },
        {
            "DESIGNTOOLKIT_RESERVATION_TTL_SECONDS": "5",
            "DESIGNTOOLKIT_RESERVATION_HEARTBEAT_SECONDS": "1",
            "DESIGNTOOLKIT_REDIS_SOCKET_TIMEOUT_SECONDS": "5",
        },
        {
            "DESIGNTOOLKIT_RESERVATION_TTL_SECONDS": "10",
            "DESIGNTOOLKIT_RESERVATION_HEARTBEAT_SECONDS": "1",
            "DESIGNTOOLKIT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": "5",
            "DESIGNTOOLKIT_REDIS_SOCKET_TIMEOUT_SECONDS": "5",
        },
    ],
)
def test_hosted_settings_reject_unsafe_reservation_timing(
    env: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="Reservation TTL"):
        HostedSettings.from_env(env)


@pytest.mark.parametrize(
    "env",
    [
        {
            "DESIGNTOOLKIT_LEASE_TTL_SECONDS": "5",
            "DESIGNTOOLKIT_LEASE_HEARTBEAT_SECONDS": "5",
        },
        {
            "DESIGNTOOLKIT_LEASE_TTL_SECONDS": "6",
            "DESIGNTOOLKIT_LEASE_HEARTBEAT_SECONDS": "1",
            "DESIGNTOOLKIT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": "2",
            "DESIGNTOOLKIT_REDIS_SOCKET_TIMEOUT_SECONDS": "3",
        },
    ],
)
def test_hosted_settings_reject_unsafe_lease_timing(
    env: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="Lease TTL"):
        HostedSettings.from_env(env)


def test_hosted_settings_read_stream_concurrency(tmp_path: Path) -> None:
    settings = HostedSettings.from_env(
        {
            "DESIGNTOOLKIT_DATA_DIR": str(tmp_path),
            "DESIGNTOOLKIT_MAX_STREAM_CONCURRENCY": "7",
        }
    )

    assert settings.max_stream_concurrency == 7


@pytest.mark.anyio
async def test_upload_is_streamed_to_an_opaque_session_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(root=tmp_path)
    lease = RecordingLease()
    original_replace = Path.replace

    def record_publication(source: Path, target: Path) -> Path:
        assert reservation.committed is True
        reservation.operations.append("rename")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", record_publication)

    asset = await storage.store_stream(
        "a" * 32,
        filename="../screen.PNG",
        content_length=5,
        chunks=_chunks(b"im", b"", b"age"),
        reservation=reservation,
        lease=lease,
    )

    assert asset.reference.startswith("asset:")
    assert asset.filename == "screen.PNG"
    assert asset.size == 5
    assert storage.resolve("a" * 32, asset.reference).read_bytes() == b"image"
    assert reservation.refresh_count >= 1
    assert lease.refresh_count >= 1
    assert reservation.committed is True
    assert reservation.finalized is True
    assert reservation.operations == ["commit", "rename", "finalize"]
    assert len(reservation.hidden_at_commit) == 1
    assert reservation.visible_at_commit == []
    assert reservation.released is False
    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve("b" * 32, asset.reference)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stage", "owner_name", "attribute"),
    [
        ("prepare", "storage", "_prepare_upload"),
        ("manifest", "module", "_write_asset_manifest"),
        ("write", "writer", "write"),
        ("publish", "storage", "_publish_upload"),
        ("discard", "storage", "_discard_upload"),
    ],
)
async def test_upload_filesystem_stages_do_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    owner_name: str,
    attribute: str,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    event_loop_thread = get_ident()
    stage_threads: list[int] = []
    progress_during_stage: list[int] = []
    ticks = 0
    stopped = False
    if owner_name == "writer":
        owner = getattr(hosted_module, "_UploadWriter", None)
        assert owner is not None, "upload writer helper is required"
    elif owner_name == "storage":
        owner = HostedStorage
    else:
        owner = hosted_module
    assert hasattr(owner, attribute), f"{stage} filesystem helper is required"
    original = getattr(owner, attribute)

    def slow_stage(*args: object, **kwargs: object) -> object:
        before = ticks
        stage_threads.append(get_ident())
        sleep(0.04)
        progress_during_stage.append(ticks - before)
        return original(*args, **kwargs)

    async def ticker() -> None:
        nonlocal ticks
        while not stopped:
            ticks += 1
            await asyncio.sleep(0)

    monkeypatch.setattr(owner, attribute, slow_stage)
    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        if stage == "discard":
            with pytest.raises(UploadRejectedError, match="stream failed"):
                await storage.store_stream(
                    "a" * 32,
                    filename="screen.png",
                    content_length=4,
                    chunks=_failing_chunks(),
                    reservation=reservation,
                )
        else:
            await storage.store_stream(
                "a" * 32,
                filename="screen.png",
                content_length=4,
                chunks=_chunks(b"data"),
                reservation=reservation,
            )
    finally:
        stopped = True
        await ticker_task

    assert stage_threads
    assert all(thread_id != event_loop_thread for thread_id in stage_threads)
    assert all(progress > 0 for progress in progress_during_stage)


@pytest.mark.anyio
async def test_upload_cancellation_waits_for_write_before_close_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    write_started = Event()
    release_write = Event()
    write_active = Event()
    close_threads: list[int] = []
    event_loop_thread = get_ident()
    writer_type = getattr(hosted_module, "_UploadWriter", None)
    assert writer_type is not None, "upload writer helper is required"
    original_write = writer_type.write
    original_close = writer_type.close

    def blocked_write(writer: object, chunk: bytes) -> None:
        write_active.set()
        write_started.set()
        try:
            assert release_write.wait(timeout=1)
            original_write(writer, chunk)
        finally:
            write_active.clear()

    def checked_close(writer: object) -> None:
        assert not write_active.is_set()
        close_threads.append(get_ident())
        original_close(writer)

    monkeypatch.setattr(writer_type, "write", blocked_write)
    monkeypatch.setattr(writer_type, "close", checked_close)

    upload = asyncio.create_task(
        storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"data"),
            reservation=reservation,
        )
    )
    while not write_started.is_set():
        await asyncio.sleep(0)

    upload.cancel()
    await asyncio.sleep(0.01)
    assert not upload.done()
    release_write.set()

    with pytest.raises(asyncio.CancelledError):
        await upload

    assert close_threads
    assert all(thread_id != event_loop_thread for thread_id in close_threads)
    assert reservation.operations.count("release") == 1
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_anyio_cancellation_during_write_cleans_owned_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    registration = RecordingUploadRegistration()
    session_id = "a" * 32
    upload_id = "1" * 32
    write_started = Event()
    release_write = Event()
    write_active = Event()
    writer_type = getattr(hosted_module, "_UploadWriter", None)
    assert writer_type is not None, "upload writer helper is required"
    original_write = writer_type.write
    original_close = writer_type.close

    def blocked_write(writer: object, chunk: bytes) -> None:
        write_active.set()
        write_started.set()
        try:
            assert release_write.wait(timeout=1)
            original_write(writer, chunk)
        finally:
            write_active.clear()

    def checked_close(writer: object) -> None:
        assert not write_active.is_set()
        original_close(writer)

    async def run_upload() -> None:
        await storage.store_stream(
            session_id,
            asset_id=upload_id,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"data"),
            reservation=reservation,
            registration=registration,
        )

    storage.claim_upload_generation(
        session_id,
        upload_id,
        token=registration.token,
    )
    monkeypatch.setattr(writer_type, "write", blocked_write)
    monkeypatch.setattr(writer_type, "close", checked_close)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_upload)
        while not write_started.is_set():
            await asyncio.sleep(0)
        task_group.cancel_scope.cancel()
        release_write.set()

    assert reservation.operations.count("release") == 1
    assert reservation.charge_count == 0
    assert list(tmp_path.rglob(".uploading")) == []
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_cancellation_preserves_original_error_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    stream_started = asyncio.Event()

    async def blocked_chunks() -> AsyncIterator[bytes]:
        yield b"im"
        stream_started.set()
        await asyncio.Event().wait()

    def fail_discard(_session_id: str, _asset_dir: Path) -> None:
        raise OSError("discard failed")

    async def run_upload() -> None:
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=blocked_chunks(),
            reservation=reservation,
        )

    monkeypatch.setattr(storage, "_discard_upload", fail_discard)
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_upload)
        await stream_started.wait()
        task_group.cancel_scope.cancel()

    assert reservation.operations == ["release"]
    assert "Failed to discard partial upload after upload error" in caplog.text


@pytest.mark.anyio
async def test_upload_cancellation_during_commit_rolls_back_committed_quota(
    tmp_path: Path,
) -> None:
    commit_started = Event()
    release_commit = Event()

    class BlockingCommitReservation(RecordingReservation):
        def commit(self) -> None:
            commit_started.set()
            assert release_commit.wait(timeout=1)
            super().commit()

    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = BlockingCommitReservation()
    upload = asyncio.create_task(
        storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"data"),
            reservation=reservation,
        )
    )
    while not commit_started.is_set():
        await asyncio.sleep(0)

    upload.cancel()
    await asyncio.sleep(0.01)
    assert not upload.done()
    release_commit.set()

    with pytest.raises(asyncio.CancelledError):
        await upload

    assert reservation.operations == ["commit", "rollback"]
    assert reservation.charge_count == 1
    assert reservation.rolled_back is True
    assert reservation.released is False
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_anyio_cancellation_during_commit_rolls_back_committed_quota(
    tmp_path: Path,
) -> None:
    commit_started = Event()
    release_commit = Event()

    class BlockingCommitReservation(RecordingReservation):
        def commit(self) -> None:
            commit_started.set()
            assert release_commit.wait(timeout=1)
            super().commit()

    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = BlockingCommitReservation()

    async def run_upload() -> None:
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"data"),
            reservation=reservation,
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_upload)
        while not commit_started.is_set():
            await asyncio.sleep(0)
        task_group.cancel_scope.cancel()
        release_commit.set()

    assert reservation.operations == ["commit", "rollback"]
    assert reservation.charge_count == 1
    assert reservation.rolled_back is True
    assert reservation.released is False
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_cancellation_during_ambiguous_commit_preserves_hidden_data(
    tmp_path: Path,
) -> None:
    commit_started = Event()
    release_commit = Event()

    class BlockingAmbiguousReservation(RecordingReservation):
        def commit(self) -> None:
            if not commit_started.is_set():
                commit_started.set()
                assert release_commit.wait(timeout=1)
            super().commit()

    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = BlockingAmbiguousReservation(
        commit_transport_errors=3,
        state_transport_errors=3,
    )

    async def run_upload() -> None:
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"data"),
            reservation=reservation,
        )

    with pytest.raises(ExceptionGroup) as error:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_upload)
            while not commit_started.is_set():
                await asyncio.sleep(0)
            task_group.cancel_scope.cancel()
            release_commit.set()

    assert len(error.value.exceptions) == 1
    assert isinstance(error.value.exceptions[0], hosted_module._AmbiguousCommitError)
    assert reservation.charge_count == 1
    assert reservation.rolled_back is False
    assert reservation.released is False
    assert len(list(tmp_path.rglob(".uploading"))) == 1
    assert list(tmp_path.rglob("screen.png")) == []


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["write", "stream", "oversize", "short"])
async def test_failed_upload_cleans_up_and_releases_quota_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    content_length = 4
    chunks: AsyncIterator[bytes]

    if failure == "write":
        writer_type = getattr(hosted_module, "_UploadWriter", None)
        assert writer_type is not None, "upload writer helper is required"

        def fail_write(_writer: object, _chunk: bytes) -> None:
            raise OSError("write failed")

        monkeypatch.setattr(writer_type, "write", fail_write)
        chunks = _chunks(b"data")
        expected_error = OSError
        match = "write failed"
    elif failure == "stream":
        chunks = _failing_chunks()
        expected_error = UploadRejectedError
        match = "stream failed"
    elif failure == "oversize":
        chunks = _chunks(b"123", b"45")
        expected_error = UploadRejectedError
        match = "declared Content-Length"
    else:
        chunks = _chunks(b"123")
        expected_error = UploadRejectedError
        match = "does not match"

    with pytest.raises(expected_error, match=match):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=content_length,
            chunks=chunks,
            reservation=reservation,
        )

    assert reservation.operations.count("release") == 1
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_random_delete_does_not_create_cancellation_state(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    upload_id = "1" * 32
    reservation = RecordingReservation()

    deletion = storage.delete_asset(session_id, f"asset:{upload_id}")

    asset = await storage.store_stream(
        session_id,
        asset_id=upload_id,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"data"),
        reservation=reservation,
    )

    assert deletion is None
    assert reservation.committed is True
    assert storage.resolve(session_id, asset.reference).read_bytes() == b"data"
    assert not (
        tmp_path / "sessions" / session_id / ".upload-cancellations" / upload_id
    ).exists()


@pytest.mark.anyio
async def test_delete_during_upload_cancels_before_commit(tmp_path: Path) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    upload_id = "1" * 32
    reservation = RecordingReservation()
    stream_started = asyncio.Event()
    resume_stream = asyncio.Event()

    async def blocked_chunks() -> AsyncIterator[bytes]:
        yield b"im"
        stream_started.set()
        await resume_stream.wait()
        yield b"ag"

    upload = asyncio.create_task(
        storage.store_stream(
            session_id,
            asset_id=upload_id,
            filename="screen.png",
            content_length=4,
            chunks=blocked_chunks(),
            reservation=reservation,
        )
    )
    await stream_started.wait()

    deletion = storage.delete_asset(session_id, f"asset:{upload_id}")
    resume_stream.set()

    with pytest.raises(UploadRejectedError, match="cancelled"):
        await upload
    assert deletion.pending is True
    assert reservation.committed is False
    assert reservation.released is True
    assert list(tmp_path.rglob("screen.png")) == []


@pytest.mark.anyio
async def test_delete_at_publication_boundary_rolls_back_committed_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    upload_id = "1" * 32
    reservation = RecordingReservation(root=tmp_path)
    original_replace = Path.replace

    def cancel_during_publication(source: Path, target: Path) -> Path:
        if source.name == ".uploading":
            deletion = storage.delete_asset(session_id, f"asset:{upload_id}")
            assert deletion.pending is True
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", cancel_during_publication)

    with pytest.raises(UploadRejectedError, match="cancelled"):
        await storage.store_stream(
            session_id,
            asset_id=upload_id,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"data"),
            reservation=reservation,
        )

    assert reservation.committed is False
    assert reservation.rolled_back is True
    assert list(tmp_path.rglob("screen.png")) == []


@pytest.mark.anyio
async def test_duplicate_upload_id_never_overwrites_existing_asset(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    upload_id = "1" * 32
    first_reservation = RecordingReservation()
    duplicate_reservation = RecordingReservation()

    first = await storage.store_stream(
        session_id,
        asset_id=upload_id,
        filename="first.png",
        content_length=5,
        chunks=_chunks(b"first"),
        reservation=first_reservation,
    )

    with pytest.raises(UploadRejectedError, match="already exists"):
        await storage.store_stream(
            session_id,
            asset_id=upload_id,
            filename="second.png",
            content_length=6,
            chunks=_chunks(b"second"),
            reservation=duplicate_reservation,
        )

    assert storage.resolve(session_id, first.reference).read_bytes() == b"first"
    assert first_reservation.charge_count == 1
    assert duplicate_reservation.charge_count == 0
    assert duplicate_reservation.released is True


@pytest.mark.anyio
async def test_upload_heartbeats_resources_while_waiting_for_next_chunk(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    lease = RecordingLease()

    asset = await storage.store_stream(
        "a" * 32,
        filename="screen.png",
        content_length=4,
        chunks=_slow_chunks(b"im", b"ag"),
        reservation=reservation,
        lease=lease,
    )

    assert storage.resolve("a" * 32, asset.reference).read_bytes() == b"imag"
    assert reservation.refresh_count >= 4
    assert lease.refresh_count >= 4


@pytest.mark.anyio
async def test_upload_registration_heartbeat_aborts_cancelled_upload(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    registration = RecordingUploadRegistration()
    storage.claim_upload_generation(
        "a" * 32,
        "1" * 32,
        token=registration.token,
    )

    async def cancelled_chunks() -> AsyncIterator[bytes]:
        yield b"im"
        registration.cancelled = True
        await asyncio.sleep(0.03)
        yield b"ag"

    with pytest.raises(UploadConflictError, match="cancelled"):
        await storage.store_stream(
            "a" * 32,
            asset_id="1" * 32,
            filename="screen.png",
            content_length=4,
            chunks=cancelled_chunks(),
            reservation=reservation,
            registration=registration,
        )

    assert registration.refresh_count >= 2
    assert reservation.committed is False
    assert reservation.released is True
    assert list(tmp_path.rglob("screen.png")) == []


@pytest.mark.anyio
async def test_upload_registration_refresh_count_is_not_per_chunk(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=200)
    reservation = RecordingReservation()
    registration = RecordingUploadRegistration()
    session_id = "a" * 32
    upload_id = "1" * 32
    storage.claim_upload_generation(
        session_id,
        upload_id,
        token=registration.token,
    )

    async def many_chunks() -> AsyncIterator[bytes]:
        for _index in range(100):
            yield b"x"

    await storage.store_stream(
        session_id,
        asset_id=upload_id,
        filename="screen.png",
        content_length=100,
        chunks=many_chunks(),
        reservation=reservation,
        registration=registration,
    )

    assert registration.refresh_count < 10


@pytest.mark.anyio
async def test_slow_registration_refresh_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    refresh_gate = Event()

    class SlowRegistration(RecordingUploadRegistration):
        def __init__(self) -> None:
            super().__init__()
            self.first_refresh_unblocked: bool | None = None

        def refresh(self) -> bool:
            self.refresh_count += 1
            unblocked = refresh_gate.wait(timeout=0.2)
            if self.first_refresh_unblocked is None:
                self.first_refresh_unblocked = unblocked
            return self.cancelled

    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    registration = SlowRegistration()
    session_id = "a" * 32
    upload_id = "1" * 32
    storage.claim_upload_generation(
        session_id,
        upload_id,
        token=registration.token,
    )

    async def open_refresh_gate() -> None:
        await asyncio.sleep(0.01)
        refresh_gate.set()

    async def delayed_chunk() -> AsyncIterator[bytes]:
        await asyncio.sleep(0.02)
        yield b"data"

    gate_task = asyncio.create_task(open_refresh_gate())
    await storage.store_stream(
        session_id,
        asset_id=upload_id,
        filename="screen.png",
        content_length=4,
        chunks=delayed_chunk(),
        reservation=reservation,
        registration=registration,
    )
    await gate_task

    assert registration.first_refresh_unblocked is True


@pytest.mark.anyio
async def test_upload_registration_heartbeat_refreshes_generation_fence(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    registration = RecordingUploadRegistration()
    session_id = "a" * 32
    upload_id = "1" * 32
    storage.claim_upload_generation(
        session_id,
        upload_id,
        token=registration.token,
    )
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    os.utime(generation, (1.0, 1.0))

    await storage.store_stream(
        session_id,
        asset_id=upload_id,
        filename="screen.png",
        content_length=4,
        chunks=_slow_chunks(b"im", b"ag"),
        reservation=reservation,
        registration=registration,
    )

    assert generation.stat().st_mtime > 1.0
    assert registration.refresh_count >= 4


def test_stale_abandon_cannot_remove_replacement_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    upload_id = "1" * 32
    first_generation = "2" * 32
    second_generation = "3" * 32
    marker_read = Event()
    release_marker_read = Event()
    original_read = hosted_module._read_upload_generation_marker

    def blocked_read(marker: Path, expected_asset_id: str) -> str | None:
        generation = original_read(marker, expected_asset_id)
        if generation == first_generation:
            marker_read.set()
            assert release_marker_read.wait(timeout=1)
        return generation

    storage.claim_upload_generation(
        session_id,
        upload_id,
        token=first_generation,
    )
    monkeypatch.setattr(
        hosted_module,
        "_read_upload_generation_marker",
        blocked_read,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        stale_cleanup = executor.submit(
            storage.abandon_upload_generation,
            session_id,
            upload_id,
            token=first_generation,
        )
        assert marker_read.wait(timeout=1)
        replacement = executor.submit(
            storage.claim_upload_generation,
            session_id,
            upload_id,
            token=second_generation,
        )
        assert not replacement.done()
        release_marker_read.set()
        assert stale_cleanup.result(timeout=1) is True
        replacement.result(timeout=1)

    assert (
        storage.abandon_upload_generation(
            session_id,
            upload_id,
            token=first_generation,
        )
        is False
    )
    marker = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    assert marker.read_text(encoding="ascii") == second_generation


@pytest.mark.anyio
async def test_upload_aborts_before_commit_after_background_heartbeat_failure(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(fail_refresh_after=2)
    lease = RecordingLease()

    with pytest.raises(ResourceUnavailableError, match="reservation expired"):
        await storage.store_stream(
        "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_slow_chunks(b"data"),
            reservation=reservation,
            lease=lease,
        )

    assert reservation.committed is False
    assert reservation.released is True
    assert list(tmp_path.rglob("screen.png")) == []
    assert list(tmp_path.rglob(".uploading")) == []


@pytest.mark.anyio
async def test_upload_stops_heartbeat_before_commit_and_publish(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = SlowCommitReservation(fail_refresh_after=2)

    asset = await storage.store_stream(
        "a" * 32,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"data"),
        reservation=reservation,
    )

    assert storage.resolve("a" * 32, asset.reference).read_bytes() == b"data"
    assert reservation.committed is True
    assert reservation.rolled_back is False


@pytest.mark.anyio
async def test_upload_surfaces_heartbeat_failure_alongside_stream_error(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(fail_refresh_after=2)

    with pytest.raises(
        ResourceUnavailableError,
        match="Upload reservation expired",
    ) as error:
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_failing_chunks(),
            reservation=reservation,
        )

    assert isinstance(error.value.__context__, UploadRejectedError)
    assert str(error.value.__context__) == "upload stream failed"
    assert reservation.released is True
    assert list(tmp_path.rglob(".uploading")) == []


@pytest.mark.anyio
async def test_delete_asset_preserves_retryable_state_until_finalized(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    owner = "a" * 32
    stranger = "b" * 32
    asset = await storage.store_stream(
        owner,
        filename="screen.png",
        content_length=5,
        chunks=_chunks(b"image"),
        reservation=RecordingReservation(),
    )

    foreign = storage.delete_asset(stranger, asset.reference)
    assert foreign is None
    assert not (tmp_path / "sessions" / stranger).exists()

    deletion = storage.delete_asset(owner, asset.reference)
    retry = storage.delete_asset(owner, asset.reference)

    assert deletion.asset_id == asset.reference.split(":", 2)[1]
    assert deletion.size == 5
    assert retry == deletion
    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(owner, asset.reference)
    assert len(list(tmp_path.rglob("screen.png"))) == 1

    storage.finalize_asset_deletion(owner, deletion.asset_id)
    storage.finalize_asset_deletion(owner, deletion.asset_id)

    assert list(tmp_path.rglob("screen.png")) == []
    assert storage.delete_asset(owner, asset.reference) is None


def test_stale_partial_deletion_carries_upload_generation(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    generation_id = "2" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / ".uploading").write_bytes(b"data")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "bytes": 4,
                "generation_id": generation_id,
            }
        ),
        encoding="utf-8",
    )
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / asset_id
    )
    generation.parent.mkdir()
    generation.write_text(generation_id, encoding="ascii")
    os.utime(generation, (1.0, 1.0))

    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        orphan_stale_after_seconds=1,
    )

    assert deletion is not None
    assert deletion.generation_id == generation_id


@pytest.mark.anyio
async def test_published_asset_deletion_carries_manifest_generation(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    registration = RecordingUploadRegistration()
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=registration.token,
    )

    await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"data"),
        reservation=RecordingReservation(),
        registration=registration,
    )
    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation=registration.token,
    )

    assert deletion is not None
    assert deletion.generation_id == registration.token


def test_deletion_snapshot_survives_active_to_published_barrier(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    generation_id = "2" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    uploading = asset_dir / ".uploading"
    uploading.write_bytes(b"data")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "bytes": 4,
                "generation_id": generation_id,
            }
        ),
        encoding="utf-8",
    )
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / asset_id
    )
    generation.parent.mkdir()
    generation.write_text(generation_id, encoding="ascii")

    deletion_snapshot = storage.inspect_upload_for_deletion(
        session_id,
        asset_id,
    )
    uploading.replace(asset_dir / "screen.png")
    published = storage.inspect_upload_for_deletion(session_id, asset_id)
    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation=generation_id,
        expected_identity=deletion_snapshot,
    )

    assert deletion_snapshot.state is UploadDeletionState.ACTIVE
    assert published.state is UploadDeletionState.PUBLISHED
    assert published.generation_id == deletion_snapshot.generation_id
    assert deletion is not None
    assert deletion.generation_id == generation_id


@pytest.mark.anyio
@pytest.mark.parametrize(
    "expected_generation",
    [None, "3" * 32],
    ids=["missing", "stale"],
)
async def test_v2_asset_deletion_requires_matching_generation(
    tmp_path: Path,
    expected_generation: str | None,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    registration = RecordingUploadRegistration()
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=registration.token,
    )
    asset = await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"data"),
        reservation=RecordingReservation(),
        registration=registration,
    )

    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation=expected_generation,
    )

    assert deletion is None
    assert storage.resolve(session_id, asset.reference).read_bytes() == b"data"
    assert not (
        tmp_path
        / "sessions"
        / session_id
        / ".upload-cancellations"
        / asset_id
    ).exists()
    assert not (
        tmp_path / "sessions" / session_id / ".trash" / asset_id
    ).exists()


@pytest.mark.anyio
async def test_generation_bound_deletion_is_idempotent_for_same_tombstone(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    registration = RecordingUploadRegistration()
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=registration.token,
    )
    await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"data"),
        reservation=RecordingReservation(),
        registration=registration,
    )

    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation=registration.token,
    )
    retry = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation=registration.token,
    )
    stale = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation="3" * 32,
    )

    assert deletion is not None
    assert retry == deletion
    assert stale is None


def test_zero_byte_partial_deletion_metadata_is_recoverable(
    tmp_path: Path,
) -> None:
    asset_id = "1" * 32
    generation_id = "2" * 32
    tombstone = tmp_path / asset_id
    tombstone.mkdir()
    (tombstone / ".deletion.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "reference": f"asset:{asset_id}",
                "bytes": 0,
                "generation_id": generation_id,
                "generation_required": True,
            }
        ),
        encoding="utf-8",
    )

    deletion = hosted_module._read_asset_deletion(tombstone, asset_id)

    assert deletion.size == 0
    assert deletion.generation_id == generation_id


@pytest.mark.anyio
async def test_asset_deletion_checks_fence_inside_rename_lock(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    registration = RecordingUploadRegistration()
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=registration.token,
    )
    asset = await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"data"),
        reservation=RecordingReservation(),
        registration=registration,
    )

    with pytest.raises(ResourceUnavailableError, match="claim expired"):
        storage.delete_asset(
            session_id,
            asset.reference,
            expected_generation=registration.token,
            before_rename=lambda: (_ for _ in ()).throw(
                ResourceUnavailableError("claim expired")
            ),
        )

    assert storage.resolve(session_id, asset.reference).read_bytes() == b"data"
    assert not (
        tmp_path / "sessions" / session_id / ".trash" / asset_id
    ).exists()


def test_legacy_v1_asset_deletion_without_generation_remains_supported(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "legacy.png").write_bytes(b"data")
    (asset_dir / ".asset.json").write_text(
        json.dumps({"asset_id": asset_id, "bytes": 4}),
        encoding="utf-8",
    )

    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation=None,
    )

    assert deletion is not None
    assert deletion.generation_id is None
    assert deletion.size == 4


@pytest.mark.anyio
async def test_late_legacy_finalizer_preserves_reused_active_generation(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "legacy.png").write_bytes(b"old")
    (asset_dir / ".asset.json").write_text(
        json.dumps({"asset_id": asset_id, "bytes": 3}),
        encoding="utf-8",
    )
    deletion_identity = storage.inspect_upload_for_deletion(
        session_id,
        asset_id,
    )
    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_identity=deletion_identity,
    )
    assert deletion is not None

    storage.finalize_asset_deletion(
        session_id,
        asset_id,
        expected_generation=None,
        expected_identity=deletion_identity,
    )

    registration = RecordingUploadRegistration()
    registration.token = "3" * 32
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=registration.token,
    )
    generation_marker = (
        tmp_path
        / "sessions"
        / session_id
        / ".upload-generations"
        / asset_id
    )
    storage._refresh_upload_generation(
        session_id,
        asset_id,
        registration.token,
    )
    heartbeat_mtime = generation_marker.stat().st_mtime_ns

    storage.finalize_asset_deletion(
        session_id,
        asset_id,
        expected_generation=None,
        expected_identity=deletion_identity,
    )
    storage.finalize_asset_deletion(
        session_id,
        asset_id,
        expected_generation=None,
        expected_identity=deletion_identity,
    )
    storage.finalize_asset_deletion(
        session_id,
        asset_id,
        expected_generation=None,
    )

    assert generation_marker.read_text(encoding="ascii") == registration.token
    assert generation_marker.stat().st_mtime_ns == heartbeat_mtime
    assert not (
        tmp_path / "sessions" / session_id / ".trash" / asset_id
    ).exists()

    published = await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="new.png",
        content_length=3,
        chunks=_chunks(b"new"),
        reservation=RecordingReservation(),
        registration=registration,
    )

    assert storage.resolve(session_id, published.reference).read_bytes() == b"new"


def test_asset_reference_parser_decodes_generation_and_quoted_filename() -> None:
    asset_id = "1" * 32
    generation_id = "2" * 32
    filename = "素材:100%.png"

    parsed = parse_asset_reference(
        f"asset:{asset_id}:{generation_id}:{quote(filename, safe='')}"
    )

    assert parsed.asset_id == asset_id
    assert parsed.generation_id == generation_id
    assert parsed.filename == filename


@pytest.mark.parametrize(
    "reference",
    [
        f"asset:{'1' * 32}:{'2' * 32}:bad%ZZname.png",
        f"asset:{'1' * 32}:{'2' * 32}:..%2Fsecret.png",
        f"asset:{'1' * 32}:{'2' * 32}:dir%5Csecret.png",
        f"asset:{'1' * 32}:{'2' * 32}:bad:name.png",
        f"asset:{'1' * 32}:{'G' * 32}:screen.png",
    ],
)
def test_asset_reference_parser_rejects_invalid_or_injectable_filename(
    reference: str,
) -> None:
    with pytest.raises(UploadRejectedError, match="not available"):
        parse_asset_reference(reference)


@pytest.mark.anyio
async def test_reused_asset_id_cannot_resolve_previous_generation(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    first_registration = RecordingUploadRegistration()
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=first_registration.token,
    )
    first = await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="first.png",
        content_length=5,
        chunks=_chunks(b"first"),
        reservation=RecordingReservation(),
        registration=first_registration,
    )
    deletion = storage.delete_asset(
        session_id,
        f"asset:{asset_id}",
        expected_generation=first_registration.token,
    )
    assert deletion is not None
    storage.finalize_asset_deletion(
        session_id,
        asset_id,
        first_registration.token,
    )

    second_registration = RecordingUploadRegistration()
    second_registration.token = "3" * 32
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=second_registration.token,
    )
    second = await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="second.png",
        content_length=6,
        chunks=_chunks(b"second"),
        reservation=RecordingReservation(),
        registration=second_registration,
    )

    assert first.reference == (
        f"asset:{asset_id}:{first_registration.token}:first.png"
    )
    assert second.reference == (
        f"asset:{asset_id}:{second_registration.token}:second.png"
    )
    second_manifest = json.loads(
        (
            tmp_path
            / "sessions"
            / session_id
            / "assets"
            / asset_id
            / ".asset.json"
        ).read_text(encoding="utf-8")
    )
    assert second_manifest["reference_version"] == 2
    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(session_id, first.reference)
    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(session_id, f"asset:{asset_id}:first.png")
    assert storage.resolve(session_id, second.reference).read_bytes() == b"second"


def test_previous_manifest_with_generation_accepts_legacy_reference(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    generation_id = "2" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "previous.png").write_bytes(b"previous")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "generation_id": generation_id,
                "bytes": 8,
            }
        ),
        encoding="utf-8",
    )

    resolved = storage.resolve(
        session_id,
        f"asset:{asset_id}:previous.png",
    )

    assert resolved.read_bytes() == b"previous"
    assert storage.resolve(
        session_id,
        f"asset:{asset_id}:{generation_id}:previous.png",
    ) == resolved


@pytest.mark.anyio
async def test_reused_v1_asset_id_rejects_legacy_reference_after_v2_upload(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    old_generation_id = "2" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "shared.png").write_bytes(b"old")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "generation_id": old_generation_id,
                "bytes": 3,
            }
        ),
        encoding="utf-8",
    )
    old_reference = f"asset:{asset_id}:shared.png"
    assert storage.resolve(session_id, old_reference).read_bytes() == b"old"
    assert storage.delete_asset(session_id, f"asset:{asset_id}") is not None
    storage.finalize_asset_deletion(session_id, asset_id)

    registration = RecordingUploadRegistration()
    registration.token = "3" * 32
    storage.claim_upload_generation(
        session_id,
        asset_id,
        token=registration.token,
    )
    replacement = await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="shared.png",
        content_length=3,
        chunks=_chunks(b"new"),
        reservation=RecordingReservation(),
        registration=registration,
    )

    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(session_id, old_reference)
    assert storage.resolve(session_id, replacement.reference).read_bytes() == b"new"


@pytest.mark.parametrize("include_generation", [False, True])
def test_explicit_v1_manifest_is_rejected(
    tmp_path: Path,
    include_generation: bool,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    generation_id = "2" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "image.png").write_bytes(b"image")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "generation_id": generation_id,
                "bytes": 5,
                "reference_version": 1,
            }
        ),
        encoding="utf-8",
    )
    reference = (
        f"asset:{asset_id}:{generation_id}:image.png"
        if include_generation
        else f"asset:{asset_id}:image.png"
    )

    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(session_id, reference)


@pytest.mark.parametrize(
    "reference_version",
    [None, 0, 3, True, 2.0, "2", [], {}],
)
def test_corrupt_manifest_reference_version_is_rejected(
    tmp_path: Path,
    reference_version: object,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    generation_id = "2" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "image.png").write_bytes(b"image")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "generation_id": generation_id,
                "bytes": 5,
                "reference_version": reference_version,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(
            session_id,
            f"asset:{asset_id}:{generation_id}:image.png",
        )


def test_v2_manifest_without_generation_is_rejected(tmp_path: Path) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "image.png").write_bytes(b"image")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": asset_id,
                "bytes": 5,
                "reference_version": 2,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(session_id, f"asset:{asset_id}:image.png")


@pytest.mark.anyio
async def test_legacy_reference_only_resolves_manifest_without_generation(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    asset_id = "1" * 32

    asset = await storage.store_stream(
        session_id,
        asset_id=asset_id,
        filename="legacy image.png",
        content_length=6,
        chunks=_chunks(b"legacy"),
        reservation=RecordingReservation(),
    )

    assert asset.reference == f"asset:{asset_id}:legacy%20image.png"
    manifest = json.loads(
        (
            tmp_path
            / "sessions"
            / session_id
            / "assets"
            / asset_id
            / ".asset.json"
        ).read_text(encoding="utf-8")
    )
    assert "reference_version" not in manifest
    assert storage.resolve(session_id, asset.reference).read_bytes() == b"legacy"
    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve(
            session_id,
            f"asset:{asset_id}:{'2' * 32}:legacy%20image.png",
        )


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "asset:not-a-uuid",
        "other:11111111111111111111111111111111",
        "asset:11111111111111111111111111111111:bad:name.png",
    ],
)
def test_delete_asset_rejects_malformed_references(
    tmp_path: Path,
    reference: str,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)

    with pytest.raises(UploadRejectedError, match="not available"):
        storage.delete_asset("a" * 32, reference)


@pytest.mark.anyio
@pytest.mark.parametrize("filename", ["bad\x00name.png", "bad\x1fname.png"])
async def test_upload_rejects_control_characters_in_filename(
    tmp_path: Path,
    filename: str,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()

    with pytest.raises(UploadRejectedError, match="File name is invalid"):
        await storage.store_stream(
            "a" * 32,
            filename=filename,
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.released is True
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
@pytest.mark.parametrize("filename", [".screen.png", ".", ".."])
async def test_upload_rejects_hidden_or_dot_filenames_before_creating_files(
    tmp_path: Path,
    filename: str,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()

    with pytest.raises(UploadRejectedError, match="File name is invalid"):
        await storage.store_stream(
            "a" * 32,
            filename=filename,
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.released is True
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_validates_type_and_declared_size_before_creating_files(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=4)

    with pytest.raises(UploadRejectedError, match="unsupported"):
        await storage.store_stream(
            "a" * 32,
            filename="payload.exe",
            content_length=1,
            chunks=_chunks(b"x"),
            reservation=RecordingReservation(),
        )
    with pytest.raises(UploadRejectedError, match="configured file limit"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=5,
            chunks=_chunks(b"12345"),
            reservation=RecordingReservation(),
        )

    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_rejects_declared_length_mismatch_and_cleans_up(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()

    with pytest.raises(UploadRejectedError, match="does not match"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=5,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.released is True
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_rejects_chunk_overflow_and_cleans_up(tmp_path: Path) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()

    with pytest.raises(UploadRejectedError, match="declared Content-Length"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"123", b"45"),
            reservation=reservation,
        )

    assert reservation.released is True
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_removes_unpublished_asset_when_commit_fails(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(root=tmp_path, fail_commit=True)

    with pytest.raises(ResourceUnavailableError, match="expired"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.released is True
    assert reservation.committed is False
    assert reservation.rolled_back is False
    assert reservation.finalized is False
    assert reservation.operations == ["commit", "release"]
    assert len(reservation.hidden_at_commit) == 1
    assert reservation.visible_at_commit == []
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_retries_commit_after_acknowledgement_loss_without_double_charge(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(
        root=tmp_path,
        commit_transport_errors=1,
    )

    asset = await storage.store_stream(
        "a" * 32,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"1234"),
        reservation=reservation,
    )

    assert storage.resolve("a" * 32, asset.reference).read_bytes() == b"1234"
    assert reservation.charge_count == 1
    assert reservation.operations == ["commit", "commit", "finalize"]


@pytest.mark.anyio
async def test_upload_resolves_committed_state_after_retry_acknowledgements_are_lost(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(
        root=tmp_path,
        commit_transport_errors=3,
        resolved_state=ReservationState.COMMITTED,
    )

    asset = await storage.store_stream(
        "a" * 32,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"1234"),
        reservation=reservation,
    )

    assert storage.resolve("a" * 32, asset.reference).read_bytes() == b"1234"
    assert reservation.charge_count == 1
    assert reservation.operations == [
        "commit",
        "commit",
        "commit",
        "state",
        "finalize",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state_transport_errors", "resolved_state"),
    [
        (3, None),
        (0, ReservationState.FINALIZED_OR_ABSENT),
    ],
)
async def test_upload_preserves_hidden_data_when_commit_state_stays_ambiguous(
    tmp_path: Path,
    state_transport_errors: int,
    resolved_state: ReservationState | None,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(
        root=tmp_path,
        commit_transport_errors=3,
        state_transport_errors=state_transport_errors,
        resolved_state=resolved_state,
    )

    with pytest.raises(ResourceUnavailableError, match="could not be confirmed"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert len(list(tmp_path.rglob(".uploading"))) == 1
    assert not list(tmp_path.rglob("screen.png"))
    assert reservation.released is False
    assert reservation.rolled_back is False


@pytest.mark.anyio
async def test_upload_cleans_hidden_data_when_failed_commit_is_still_reserved(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(
        root=tmp_path,
        commit_transport_errors=3,
        resolved_state=ReservationState.RESERVED,
    )

    with pytest.raises(ResourceUnavailableError, match="was not committed"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.released is True
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_rolls_back_commit_when_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(root=tmp_path)

    def fail_publication(source: Path, target: Path) -> Path:
        assert source.name == ".uploading"
        assert target.name == "screen.png"
        assert reservation.committed is True
        reservation.operations.append("rename")
        raise OSError("publication failed")

    monkeypatch.setattr(Path, "replace", fail_publication)

    with pytest.raises(OSError, match="publication failed"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.operations == ["commit", "rename", "rollback"]
    assert reservation.committed is False
    assert reservation.rolled_back is True
    assert reservation.finalized is False
    assert reservation.released is False
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_resolves_rollback_acknowledgement_loss_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(
        root=tmp_path,
        rollback_transport_errors=1,
    )

    def fail_publication(_source: Path, _target: Path) -> Path:
        raise OSError("publication failed")

    monkeypatch.setattr(Path, "replace", fail_publication)

    with pytest.raises(OSError, match="publication failed"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.operations == ["commit", "rollback", "rollback"]
    assert reservation.charge_count == 1
    assert reservation.rolled_back is True
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_preserves_hidden_data_when_rollback_stays_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(
        root=tmp_path,
        rollback_transport_errors=3,
    )

    def fail_publication(_source: Path, _target: Path) -> Path:
        raise OSError("publication failed")

    monkeypatch.setattr(Path, "replace", fail_publication)

    with pytest.raises(OSError, match="publication failed"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert len(list(tmp_path.rglob(".uploading"))) == 1
    assert not list(tmp_path.rglob("screen.png"))


@pytest.mark.anyio
@pytest.mark.parametrize("finalize_transport_errors", [1, 3])
async def test_finalize_acknowledgement_loss_never_deletes_published_asset(
    tmp_path: Path,
    finalize_transport_errors: int,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation(
        root=tmp_path,
        finalize_transport_errors=finalize_transport_errors,
    )

    asset = await storage.store_stream(
        "a" * 32,
        filename="screen.png",
        content_length=4,
        chunks=_chunks(b"1234"),
        reservation=reservation,
    )

    assert storage.resolve("a" * 32, asset.reference).read_bytes() == b"1234"
    finalize_attempts = min(finalize_transport_errors + 1, 3)
    assert reservation.operations == ["commit"] + ["finalize"] * finalize_attempts
    assert reservation.rolled_back is False


@pytest.mark.anyio
async def test_upload_releases_reservation_when_stream_disconnects(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()

    async def disconnected_chunks() -> AsyncIterator[bytes]:
        yield b"12"
        raise ConnectionError("client disconnected")

    with pytest.raises(ConnectionError, match="disconnected"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=disconnected_chunks(),
            reservation=reservation,
        )

    assert reservation.released is True
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.anyio
async def test_upload_releases_reservation_when_asset_directory_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()

    def fail_mkdir(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with pytest.raises(OSError, match="disk unavailable"):
        await storage.store_stream(
            "a" * 32,
            filename="screen.png",
            content_length=4,
            chunks=_chunks(b"1234"),
            reservation=reservation,
        )

    assert reservation.released is True


def test_cleanup_removes_only_expired_session_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    expired = tmp_path / "sessions" / ("a" * 32)
    current = tmp_path / "sessions" / ("b" * 32)
    invalid = tmp_path / "sessions" / "invalid"
    expired.mkdir(parents=True)
    current.mkdir(parents=True)
    invalid.mkdir(parents=True)
    (expired / "asset.bin").write_bytes(b"old")
    (current / "asset.bin").write_bytes(b"new")
    (invalid / "asset.bin").write_bytes(b"ignored")
    coordinator = RecordingCleanupCoordinator()
    monkeypatch.setattr(
        "designtoolkit.web.hosted.time",
        lambda: 10_000.0,
    )
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda path: 1_000.0 if path.name == "a" * 32 else 9_500.0,
    )

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert not expired.exists()
    assert current.is_dir()
    assert invalid.is_dir()
    assert coordinator.cleared_sessions == ["a" * 32]
    assert coordinator.claims == {}


@pytest.mark.parametrize("error_type", [FileNotFoundError, OSError])
def test_cleanup_skips_unreadable_session_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[OSError],
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    unreadable_id = "a" * 32
    expired_id = "b" * 32
    unreadable = tmp_path / "sessions" / unreadable_id
    expired = tmp_path / "sessions" / expired_id
    unreadable.mkdir(parents=True)
    expired.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator()
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)

    def session_mtime(path: Path) -> float:
        if path.name == unreadable_id:
            raise error_type("session changed during cleanup")
        return 1_000.0

    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        session_mtime,
    )
    caplog.set_level("WARNING", logger="designtoolkit.web.hosted")

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert unreadable.is_dir()
    assert not expired.exists()
    assert "Failed to inspect or clean hosted session" in caplog.text
    assert coordinator.claim_calls == [expired_id]


def test_cleanup_logs_failed_session_and_continues_with_next_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    failed_id = "a" * 32
    expired_id = "b" * 32
    failed = tmp_path / "sessions" / failed_id
    expired = tmp_path / "sessions" / expired_id
    failed.mkdir(parents=True)
    expired.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator()
    original_cleanup = HostedStorage._cleanup_expired_session
    original_iterdir = Path.iterdir
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )

    def ordered_iterdir(path: Path):
        if path == failed.parent:
            return iter((failed, expired))
        return original_iterdir(path)

    def fail_first_cleanup(
        instance: HostedStorage,
        session_dir: Path,
        *,
        cutoff: float,
        coordinator: RecordingCleanupCoordinator,
    ) -> int:
        if session_dir == failed:
            raise RuntimeError("session cleanup failed")
        return original_cleanup(
            instance,
            session_dir,
            cutoff=cutoff,
            coordinator=coordinator,
        )

    monkeypatch.setattr(Path, "iterdir", ordered_iterdir)
    monkeypatch.setattr(
        HostedStorage,
        "_cleanup_expired_session",
        fail_first_cleanup,
    )
    caplog.set_level("WARNING", logger="designtoolkit.web.hosted")

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert failed.is_dir()
    assert not expired.exists()
    assert "Failed to inspect or clean hosted session" in caplog.text


def test_cleanup_preserves_expired_session_with_active_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    active = tmp_path / "sessions" / ("a" * 32)
    active.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator(active_sessions={"a" * 32})
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 0
    assert active.is_dir()
    assert coordinator.claim_calls == ["a" * 32]


def test_cleanup_uses_session_root_mtime_when_descendants_are_older(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    child = session_dir / "asset.bin"
    child.write_bytes(b"old")
    os.utime(child, (1_000.0, 1_000.0))
    os.utime(session_dir, (9_500.0, 9_500.0))
    coordinator = RecordingCleanupCoordinator()
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 0
    assert child.read_bytes() == b"old"
    assert coordinator.claim_calls == []


def test_cleanup_rechecks_recursive_mtime_after_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator()
    mtimes = iter([1_000.0, 9_500.0])
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: next(mtimes),
    )

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 0
    assert session_dir.is_dir()
    assert coordinator.release_calls == [(session_id, "0" * 31 + "1")]
    assert coordinator.cleared_sessions == []


def test_cleanup_heartbeats_claim_during_slow_recursive_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator()
    original_replace = os.replace
    mtime_calls = 0
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)

    def slow_second_mtime(_path: Path) -> float:
        nonlocal mtime_calls
        mtime_calls += 1
        if mtime_calls == 2:
            sleep(0.04)
        return 1_000.0

    def replace_while_claim_is_fresh(source: Path, destination: Path) -> None:
        assert coordinator.refresh_calls
        original_replace(source, destination)

    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        slow_second_mtime,
    )
    monkeypatch.setattr(
        "designtoolkit.web.hosted.os.replace",
        replace_while_claim_is_fresh,
    )

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert mtime_calls == 2


def test_cleanup_aborts_before_rename_when_claim_heartbeat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)

    class FailingHeartbeatCoordinator(RecordingCleanupCoordinator):
        def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
            super().refresh_cleanup_claim(session_id, token)
            if len(self.refresh_calls) >= 2:
                raise ResourceUnavailableError("Cleanup claim expired")

    coordinator = FailingHeartbeatCoordinator()
    mtime_calls = 0
    rename_called = False
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)

    def slow_second_mtime(_path: Path) -> float:
        nonlocal mtime_calls
        mtime_calls += 1
        if mtime_calls == 2:
            sleep(0.04)
        return 1_000.0

    def record_replace(_source: Path, _destination: Path) -> None:
        nonlocal rename_called
        rename_called = True

    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        slow_second_mtime,
    )
    monkeypatch.setattr("designtoolkit.web.hosted.os.replace", record_replace)

    with pytest.raises(ResourceUnavailableError, match="Cleanup claim expired"):
        storage._cleanup_expired_session(
            session_dir,
            cutoff=8_000.0,
            coordinator=coordinator,
        )

    assert rename_called is False
    assert session_dir.is_dir()
    assert coordinator.claims == {}


def test_cleanup_commit_waits_for_in_flight_claim_failure_and_blocks_rename() -> None:
    entered_refresh = Event()
    allow_failure = Event()
    rename_called = Event()

    class InFlightFailureCoordinator(RecordingCleanupCoordinator):
        cleanup_claim_heartbeat_interval = 0.001

        def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
            super().refresh_cleanup_claim(session_id, token)
            if len(self.refresh_calls) == 2:
                entered_refresh.set()
                assert allow_failure.wait(timeout=1)
                raise ResourceUnavailableError(
                    "Cleanup claim refresh failed in flight"
                )

    session_id = "a" * 32
    coordinator = InFlightFailureCoordinator()
    token = coordinator.claim_session_cleanup(session_id)
    assert token is not None

    with pytest.raises(
        ResourceUnavailableError,
        match="refresh failed in flight",
    ):
        with _CleanupClaimHeartbeat(coordinator, session_id, token) as heartbeat:
            assert entered_refresh.wait(timeout=1)
            with ThreadPoolExecutor(max_workers=1) as executor:
                commit = executor.submit(heartbeat.commit, rename_called.set)
                try:
                    assert not rename_called.wait(timeout=0.05)
                    assert not commit.done()
                finally:
                    allow_failure.set()
                commit.result(timeout=1)

    assert rename_called.is_set() is False


def test_cleanup_synchronous_refresh_failure_blocks_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    rename_called = False

    class FinalRefreshFailureCoordinator(RecordingCleanupCoordinator):
        cleanup_claim_heartbeat_interval = 10

        def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
            super().refresh_cleanup_claim(session_id, token)
            if len(self.refresh_calls) == 2:
                raise ResourceUnavailableError(
                    "Cleanup claim failed before rename"
                )

    coordinator = FinalRefreshFailureCoordinator()
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )

    def record_replace(_source: Path, _destination: Path) -> None:
        nonlocal rename_called
        rename_called = True

    monkeypatch.setattr("designtoolkit.web.hosted.os.replace", record_replace)

    with pytest.raises(
        ResourceUnavailableError,
        match="failed before rename",
    ):
        storage._cleanup_expired_session(
            session_dir,
            cutoff=8_000.0,
            coordinator=coordinator,
        )

    assert rename_called is False
    assert session_dir.is_dir()
    assert coordinator.claims == {}


def test_cleanup_restarts_heartbeat_for_committed_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered_recovery_refresh = Event()
    allow_failure = Event()
    rename_called = Event()

    class RecoveryFailureCoordinator(RecordingCleanupCoordinator):
        cleanup_claim_heartbeat_interval = 0.001

        def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
            super().refresh_cleanup_claim(session_id, token)
            if len(self.refresh_calls) == 3:
                entered_recovery_refresh.set()
                assert allow_failure.wait(timeout=1)
                raise ResourceUnavailableError(
                    "Cleanup claim failed during committed recovery"
                )

    session_id = "a" * 32
    coordinator = RecoveryFailureCoordinator()
    token = coordinator.claim_session_cleanup(session_id)
    assert token is not None
    caplog.set_level("WARNING", logger="designtoolkit.web.hosted")

    with _CleanupClaimHeartbeat(coordinator, session_id, token) as heartbeat:
        heartbeat.commit(rename_called.set)
        assert rename_called.is_set()
        assert entered_recovery_refresh.wait(timeout=1)
        allow_failure.set()
        heartbeat._thread.join(timeout=1)

    assert "heartbeat failed after committed cleanup" in caplog.text


def test_cleanup_raises_heartbeat_failure_recorded_during_context_exit() -> None:
    entered_refresh = Event()
    allow_failure = Event()

    class FinalRefreshFailureCoordinator(RecordingCleanupCoordinator):
        cleanup_claim_heartbeat_interval = 0.001

        def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
            super().refresh_cleanup_claim(session_id, token)
            if len(self.refresh_calls) == 2:
                entered_refresh.set()
                assert allow_failure.wait(timeout=1)
                raise ResourceUnavailableError("Cleanup claim expired at exit")

    session_id = "a" * 32
    coordinator = FinalRefreshFailureCoordinator()
    token = coordinator.claim_session_cleanup(session_id)
    assert token is not None

    with pytest.raises(ResourceUnavailableError, match="expired at exit"):
        with _CleanupClaimHeartbeat(coordinator, session_id, token) as heartbeat:
            assert entered_refresh.wait(timeout=1)
            heartbeat.raise_if_failed()
            allow_failure.set()


def test_cleanup_preserves_body_error_and_records_exit_heartbeat_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered_refresh = Event()
    allow_failure = Event()

    class FinalRefreshFailureCoordinator(RecordingCleanupCoordinator):
        cleanup_claim_heartbeat_interval = 0.001

        def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
            super().refresh_cleanup_claim(session_id, token)
            if len(self.refresh_calls) == 2:
                entered_refresh.set()
                assert allow_failure.wait(timeout=1)
                raise ResourceUnavailableError("Cleanup claim expired at exit")

    session_id = "a" * 32
    coordinator = FinalRefreshFailureCoordinator()
    token = coordinator.claim_session_cleanup(session_id)
    assert token is not None
    caplog.set_level("ERROR", logger="designtoolkit.web.hosted")

    with pytest.raises(ValueError, match="primary cleanup failure") as raised:
        with _CleanupClaimHeartbeat(coordinator, session_id, token) as heartbeat:
            assert entered_refresh.wait(timeout=1)
            heartbeat.raise_if_failed()
            allow_failure.set()
            raise ValueError("primary cleanup failure")

    assert any(
        "Cleanup claim heartbeat also failed" in note
        for note in raised.value.__notes__
    )
    assert "Cleanup claim heartbeat failed during cleanup error" in caplog.text


def test_cleanup_rename_failure_releases_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator()
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr("designtoolkit.web.hosted.os.replace", fail_replace)

    with pytest.raises(OSError, match="rename failed"):
        storage._cleanup_expired_session(
            session_dir,
            cutoff=8_000.0,
            coordinator=coordinator,
        )

    assert session_dir.is_dir()
    assert coordinator.release_calls == [(session_id, "0" * 31 + "1")]
    assert coordinator.claims == {}


def test_cleanup_retries_orphaned_quarantine_without_session_state(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    orphan = tmp_path / ".trash" / f"{'a' * 32}-{'1' * 32}"
    orphan.mkdir(parents=True)
    (orphan / "asset.bin").write_bytes(b"old")
    coordinator = RecordingCleanupCoordinator()

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert not orphan.exists()
    assert coordinator.claim_calls == ["a" * 32]
    assert coordinator.claims == {}


def test_cleanup_preserves_unauditable_quarantine_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    quarantined_session_id = "a" * 32
    generation_id = "1" * 32
    quarantine = (
        tmp_path / ".trash" / f"{quarantined_session_id}-{generation_id}"
    )
    invalid_asset = quarantine / "assets" / "not-an-asset-id"
    invalid_asset.mkdir(parents=True)
    (invalid_asset / "old.bin").write_bytes(b"old")
    expired_session_id = "b" * 32
    expired_session = tmp_path / "sessions" / expired_session_id
    expired_session.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator()
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )
    caplog.set_level("WARNING", logger="designtoolkit.web.hosted")

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert quarantine.is_dir()
    assert not expired_session.exists()
    failure = next(
        record
        for record in caplog.records
        if record.message == "Failed to recover quarantined session"
    )
    assert failure.session_id == quarantined_session_id
    assert failure.generation_id == generation_id
    assert failure.quarantine == str(quarantine)
    assert coordinator.claims == {}


def test_cleanup_preserves_orphaned_quarantine_while_new_lease_is_active(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    generation_id = "1" * 32
    orphan = tmp_path / ".trash" / f"{session_id}-{generation_id}"
    orphan.mkdir(parents=True)
    (orphan / "asset.bin").write_bytes(b"old")
    coordinator = RecordingCleanupCoordinator(
        active_sessions={session_id},
        cleanup_markers=[
            SimpleNamespace(
                session_id=session_id,
                generation_id=generation_id,
                legacy=False,
            )
        ],
    )
    coordinator.cleared_generations.add(generation_id)

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 0
    assert orphan.is_dir()
    assert coordinator.claim_calls == [session_id]
    assert coordinator.cleared_sessions == []
    assert coordinator.finalized_generations == []


def test_cleanup_finalizes_markers_left_after_quarantine_deletion(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    generation_id = "1" * 32
    legacy_generation_id = "2" * 32
    coordinator = RecordingCleanupCoordinator(
        cleanup_markers=[
            SimpleNamespace(
                session_id=session_id,
                generation_id=generation_id,
                legacy=False,
            ),
            SimpleNamespace(
                session_id=session_id,
                generation_id=legacy_generation_id,
                legacy=True,
            ),
        ]
    )
    coordinator.cleared_generations.add(generation_id)
    coordinator.reconciled_legacy[legacy_generation_id] = 4

    assert storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    ) == 0

    assert coordinator.finalized_generations == [generation_id]
    assert coordinator.finalized_legacy == [legacy_generation_id]


def test_cleanup_deletes_legacy_orphan_without_clearing_recreated_session_usage(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    orphan = tmp_path / ".trash" / f"{session_id}-{'1' * 32}"
    orphan.mkdir(parents=True)
    (orphan / "old.bin").write_bytes(b"old")
    recreated = (
        tmp_path / "sessions" / session_id / "assets" / ("2" * 32)
    )
    recreated.mkdir(parents=True)
    (recreated / "new.bin").write_bytes(b"new")
    committed_bytes = {session_id: 3}
    coordinator = RecordingCleanupCoordinator(committed_bytes=committed_bytes)

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert not orphan.exists()
    assert (recreated / "new.bin").read_bytes() == b"new"
    assert committed_bytes == {session_id: 3}
    assert coordinator.reconciled_legacy == {"1" * 32: 0}


def test_cleanup_reconciles_legacy_usage_after_crash_without_recreated_session(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    generation_id = "1" * 32
    orphan_asset = (
        tmp_path / ".trash" / f"{session_id}-{generation_id}" / "assets" / ("2" * 32)
    )
    orphan_asset.mkdir(parents=True)
    (orphan_asset / "old.bin").write_bytes(b"old!")
    committed_bytes = {session_id: 4}
    coordinator = RecordingCleanupCoordinator(committed_bytes=committed_bytes)

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert committed_bytes == {}
    assert coordinator.reconciled_legacy == {generation_id: 4}


def test_cleanup_reconciles_legacy_usage_without_clearing_recreated_usage(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    generation_id = "1" * 32
    orphan_asset = (
        tmp_path / ".trash" / f"{session_id}-{generation_id}" / "assets" / ("2" * 32)
    )
    orphan_asset.mkdir(parents=True)
    (orphan_asset / "old.bin").write_bytes(b"old!")
    recreated_asset = (
        tmp_path / "sessions" / session_id / "assets" / ("3" * 32)
    )
    recreated_asset.mkdir(parents=True)
    (recreated_asset / "new.bin").write_bytes(b"new")
    committed_bytes = {session_id: 7}
    coordinator = RecordingCleanupCoordinator(committed_bytes=committed_bytes)

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert committed_bytes == {session_id: 3}
    assert coordinator.reconciled_legacy == {generation_id: 4}
    assert (recreated_asset / "new.bin").read_bytes() == b"new"


def test_cleanup_retries_quarantine_after_transient_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    orphan = tmp_path / ".trash" / f"{'a' * 32}-{'1' * 32}"
    orphan.mkdir(parents=True)
    (orphan / "asset.bin").write_bytes(b"old")
    coordinator = RecordingCleanupCoordinator()
    original_rmtree = shutil.rmtree
    attempts = 0

    def transient_failure(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("file is busy")
        original_rmtree(path)

    monkeypatch.setattr(
        "designtoolkit.web.hosted.shutil.rmtree",
        transient_failure,
    )

    first_removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )
    second_removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert first_removed == 0
    assert second_removed == 1
    assert not orphan.exists()


def test_cleanup_refreshes_claim_during_slow_recursive_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)
    coordinator = RecordingCleanupCoordinator()
    original_rmtree = shutil.rmtree
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )

    def slow_rmtree(path: Path) -> None:
        sleep(0.04)
        original_rmtree(path)

    monkeypatch.setattr("designtoolkit.web.hosted.shutil.rmtree", slow_rmtree)

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 1
    assert len(coordinator.refresh_calls) >= 2


def test_cleanup_recovers_trash_after_crash_following_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    session_dir.mkdir(parents=True)

    committed_bytes = {session_id: 4}
    old_marker = "1" * 32
    new_marker = "2" * 32
    release_markers = {session_id: {old_marker}}

    class StatefulCoordinator(RecordingCleanupCoordinator):
        def __init__(self) -> None:
            super().__init__(committed_bytes=committed_bytes)

        def capture_session_usage(
            self,
            session_id: str,
            claim_token: str,
        ) -> SimpleNamespace:
            generation = super().capture_session_usage(session_id, claim_token)
            generation.released_asset_ids = tuple(
                sorted(release_markers.get(session_id, set()))
            )
            return generation

        def clear_session_usage_generation(
            self,
            session_id: str,
            claim_token: str,
            generation: SimpleNamespace,
        ) -> None:
            super().clear_session_usage_generation(
                session_id,
                claim_token,
                generation,
            )
            release_markers.setdefault(session_id, set()).difference_update(
                generation.released_asset_ids
            )

    class CrashingCoordinator(StatefulCoordinator):
        def clear_session_usage_generation(
            self,
            session_id: str,
            claim_token: str,
            generation: SimpleNamespace,
        ) -> None:
            raise RuntimeError("cleanup process crashed")

    coordinator = CrashingCoordinator()
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )

    with pytest.raises(RuntimeError, match="cleanup process crashed"):
        storage._cleanup_expired_session(
            session_dir,
            cutoff=8_000.0,
            coordinator=coordinator,
        )

    assert not session_dir.exists()
    assert len(list((tmp_path / ".trash").iterdir())) == 1
    assert coordinator.claims == {}
    assert committed_bytes == {session_id: 4}
    assert release_markers == {session_id: {old_marker}}

    session_dir.mkdir(parents=True)
    (session_dir / "new.bin").write_bytes(b"new")
    committed_bytes[session_id] += 3
    release_markers[session_id].add(new_marker)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 9_500.0,
    )
    recovery = StatefulCoordinator()
    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=recovery,
    )

    assert removed == 1
    assert recovery.cleared_sessions == [session_id]
    assert committed_bytes == {session_id: 3}
    assert release_markers == {session_id: {new_marker}}
    assert session_dir.is_dir()
    assert (session_dir / "new.bin").read_bytes() == b"new"
    assert not (tmp_path / ".trash").exists()

    assert storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=recovery,
    ) == 0


def test_directory_result_is_packaged_as_a_zip(tmp_path: Path) -> None:
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    (output_dir / "frame_000001.png").write_bytes(b"png")

    artifact = prepare_download_artifact("task-1", str(output_dir))

    assert artifact == tmp_path / "task-1.zip"
    assert artifact.is_file()
    assert not output_dir.exists()


def test_single_file_result_is_not_packaged(tmp_path: Path) -> None:
    output = tmp_path / "result.png"
    output.write_bytes(b"png")

    artifact = prepare_download_artifact("task-1", [output])

    assert artifact == output
    assert output.read_bytes() == b"png"
    assert not (tmp_path / "task-1.zip").exists()


def test_structured_multiple_output_paths_are_packaged_as_a_zip(
    tmp_path: Path,
) -> None:
    outputs = [tmp_path / "one.png", tmp_path / "two.png"]
    for output in outputs:
        output.write_bytes(output.stem.encode())

    artifact = prepare_download_artifact(
        "task-1",
        outputs,
        max_output_bytes=512,
    )

    with ZipFile(artifact) as archive:
        assert archive.namelist() == ["one.png", "two.png"]
        assert archive.read("one.png") == b"one"
        assert archive.read("two.png") == b"two"
    assert all(not output.exists() for output in outputs)


def test_archive_stops_when_source_plus_zip_exceeds_physical_budget(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    source_bytes = 64 * 1024
    random = Random(0)
    for index in range(2):
        (output_dir / f"frame_{index}.bin").write_bytes(
            random.randbytes(source_bytes)
        )
    archive_path = tmp_path / "task-1.zip"

    with pytest.raises(OutputLimitExceeded, match="configured 132096-byte limit"):
        prepare_download_artifact(
            "task-1",
            [output_dir],
            max_output_bytes=2 * source_bytes + 1024,
        )

    assert not archive_path.exists()
    assert not output_dir.exists()


def test_legacy_delimited_output_record_is_still_supported(tmp_path: Path) -> None:
    outputs = [tmp_path / "one.png", tmp_path / "two.png"]
    for output in outputs:
        output.write_bytes(output.stem.encode())

    artifact = prepare_download_artifact(
        "task-1",
        "; ".join(str(output) for output in outputs),
    )

    with ZipFile(artifact) as archive:
        assert archive.namelist() == ["one.png", "two.png"]
    assert all(not output.exists() for output in outputs)


def test_download_artifact_rejects_linked_directory_child(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    link = output_dir / "linked"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory links are unavailable: {exc}")
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(outside_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            pytest.skip(f"directory links are unavailable: {result.stderr}")

    with pytest.raises(UploadRejectedError, match="link|junction"):
        prepare_download_artifact("task-1", str(output_dir))

    assert outside_file.read_text(encoding="utf-8") == "secret"
    assert not (tmp_path / "task-1.zip").exists()


@pytest.mark.parametrize("failure_mode", ["write", "close"])
def test_partial_zip_is_removed_after_packaging_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    source = output_dir / "frame_000001.png"
    source.write_bytes(b"png")
    archive_path = tmp_path / "task-1.zip"

    class FailingZipFile:
        def __init__(self, archive_file, *_args, **_kwargs) -> None:
            self.archive_file = archive_file

        def __enter__(self):
            self.archive_file.write(b"partial zip")
            return self

        def write(self, _path: Path, *, arcname: str) -> None:
            del arcname
            if failure_mode == "write":
                raise OSError("zip write failed")

        def open(self, _name: str, _mode: str):
            class FailingArchiveMember:
                def __enter__(self):
                    return self

                def __exit__(self, _exc_type, _exc, _traceback) -> None:
                    pass

                def write(self, _data: bytes) -> None:
                    if failure_mode == "write":
                        raise OSError("zip write failed")

            return FailingArchiveMember()

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            if failure_mode == "close":
                raise OSError("zip close failed")

    monkeypatch.setattr("designtoolkit.web.hosted.ZipFile", FailingZipFile)

    with pytest.raises(OSError, match=f"zip {failure_mode} failed"):
        prepare_download_artifact("task-1", str(output_dir))

    assert not archive_path.exists()
    assert not output_dir.exists()


def test_oversized_task_result_is_removed(tmp_path: Path) -> None:
    output = tmp_path / "result.png"
    output.write_bytes(b"12345")

    with pytest.raises(UploadRejectedError, match="output exceeds"):
        prepare_download_artifact(
            "task-1",
            str(output),
            max_output_bytes=4,
        )

    assert not output.exists()
