from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Timer
from time import monotonic, sleep
from unittest.mock import Mock
from urllib.parse import quote
from uuid import uuid4

import fakeredis
import numpy as np
import pytest
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from PIL import Image
from redis import Redis
from redis.exceptions import (
    BusyLoadingError,
    OutOfMemoryError,
    ReadOnlyError,
    RedisError,
    ResponseError,
)
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from rq import Queue

from designtoolkit.chroma_keyer.video_models import VideoMetadata
from designtoolkit.web import app as app_module
from designtoolkit.web import routes as routes_module
from designtoolkit.web.app import create_app
from designtoolkit.web.chroma_previews import ChromaPreviewStore
from designtoolkit.web.heartbeat import HeartbeatStopTimeoutError
from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    UploadRejectedError,
)
from designtoolkit.web.logs import LogStore
from designtoolkit.web.media_limits import HostedMediaValidator, MediaLimitError
from designtoolkit.web.redis_tasks import RedisTaskManager
from designtoolkit.web.resources import (
    AssetReleaseMismatchError,
    RedisResourceCoordinator,
    ResourceUnavailableError,
)
from designtoolkit.web.schemas import (
    LogLevel,
    LogPhase,
    TaskKind,
    TaskStatus,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskManager


class StubReservation:
    def __init__(
        self,
        coordinator: StubCoordinator,
        session_id: str,
        reserved_bytes: int,
        asset_id: str | None = None,
        generation_id: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.session_id = session_id
        self.reserved_bytes = reserved_bytes
        self.asset_id = asset_id
        self.generation_id = generation_id
        self.active = True
        self.rollback_eligible = False

    def refresh(self) -> None:
        if not self.active:
            raise ResourceUnavailableError("Upload reservation expired")

    def commit(self) -> None:
        if not self.active:
            raise ResourceUnavailableError("Upload reservation expired")
        self.active = False
        self.coordinator.active_uploads -= 1
        self.coordinator.committed[self.session_id] = (
            self.coordinator.committed.get(self.session_id, 0)
            + self.reserved_bytes
        )
        if self.asset_id is not None and self.generation_id is not None:
            self.coordinator.committed_generations[
                (self.session_id, self.asset_id, self.generation_id)
            ] = self.reserved_bytes
        self.rollback_eligible = True

    def rollback(self) -> None:
        if not self.rollback_eligible:
            return
        self.rollback_eligible = False
        remaining = (
            self.coordinator.committed.get(self.session_id, 0)
            - self.reserved_bytes
        )
        if remaining:
            self.coordinator.committed[self.session_id] = remaining
        else:
            self.coordinator.committed.pop(self.session_id, None)
        if self.asset_id is not None and self.generation_id is not None:
            self.coordinator.committed_generations.pop(
                (self.session_id, self.asset_id, self.generation_id),
                None,
            )

    def finalize(self) -> None:
        self.rollback_eligible = False

    def release(self) -> None:
        if self.active:
            self.active = False
            self.coordinator.active_uploads -= 1


class StubLease:
    def __init__(self, coordinator: StubCoordinator) -> None:
        self.coordinator = coordinator
        self.active = True

    def refresh(self) -> None:
        self.coordinator.lease_refresh_calls += 1
        if not self.active:
            raise ResourceUnavailableError("Session lease expired")
        if self.coordinator.fail_lease_refresh:
            raise ResourceUnavailableError("Session lease heartbeat failed")

    def release(self) -> None:
        self.coordinator.lease_release_calls += 1
        if self.active:
            self.active = False
            self.coordinator.active_leases -= 1


class StubUploadRegistration:
    def __init__(
        self,
        coordinator: StubCoordinator,
        session_id: str,
        upload_id: str,
        token: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.session_id = session_id
        self.upload_id = upload_id
        self.token = token or uuid4().hex

    def refresh(self) -> bool:
        if (self.session_id, self.upload_id) not in self.coordinator.inflight:
            raise ResourceUnavailableError("Upload registration expired")
        return self.coordinator.inflight[(self.session_id, self.upload_id)]

    def finish(self) -> None:
        key = (self.session_id, self.upload_id)
        self.coordinator.inflight.pop(key, None)
        self.coordinator.inflight_generations.pop(key, None)


class StubCoordinator:
    def __init__(
        self,
        *,
        max_upload_concurrency: int,
        max_session_bytes: int,
        min_free_bytes: int,
    ) -> None:
        self.max_upload_concurrency = max_upload_concurrency
        self.max_session_bytes = max_session_bytes
        self.min_free_bytes = min_free_bytes
        self.active_uploads = 0
        self.committed: dict[str, int] = {}
        self.committed_generations: dict[tuple[str, str, str], int] = {}
        self.released_generations: set[tuple[str, str, str]] = set()
        self.pending_release_generations: dict[tuple[str, str], str] = {}
        self.released_assets: dict[str, tuple[str, int]] = {}
        self.release_calls = 0
        self.release_query_calls = 0
        self.finalize_release_calls = 0
        self.finalized_assets: set[tuple[str, str]] = set()
        self.reserve_calls = 0
        self.active_leases = 0
        self.lease_acquisitions = 0
        self.lease_refresh_calls = 0
        self.lease_release_calls = 0
        self.fail_lease_refresh = False
        self.session_lease_heartbeat_interval = 5.0
        self.reservation_heartbeat_interval = 5.0
        self.upload_registration_ttl_seconds = 60.0
        self.inflight: dict[tuple[str, str], bool] = {}
        self.inflight_generations: dict[tuple[str, str], str] = {}

    def register_upload(
        self,
        session_id: str,
        upload_id: str,
        *,
        generation_token: str | None = None,
    ) -> StubUploadRegistration:
        key = (session_id, upload_id)
        if key in self.inflight:
            raise ResourceUnavailableError("Upload is already in progress")
        released = self.released_assets.get(upload_id)
        if released is not None and released[0] == session_id:
            if key not in self.finalized_assets:
                raise ResourceUnavailableError("Upload deletion is still pending")
            self.released_assets.pop(upload_id)
            self.finalized_assets.discard(key)
        if key in self.pending_release_generations:
            raise ResourceUnavailableError("Upload deletion is still pending")
        registration = StubUploadRegistration(
            self,
            session_id,
            upload_id,
            generation_token,
        )
        self.inflight[key] = False
        self.inflight_generations[key] = registration.token
        return registration

    def cancel_upload(
        self,
        session_id: str,
        upload_id: str,
        expected_generation: str | None = None,
    ) -> bool:
        key = (session_id, upload_id)
        if key not in self.inflight:
            return False
        if (
            expected_generation is not None
            and self.inflight_generations.get(key) != expected_generation
        ):
            return False
        self.inflight[key] = True
        return True

    def reserve_upload(
        self,
        session_id: str,
        requested_bytes: int,
        *,
        free_bytes: int,
        asset_id: str | None = None,
        generation_id: str | None = None,
    ) -> StubReservation:
        self.reserve_calls += 1
        if self.active_uploads >= self.max_upload_concurrency:
            raise ResourceUnavailableError("Upload concurrency limit reached")
        if (
            self.committed.get(session_id, 0) + requested_bytes
            > self.max_session_bytes
        ):
            raise ResourceUnavailableError("Upload session byte limit reached")
        if free_bytes - requested_bytes < self.min_free_bytes:
            raise ResourceUnavailableError("Insufficient free disk capacity")
        self.active_uploads += 1
        return StubReservation(
            self,
            session_id,
            requested_bytes,
            asset_id,
            generation_id,
        )

    def acquire_session(self, _session_id: str) -> StubLease:
        self.active_leases += 1
        self.lease_acquisitions += 1
        return StubLease(self)

    def release_committed_asset(
        self,
        session_id: str,
        asset_id: str,
        generation_or_released_bytes: str | int,
        *,
        expected_remaining_bytes: int | None = None,
    ) -> None:
        self.release_calls += 1
        if isinstance(generation_or_released_bytes, str):
            generation_id = generation_or_released_bytes
            key = (session_id, asset_id, generation_id)
            if key in self.released_generations:
                return
            released_bytes = self.committed_generations.pop(key, None)
            if released_bytes is None:
                return
            current = self.committed.get(session_id, 0)
            remaining = current - released_bytes
            if remaining:
                self.committed[session_id] = remaining
            else:
                self.committed.pop(session_id, None)
            self.released_generations.add(key)
            self.pending_release_generations[(session_id, asset_id)] = (
                generation_id
            )
            self.released_assets[asset_id] = (session_id, released_bytes)
            return
        released_bytes = generation_or_released_bytes
        released = self.released_assets.get(asset_id)
        if released is not None:
            if released[0] != session_id:
                raise AssetReleaseMismatchError("Uploaded asset is not available")
            if released_bytes != 0 and released[1] != released_bytes:
                raise ResourceUnavailableError("Asset release metadata mismatch")
            return
        current = self.committed.get(session_id, 0)
        if expected_remaining_bytes is not None:
            if current == expected_remaining_bytes:
                remaining = current
            elif current >= expected_remaining_bytes + released_bytes:
                remaining = current - released_bytes
            else:
                raise ResourceUnavailableError(
                    "Asset release does not match audited committed usage"
                )
        else:
            if released_bytes == 0:
                return
            if released_bytes > current:
                raise ResourceUnavailableError("Asset release bytes mismatch")
            remaining = current - released_bytes
        if remaining:
            self.committed[session_id] = remaining
        else:
            self.committed.pop(session_id, None)
        self.released_assets[asset_id] = (session_id, released_bytes)

    def release_legacy_committed_asset(
        self,
        session_id: str,
        asset_id: str,
        released_bytes: int,
        *,
        expected_remaining_bytes: int | None = None,
    ) -> None:
        self.release_committed_asset(
            session_id,
            asset_id,
            released_bytes,
            expected_remaining_bytes=expected_remaining_bytes,
        )

    def is_committed_asset_released(
        self,
        session_id: str,
        asset_id: str,
        generation_id: str | None = None,
    ) -> bool:
        self.release_query_calls += 1
        if generation_id is not None:
            return (session_id, asset_id, generation_id) in (
                self.released_generations
            )
        released = self.released_assets.get(asset_id)
        return released is not None and released[0] == session_id

    def pending_asset_release_generation(
        self,
        session_id: str,
        asset_id: str,
    ) -> str | None:
        self.release_query_calls += 1
        return self.pending_release_generations.get((session_id, asset_id))

    def finalize_asset_release(
        self,
        session_id: str,
        asset_id: str,
        generation_id: str | None = None,
    ) -> None:
        self.finalize_release_calls += 1
        if generation_id is not None:
            key = (session_id, asset_id)
            if self.pending_release_generations.get(key) == generation_id:
                self.pending_release_generations.pop(key)
        released = self.released_assets.get(asset_id)
        if released is not None and released[0] == session_id:
            self.finalized_assets.add((session_id, asset_id))


class StubHostedVideoDecoder:
    def __init__(self, metadata: VideoMetadata | None = None) -> None:
        self.metadata = metadata or VideoMetadata(
            width=2,
            height=2,
            fps=1.0,
            duration_seconds=1.0,
            estimated_frame_count=1,
            codec="stub",
        )
        self.probe_calls = 0
        self.decode_calls = 0

    def probe(self, _path: Path) -> VideoMetadata:
        self.probe_calls += 1
        return self.metadata

    def read_frame(self, _path: Path, _timestamp_seconds: float) -> np.ndarray:
        self.decode_calls += 1
        return np.zeros((2, 2, 3), dtype=np.uint8)

    def read_preview_frame(
        self,
        path: Path,
        timestamp_seconds: float,
    ) -> np.ndarray:
        return self.read_frame(path, timestamp_seconds)

    def read_frame_with_metadata(
        self,
        path: Path,
        timestamp_seconds: float,
        *,
        metadata: VideoMetadata,
    ) -> np.ndarray:
        return self.read_frame(path, timestamp_seconds)

    def read_preview_frame_with_metadata(
        self,
        path: Path,
        timestamp_seconds: float,
        *,
        metadata: VideoMetadata,
    ) -> np.ndarray:
        return self.read_frame(path, timestamp_seconds)


def _png_bytes(size: tuple[int, int] = (2, 1)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (0, 255, 0)).save(output, format="PNG")
    return output.getvalue()


def _animated_gif_bytes() -> bytes:
    output = BytesIO()
    frames = [
        Image.new("RGB", (2, 2), color)
        for color in ((255, 0, 0), (0, 255, 0))
    ]
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=10,
    )
    return output.getvalue()


def _client(
    tmp_path: Path,
    *,
    max_bytes: int = 500 * 1024 * 1024,
    max_session_bytes: int = 2 * 1024 * 1024 * 1024,
    max_upload_concurrency: int = 4,
    min_free_bytes: int = 0,
    task_manager: TaskManager | RedisTaskManager | None = None,
    upload_requests_per_hour: int = 30,
    api_requests_per_minute: int = 120,
    max_stream_concurrency: int = 16,
    resource_coordinator: StubCoordinator | None = None,
    hosted_storage: HostedStorage | None = None,
    max_image_pixels: int = 100_000_000,
    max_video_width: int = 3840,
    max_video_height: int = 2160,
    max_video_duration_seconds: float = 1800.0,
    video_decoder: StubHostedVideoDecoder | None = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    if task_manager is None:
        redis_connection = fakeredis.FakeRedis(decode_responses=False)
        task_manager = RedisTaskManager(
            redis_connection=redis_connection,
            queue=Queue("designtoolkit", connection=redis_connection),
            data_dir=tmp_path,
            max_queued_tasks=20,
            retention_seconds=7200,
        )
    settings = HostedSettings(
        enabled=True,
        data_dir=tmp_path,
        redis_url="redis://unused",
        max_upload_bytes=max_bytes,
        max_session_bytes=max_session_bytes,
        max_upload_concurrency=max_upload_concurrency,
        min_free_bytes=min_free_bytes,
        max_image_pixels=max_image_pixels,
        max_video_width=max_video_width,
        max_video_height=max_video_height,
        max_video_duration_seconds=max_video_duration_seconds,
        secure_cookies=False,
        upload_requests_per_hour=upload_requests_per_hour,
        api_requests_per_minute=api_requests_per_minute,
        max_stream_concurrency=max_stream_concurrency,
    )
    coordinator = resource_coordinator or StubCoordinator(
        max_upload_concurrency=max_upload_concurrency,
        max_session_bytes=max_session_bytes,
        min_free_bytes=min_free_bytes,
    )
    return TestClient(
        create_app(
            task_manager=task_manager,
            hosted_settings=settings,
            hosted_storage=hosted_storage
            or HostedStorage(tmp_path, max_upload_bytes=max_bytes),
            resource_coordinator=coordinator,
            video_decoder=video_decoder or StubHostedVideoDecoder(),
        ),
        raise_server_exceptions=raise_server_exceptions,
    )


def _upload(
    client: TestClient,
    filename: str,
    content: bytes,
    *,
    content_length: int | None = None,
    generation_token: str | None = None,
    upload_id: str | None = None,
):
    headers = {
        "content-length": str(
            len(content) if content_length is None else content_length
        ),
        "content-type": "application/octet-stream",
        "x-upload-filename": quote(filename, safe=""),
        "x-upload-id": upload_id or uuid4().hex,
    }
    if generation_token is not None:
        headers["x-upload-generation"] = generation_token
    return client.post(
        "/api/uploads",
        content=content,
        headers=headers,
    )


def _delete_uploaded_asset(client: TestClient, reference: str):
    _scheme, asset_id, generation_id, _filename = reference.split(":", 3)
    return client.delete(
        f"/api/uploads/{asset_id}?generation={generation_id}"
    )


@pytest.mark.parametrize(
    ("status", "error", "expected_level", "expected_phase", "expected_message"),
    [
        (
            TaskStatus.SUCCEEDED,
            None,
            LogLevel.SUCCESS,
            LogPhase.COMPLETED,
            "Task completed",
        ),
        (
            TaskStatus.FAILED,
            "worker failed",
            LogLevel.ERROR,
            LogPhase.FAILED,
            "worker failed",
        ),
    ],
)
def test_task_event_stream_synthesizes_terminal_event_from_task_state(
    tmp_path: Path,
    status: TaskStatus,
    error: str | None,
    expected_level: LogLevel,
    expected_phase: LogPhase,
    expected_message: str,
) -> None:
    class EmptyThenFailSubscriber:
        calls = 0

        def get(self, timeout: float):
            del timeout
            type(self).calls += 1
            if type(self).calls == 1:
                raise routes_module.Empty
            raise AssertionError("terminal task stream did not close")

    class MissingTerminalLogStore(LogStore):
        def history_and_subscribe(self, task_id: str):
            return self.history(task_id), EmptyThenFailSubscriber()

        def unsubscribe(self, task_id: str, subscriber) -> None:
            del task_id, subscriber

    manager = TaskManager(
        log_store=MissingTerminalLogStore(),
        run_async=False,
    )
    with _client(tmp_path, task_manager=manager) as client:
        client.get("/api/config")
        task = manager.reserve_task(
            kind=TaskKind.VIDEO_FRAMES,
            input_path="clip.mp4",
            output_path=None,
            owner_id=client.cookies["dt_session"],
        )
        manager.mark_finished(task.task_id, status=status, error=error)

        response = client.get(f"/api/tasks/{task.task_id}/events")

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    terminal = payloads[-1]
    assert terminal == {
        "task_id": task.task_id,
        "timestamp": task.finished_at.isoformat().replace("+00:00", "Z"),
        "level": expected_level.value,
        "phase": expected_phase.value,
        "message": expected_message,
        "details": {
            "source": "task_state",
            "status": status.value,
        },
    }
    assert len(
        [
            payload
            for payload in payloads
            if payload["phase"] in {"completed", "failed"}
        ]
    ) == 1
    assert EmptyThenFailSubscriber.calls == 1


def test_hosted_old_redis_task_is_sanitized_only_at_get_and_sse_boundaries(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    task_id = "legacy-public-task"
    input_path = tmp_path / "sessions" / ("1" * 32) / "legacy.mov"
    output_path = tmp_path / "sessions" / ("1" * 32) / "result.png"
    private_path = tmp_path / "sessions" / ("2" * 32) / "failure.log"
    finished_at = datetime.now(UTC)

    with _client(tmp_path, task_manager=manager) as client:
        assert client.get("/api/config").status_code == 200
        owner_id = client.cookies["dt_session"]
        redis_connection.set(
            f"dt:task:{task_id}",
            json.dumps(
                {
                    "task_id": task_id,
                    "kind": TaskKind.VIDEO_FRAMES.value,
                    "status": TaskStatus.FAILED.value,
                    "created_at": finished_at.isoformat(),
                    "started_at": finished_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "input_path": str(input_path),
                    "output_path": str(output_path),
                    "error": f"failed at {private_path}",
                    "owner_id": owner_id,
                    "result_path": str(output_path),
                }
            ),
            ex=7200,
        )

        task_response = client.get(f"/api/tasks/{task_id}")
        event_response = client.get(f"/api/tasks/{task_id}/events")

    assert task_response.status_code == 200
    assert task_response.json()["input_path"] == "legacy.mov"
    assert task_response.json()["output_path"] == "result.png"
    assert task_response.json()["error"] == "failed at [managed path]"

    terminal_payload = json.loads(
        next(
            line.removeprefix("data: ")
            for line in event_response.text.splitlines()
            if line.startswith("data: ")
        )
    )
    assert terminal_payload["message"] == "failed at [managed path]"

    internal_task = manager.get_task(task_id)
    assert internal_task is not None
    assert internal_task.input_path == str(input_path)
    assert internal_task.output_path == str(output_path)
    assert internal_task.error == f"failed at {private_path}"


def test_hosted_event_stream_sanitizes_injected_memory_history_and_live_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_store = LogStore()
    manager = TaskManager(log_store=log_store, run_async=False)
    subscribed = Event()
    original_subscribe = log_store.history_and_subscribe

    def tracked_subscribe(task_id: str):
        result = original_subscribe(task_id)
        subscribed.set()
        return result

    monkeypatch.setattr(log_store, "history_and_subscribe", tracked_subscribe)
    private_path = tmp_path / "sessions" / ("3" * 32) / "private.log"
    with _client(tmp_path, task_manager=manager) as client:
        client.get("/api/config")
        task = manager.reserve_task(
            kind=TaskKind.VIDEO_FRAMES,
            input_path=str(private_path),
            output_path=None,
            owner_id=client.cookies["dt_session"],
        )
        log_store.append(
            task_id=task.task_id,
            level=LogLevel.INFO,
            phase=LogPhase.RUNNING,
            message=f"History {private_path}",
            details={"history_path": str(private_path)},
        )

        def publish_live_event() -> None:
            assert subscribed.wait(timeout=1)
            log_store.append(
                task_id=task.task_id,
                level=LogLevel.SUCCESS,
                phase=LogPhase.COMPLETED,
                message=f"Live [managed path] {private_path}",
                details={"live_path": private_path.as_uri()},
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            publisher = executor.submit(publish_live_event)
            response = client.get(f"/api/tasks/{task.task_id}/events")
            publisher.result(timeout=2)

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    history = next(
        payload for payload in payloads if payload["phase"] == "running"
    )
    live = next(
        payload for payload in payloads if payload["phase"] == "completed"
    )
    assert history["message"] == "History private.log"
    assert history["details"] == {"history_path": "private.log"}
    assert live["message"] == "Live [managed path] private.log"
    assert live["details"] == {"live_path": "private.log"}
    assert str(private_path) not in response.text
    assert "3" * 32 not in response.text


def test_local_event_stream_preserves_injected_memory_store_paths(
    tmp_path: Path,
) -> None:
    log_store = LogStore()
    manager = TaskManager(log_store=log_store, run_async=False)
    private_path = tmp_path / "sessions" / ("4" * 32) / "private.log"
    task = manager.reserve_task(
        kind=TaskKind.VIDEO_FRAMES,
        input_path=str(private_path),
        output_path=None,
    )
    log_store.append(
        task_id=task.task_id,
        level=LogLevel.ERROR,
        phase=LogPhase.FAILED,
        message=f"Failed at {private_path}",
        details={"path": str(private_path)},
    )

    with TestClient(
        create_app(
            task_manager=manager,
            hosted_settings=HostedSettings.from_env({}),
            hosted_storage=None,
        )
    ) as client:
        response = client.get(f"/api/tasks/{task.task_id}/events")

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert payloads[-1]["message"] == f"Failed at {private_path}"
    assert payloads[-1]["details"] == {"path": str(private_path)}


def test_task_event_stream_slow_subscriber_has_no_duplicates_or_lost_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    subscribed = Event()
    original_subscribe = manager.log_store.history_and_subscribe

    def tracked_subscribe(task_id: str):
        result = original_subscribe(task_id)
        subscribed.set()
        return result

    monkeypatch.setattr(
        manager.log_store,
        "history_and_subscribe",
        tracked_subscribe,
    )
    with _client(tmp_path, task_manager=manager) as client:
        client.get("/api/config")
        task = manager.create_video_frames_task(
            VideoFramesTaskRequest(input_path=str(tmp_path / "clip.mp4")),
            owner_id=client.cookies["dt_session"],
        )

        def publish_events() -> None:
            assert subscribed.wait(timeout=1)
            sleep(0.06)
            manager.mark_running(task.task_id)
            sleep(0.06)
            manager.mark_finished(
                task.task_id,
                status=TaskStatus.FAILED,
                error="worker failed",
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            publisher = executor.submit(publish_events)
            response = client.get(f"/api/tasks/{task.task_id}/events")
            publisher.result(timeout=2)

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [payload["phase"] for payload in payloads] == [
        "created",
        "running",
        "failed",
    ]
    assert [payload["message"] for payload in payloads] == [
        "Created video_frames task",
        "Task is running",
        "worker failed",
    ]


def test_hosted_config_and_upload_issue_anonymous_session(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        config = client.get("/api/config")
        upload = _upload(client, "screen.png", _png_bytes())

    assert config.json() == {
        "hosted": True,
        "max_upload_bytes": 500 * 1024 * 1024,
    }
    assert "dt_session=" in config.headers["set-cookie"]
    assert upload.status_code == 201
    assert upload.json()["reference"].startswith("asset:")
    assert upload.json()["filename"] == "screen.png"


def test_hosted_upload_uses_validated_client_upload_id(tmp_path: Path) -> None:
    upload_id = "1" * 32

    with _client(tmp_path) as client:
        upload = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )

    assert upload.status_code == 201
    reference_parts = upload.json()["reference"].split(":")
    assert reference_parts == [
        "asset",
        upload_id,
        reference_parts[2],
        "screen.png",
    ]
    assert len(reference_parts[2]) == 32
    session_id = client.cookies["dt_session"]
    generation_marker = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    assert generation_marker.read_text(encoding="ascii") == reference_parts[2]


@pytest.mark.parametrize(
    "upload_id",
    [
        None,
        "",
        "1" * 31,
        "1" * 33,
        "G" * 32,
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "11111111-1111-1111-1111-111111111111",
    ],
)
def test_hosted_upload_requires_canonical_upload_id(
    tmp_path: Path,
    upload_id: str | None,
) -> None:
    headers = {
        "content-length": "5",
        "x-upload-filename": "screen.png",
    }
    if upload_id is not None:
        headers["x-upload-id"] = upload_id

    with _client(tmp_path) as client:
        response = client.post("/api/uploads", content=b"image", headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_upload"
    assert list(tmp_path.rglob(".uploading")) == []


def test_hosted_upload_requires_content_length(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        request = client.build_request(
            "POST",
            "/api/uploads",
            content=b"image",
            headers={"x-upload-filename": "screen.png"},
        )
        del request.headers["content-length"]
        response = client.send(request)

    assert response.status_code == 411
    assert response.json()["error"]["code"] == "length_required"


@pytest.mark.parametrize("value", ["0", "not-a-number"])
def test_hosted_upload_rejects_invalid_content_length(
    tmp_path: Path,
    value: str,
) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/uploads",
            content=b"image",
            headers={
                "content-length": value,
                "x-upload-filename": "screen.png",
            },
        )

    assert response.status_code == 411
    assert response.json()["error"]["code"] == "length_required"


@pytest.mark.parametrize("filename", [None, "%FF", "%ZZ.png"])
def test_hosted_upload_requires_valid_filename_header(
    tmp_path: Path,
    filename: str | None,
) -> None:
    headers = {"content-length": "5"}
    if filename is not None:
        headers["x-upload-filename"] = filename
    with _client(tmp_path) as client:
        response = client.post("/api/uploads", content=b"image", headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_upload"


@pytest.mark.parametrize("filename", ["bad%00name.png", "bad%1Fname.png"])
def test_hosted_upload_rejects_decoded_control_filename(
    tmp_path: Path,
    filename: str,
) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/uploads",
            content=b"image",
            headers={
                "content-length": "5",
                "x-upload-filename": filename,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_upload"
    assert list(tmp_path.rglob(".uploading")) == []


@pytest.mark.parametrize("filename", [".screen.png", ".", ".."])
def test_hosted_upload_rejects_hidden_filename_before_reservation(
    tmp_path: Path,
    filename: str,
) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=10,
        min_free_bytes=0,
    )
    with _client(tmp_path, resource_coordinator=coordinator) as client:
        response = _upload(client, filename, b"image")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_upload"
    assert coordinator.reserve_calls == 0
    assert list(tmp_path.rglob("*")) == []


def test_successful_upload_ignores_redis_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExecuteThenRaiseLease:
        release_count = 0

        def refresh(self) -> None:
            pass

        def release(self) -> None:
            self.release_count += 1
            raise RedisConnectionError("lease release acknowledgement lost")

    lease = ExecuteThenRaiseLease()

    def fail_reservation_release(_reservation: StubReservation) -> None:
        raise RedisConnectionError("reservation release unavailable")

    monkeypatch.setattr(StubReservation, "release", fail_reservation_release)
    with _client(tmp_path) as client:
        coordinator = client.app.state.resource_coordinator
        monkeypatch.setattr(
            coordinator,
            "acquire_session",
            lambda _session_id: lease,
        )

        response = _upload(client, "screen.png", b"image")

    assert response.status_code == 201
    assert response.json()["size"] == 5
    assert lease.release_count == 1
    assert len(list(tmp_path.rglob("screen.png"))) == 1


def test_hosted_upload_rejects_declared_actual_length_mismatch(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, "screen.png", b"image", content_length=6)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_upload"
    assert list(tmp_path.rglob(".uploading")) == []


def test_hosted_upload_commits_session_usage(tmp_path: Path) -> None:
    with _client(tmp_path, max_session_bytes=5) as client:
        response = _upload(client, "screen.png", b"image")
        session_id = client.cookies["dt_session"]
        coordinator = client.app.state.resource_coordinator

        with pytest.raises(ResourceUnavailableError, match="session byte"):
            coordinator.reserve_upload(session_id, 1, free_bytes=10**12)

    assert response.status_code == 201


def test_hosted_upload_session_quota_returns_413(tmp_path: Path) -> None:
    with _client(tmp_path, max_session_bytes=6) as client:
        first = _upload(client, "first.png", b"1234")
        second = _upload(client, "second.png", b"567")

    assert first.status_code == 201
    assert second.status_code == 413
    assert second.json()["error"]["code"] == "upload_too_large"


def test_duplicate_upload_id_does_not_overwrite_or_double_charge(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    with _client(tmp_path, max_session_bytes=5) as client:
        first = _upload(client, "first.png", b"image", upload_id=upload_id)
        duplicate = _upload(client, "second.png", b"image", upload_id=upload_id)
        overflow = _upload(client, "overflow.png", b"x")
        session_id = client.cookies["dt_session"]
        coordinator = client.app.state.resource_coordinator

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "upload_conflict"
    assert overflow.status_code == 413
    assert coordinator.committed[session_id] == 5
    assert coordinator.reserve_calls == 2
    assert len(list(tmp_path.rglob("first.png"))) == 1
    assert list(tmp_path.rglob("second.png")) == []


def test_hosted_upload_uses_client_supplied_generation_without_exposing_it(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    generation = "2" * 32

    with _client(tmp_path) as client:
        response = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
            generation_token=generation,
        )

    assert response.status_code == 201
    assert response.json() == {
        "reference": f"asset:{upload_id}:{generation}:screen.png",
        "filename": "screen.png",
        "size": 5,
    }


@pytest.mark.parametrize(
    "generation",
    ["2" * 31, "2" * 31 + "G", "2" * 33],
)
def test_hosted_upload_rejects_invalid_client_generation(
    tmp_path: Path,
    generation: str,
) -> None:
    with _client(tmp_path) as client:
        response = _upload(
            client,
            "screen.png",
            b"image",
            generation_token=generation,
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_upload"


def test_known_upload_id_recovers_committed_upload_after_response_loss(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    with _client(tmp_path, max_session_bytes=5) as client:
        uploaded = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )
        deleted = _delete_uploaded_asset(
            client,
            uploaded.json()["reference"],
        )
        replacement = _upload(client, "replacement.png", b"12345")
        session_id = client.cookies["dt_session"]
        coordinator = client.app.state.resource_coordinator

    assert uploaded.status_code == 201
    assert deleted.status_code == 204
    assert replacement.status_code == 201
    assert coordinator.committed[session_id] == 5
    assert list(tmp_path.rglob("screen.png")) == []


def test_reused_upload_id_releases_each_generation_quota(tmp_path: Path) -> None:
    upload_id = "1" * 32
    with _client(tmp_path, max_session_bytes=5) as client:
        first_upload = _upload(
            client,
            "first.png",
            b"first",
            upload_id=upload_id,
        )
        first_reference = first_upload.json()["reference"]
        first_generation = first_reference.split(":", 3)[2]
        first_delete = _delete_uploaded_asset(
            client,
            first_reference,
        )
        second_upload = _upload(
            client,
            "second.png",
            b"other",
            upload_id=upload_id,
        )
        session_id = client.cookies["dt_session"]
        coordinator = client.app.state.resource_coordinator
        coordinator.release_committed_asset(
            session_id,
            upload_id,
            first_generation,
        )
        assert coordinator.committed[session_id] == 5
        second_delete = _delete_uploaded_asset(
            client,
            second_upload.json()["reference"],
        )

    assert first_upload.status_code == 201
    assert first_delete.status_code == 204
    assert second_upload.status_code == 201
    assert second_delete.status_code == 204
    assert coordinator.committed.get(session_id, 0) == 0
    assert coordinator.release_calls == 3


@pytest.mark.parametrize("generation_transport", ["query", "header"])
def test_v2_delete_requires_and_accepts_matching_generation(
    tmp_path: Path,
    generation_transport: str,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as client:
        uploaded = _upload(client, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        _scheme, asset_id, generation_id, _filename = reference.split(":", 3)
        session_id = client.cookies["dt_session"]
        coordinator = client.app.state.resource_coordinator
        inflight_key = (session_id, asset_id)
        coordinator.inflight[inflight_key] = False
        coordinator.inflight_generations[inflight_key] = generation_id

        missing_generation = client.delete(f"/api/uploads/{asset_id}")
        preserved_contents = storage.resolve(
            session_id,
            reference,
        ).read_bytes()
        cancellation_state = coordinator.inflight[inflight_key]
        release_calls = coordinator.release_calls
        coordinator.inflight.pop(inflight_key)
        coordinator.inflight_generations.pop(inflight_key)
        if generation_transport == "query":
            matching = client.delete(
                f"/api/uploads/{asset_id}?generation={generation_id}"
            )
        else:
            matching = client.delete(
                f"/api/uploads/{asset_id}",
                headers={"X-Upload-Generation": generation_id},
            )

    assert missing_generation.status_code == 404
    assert cancellation_state is False
    assert release_calls == 0
    assert preserved_contents == b"image"
    assert matching.status_code == 204
    assert coordinator.committed.get(session_id, 0) == 0


def test_v1_delete_without_generation_remains_supported(tmp_path: Path) -> None:
    session_id = "a" * 32
    asset_id = "1" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "legacy.png").write_bytes(b"data")
    (asset_dir / ".asset.json").write_text(
        json.dumps({"asset_id": asset_id, "bytes": 4}),
        encoding="utf-8",
    )

    with _client(tmp_path, max_session_bytes=5) as client:
        client.cookies.set("dt_session", session_id)
        coordinator = client.app.state.resource_coordinator
        coordinator.committed[session_id] = 4
        deleted = client.delete(f"/api/uploads/{asset_id}")

    assert deleted.status_code == 204
    assert coordinator.committed.get(session_id, 0) == 0
    assert not asset_dir.exists()


def test_stale_generation_delete_does_not_affect_reused_upload_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_id = "1" * 32
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    second_started = Event()
    resume_second = Event()
    original_store = storage.store_stream

    async def pause_second(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        second_started.set()
        while not resume_second.is_set():
            await asyncio.sleep(0.005)
        return await original_store(*args, **kwargs)

    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as client:
        first = _upload(
            client,
            "first.png",
            b"first",
            upload_id=upload_id,
        )
        first_reference = first.json()["reference"]
        first_generation = first_reference.split(":", 3)[2]
        first_delete = client.delete(
            f"/api/uploads/{upload_id}?generation={first_generation}"
        )
        monkeypatch.setattr(storage, "store_stream", pause_second)

        with ThreadPoolExecutor(max_workers=1) as executor:
            second_future = executor.submit(
                _upload,
                client,
                "second.png",
                b"other",
                upload_id=upload_id,
            )
            try:
                assert second_started.wait(timeout=2)
                stale_inflight = client.delete(
                    f"/api/uploads/{upload_id}?generation={first_generation}"
                )
            finally:
                resume_second.set()
            second = second_future.result(timeout=2)

        second_reference = second.json()["reference"]
        second_generation = second_reference.split(":", 3)[2]
        session_id = client.cookies["dt_session"]
        stale_published = client.delete(
            f"/api/uploads/{upload_id}?generation={first_generation}"
        )
        second_contents = storage.resolve(
            session_id,
            second_reference,
        ).read_bytes()
        matching_delete = client.delete(
            f"/api/uploads/{upload_id}?generation={second_generation}"
        )
        coordinator = client.app.state.resource_coordinator

    assert first_delete.status_code == 204
    assert stale_inflight.status_code == 404
    assert second.status_code == 201
    assert stale_published.status_code == 404
    assert second_contents == b"other"
    assert matching_delete.status_code == 204
    assert coordinator.committed.get(session_id, 0) == 0


def test_random_delete_returns_404_without_blocking_later_upload(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    with _client(tmp_path, max_session_bytes=5) as client:
        client.get("/api/config")
        deleted = client.delete(f"/api/uploads/{upload_id}")
        rejected = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )
        session_id = client.cookies["dt_session"]
        coordinator = client.app.state.resource_coordinator

    assert deleted.status_code == 404
    assert rejected.status_code == 201
    assert coordinator.committed[session_id] == 5
    assert len(list(tmp_path.rglob("screen.png"))) == 1
    assert not (
        tmp_path / "sessions" / session_id / ".upload-cancellations" / upload_id
    ).exists()


def test_orphaned_upload_delete_converges_after_restart_and_allows_retry(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    session_id = "a" * 32
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=5,
        min_free_bytes=0,
    )
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    generation.parent.mkdir(parents=True)
    generation.write_text("2" * 32, encoding="ascii")
    coordinator.inflight[(session_id, upload_id)] = False

    with _client(tmp_path, resource_coordinator=coordinator) as interrupted:
        interrupted.cookies.set("dt_session", session_id)
        pending = interrupted.delete(f"/api/uploads/{upload_id}")

    cancellation = (
        tmp_path / "sessions" / session_id / ".upload-cancellations" / upload_id
    )
    assert pending.status_code == 409
    coordinator.inflight.pop((session_id, upload_id))
    for marker in (generation, cancellation):
        os.utime(marker, (1.0, 1.0))

    restarted_storage = HostedStorage(tmp_path, max_upload_bytes=5)
    with _client(
        tmp_path,
        max_session_bytes=5,
        resource_coordinator=coordinator,
        hosted_storage=restarted_storage,
    ) as restarted:
        restarted.cookies.set("dt_session", session_id)
        deleted = restarted.delete(f"/api/uploads/{upload_id}")
        uploaded = _upload(
            restarted,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )

    assert deleted.status_code == 404
    assert uploaded.status_code == 201
    assert not cancellation.exists()
    assert restarted_storage.resolve(
        session_id,
        uploaded.json()["reference"],
    ).read_bytes() == b"image"


def test_delete_reclaims_upload_crashed_after_quota_commit_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    upload_id = "1" * 32
    session_id = "a" * 32
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=5,
        min_free_bytes=0,
    )
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    original_replace = Path.replace

    def crash_before_publish(source: Path, target: Path) -> Path:
        if source.name == ".uploading":
            raise SimulatedProcessCrash
        return original_replace(source, target)

    async def upload_body() -> AsyncIterator[bytes]:
        yield b"image"

    registration = coordinator.register_upload(session_id, upload_id)
    storage.claim_upload_generation(
        session_id,
        upload_id,
        token=registration.token,
    )
    reservation = coordinator.reserve_upload(
        session_id,
        5,
        free_bytes=10**12,
        asset_id=upload_id,
        generation_id=registration.token,
    )
    lease = coordinator.acquire_session(session_id)
    with monkeypatch.context() as crash:
        crash.setattr(Path, "replace", crash_before_publish)
        with pytest.raises(SimulatedProcessCrash):
            asyncio.run(
                storage.store_stream(
                    session_id,
                    asset_id=upload_id,
                    filename="screen.png",
                    content_length=5,
                    chunks=upload_body(),
                    reservation=reservation,
                    lease=lease,
                    registration=registration,
                )
            )
    registration.finish()
    lease.release()
    generation = (
        tmp_path
        / "sessions"
        / session_id
        / ".upload-generations"
        / upload_id
    )
    os.utime(generation, (1.0, 1.0))

    with _client(
        tmp_path,
        max_session_bytes=5,
        resource_coordinator=coordinator,
        hosted_storage=storage,
    ) as client:
        client.cookies.set("dt_session", session_id)
        deleted = client.delete(f"/api/uploads/{upload_id}")
        assert not generation.exists()
        assert not (
            tmp_path
            / "sessions"
            / session_id
            / ".upload-cancellations"
            / upload_id
        ).exists()
        replacement = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )

    assert deleted.status_code == 204
    assert replacement.status_code == 201
    assert coordinator.committed[session_id] == 5
    assert coordinator.release_calls == 1
    assert list(tmp_path.rglob(".uploading")) == []
    assert storage.resolve(
        session_id,
        replacement.json()["reference"],
    ).read_bytes() == b"image"


def test_delete_reclaims_precommit_partial_without_releasing_published_quota(
    tmp_path: Path,
) -> None:
    stale_upload_id = "1" * 32
    published_upload_id = "2" * 32
    session_id = "a" * 32
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=9,
        min_free_bytes=0,
    )
    storage = HostedStorage(tmp_path, max_upload_bytes=5)

    with _client(
        tmp_path,
        max_session_bytes=9,
        resource_coordinator=coordinator,
        hosted_storage=storage,
    ) as client:
        client.cookies.set("dt_session", session_id)
        published = _upload(
            client,
            "published.png",
            b"live",
            upload_id=published_upload_id,
        )
        stale_dir = (
            tmp_path / "sessions" / session_id / "assets" / stale_upload_id
        )
        stale_dir.mkdir()
        (stale_dir / ".uploading").write_bytes(b"image")
        generation = (
            tmp_path
            / "sessions"
            / session_id
            / ".upload-generations"
            / stale_upload_id
        )
        generation.parent.mkdir(exist_ok=True)
        generation.write_text("3" * 32, encoding="ascii")
        (stale_dir / ".asset.json").write_text(
            json.dumps(
                {
                    "asset_id": stale_upload_id,
                    "bytes": 5,
                    "generation_id": "3" * 32,
                }
            ),
            encoding="utf-8",
        )
        os.utime(generation, (1.0, 1.0))

        deleted = client.delete(f"/api/uploads/{stale_upload_id}")
        assert not generation.exists()
        assert not (
            tmp_path
            / "sessions"
            / session_id
            / ".upload-cancellations"
            / stale_upload_id
        ).exists()
        replacement = _upload(
            client,
            "replacement.png",
            b"image",
            upload_id=stale_upload_id,
        )

    assert published.status_code == 201
    assert deleted.status_code == 204
    assert replacement.status_code == 201
    assert coordinator.committed[session_id] == 9
    assert storage.resolve(
        session_id,
        published.json()["reference"],
    ).read_bytes() == b"live"


def test_delete_reclaims_multiple_stale_partials_and_reuses_their_ids(
    tmp_path: Path,
) -> None:
    session_id = "a" * 32
    uploads = {
        "1" * 32: b"four",
        "2" * 32: b"other",
    }
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=9,
        min_free_bytes=0,
    )
    coordinator.committed[session_id] = 4
    for index, (upload_id, content) in enumerate(uploads.items()):
        asset_dir = tmp_path / "sessions" / session_id / "assets" / upload_id
        asset_dir.mkdir(parents=True)
        (asset_dir / ".uploading").write_bytes(content)
        generation = (
            tmp_path
            / "sessions"
            / session_id
            / ".upload-generations"
            / upload_id
        )
        generation.parent.mkdir(exist_ok=True)
        generation_id = f"{index + 3:032x}"
        generation.write_text(generation_id, encoding="ascii")
        (asset_dir / ".asset.json").write_text(
            json.dumps(
                {
                    "asset_id": upload_id,
                    "bytes": len(content),
                    "generation_id": generation_id,
                }
            ),
            encoding="utf-8",
        )
        if index == 0:
            coordinator.committed_generations[
                (session_id, upload_id, generation_id)
            ] = len(content)
        os.utime(generation, (1.0, 1.0))

    with _client(
        tmp_path,
        max_session_bytes=9,
        resource_coordinator=coordinator,
    ) as client:
        client.cookies.set("dt_session", session_id)
        deleted = [
            client.delete(f"/api/uploads/{upload_id}") for upload_id in uploads
        ]
        assert coordinator.committed.get(session_id, 0) == 0

        replacements = [
            _upload(
                client,
                f"{index}.png",
                content,
                upload_id=upload_id,
            )
            for index, (upload_id, content) in enumerate(uploads.items())
        ]
        replacement_deletes = [
            _delete_uploaded_asset(client, response.json()["reference"])
            for response in replacements
        ]

    assert [response.status_code for response in deleted] == [204, 204]
    assert [response.status_code for response in replacements] == [201, 201]
    assert [response.status_code for response in replacement_deletes] == [204, 204]
    assert coordinator.committed.get(session_id, 0) == 0
    assert list(tmp_path.rglob(".uploading")) == []


def test_delete_does_not_reclaim_active_partial_upload(tmp_path: Path) -> None:
    upload_id = "1" * 32
    session_id = "a" * 32
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=5,
        min_free_bytes=0,
    )
    coordinator.inflight[(session_id, upload_id)] = False
    coordinator.inflight_generations[(session_id, upload_id)] = "2" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / upload_id
    asset_dir.mkdir(parents=True)
    partial = asset_dir / ".uploading"
    partial.write_bytes(b"image")
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    generation.parent.mkdir(parents=True)
    generation.write_text("2" * 32, encoding="ascii")
    os.utime(generation, (1.0, 1.0))

    with _client(tmp_path, resource_coordinator=coordinator) as client:
        client.cookies.set("dt_session", session_id)
        deleted = client.delete(f"/api/uploads/{upload_id}")

    assert deleted.status_code == 409
    assert partial.read_bytes() == b"image"
    assert coordinator.release_calls == 0


def test_delete_does_not_reclaim_recent_partial_after_inflight_ack_loss(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    session_id = "a" * 32
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=5,
        min_free_bytes=0,
    )
    coordinator.committed[session_id] = 5
    asset_dir = tmp_path / "sessions" / session_id / "assets" / upload_id
    asset_dir.mkdir(parents=True)
    partial = asset_dir / ".uploading"
    partial.write_bytes(b"image")
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    generation.parent.mkdir(parents=True)
    generation.write_text("2" * 32, encoding="ascii")

    with _client(tmp_path, resource_coordinator=coordinator) as client:
        client.cookies.set("dt_session", session_id)
        deleted = client.delete(f"/api/uploads/{upload_id}")

    assert deleted.status_code == 409
    assert partial.read_bytes() == b"image"
    assert coordinator.committed[session_id] == 5
    assert coordinator.release_calls == 0


def test_stale_generation_does_not_make_published_asset_an_orphan(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    storage = HostedStorage(tmp_path, max_upload_bytes=5)

    with _client(
        tmp_path,
        max_session_bytes=10,
        hosted_storage=storage,
    ) as client:
        uploaded = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )
        session_id = client.cookies["dt_session"]
        generation = (
            tmp_path
            / "sessions"
            / session_id
            / ".upload-generations"
            / upload_id
        )
        os.utime(generation, (1.0, 1.0))

        duplicate = _upload(
            client,
            "replacement.png",
            b"other",
            upload_id=upload_id,
        )

    assert uploaded.status_code == 201
    assert duplicate.status_code == 409
    assert storage.resolve(
        session_id,
        uploaded.json()["reference"],
    ).read_bytes() == b"image"


def test_new_upload_recovers_stale_generation_and_cancellation(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    session_id = "a" * 32
    session_dir = tmp_path / "sessions" / session_id
    generation = session_dir / ".upload-generations" / upload_id
    cancellation = session_dir / ".upload-cancellations" / upload_id
    generation.parent.mkdir(parents=True)
    cancellation.parent.mkdir(parents=True)
    generation.write_text("2" * 32, encoding="ascii")
    cancellation.touch()
    for marker in (generation, cancellation):
        os.utime(marker, (1.0, 1.0))

    with _client(tmp_path, max_session_bytes=5) as client:
        client.cookies.set("dt_session", session_id)
        uploaded = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )

    assert uploaded.status_code == 201
    assert not cancellation.exists()
    assert generation.read_text(encoding="ascii") != "2" * 32


def test_active_upload_state_is_not_recovered_even_when_marker_is_old(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    session_id = "a" * 32
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=5,
        min_free_bytes=0,
    )
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    generation.parent.mkdir(parents=True)
    generation.write_text("2" * 32, encoding="ascii")
    os.utime(generation, (1.0, 1.0))
    coordinator.inflight[(session_id, upload_id)] = False
    coordinator.inflight_generations[(session_id, upload_id)] = "2" * 32

    with _client(tmp_path, resource_coordinator=coordinator) as client:
        client.cookies.set("dt_session", session_id)
        deleted = client.delete(f"/api/uploads/{upload_id}")
        duplicate = _upload(
            client,
            "screen.png",
            b"image",
            upload_id=upload_id,
        )

    assert deleted.status_code == 409
    assert duplicate.status_code == 409
    assert generation.is_file()


def test_recent_generation_is_not_recovered_when_inflight_is_not_visible(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    session_id = "a" * 32
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    generation.parent.mkdir(parents=True)
    generation.write_text("2" * 32, encoding="ascii")

    with _client(tmp_path) as client:
        client.cookies.set("dt_session", session_id)
        deleted = client.delete(f"/api/uploads/{upload_id}")
        cancellation = (
            tmp_path
            / "sessions"
            / session_id
            / ".upload-cancellations"
            / upload_id
        )
        os.utime(cancellation, (1.0, 1.0))
        retried = client.delete(f"/api/uploads/{upload_id}")

    assert deleted.status_code == 409
    assert retried.status_code == 409
    assert generation.is_file()
    assert cancellation.stat().st_mtime == 1.0


def test_orphaned_upload_recovery_is_owner_scoped(tmp_path: Path) -> None:
    upload_id = "1" * 32
    owner_session = "a" * 32
    stranger_session = "b" * 32
    generation = (
        tmp_path
        / "sessions"
        / owner_session
        / ".upload-generations"
        / upload_id
    )
    generation.parent.mkdir(parents=True)
    generation.write_text("2" * 32, encoding="ascii")
    os.utime(generation, (1.0, 1.0))

    with _client(tmp_path) as stranger:
        stranger.cookies.set("dt_session", stranger_session)
        stranger_delete = stranger.delete(f"/api/uploads/{upload_id}")
    assert generation.exists()

    with _client(tmp_path) as owner:
        owner.cookies.set("dt_session", owner_session)
        owner_delete = owner.delete(f"/api/uploads/{upload_id}")

    assert stranger_delete.status_code == 404
    assert owner_delete.status_code == 404
    assert not generation.exists()


def test_random_delete_does_not_scan_other_session_namespaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for index in range(10_000):
        (sessions_dir / f"{index:032x}").mkdir()
    original_iterdir = Path.iterdir

    def reject_global_session_scan(path: Path):
        if path == sessions_dir:
            raise AssertionError("DELETE scanned all session namespaces")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", reject_global_session_scan)
    with _client(tmp_path) as client:
        client.get("/api/config")
        session_id = client.cookies["dt_session"]
        response = client.delete(f"/api/uploads/{'f' * 32}")

    assert response.status_code == 404
    assert not (sessions_dir / session_id).exists()


def test_delete_redis_queries_use_session_operation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=2 * 1024 * 1024 * 1024,
        min_free_bytes=0,
    )
    inside_commit = False
    original_commit = routes_module._HostedSessionOperation.commit
    original_query = coordinator.is_committed_asset_released

    def track_commit(operation, action):  # noqa: ANN001, ANN202
        def tracked_action():
            nonlocal inside_commit
            inside_commit = True
            try:
                return action()
            finally:
                inside_commit = False

        return original_commit(operation, tracked_action)

    def guarded_query(session_id: str, asset_id: str) -> bool:
        assert inside_commit
        return original_query(session_id, asset_id)

    monkeypatch.setattr(
        routes_module._HostedSessionOperation,
        "commit",
        track_commit,
    )
    monkeypatch.setattr(
        coordinator,
        "is_committed_asset_released",
        guarded_query,
    )

    with _client(tmp_path, resource_coordinator=coordinator) as client:
        client.get("/api/config")
        response = client.delete(f"/api/uploads/{'f' * 32}")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "asset_id",
    [
        "1" * 31,
        "1" * 33,
        "A" * 32,
        "g" * 32,
        "11111111-1111-1111-1111-111111111111",
    ],
)
def test_delete_upload_returns_stable_404_for_noncanonical_asset_id(
    tmp_path: Path,
    asset_id: str,
) -> None:
    with _client(tmp_path) as client:
        response = client.delete(f"/api/uploads/{asset_id}")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "asset_not_found",
            "message": "Uploaded asset is not available",
        }
    }


def test_delete_in_progress_upload_returns_409_with_cancellation_marker(
    tmp_path: Path,
) -> None:
    upload_id = "1" * 32
    with _client(tmp_path) as client:
        client.get("/api/config")
        session_id = client.cookies["dt_session"]
        asset_dir = tmp_path / "sessions" / session_id / "assets" / upload_id
        asset_dir.mkdir(parents=True)
        (asset_dir / ".uploading").write_bytes(b"partial")

        deleted = client.delete(f"/api/uploads/{upload_id}")

    assert deleted.status_code == 409
    assert (asset_dir.parent.parent / ".upload-cancellations" / upload_id).is_file()
    assert (asset_dir / ".uploading").read_bytes() == b"partial"


def test_delete_inflight_upload_prevents_publish_and_releases_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    started = Event()
    resume = Event()
    original_store = storage.store_stream

    async def paused_store(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        started.set()
        while not resume.is_set():
            await asyncio.sleep(0.005)
        return await original_store(*args, **kwargs)

    monkeypatch.setattr(storage, "store_stream", paused_store)
    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as client:
        client.get("/api/config")
        coordinator = client.app.state.resource_coordinator
        with ThreadPoolExecutor(max_workers=1) as executor:
            upload_future = executor.submit(
                _upload,
                client,
                "screen.png",
                b"image",
                upload_id="1" * 32,
            )
            assert started.wait(timeout=2)
            deleted = client.delete(f"/api/uploads/{'1' * 32}")
            resume.set()
            uploaded = upload_future.result(timeout=2)

    assert deleted.status_code == 409
    assert uploaded.status_code == 409
    assert coordinator.active_uploads == 0
    assert coordinator.committed == {}
    assert coordinator.inflight == {}
    assert list(tmp_path.rglob("screen.png")) == []


def test_delete_upload_is_owner_scoped_idempotent_and_reclaims_quota(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, max_session_bytes=5) as owner:
        coordinator = owner.app.state.resource_coordinator
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        first_delete = _delete_uploaded_asset(owner, reference)
        second_delete = _delete_uploaded_asset(owner, reference)
        replacement = _upload(owner, "replacement.png", b"12345")

    assert first_delete.status_code == 204
    assert second_delete.status_code == 404
    assert replacement.status_code == 201
    assert coordinator.finalize_release_calls == 1
    assert coordinator.finalized_assets == {
        (owner.cookies["dt_session"], asset_id)
    }


def test_delete_upload_heartbeat_failure_before_start_preserves_asset_and_usage(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        asset_path = storage.resolve(session_id, reference)
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        coordinator = owner.app.state.resource_coordinator
        coordinator.fail_lease_refresh = True

        response = _delete_uploaded_asset(owner, reference)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert asset_path.read_bytes() == b"image"
    assert coordinator.committed[session_id] == 5
    assert coordinator.release_calls == 0
    assert coordinator.released_assets == {}
    assert not tombstone.exists()


def test_delete_upload_heartbeat_failure_after_quiesce_blocks_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        session_id = owner.cookies["dt_session"]
        asset_path = storage.resolve(session_id, reference)
        coordinator = owner.app.state.resource_coordinator
        original_stop = routes_module._SessionLeaseHeartbeat.stop

        def stop_then_fail(heartbeat) -> None:
            original_stop(heartbeat)
            heartbeat._failure = ResourceUnavailableError(
                "Session lease heartbeat failed before rename"
            )

        monkeypatch.setattr(
            routes_module._SessionLeaseHeartbeat,
            "stop",
            stop_then_fail,
        )

        response = _delete_uploaded_asset(owner, reference)

    assert response.status_code == 503
    assert asset_path.read_bytes() == b"image"
    assert coordinator.committed[session_id] == 5
    assert coordinator.release_calls == 0


def test_delete_upload_has_no_periodic_refresh_during_slow_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    original_rmtree = shutil.rmtree
    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        coordinator = owner.app.state.resource_coordinator
        coordinator.session_lease_heartbeat_interval = 0.001
        coordinator.lease_acquisitions = 0
        coordinator.lease_refresh_calls = 0
        coordinator.lease_release_calls = 0
        refreshes_during_removal: list[tuple[int, int]] = []

        def slow_rmtree(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            if Path(path) == tombstone:
                refresh_calls = coordinator.lease_refresh_calls
                sleep(0.04)
                refreshes_during_removal.append(
                    (refresh_calls, coordinator.lease_refresh_calls)
                )
            original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "designtoolkit.web.hosted.shutil.rmtree",
            slow_rmtree,
        )

        response = _delete_uploaded_asset(owner, reference)
    assert response.status_code == 204
    assert len(refreshes_during_removal) == 1
    assert refreshes_during_removal[0][0] >= 3
    assert refreshes_during_removal[0][0] == refreshes_during_removal[0][1]
    assert coordinator.lease_acquisitions == 1
    assert coordinator.lease_release_calls == 1
    assert coordinator.active_leases == 0
    assert not tombstone.exists()
    assert coordinator.committed.get(session_id, 0) == 0
    assert coordinator.finalized_assets == {(session_id, asset_id)}


def test_delete_absent_upload_does_not_touch_known_release_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        _scheme, asset_id, generation_id, _filename = reference.split(":", 3)
        session_id = owner.cookies["dt_session"]
        coordinator = owner.app.state.resource_coordinator
        deletion = storage.delete_asset(
            session_id,
            f"asset:{asset_id}",
            expected_generation=generation_id,
        )
        coordinator.release_committed_asset(
            session_id,
            deletion.asset_id,
            generation_id,
        )
        storage.finalize_asset_deletion(
            session_id,
            asset_id,
            generation_id,
        )
        finalize_attempts = 0

        def fail_finalize(
            _session_id: str,
            _asset_id: str,
            _generation_id: str,
        ) -> None:
            nonlocal finalize_attempts
            finalize_attempts += 1
            raise RedisConnectionError("finalize unavailable")

        monkeypatch.setattr(
            coordinator,
            "finalize_asset_release",
            fail_finalize,
        )

        response = _delete_uploaded_asset(owner, reference)

    assert response.status_code == 404
    assert finalize_attempts == 0
    assert asset_id in coordinator.released_assets


def test_delete_upload_retries_quota_release_from_durable_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path, max_session_bytes=5) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        coordinator = owner.app.state.resource_coordinator
        original_release = coordinator.release_committed_asset
        attempts = 0

        def fail_once(
            release_session_id: str,
            release_asset_id: str,
            generation_or_released_bytes: str | int,
        ) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RedisConnectionError("release unavailable")
            original_release(
                release_session_id,
                release_asset_id,
                generation_or_released_bytes,
            )

        monkeypatch.setattr(coordinator, "release_committed_asset", fail_once)

        first_delete = _delete_uploaded_asset(owner, reference)
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        assert first_delete.status_code == 503
        assert tombstone.is_dir()
        blocked_replacement = _upload(owner, "blocked.png", b"12345")
        retry = _delete_uploaded_asset(owner, reference)
        replacement = _upload(owner, "replacement.png", b"12345")

    assert blocked_replacement.status_code == 413
    assert retry.status_code == 204
    assert replacement.status_code == 201
    assert attempts == 2
    assert not tombstone.exists()


def test_delete_upload_retries_tombstone_removal_without_double_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removal_attempts = 0
    with _client(tmp_path, max_session_bytes=5) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        coordinator = owner.app.state.resource_coordinator
        original_rmtree = shutil.rmtree

        def fail_removal_once(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal removal_attempts
            if Path(path) == tombstone:
                removal_attempts += 1
                if removal_attempts == 1:
                    raise OSError("removal unavailable")
            original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "designtoolkit.web.hosted.shutil.rmtree",
            fail_removal_once,
        )

        first_delete = _delete_uploaded_asset(owner, reference)
        assert first_delete.status_code == 503
        assert tombstone.is_dir()
        assert coordinator.finalize_release_calls == 0
        assert asset_id in coordinator.released_assets
        replacement = _upload(owner, "replacement.png", b"12345")
        retry = _delete_uploaded_asset(owner, reference)
        overflow = _upload(owner, "overflow.png", b"x")

    assert replacement.status_code == 201
    assert retry.status_code == 204
    assert overflow.status_code == 413
    assert removal_attempts == 2
    assert not tombstone.exists()
    assert coordinator.finalize_release_calls == 1
    assert (session_id, asset_id) in coordinator.finalized_assets


def test_delete_upload_recovers_when_rmtree_removed_metadata_before_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removal_attempts = 0
    with _client(tmp_path, max_session_bytes=5) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        metadata = tombstone / ".deletion.json"
        coordinator = owner.app.state.resource_coordinator
        original_rmtree = shutil.rmtree

        def delete_metadata_then_fail_once(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal removal_attempts
            if Path(path) == tombstone:
                removal_attempts += 1
                if removal_attempts == 1:
                    metadata.unlink()
                    raise OSError("partial removal")
            original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "designtoolkit.web.hosted.shutil.rmtree",
            delete_metadata_then_fail_once,
        )

        first_delete = _delete_uploaded_asset(owner, reference)
        assert first_delete.status_code == 503
        assert tombstone.is_dir()
        assert not metadata.exists()

        retry = _delete_uploaded_asset(owner, reference)
        replacement = _upload(owner, "replacement.png", b"12345")

    assert retry.status_code == 204
    assert replacement.status_code == 201
    assert coordinator.release_calls == 1
    assert coordinator.release_query_calls == 1
    assert coordinator.committed[session_id] == 5
    assert not tombstone.exists()


def test_delete_upload_retains_invalid_tombstone_without_release_marker(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    with _client(
        tmp_path,
        max_session_bytes=5,
        hosted_storage=storage,
    ) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        generation_id = reference.split(":", 3)[2]
        storage.delete_asset(
            session_id,
            reference,
            expected_generation=generation_id,
        )
        (tombstone / ".deletion.json").unlink()

        rejected = _delete_uploaded_asset(owner, reference)

    assert rejected.status_code == 503
    assert tombstone.is_dir()
    assert owner.app.state.resource_coordinator.committed[session_id] == 5


def test_concurrent_retries_recover_partial_tombstone_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path, max_session_bytes=5) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        metadata = tombstone / ".deletion.json"
        original_rmtree = shutil.rmtree
        failed = False

        def fail_after_metadata_once(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal failed
            if Path(path) == tombstone and not failed:
                failed = True
                metadata.unlink()
                raise OSError("partial removal")
            original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "designtoolkit.web.hosted.shutil.rmtree",
            fail_after_metadata_once,
        )
        assert _delete_uploaded_asset(owner, reference).status_code == 503

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(
                executor.map(
                    lambda _index: _delete_uploaded_asset(
                        owner,
                        reference,
                    ).status_code,
                    range(2),
                )
            )

    assert 204 in statuses
    assert set(statuses) <= {204, 404}
    assert owner.app.state.resource_coordinator.release_calls == 1
    assert not tombstone.exists()


def test_delete_upload_cannot_remove_another_sessions_asset(tmp_path: Path) -> None:
    with _client(tmp_path) as owner:
        uploaded = _upload(owner, "screen.png", _png_bytes())
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]

        with _client(tmp_path) as stranger:
            rejected = stranger.delete(f"/api/uploads/{asset_id}")

        inspection = owner.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": reference},
        )

    assert rejected.status_code == 404
    assert inspection.status_code == 200


def test_deleted_asset_token_remains_isolated_from_other_sessions(
    tmp_path: Path,
) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=2 * 1024 * 1024 * 1024,
        min_free_bytes=0,
    )
    with _client(tmp_path, resource_coordinator=coordinator) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        deleted = _delete_uploaded_asset(owner, reference)

        with _client(tmp_path, resource_coordinator=coordinator) as stranger:
            rejected = stranger.delete(f"/api/uploads/{asset_id}")

    assert deleted.status_code == 204
    assert rejected.status_code == 404


def test_hosted_upload_reference_can_drive_image_inspection(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        reference = _upload(client, "screen.png", _png_bytes()).json()["reference"]
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": reference},
        )
        own_preview = client.get(response.json()["preview_url"])

    with _client(tmp_path) as stranger:
        stranger_preview = stranger.get(response.json()["preview_url"])

    assert response.status_code == 200
    assert response.json()["input_path"] == reference
    assert response.json()["width"] == 2
    assert response.json()["height"] == 1
    assert own_preview.status_code == 200
    assert stranger_preview.status_code == 404


def test_hosted_concurrent_same_key_previews_are_owner_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_generation_started = Event()
    release_generation = Event()
    second_lookup_entered = Event()
    lookup_count = 0
    lookup_count_lock = Lock()
    generation_calls = 0
    generation_calls_lock = Lock()

    class CoordinatedPreviewStore(ChromaPreviewStore):
        def __init__(self) -> None:
            super().__init__(max_entries=2)

        def get_or_put_entry(
            self,
            cache_key: str,
            create_png_bytes,
            *,
            owner_id: str | None = None,
        ):
            nonlocal lookup_count
            with lookup_count_lock:
                lookup_count += 1
                if lookup_count == 2:
                    second_lookup_entered.set()
            if owner_id is None:
                return super().get_or_put_entry(cache_key, create_png_bytes)
            return super().get_or_put_entry(
                cache_key,
                create_png_bytes,
                owner_id=owner_id,
            )

    original_make_preview_png = routes_module.make_preview_png

    def make_blocking_preview(*args, **kwargs) -> bytes:
        nonlocal generation_calls
        with generation_calls_lock:
            generation_calls += 1
        first_generation_started.set()
        assert release_generation.wait(timeout=5)
        return original_make_preview_png(*args, **kwargs)

    shared_rgb = np.zeros((1, 2, 3), dtype=np.uint8)
    shared_fingerprint = {
        "path": "shared-image",
        "mtime_ns": 1,
        "size": 1,
        "content_sha256": "shared",
    }
    monkeypatch.setattr(routes_module, "ChromaPreviewStore", CoordinatedPreviewStore)
    monkeypatch.setattr(routes_module, "make_preview_png", make_blocking_preview)
    monkeypatch.setattr(
        routes_module,
        "_read_rgb_snapshot",
        lambda _path: (shared_rgb, shared_fingerprint),
    )

    with _client(tmp_path) as owner_a:
        owner_b = TestClient(owner_a.app)
        owner_a.cookies.set("dt_session", "a" * 32)
        owner_b.cookies.set("dt_session", "b" * 32)
        try:
            reference_a = _upload(
                owner_a,
                "owner-a.png",
                _png_bytes(),
            ).json()["reference"]
            reference_b = _upload(
                owner_b,
                "owner-b.png",
                _png_bytes(),
            ).json()["reference"]

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    owner_a.post,
                    "/api/chroma-keyer/images/inspect",
                    json={"input_path": reference_a},
                )
                assert first_generation_started.wait(timeout=5)
                second = executor.submit(
                    owner_b.post,
                    "/api/chroma-keyer/images/inspect",
                    json={"input_path": reference_b},
                )
                assert second_lookup_entered.wait(timeout=5)
                release_generation.set()
                response_a = first.result(timeout=5)
                response_b = second.result(timeout=5)

            preview_url_a = response_a.json()["preview_url"]
            preview_url_b = response_b.json()["preview_url"]
            own_a = owner_a.get(preview_url_a)
            own_b = owner_b.get(preview_url_b)
            cross_a = owner_a.get(preview_url_b)
            cross_b = owner_b.get(preview_url_a)
            unknown = owner_a.get(
                f"/api/chroma-keyer/previews/{uuid4()}"
            )
        finally:
            owner_b.close()

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert preview_url_a != preview_url_b
    assert generation_calls == 2
    assert own_a.status_code == 200
    assert own_b.status_code == 200
    assert cross_a.status_code == 404
    assert cross_b.status_code == 404
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "preview_not_found"


def test_hosted_image_inspection_holds_one_session_lease_during_resolution(
    tmp_path: Path,
) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )

    class LeaseCheckingStorage(HostedStorage):
        def resolve(self, session_id: str, reference: str) -> Path:
            assert coordinator.active_leases == 1
            return super().resolve(session_id, reference)

    storage = LeaseCheckingStorage(tmp_path, max_upload_bytes=1024)
    with _client(
        tmp_path,
        resource_coordinator=coordinator,
        hosted_storage=storage,
    ) as client:
        reference = _upload(client, "screen.png", _png_bytes()).json()["reference"]
        coordinator.lease_acquisitions = 0
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": reference},
        )

    assert response.status_code == 200
    assert coordinator.lease_acquisitions == 1
    assert coordinator.active_leases == 0


def test_hosted_preview_error_releases_session_lease(tmp_path: Path) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    with _client(tmp_path, resource_coordinator=coordinator) as client:
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": f"asset:{'f' * 32}"},
        )

    assert response.status_code == 404
    assert coordinator.lease_acquisitions == 1
    assert coordinator.active_leases == 0


def test_hosted_mode_rejects_local_picker_and_oversized_upload(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, max_bytes=4) as client:
        picker = client.post(
            "/api/local-paths/pick-files",
            json={"file_kind": "image", "multiple": False},
        )
        upload = _upload(client, "screen.png", b"12345")

    assert picker.status_code == 404
    assert upload.status_code == 413
    assert upload.json()["error"]["code"] == "upload_too_large"


def test_hosted_upload_concurrency_limit_returns_429(tmp_path: Path) -> None:
    with _client(tmp_path, max_upload_concurrency=1) as client:
        coordinator = client.app.state.resource_coordinator
        held = coordinator.reserve_upload("a" * 32, 1, free_bytes=10**12)
        try:
            response = _upload(client, "screen.png", b"image")
        finally:
            held.release()

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upload_concurrency"


def test_hosted_upload_disk_reservation_failure_returns_503(tmp_path: Path) -> None:
    with _client(tmp_path, min_free_bytes=10**18) as client:
        response = _upload(client, "screen.png", b"image")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"


def test_hosted_upload_endpoint_is_rate_limited(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(
        tmp_path,
        task_manager=manager,
        upload_requests_per_hour=1,
    ) as client:
        first = _upload(client, "first.png", _png_bytes())
        second = _upload(client, "second.png", _png_bytes())

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


def test_real_redis_hosted_get_endpoint_is_rate_limited(tmp_path: Path) -> None:
    url = os.environ.get(
        "DESIGNTOOLKIT_TEST_REDIS_URL",
        "redis://localhost:6379/15",
    )
    redis_connection = Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )
    try:
        redis_connection.ping()
    except RedisError as error:
        redis_connection.close()
        pytest.skip(f"real Redis unavailable at {url}: {error}")
    rate_keys = list(redis_connection.scan_iter(match="dt:rate:*"))
    if rate_keys:
        redis_connection.delete(*rate_keys)
    try:
        manager = RedisTaskManager(
            redis_connection=redis_connection,
            queue=Queue("designtoolkit", connection=redis_connection),
            data_dir=tmp_path,
            max_queued_tasks=20,
            retention_seconds=7200,
        )
        with _client(
            tmp_path,
            task_manager=manager,
            api_requests_per_minute=1,
        ) as client:
            first = client.get("/api/config")
            second = client.get("/api/config")

        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"
    finally:
        rate_keys = list(redis_connection.scan_iter(match="dt:rate:*"))
        if rate_keys:
            redis_connection.delete(*rate_keys)
        redis_connection.close()


def test_real_redis_random_delete_does_not_grow_upload_state(
    tmp_path: Path,
) -> None:
    url = os.environ.get(
        "DESIGNTOOLKIT_TEST_REDIS_URL",
        "redis://localhost:6379/15",
    )
    redis_connection = Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )
    try:
        redis_connection.ping()
    except RedisError as error:
        redis_connection.close()
        pytest.skip(f"real Redis unavailable at {url}: {error}")
    resource_keys = list(redis_connection.scan_iter(match="dt:resource:*"))
    if resource_keys:
        redis_connection.delete(*resource_keys)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    resources = RedisResourceCoordinator(
        redis_connection=redis_connection,
        max_upload_concurrency=4,
        max_session_bytes=2 * 1024 * 1024 * 1024,
        min_free_bytes=0,
        reservation_ttl=30,
        lease_ttl=30,
    )
    try:
        with _client(
            tmp_path,
            task_manager=manager,
            resource_coordinator=resources,
        ) as client:
            client.get("/api/config")
            for index in range(100):
                response = client.delete(f"/api/uploads/{index:032x}")
                assert response.status_code == 404

        assert list(
            redis_connection.scan_iter(
                match="dt:resource:upload-inflight:*"
            )
        ) == []
        assert list(
            redis_connection.scan_iter(match="dt:resource:session:*")
        ) == []
        assert list(tmp_path.rglob(".upload-cancellations")) == []
    finally:
        resource_keys = list(redis_connection.scan_iter(match="dt:resource:*"))
        if resource_keys:
            redis_connection.delete(*resource_keys)
        redis_connection.close()


def _real_redis_upload_resources(
    tmp_path: Path,
) -> tuple[Redis, RedisResourceCoordinator]:
    url = os.environ.get(
        "DESIGNTOOLKIT_TEST_REDIS_URL",
        "redis://localhost:6379/15",
    )
    redis_connection = Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )
    try:
        redis_connection.ping()
    except RedisError as error:
        redis_connection.close()
        pytest.skip(f"real Redis unavailable at {url}: {error}")
    resource_keys = list(redis_connection.scan_iter(match="dt:resource:*"))
    if resource_keys:
        redis_connection.delete(*resource_keys)
    return redis_connection, RedisResourceCoordinator(
        redis_connection=redis_connection,
        max_upload_concurrency=4,
        max_session_bytes=2 * 1024 * 1024 * 1024,
        min_free_bytes=0,
        reservation_ttl=30,
        lease_ttl=30,
    )


def _close_real_redis_upload_resources(redis_connection: Redis) -> None:
    resource_keys = list(redis_connection.scan_iter(match="dt:resource:*"))
    if resource_keys:
        redis_connection.delete(*resource_keys)
    redis_connection.close()


class InspectBarrierStorage(HostedStorage):
    def __init__(self, root: Path, *, max_upload_bytes: int) -> None:
        super().__init__(root, max_upload_bytes=max_upload_bytes)
        self.inspected = Event()
        self.resume = Event()

    def inspect_upload_for_deletion(
        self,
        session_id: str,
        asset_id: str,
    ):
        inspection = super().inspect_upload_for_deletion(session_id, asset_id)
        self.inspected.set()
        assert self.resume.wait(timeout=5)
        return inspection


def test_delete_snapshot_accepts_same_generation_published_at_barrier(
    tmp_path: Path,
) -> None:
    session_id = "a" * 32
    upload_id = "1" * 32
    generation_id = "2" * 32
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=5,
        min_free_bytes=0,
    )
    coordinator.inflight[(session_id, upload_id)] = False
    coordinator.inflight_generations[(session_id, upload_id)] = generation_id
    coordinator.committed[session_id] = 4
    coordinator.committed_generations[
        (session_id, upload_id, generation_id)
    ] = 4
    storage = InspectBarrierStorage(tmp_path, max_upload_bytes=5)
    asset_dir = tmp_path / "sessions" / session_id / "assets" / upload_id
    asset_dir.mkdir(parents=True)
    uploading = asset_dir / ".uploading"
    uploading.write_bytes(b"data")
    (asset_dir / ".asset.json").write_text(
        json.dumps(
            {
                "asset_id": upload_id,
                "bytes": 4,
                "generation_id": generation_id,
            }
        ),
        encoding="utf-8",
    )
    generation = (
        tmp_path / "sessions" / session_id / ".upload-generations" / upload_id
    )
    generation.parent.mkdir()
    generation.write_text(generation_id, encoding="ascii")

    with _client(
        tmp_path,
        resource_coordinator=coordinator,
        hosted_storage=storage,
    ) as client:
        client.cookies.set("dt_session", session_id)
        with ThreadPoolExecutor(max_workers=1) as executor:
            delete_future = executor.submit(
                client.delete,
                f"/api/uploads/{upload_id}?generation={generation_id}",
            )
            assert storage.inspected.wait(timeout=2)
            uploading.replace(asset_dir / "screen.png")
            coordinator.inflight.pop((session_id, upload_id))
            coordinator.inflight_generations.pop((session_id, upload_id))
            storage.resume.set()
            deleted = delete_future.result(timeout=2)

    assert deleted.status_code == 204
    assert not asset_dir.exists()
    assert coordinator.committed.get(session_id, 0) == 0


def test_real_redis_empty_delete_snapshot_does_not_cancel_new_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection, resources = _real_redis_upload_resources(tmp_path)
    storage = InspectBarrierStorage(tmp_path, max_upload_bytes=5)
    session_id = "a" * 32
    upload_id = "1" * 32
    cancel_upload = Mock(wraps=resources.cancel_upload)
    monkeypatch.setattr(resources, "cancel_upload", cancel_upload)
    try:
        with _client(
            tmp_path,
            resource_coordinator=resources,
            hosted_storage=storage,
        ) as client:
            client.cookies.set("dt_session", session_id)
            with ThreadPoolExecutor(max_workers=1) as executor:
                deletion = executor.submit(
                    client.delete,
                    f"/api/uploads/{upload_id}",
                )
                assert storage.inspected.wait(timeout=5)
                registration = resources.register_upload(session_id, upload_id)
                storage.claim_upload_generation(
                    session_id,
                    upload_id,
                    token=registration.token,
                )
                storage.resume.set()
                response = deletion.result(timeout=5)

            assert response.status_code == 404
            assert cancel_upload.call_count == 0
            assert registration.refresh() is False
            assert (
                tmp_path
                / "sessions"
                / session_id
                / ".upload-generations"
                / upload_id
            ).read_text(encoding="ascii") == registration.token
            assert list(
                redis_connection.scan_iter(
                    match=f"dt:resource:upload-inflight:{session_id}:{upload_id}"
                )
            ) == [
                f"dt:resource:upload-inflight:{session_id}:{upload_id}".encode()
            ]
    finally:
        _close_real_redis_upload_resources(redis_connection)


def test_real_redis_no_generation_delete_fences_replaced_upload_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection, resources = _real_redis_upload_resources(tmp_path)
    storage = InspectBarrierStorage(tmp_path, max_upload_bytes=5)
    session_id = "a" * 32
    upload_id = "1" * 32
    old_generation = "2" * 32
    cancel_upload = Mock(wraps=resources.cancel_upload)
    monkeypatch.setattr(resources, "cancel_upload", cancel_upload)
    storage.claim_upload_generation(
        session_id,
        upload_id,
        token=old_generation,
    )
    try:
        with _client(
            tmp_path,
            resource_coordinator=resources,
            hosted_storage=storage,
        ) as client:
            client.cookies.set("dt_session", session_id)
            with ThreadPoolExecutor(max_workers=1) as executor:
                deletion = executor.submit(
                    client.delete,
                    f"/api/uploads/{upload_id}",
                )
                assert storage.inspected.wait(timeout=5)
                registration = resources.register_upload(session_id, upload_id)
                storage.claim_upload_generation(
                    session_id,
                    upload_id,
                    token=registration.token,
                )
                storage.resume.set()
                response = deletion.result(timeout=5)

            assert response.status_code == 404
            assert cancel_upload.call_count == 1
            assert cancel_upload.call_args.args == (
                session_id,
                upload_id,
                old_generation,
            )
            assert registration.refresh() is False
            assert (
                tmp_path
                / "sessions"
                / session_id
                / ".upload-generations"
                / upload_id
            ).read_text(encoding="ascii") == registration.token
            assert not (
                tmp_path
                / "sessions"
                / session_id
                / ".upload-cancellations"
                / upload_id
            ).exists()
    finally:
        _close_real_redis_upload_resources(redis_connection)


def test_real_redis_legacy_snapshot_cannot_delete_new_generation_marker(
    tmp_path: Path,
) -> None:
    redis_connection, resources = _real_redis_upload_resources(tmp_path)
    storage = InspectBarrierStorage(tmp_path, max_upload_bytes=5)
    session_id = "a" * 32
    upload_id = "1" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / upload_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "legacy.png").write_bytes(b"data")
    (asset_dir / ".asset.json").write_text(
        json.dumps({"asset_id": upload_id, "bytes": 4}),
        encoding="utf-8",
    )
    try:
        with _client(
            tmp_path,
            resource_coordinator=resources,
            hosted_storage=storage,
        ) as client:
            client.cookies.set("dt_session", session_id)
            with ThreadPoolExecutor(max_workers=1) as executor:
                deletion = executor.submit(
                    client.delete,
                    f"/api/uploads/{upload_id}",
                )
                assert storage.inspected.wait(timeout=5)
                shutil.rmtree(asset_dir)
                registration = resources.register_upload(session_id, upload_id)
                storage.claim_upload_generation(
                    session_id,
                    upload_id,
                    token=registration.token,
                )
                storage.resume.set()
                response = deletion.result(timeout=5)

            assert response.status_code == 404
            assert registration.refresh() is False
            assert (
                tmp_path
                / "sessions"
                / session_id
                / ".upload-generations"
                / upload_id
            ).read_text(encoding="ascii") == registration.token
            assert not (
                tmp_path
                / "sessions"
                / session_id
                / ".upload-cancellations"
                / upload_id
            ).exists()
    finally:
        _close_real_redis_upload_resources(redis_connection)


def test_real_redis_no_generation_delete_cancels_matching_active_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection, resources = _real_redis_upload_resources(tmp_path)
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    session_id = "a" * 32
    upload_id = "1" * 32
    try:
        registration = resources.register_upload(session_id, upload_id)
        cancel_upload = Mock(wraps=resources.cancel_upload)
        monkeypatch.setattr(resources, "cancel_upload", cancel_upload)
        storage.claim_upload_generation(
            session_id,
            upload_id,
            token=registration.token,
        )
        with _client(
            tmp_path,
            resource_coordinator=resources,
            hosted_storage=storage,
        ) as client:
            client.cookies.set("dt_session", session_id)
            response = client.delete(f"/api/uploads/{upload_id}")

        assert response.status_code == 409
        assert cancel_upload.call_count == 1
        assert cancel_upload.call_args.args == (
            session_id,
            upload_id,
            registration.token,
        )
        assert registration.refresh() is True
        assert (
            tmp_path
            / "sessions"
            / session_id
            / ".upload-cancellations"
            / upload_id
        ).is_file()
    finally:
        _close_real_redis_upload_resources(redis_connection)


def test_real_redis_legacy_delete_does_not_cancel_arbitrary_inflight_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection, resources = _real_redis_upload_resources(tmp_path)
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    session_id = "a" * 32
    upload_id = "1" * 32
    asset_dir = tmp_path / "sessions" / session_id / "assets" / upload_id
    asset_dir.mkdir(parents=True)
    (asset_dir / "legacy.png").write_bytes(b"data")
    (asset_dir / ".asset.json").write_text(
        json.dumps({"asset_id": upload_id, "bytes": 4}),
        encoding="utf-8",
    )
    try:
        reservation = resources.reserve_upload(
            session_id,
            4,
            free_bytes=10**12,
        )
        reservation.commit()
        reservation.finalize()
        registration = resources.register_upload(session_id, upload_id)
        cancel_upload = Mock(wraps=resources.cancel_upload)
        monkeypatch.setattr(resources, "cancel_upload", cancel_upload)
        with _client(
            tmp_path,
            resource_coordinator=resources,
            hosted_storage=storage,
        ) as client:
            client.cookies.set("dt_session", session_id)
            response = client.delete(f"/api/uploads/{upload_id}")

        assert response.status_code == 204
        assert cancel_upload.call_count == 0
        assert registration.refresh() is False
        assert not asset_dir.exists()
    finally:
        _close_real_redis_upload_resources(redis_connection)


def test_real_redis_published_generation_requires_explicit_delete_identity(
    tmp_path: Path,
) -> None:
    redis_connection, resources = _real_redis_upload_resources(tmp_path)
    storage = HostedStorage(tmp_path, max_upload_bytes=5)
    session_id = "a" * 32
    try:
        with _client(
            tmp_path,
            max_session_bytes=5,
            resource_coordinator=resources,
            hosted_storage=storage,
        ) as client:
            client.cookies.set("dt_session", session_id)
            uploaded = _upload(client, "screen.png", b"image")
            reference = uploaded.json()["reference"]
            _scheme, upload_id, generation_id, _filename = reference.split(
                ":",
                3,
            )

            missing = client.delete(f"/api/uploads/{upload_id}")
            contents = storage.resolve(session_id, reference).read_bytes()
            matching = client.delete(
                f"/api/uploads/{upload_id}?generation={generation_id}"
            )

        assert uploaded.status_code == 201
        assert missing.status_code == 404
        assert contents == b"image"
        assert matching.status_code == 204
    finally:
        _close_real_redis_upload_resources(redis_connection)


def test_hosted_upload_also_consumes_api_rate_limit(tmp_path: Path) -> None:
    with _client(tmp_path, api_requests_per_minute=1) as client:
        upload = _upload(client, "screen.png", _png_bytes())
        session_id = client.cookies["dt_session"]
        redis_connection = client.app.state.task_manager.queue.connection
        config = client.get("/api/config")

    assert upload.status_code == 201
    assert config.status_code == 429
    assert config.json()["error"]["code"] == "rate_limited"
    rate_keys = {
        key.decode() for key in redis_connection.scan_iter(match="dt:rate:*")
    }
    expected_prefixes = {
        "dt:rate:ip:testclient:api:",
        f"dt:rate:session:{session_id}:api:",
        "dt:rate:ip:testclient:upload:",
        f"dt:rate:session:{session_id}:upload:",
    }
    assert all(
        any(key.startswith(prefix) for key in rate_keys)
        for prefix in expected_prefixes
    )


@pytest.mark.parametrize(
    "path",
    [
        f"/api/tasks/{'0' * 32}/download",
        f"/api/tasks/{'0' * 32}/events",
    ],
)
def test_hosted_get_endpoints_are_rate_limited_before_resource_acquisition(
    tmp_path: Path,
    path: str,
) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    with _client(
        tmp_path,
        api_requests_per_minute=1,
        resource_coordinator=coordinator,
    ) as client:
        first = client.get(path)
        second = client.get(path)

    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert coordinator.lease_acquisitions == 0


def test_hosted_delete_is_rate_limited_before_second_lease(
    tmp_path: Path,
) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    with _client(
        tmp_path,
        api_requests_per_minute=1,
        resource_coordinator=coordinator,
    ) as client:
        first = client.delete(f"/api/uploads/{'0' * 32}")
        second = client.delete(f"/api/uploads/{'0' * 32}")

    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert coordinator.lease_acquisitions == 1


def test_hosted_static_frontend_and_health_are_not_rate_limited(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, api_requests_per_minute=1) as client:
        responses = [
            client.get("/"),
            client.get("/"),
            client.get("/api/health"),
            client.get("/api/health"),
        ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]


def test_slow_rate_limit_does_not_block_exempt_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path)
    manager = client.app.state.task_manager
    entered = Event()
    release = Event()
    request_context: ContextVar[str | None] = ContextVar(
        "request_context",
        default=None,
    )

    def slow_allow_request(_bucket: str, *, limit: int, window: int) -> bool:
        assert limit > 0
        assert window > 0
        assert request_context.get() == "rate-limited-request"
        entered.set()
        assert release.wait(timeout=2)
        return True

    async def get_before_release(
        async_client: AsyncClient,
        path: str,
    ) -> tuple[int, bool]:
        response = await async_client.get(path)
        return response.status_code, not release.is_set()

    async def exercise() -> tuple[tuple[int, bool], tuple[int, bool]]:
        transport = ASGITransport(app=client.app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as async_client:
            async def get_rate_limited() -> None:
                token = request_context.set("rate-limited-request")
                try:
                    await async_client.get("/api/config")
                finally:
                    request_context.reset(token)

            rate_limited = asyncio.create_task(get_rate_limited())
            health = asyncio.create_task(
                get_before_release(async_client, "/api/health")
            )
            static = asyncio.create_task(get_before_release(async_client, "/"))
            health_result, static_result = await asyncio.gather(health, static)
            await rate_limited
            return health_result, static_result

    monkeypatch.setattr(manager, "allow_request", slow_allow_request)
    timer = Timer(1, release.set)
    timer.start()
    try:
        health_result, static_result = asyncio.run(exercise())
    finally:
        release.set()
        timer.cancel()
        timer.join()
        client.close()

    assert entered.is_set()
    assert health_result == (200, True)
    assert static_result == (200, True)


@pytest.mark.parametrize(
    "error",
    [
        RedisConnectionError("rate limit store unavailable"),
        RedisTimeoutError("rate limit store timed out"),
        BusyLoadingError("Redis is loading the dataset in memory"),
        ReadOnlyError("You can't write against a read only replica"),
        OutOfMemoryError("command not allowed when used memory > maxmemory"),
        ResponseError("LOADING Redis is loading the dataset in memory"),
        ResponseError("READONLY You can't write against a read only replica"),
        ResponseError("OOM command not allowed when used memory > maxmemory"),
        ResponseError(
            "Command # 1 (SET key value) of pipeline caused error: "
            "READONLY You can't write against a read only replica"
        ),
        ResponseError(
            "Command # 1 (SET key value) of pipeline caused error: "
            "OOM command not allowed when used memory > maxmemory"
        ),
    ],
)
def test_rate_limit_redis_unavailability_returns_structured_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    with _client(tmp_path) as client:
        manager = client.app.state.task_manager
        monkeypatch.setattr(manager, "allow_request", Mock(side_effect=error))

        response = client.get("/api/config")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "storage_unavailable",
            "message": "Rate limit storage is temporarily unavailable",
        }
    }
    assert "dt_session=" in response.headers["set-cookie"]


@pytest.mark.parametrize(
    "error",
    [
        ResponseError("WRONGTYPE Operation against a key holding the wrong kind"),
        ResponseError("Error running script: @user_script:1: programming error"),
    ],
)
def test_rate_limit_redis_programming_failure_is_not_mapped_to_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: ResponseError,
) -> None:
    with _client(tmp_path, raise_server_exceptions=False) as client:
        manager = client.app.state.task_manager
        monkeypatch.setattr(manager, "allow_request", Mock(side_effect=error))

        response = client.get("/api/config")

    assert response.status_code == 500


@pytest.mark.parametrize(
    "error",
    [
        ResponseError("WRONGTYPE Operation against a key holding the wrong kind"),
        ResponseError("Error running script: @user_script:1: programming error"),
    ],
)
def test_hosted_task_redis_programming_failure_is_not_mapped_to_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: ResponseError,
) -> None:
    with _client(tmp_path, raise_server_exceptions=False) as client:
        manager = client.app.state.task_manager
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        monkeypatch.setattr(
            manager,
            "acquire_asset_read_pin",
            Mock(side_effect=error),
        )

        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 500


@pytest.mark.parametrize(
    "path",
    [
        f"/api/tasks/{'0' * 32}",
        f"/api/tasks/{'0' * 32}/download",
        f"/api/tasks/{'0' * 32}/events",
    ],
)
def test_hosted_task_redis_read_outage_after_rate_limit_returns_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    with _client(tmp_path, raise_server_exceptions=False) as client:
        manager = client.app.state.task_manager
        monkeypatch.setattr(
            manager,
            "get_task",
            Mock(side_effect=RedisConnectionError("task storage unavailable")),
        )

        response = client.get(path)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert "dt_session=" in response.headers["set-cookie"]


def test_hosted_event_subscription_outage_before_response_returns_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(
        tmp_path,
        task_manager=manager,
        raise_server_exceptions=False,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        task_id = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        ).json()["task_id"]
        monkeypatch.setattr(
            manager.log_store,
            "history_and_subscribe",
            Mock(side_effect=ResponseError("LOADING Redis is loading the dataset")),
        )

        response = client.get(f"/api/tasks/{task_id}/events")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert "dt_session=" in response.headers["set-cookie"]


def test_hosted_event_stream_emits_error_and_closes_on_midstream_redis_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DisconnectedSubscriber:
        calls = 0

        def get(self, timeout: float):
            del timeout
            type(self).calls += 1
            raise RedisConnectionError("event storage disconnected")

    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    unsubscribe = Mock()
    with _client(
        tmp_path,
        task_manager=manager,
        raise_server_exceptions=False,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        task_id = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        ).json()["task_id"]
        monkeypatch.setattr(
            manager.log_store,
            "history_and_subscribe",
            Mock(return_value=([], DisconnectedSubscriber())),
        )
        monkeypatch.setattr(manager.log_store, "unsubscribe", unsubscribe)

        response = client.get(f"/api/tasks/{task_id}/events")

    assert response.status_code == 200
    assert response.text == (
        "event: error\n"
        'data: {"error":{"code":"storage_unavailable",'
        '"message":"Task event storage is temporarily unavailable"}}\n\n'
    )
    assert DisconnectedSubscriber.calls == 1
    unsubscribe.assert_called_once()


def test_hosted_event_stream_reports_corrupt_cursor_event_once_and_unsubscribes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    original_subscribe = manager.log_store.history_and_subscribe
    unsubscribe = Mock()

    def subscribe_then_corrupt_next_event(task_id: str):
        history, subscriber = original_subscribe(task_id)
        event_index_key = f"dt:task:{task_id}:event-index"
        next_sequence = int(
            redis_connection.hget(event_index_key, "next_sequence")
        ) + 1
        redis_connection.hset(
            event_index_key,
            mapping={
                "next_sequence": next_sequence,
                str(next_sequence): b"\xff",
            },
        )
        return history, subscriber

    monkeypatch.setattr(
        manager.log_store,
        "history_and_subscribe",
        subscribe_then_corrupt_next_event,
    )
    monkeypatch.setattr(manager.log_store, "unsubscribe", unsubscribe)
    with _client(
        tmp_path,
        task_manager=manager,
        raise_server_exceptions=False,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        task_id = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        ).json()["task_id"]

        response = client.get(f"/api/tasks/{task_id}/events")

    assert response.status_code == 200
    unavailable_event = (
        "event: error\n"
        'data: {"error":{"code":"storage_unavailable",'
        '"message":"Task event storage is temporarily unavailable"}}\n\n'
    )
    assert response.text.endswith(unavailable_event)
    assert response.text.count(unavailable_event) == 1
    unsubscribe.assert_called_once()


def test_rate_limit_unknown_failure_is_not_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path) as client:
        manager = client.app.state.task_manager
        monkeypatch.setattr(
            manager,
            "allow_request",
            Mock(side_effect=RuntimeError("unexpected rate limit failure")),
        )

        with pytest.raises(RuntimeError, match="unexpected rate limit failure"):
            client.get("/api/config")


def test_hosted_task_uses_uploaded_asset_and_is_session_isolated(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as owner:
        reference = _upload(owner, "clip.mp4", b"video").json()["reference"]
        created = owner.post(
            "/api/tasks/video-frames",
            json={
                "input_path": reference,
                "image_format": "png",
                "overwrite": False,
            },
        )
        task_id = created.json()["task_id"]
        own_status = owner.get(f"/api/tasks/{task_id}")

    with _client(tmp_path, task_manager=manager) as stranger:
        foreign_submission = stranger.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )
        stranger_status = stranger.get(f"/api/tasks/{task_id}")

    assert created.status_code == 202
    assert own_status.status_code == 200
    assert own_status.json()["input_path"] == "clip.mp4"
    assert foreign_submission.status_code == 404
    assert foreign_submission.json()["error"]["code"] == "asset_not_found"
    assert stranger_status.status_code == 404


def test_hosted_task_accepts_previous_generation_manifest_legacy_reference(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    asset_id = "1" * 32
    generation_id = "2" * 32
    with _client(tmp_path, task_manager=manager) as client:
        assert client.get("/api/config").status_code == 200
        session_id = client.cookies["dt_session"]
        asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
        asset_dir.mkdir(parents=True)
        (asset_dir / "previous.mp4").write_bytes(b"video")
        (asset_dir / ".asset.json").write_text(
            json.dumps(
                {
                    "asset_id": asset_id,
                    "generation_id": generation_id,
                    "bytes": 5,
                }
            ),
            encoding="utf-8",
        )

        created = client.post(
            "/api/tasks/video-frames",
            json={
                "input_path": f"asset:{asset_id}:previous.mp4",
                "image_format": "png",
            },
        )

    assert created.status_code == 202
    task_pin = redis_connection.hgetall(
        f"dt:resource:task-pin:{created.json()['task_id']}"
    )
    assert json.loads(task_pin[b"asset_ids"]) == [asset_id]
    assert json.loads(task_pin[b"asset_identities"]) == [[asset_id, None]]


@pytest.mark.parametrize("include_generation", [False, True])
def test_hosted_task_rejects_explicit_v1_manifest_references(
    tmp_path: Path,
    include_generation: bool,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    asset_id = "1" * 32
    generation_id = "2" * 32
    with _client(tmp_path, task_manager=manager) as client:
        assert client.get("/api/config").status_code == 200
        session_id = client.cookies["dt_session"]
        asset_dir = tmp_path / "sessions" / session_id / "assets" / asset_id
        asset_dir.mkdir(parents=True)
        (asset_dir / "previous.mp4").write_bytes(b"video")
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
            f"asset:{asset_id}:{generation_id}:previous.mp4"
            if include_generation
            else f"asset:{asset_id}:previous.mp4"
        )

        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "asset_not_found"
    assert manager.queue.count == 0
    assert list(
        redis_connection.scan_iter(match="dt:resource:task-pin:*")
    ) == []


def test_reused_upload_id_rejects_stale_generation_without_pin_or_task_leak(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    upload_id = "1" * 32
    with _client(tmp_path, task_manager=manager) as client:
        first_reference = _upload(
            client,
            "first.mp4",
            b"first",
            upload_id=upload_id,
        ).json()["reference"]
        assert _delete_uploaded_asset(client, first_reference).status_code == 204
        second_reference = _upload(
            client,
            "second.mp4",
            b"second",
            upload_id=upload_id,
        ).json()["reference"]

        stale = client.post(
            "/api/tasks/video-frames",
            json={"input_path": first_reference, "image_format": "png"},
        )
        legacy = client.post(
            "/api/tasks/video-frames",
            json={
                "input_path": f"asset:{upload_id}:first.mp4",
                "image_format": "png",
            },
        )
        current = client.post(
            "/api/tasks/video-frames",
            json={"input_path": second_reference, "image_format": "png"},
        )

    assert stale.status_code == 404
    assert stale.json()["error"]["code"] == "asset_not_found"
    assert legacy.status_code == 404
    assert legacy.json()["error"]["code"] == "asset_not_found"
    assert current.status_code == 202
    assert manager.queue.count == 1
    task_pin = redis_connection.hgetall(
        f"dt:resource:task-pin:{current.json()['task_id']}"
    )
    assert json.loads(task_pin[b"asset_identities"]) == [
        [upload_id, second_reference.split(":")[2]]
    ]
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


def test_hosted_batch_generation_mismatch_is_all_or_nothing(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        first = _upload(
            client,
            "first.mp4",
            b"first",
            upload_id="1" * 32,
        ).json()["reference"]
        second = _upload(
            client,
            "second.mp4",
            b"second",
            upload_id="2" * 32,
        ).json()["reference"]
        second_parts = second.split(":")
        second_parts[2] = "f" * 32

        response = client.post(
            "/api/tasks/video-frames",
            json={
                "input_paths": [first, ":".join(second_parts)],
                "image_format": "png",
            },
        )

    assert response.status_code == 404
    assert manager.queue.count == 0
    assert list(redis_connection.scan_iter(match="dt:task:*")) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


@pytest.mark.parametrize(
    ("route", "extra_payload", "filename", "content"),
    [
        (
            "/api/tasks/video-frames",
            {"image_format": "png"},
            "clip.mp4",
            b"video",
        ),
        (
            "/api/tasks/media-resize",
            {"size": "100x100"},
            "image.png",
            _png_bytes(),
        ),
    ],
)
@pytest.mark.parametrize(
    "use_legacy_reference",
    [False, True],
    ids=["stale-generation", "legacy-generation"],
)
@pytest.mark.parametrize(
    "current_first",
    [True, False],
    ids=["current-first", "stale-first"],
)
def test_hosted_batch_rejects_conflicting_asset_generations_without_state(
    tmp_path: Path,
    route: str,
    extra_payload: dict[str, object],
    filename: str,
    content: bytes,
    use_legacy_reference: bool,
    current_first: bool,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    upload_id = "1" * 32
    with _client(
        tmp_path,
        task_manager=manager,
        raise_server_exceptions=False,
    ) as client:
        stale_reference = _upload(
            client,
            filename,
            content,
            upload_id=upload_id,
        ).json()["reference"]
        assert _delete_uploaded_asset(client, stale_reference).status_code == 204
        current_reference = _upload(
            client,
            filename,
            content,
            upload_id=upload_id,
        ).json()["reference"]
        if use_legacy_reference:
            stale_reference = f"asset:{upload_id}:{filename}"
        input_paths = [current_reference, stale_reference]
        if not current_first:
            input_paths.reverse()

        response = client.post(
            route,
            json={"input_paths": input_paths, **extra_payload},
        )

    current_generation = current_reference.split(":")[2]
    stale_generation = stale_reference.split(":")[2]
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "asset_not_found",
            "message": "Uploaded asset is not available",
        }
    }
    assert current_generation not in response.text
    if not use_legacy_reference:
        assert stale_generation not in response.text
    assert manager.queue.count == 0
    for pattern in (
        "dt:task:*",
        "dt:task-work:*",
        "dt:resource:task:*",
        "dt:resource:task-pin:*",
        "dt:resource:asset-read-pin:*",
        "dt:resource:asset-readers:*",
    ):
        assert list(redis_connection.scan_iter(match=pattern)) == []
    assert list(tmp_path.glob("sessions/*/tasks/*")) == []


@pytest.mark.parametrize(
    ("route", "extra_payload", "filename", "content"),
    [
        (
            "/api/tasks/video-frames",
            {"image_format": "png"},
            "clip.mp4",
            b"video",
        ),
        (
            "/api/tasks/media-resize",
            {"size": "100x100"},
            "image.png",
            _png_bytes(),
        ),
    ],
)
def test_hosted_batch_deduplicates_identical_asset_identity_only_for_pinning(
    tmp_path: Path,
    route: str,
    extra_payload: dict[str, object],
    filename: str,
    content: bytes,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, filename, content).json()["reference"]
        response = client.post(
            route,
            json={"input_paths": [reference, reference], **extra_payload},
        )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    task_pin = redis_connection.hgetall(f"dt:resource:task-pin:{task_id}")
    reference_parts = reference.split(":")
    assert json.loads(task_pin[b"asset_identities"]) == [
        [reference_parts[1], reference_parts[2]]
    ]
    job = manager.queue.fetch_job(task_id)
    assert job is not None
    input_paths = job.args[2]["input_paths"]
    assert len(input_paths) == 2
    assert input_paths[0] == input_paths[1]


def test_in_memory_hosted_task_retains_session_owner(tmp_path: Path) -> None:
    manager = TaskManager(
        log_store=LogStore(),
        exporter=None,
        run_async=False,
    )
    with _client(tmp_path, task_manager=manager) as owner:
        reference = _upload(owner, "clip.mp4", b"video").json()["reference"]
        created = owner.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )
        task_id = created.json()["task_id"]
        own_status = owner.get(f"/api/tasks/{task_id}")

    with _client(tmp_path, task_manager=manager) as stranger:
        stranger_status = stranger.get(f"/api/tasks/{task_id}")

    assert created.status_code == 202
    assert own_status.status_code == 200
    assert stranger_status.status_code == 404
    assert manager.get_task(task_id) is not None


def test_delete_upload_is_blocked_from_validation_through_task_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
        asset_read_pin_ttl=0.05,
        asset_read_pin_heartbeat_interval=0.01,
    )
    validation_complete = Event()
    allow_enqueue = Event()
    original_validate = HostedMediaValidator.validate_media_many

    def pause_after_validation(self, paths):
        validated = original_validate(self, paths)
        validation_complete.set()
        assert allow_enqueue.wait(timeout=5)
        return validated

    monkeypatch.setattr(
        HostedMediaValidator,
        "validate_media_many",
        pause_after_validation,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            create = executor.submit(
                client.post,
                "/api/tasks/video-frames",
                json={"input_path": reference, "image_format": "png"},
            )
            try:
                assert validation_complete.wait(timeout=5)
                sleep(0.1)
                during_validation = _delete_uploaded_asset(client, reference)
            finally:
                allow_enqueue.set()
            created = create.result(timeout=5)

        after_handoff = _delete_uploaded_asset(client, reference)
        manager.mark_finished(
            created.json()["task_id"],
            status=TaskStatus.SUCCEEDED,
        )
        after_terminal = _delete_uploaded_asset(client, reference)

    assert during_validation.status_code == 409
    assert during_validation.json()["error"]["code"] == "asset_in_use"
    assert created.status_code == 202
    assert after_handoff.status_code == 409
    assert after_terminal.status_code == 204


def test_hosted_image_inspection_pins_generation_before_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
        asset_read_pin_ttl=0.05,
        asset_read_pin_heartbeat_interval=0.01,
    )
    reader_entered = Event()
    resume_reader = Event()
    original_read_snapshot = routes_module._read_rgb_snapshot

    def read_after_barrier(path: Path):
        reader_entered.set()
        assert resume_reader.wait(timeout=5)
        return original_read_snapshot(path)

    monkeypatch.setattr(routes_module, "_read_rgb_snapshot", read_after_barrier)
    upload_id = "1" * 32
    with _client(tmp_path, task_manager=manager) as client:
        first_reference = _upload(
            client,
            "screen.png",
            _png_bytes((2, 1)),
            upload_id=upload_id,
        ).json()["reference"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            inspection = executor.submit(
                client.post,
                "/api/chroma-keyer/images/inspect",
                json={"input_path": first_reference},
            )
            try:
                assert reader_entered.wait(timeout=5)
                sleep(0.1)
                during_read = _delete_uploaded_asset(client, first_reference)
                replacement = _upload(
                    client,
                    "screen.png",
                    _png_bytes((3, 1)),
                    upload_id=upload_id,
                )
            finally:
                resume_reader.set()
            inspected = inspection.result(timeout=5)

        after_read = _delete_uploaded_asset(client, first_reference)

    assert during_read.status_code == 409
    assert during_read.json()["error"]["code"] == "asset_in_use"
    assert replacement.status_code == 409
    assert inspected.status_code == 200
    assert inspected.json()["width"] == 2
    assert after_read.status_code == 204


@pytest.mark.parametrize(
    ("route", "payload", "media_kind", "reader_kind"),
    [
        ("/api/chroma-keyer/images/inspect", {}, "image", "snapshot"),
        (
            "/api/chroma-keyer/images/sample",
            {
                "x": 0.0,
                "y": 0.0,
                "preview_width": 2.0,
                "preview_height": 1.0,
            },
            "image",
            "image",
        ),
        (
            "/api/chroma-keyer/preview",
            {"samples": [[0, 255, 0]], "params": {}},
            "image",
            "snapshot",
        ),
        (
            "/api/chroma-keyer/previews",
            {"samples": [[0, 255, 0]], "params": {}},
            "image",
            "snapshot",
        ),
        ("/api/chroma-keyer/videos/inspect", {}, "video", "video"),
        (
            "/api/chroma-keyer/videos/frame",
            {"timestamp_seconds": 0.0},
            "video",
            "video",
        ),
        (
            "/api/chroma-keyer/videos/sample",
            {
                "timestamp_seconds": 0.0,
                "x": 0.0,
                "y": 0.0,
                "preview_width": 2.0,
                "preview_height": 2.0,
            },
            "video",
            "video",
        ),
        (
            "/api/chroma-keyer/videos/preview",
            {
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
            "video",
            "video",
        ),
    ],
)
def test_hosted_preview_readers_hold_generation_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    payload: dict[str, object],
    media_kind: str,
    reader_kind: str,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    decoder = StubHostedVideoDecoder()
    reader_observed_pin = Event()

    with _client(
        tmp_path,
        task_manager=manager,
        video_decoder=decoder,
    ) as client:
        filename = "clip.mp4" if media_kind == "video" else "screen.png"
        content = b"video" if media_kind == "video" else _png_bytes()
        reference = _upload(client, filename, content).json()["reference"]
        _scheme, asset_id, _generation_id, _filename = reference.split(":", 3)
        session_id = client.cookies["dt_session"]
        readers_key = f"dt:resource:asset-readers:{session_id}:{asset_id}"

        def assert_pin_held() -> None:
            assert redis_connection.zcard(readers_key) == 1
            reader_observed_pin.set()

        if reader_kind == "snapshot":
            original_reader = routes_module._read_rgb_snapshot

            def read_snapshot(path: Path):
                assert_pin_held()
                return original_reader(path)

            monkeypatch.setattr(routes_module, "_read_rgb_snapshot", read_snapshot)
        elif reader_kind == "image":
            original_reader = routes_module.read_rgb_image

            def read_image(path: Path):
                assert_pin_held()
                return original_reader(path)

            monkeypatch.setattr(routes_module, "read_rgb_image", read_image)
        else:
            original_reader = decoder.read_frame

            def read_video(path: Path, timestamp_seconds: float):
                assert_pin_held()
                frame = original_reader(path, timestamp_seconds)
                frame[:, :] = (0, 255, 0)
                return frame

            monkeypatch.setattr(decoder, "read_frame", read_video)

        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 200
    assert reader_observed_pin.is_set()
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


def test_hosted_preview_reader_error_releases_generation_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )

    def fail_read(_path: Path):
        raise RuntimeError("decode failed")

    monkeypatch.setattr(routes_module, "_read_rgb_snapshot", fail_read)
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(
            client,
            "screen.png",
            _png_bytes(),
        ).json()["reference"]
        with pytest.raises(RuntimeError, match="decode failed"):
            client.post(
                "/api/chroma-keyer/images/inspect",
                json={"input_path": reference},
            )
        deleted = _delete_uploaded_asset(client, reference)

    assert deleted.status_code == 204
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


def test_hosted_preview_heartbeat_failure_fails_and_releases_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
        asset_read_pin_heartbeat_interval=0.001,
    )
    heartbeat_failed = Event()
    original_read_snapshot = routes_module._read_rgb_snapshot

    def expire_pin(_session_id, _asset_identities, _token):
        heartbeat_failed.set()
        return False

    def read_after_heartbeat(path: Path):
        assert heartbeat_failed.wait(timeout=5)
        return original_read_snapshot(path)

    monkeypatch.setattr(manager, "refresh_asset_read_pin", expire_pin)
    monkeypatch.setattr(routes_module, "_read_rgb_snapshot", read_after_heartbeat)
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(
            client,
            "screen.png",
            _png_bytes(),
        ).json()["reference"]
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": reference},
        )
        deleted = _delete_uploaded_asset(client, reference)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "asset_in_use"
    assert deleted.status_code == 204
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


def test_hosted_preview_heartbeat_redis_outage_returns_503_and_releases_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
        asset_read_pin_heartbeat_interval=0.001,
    )
    heartbeat_failed = Event()
    original_read_snapshot = routes_module._read_rgb_snapshot

    def fail_refresh(_session_id, _asset_identities, _token):
        heartbeat_failed.set()
        raise RedisConnectionError("redis unavailable")

    def read_after_heartbeat(path: Path):
        assert heartbeat_failed.wait(timeout=5)
        return original_read_snapshot(path)

    monkeypatch.setattr(manager, "refresh_asset_read_pin", fail_refresh)
    monkeypatch.setattr(routes_module, "_read_rgb_snapshot", read_after_heartbeat)
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(
            client,
            "screen.png",
            _png_bytes(),
        ).json()["reference"]
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": reference},
        )
        deleted = _delete_uploaded_asset(client, reference)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert deleted.status_code == 204
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


def test_hosted_preview_pin_outlives_failed_heartbeat_until_blocked_read_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
        asset_read_pin_ttl=0.05,
        asset_read_pin_heartbeat_interval=0.01,
    )
    reader_entered = Event()
    heartbeat_failed = Event()
    resume_reader = Event()
    original_read_snapshot = routes_module._read_rgb_snapshot

    def fail_refresh(_session_id, _asset_identities, _token):
        heartbeat_failed.set()
        raise RedisConnectionError("redis unavailable")

    def blocked_read(path: Path):
        reader_entered.set()
        assert resume_reader.wait(timeout=5)
        return original_read_snapshot(path)

    monkeypatch.setattr(manager, "refresh_asset_read_pin", fail_refresh)
    monkeypatch.setattr(routes_module, "_read_rgb_snapshot", blocked_read)
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(
            client,
            "screen.png",
            _png_bytes(),
        ).json()["reference"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            preview = executor.submit(
                client.post,
                "/api/chroma-keyer/images/inspect",
                json={"input_path": reference},
            )
            try:
                assert reader_entered.wait(timeout=5)
                assert heartbeat_failed.wait(timeout=5)
                pin_key = next(
                    redis_connection.scan_iter(
                        match="dt:resource:asset-read-pin:*"
                    )
                )
                assert redis_connection.hget(pin_key, "ttl_ms") == b"7200000"
                sleep(0.1)
                during_read = _delete_uploaded_asset(client, reference)
            finally:
                resume_reader.set()
            response = preview.result(timeout=5)

        after_read = _delete_uploaded_asset(client, reference)

    assert during_read.status_code == 409
    assert during_read.json()["error"]["code"] == "asset_in_use"
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert after_read.status_code == 204


def test_hosted_preview_decode_error_survives_pin_heartbeat_stop_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_stop = routes_module._AssetReadPinHeartbeat.stop

    def stop_then_timeout(heartbeat) -> None:
        original_stop(heartbeat)
        raise HeartbeatStopTimeoutError(
            "Asset read pin heartbeat did not stop within 0.01 seconds"
        )

    def fail_decode(_path: Path):
        raise ValueError("decode failed")

    monkeypatch.setattr(
        routes_module._AssetReadPinHeartbeat,
        "stop",
        stop_then_timeout,
    )
    monkeypatch.setattr(routes_module, "_read_rgb_snapshot", fail_decode)
    caplog.set_level("ERROR", logger="designtoolkit.web.routes")

    with _client(tmp_path) as client:
        reference = _upload(
            client,
            "screen.png",
            _png_bytes(),
        ).json()["reference"]
        with pytest.raises(ValueError, match="decode failed"):
            client.post(
                "/api/chroma-keyer/images/inspect",
                json={"input_path": reference},
            )
        deleted = _delete_uploaded_asset(client, reference)

    assert deleted.status_code == 204
    assert "asset read pin heartbeat failed during preview endpoint error" in (
        caplog.text
    )
    assert "Asset read pin heartbeat did not stop" in caplog.text


def test_hosted_preview_pin_redis_outage_returns_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path) as client:
        manager = client.app.state.task_manager
        reference = _upload(
            client,
            "screen.png",
            _png_bytes(),
        ).json()["reference"]
        monkeypatch.setattr(
            manager,
            "acquire_asset_read_pin",
            Mock(side_effect=RedisConnectionError("redis unavailable")),
        )

        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": reference},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"


def test_hosted_preview_rejects_malformed_reference_before_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path) as client:
        manager = client.app.state.task_manager
        acquire_pin = Mock(side_effect=AssertionError("pin must not be acquired"))
        monkeypatch.setattr(manager, "acquire_asset_read_pin", acquire_pin)

        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": "not-an-asset"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "asset_not_found"
    acquire_pin.assert_not_called()


def test_hosted_preview_pins_stale_generation_before_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    storage = HostedStorage(tmp_path, max_upload_bytes=1024)
    calls: list[tuple[str, object]] = []
    original_acquire = manager.acquire_asset_read_pin
    original_resolve = storage.resolve

    def acquire_pin(session_id, asset_identities, **kwargs):
        calls.append(("acquire", asset_identities))
        return original_acquire(session_id, asset_identities, **kwargs)

    def resolve_asset(session_id: str, reference: str) -> Path:
        calls.append(("resolve", reference))
        return original_resolve(session_id, reference)

    monkeypatch.setattr(manager, "acquire_asset_read_pin", acquire_pin)
    monkeypatch.setattr(storage, "resolve", resolve_asset)
    with _client(
        tmp_path,
        task_manager=manager,
        hosted_storage=storage,
    ) as client:
        reference = _upload(
            client,
            "screen.png",
            _png_bytes(),
        ).json()["reference"]
        scheme, asset_id, generation_id, filename = reference.split(":", 3)
        stale_generation = "f" * 32 if generation_id != "f" * 32 else "e" * 32
        stale_reference = f"{scheme}:{asset_id}:{stale_generation}:{filename}"
        calls.clear()

        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": stale_reference},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "asset_not_found"
    assert calls == [
        ("acquire", ((asset_id, stale_generation),)),
        ("resolve", stale_reference),
    ]
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


def test_failed_hosted_task_validation_releases_asset_read_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )

    def reject_media(_self, _paths):
        raise MediaLimitError("invalid_media", "Media validation failed")

    monkeypatch.setattr(
        HostedMediaValidator,
        "validate_media_many",
        reject_media,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        rejected = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )
        deleted = _delete_uploaded_asset(client, reference)

    assert rejected.status_code == 422
    assert deleted.status_code == 204
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


def test_interrupted_hosted_task_validation_releases_asset_read_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )

    def interrupt_validation(_self, _paths):
        raise RuntimeError("client disconnected")

    monkeypatch.setattr(
        HostedMediaValidator,
        "validate_media_many",
        interrupt_validation,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        with pytest.raises(RuntimeError, match="client disconnected"):
            client.post(
                "/api/tasks/video-frames",
                json={"input_path": reference, "image_format": "png"},
            )
        deleted = _delete_uploaded_asset(client, reference)

    assert deleted.status_code == 204
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-read-pin:*")
    ) == []
    assert list(
        redis_connection.scan_iter(match="dt:resource:asset-readers:*")
    ) == []


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
def test_delete_upload_rejects_asset_referenced_by_active_task(
    tmp_path: Path,
    status: TaskStatus,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        _scheme, asset_id, generation_id, _filename = reference.split(":", 3)
        session_id = client.cookies["dt_session"]
        asset_path = HostedStorage(tmp_path, max_upload_bytes=1024).resolve(
            session_id,
            reference,
        )
        created = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )
        task_id = created.json()["task_id"]
        if status is TaskStatus.RUNNING:
            manager.mark_running(task_id)

        stale = client.delete(
            f"/api/uploads/{asset_id}?generation={'f' * 32}"
        )
        deleted = client.delete(
            f"/api/uploads/{asset_id}?generation={generation_id}"
        )

    assert created.status_code == 202
    assert stale.status_code == 404
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "asset_in_use"
    assert asset_path.read_bytes() == b"video"


def test_delete_upload_accepts_asset_after_task_becomes_terminal(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        asset_id = reference.split(":", 2)[1]
        created = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )
        manager.mark_finished(
            created.json()["task_id"],
            status=TaskStatus.SUCCEEDED,
        )

        deleted = _delete_uploaded_asset(client, reference)

    assert created.status_code == 202
    assert deleted.status_code == 204
    assert list(tmp_path.glob(f"sessions/*/assets/{asset_id}")) == []


def test_delete_upload_waits_for_all_tasks_sharing_asset_to_finish(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        task_ids = [
            client.post(
                "/api/tasks/video-frames",
                json={"input_path": reference, "image_format": "png"},
            ).json()["task_id"]
            for _index in range(2)
        ]
        manager.mark_finished(task_ids[0], status=TaskStatus.SUCCEEDED)

        still_referenced = _delete_uploaded_asset(client, reference)
        manager.mark_finished(task_ids[1], status=TaskStatus.FAILED, error="failed")
        deleted = _delete_uploaded_asset(client, reference)

    assert still_referenced.status_code == 409
    assert deleted.status_code == 204


def test_hosted_task_holds_session_lease_through_queue_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    original_create = manager.create_video_frames_task

    def create_while_leased(*args, **kwargs):
        assert coordinator.active_leases == 1
        return original_create(*args, **kwargs)

    monkeypatch.setattr(
        manager,
        "create_video_frames_task",
        create_while_leased,
    )
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.lease_acquisitions = 0
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 202
    assert coordinator.lease_acquisitions == 1
    assert coordinator.active_leases == 0


def test_session_commit_waits_for_in_flight_refresh_failure_and_blocks_action() -> None:
    entered_refresh = Event()
    allow_failure = Event()
    action_called = Event()

    class InFlightFailureLease:
        def __init__(self) -> None:
            self.refresh_calls = 0

        def refresh(self) -> None:
            self.refresh_calls += 1
            if self.refresh_calls == 1:
                entered_refresh.set()
                assert allow_failure.wait(timeout=1)
                raise ResourceUnavailableError("Session lease refresh failed in flight")

    lease = InFlightFailureLease()
    heartbeat = routes_module._SessionLeaseHeartbeat(lease, 0.001)
    executor = ThreadPoolExecutor(max_workers=1)
    heartbeat.start()
    try:
        assert entered_refresh.wait(timeout=1)
        commit = executor.submit(
            heartbeat.commit,
            lambda: action_called.set(),
        )
        try:
            assert not action_called.wait(timeout=0.05)
            assert not commit.done()
        finally:
            allow_failure.set()
        with pytest.raises(
            ResourceUnavailableError,
            match="refresh failed in flight",
        ):
            commit.result(timeout=1)
    finally:
        allow_failure.set()
        heartbeat.stop()
        executor.shutdown(wait=True)


def test_session_heartbeat_stop_timeout_is_bounded_and_observable() -> None:
    entered_refresh = Event()
    allow_refresh = Event()

    class BlockingLease:
        coordinator = type(
            "Coordinator",
            (),
            {"heartbeat_stop_timeout_seconds": 0.02},
        )()

        def refresh(self) -> None:
            entered_refresh.set()
            assert allow_refresh.wait(timeout=1)

    heartbeat = routes_module._SessionLeaseHeartbeat(BlockingLease(), 0.001)
    executor = ThreadPoolExecutor(max_workers=1)
    heartbeat.start()
    try:
        assert entered_refresh.wait(timeout=1)
        stop = executor.submit(heartbeat.stop)
        with pytest.raises(
            ResourceUnavailableError,
            match="Session lease heartbeat did not stop",
        ):
            stop.result(timeout=0.2)
        with pytest.raises(
            ResourceUnavailableError,
            match="Session lease heartbeat did not stop",
        ):
            heartbeat.raise_if_failed()
    finally:
        allow_refresh.set()
        heartbeat._thread.join(timeout=1)
        executor.shutdown(wait=True)


def test_hosted_task_returns_success_after_enqueue_when_late_heartbeat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    original_create = manager.create_video_frames_task

    def create_then_fail_heartbeat(*args, **kwargs):
        task = original_create(*args, **kwargs)
        refresh_calls = coordinator.lease_refresh_calls
        coordinator.fail_lease_refresh = True
        sleep(0.04)
        assert coordinator.lease_refresh_calls == refresh_calls
        return task

    monkeypatch.setattr(
        manager,
        "create_video_frames_task",
        create_then_fail_heartbeat,
    )
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.session_lease_heartbeat_interval = 0.01
        coordinator.lease_refresh_calls = 0
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 202
    assert manager.queue.count == 1
    assert coordinator.active_leases == 0


def test_hosted_task_heartbeat_failure_before_enqueue_returns_503_without_job(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.fail_lease_refresh = True
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert manager.queue.count == 0
    assert coordinator.active_leases == 0


def test_hosted_task_heartbeat_failure_after_barrier_rolls_back_before_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    original_save_work = manager._save_work

    def save_work_then_fail_heartbeat(*args, **kwargs) -> None:
        original_save_work(*args, **kwargs)
        refresh_calls = coordinator.lease_refresh_calls
        coordinator.fail_lease_refresh = True
        deadline = monotonic() + 1
        while (
            coordinator.lease_refresh_calls == refresh_calls
            and monotonic() < deadline
        ):
            sleep(0.001)
        assert coordinator.lease_refresh_calls > refresh_calls

    monkeypatch.setattr(manager, "_save_work", save_work_then_fail_heartbeat)
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.session_lease_heartbeat_interval = 0.001
        coordinator.lease_refresh_calls = 0
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert manager.queue.count == 0
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert list(redis_connection.scan_iter(match="dt:task:*")) == []
    assert list(redis_connection.scan_iter(match="dt:task-work:*")) == []
    assert coordinator.active_leases == 0


def test_hosted_task_failure_injected_after_quiesce_blocks_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    original_stop = routes_module._SessionLeaseHeartbeat.stop

    def stop_then_fail(heartbeat) -> None:
        original_stop(heartbeat)
        heartbeat._failure = ResourceUnavailableError(
            "Session lease heartbeat failed before enqueue"
        )

    monkeypatch.setattr(
        routes_module._SessionLeaseHeartbeat,
        "stop",
        stop_then_fail,
    )
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert manager.queue.count == 0
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert list(redis_connection.scan_iter(match="dt:task:*")) == []
    assert list(redis_connection.scan_iter(match="dt:task-work:*")) == []


def test_hosted_task_recovers_enqueue_acknowledgement_loss_without_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    original_enqueue = manager._commit_enqueue_job

    def enqueue_then_lose_ack(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        raise RedisConnectionError("enqueue acknowledgement lost")

    monkeypatch.setattr(manager, "_commit_enqueue_job", enqueue_then_lose_ack)
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.session_lease_heartbeat_interval = 0.001
        coordinator.lease_refresh_calls = 0
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )
        task_id = response.json()["task_id"]
        task_response = client.get(f"/api/tasks/{task_id}")

    assert response.status_code == 202
    assert task_response.status_code == 200
    assert task_response.json()["status"] == TaskStatus.QUEUED
    assert manager.queue.get_job_ids() == [task_id]
    assert manager.queue.fetch_job(task_id) is not None
    assert manager.task_work(task_id) is not None
    assert redis_connection.lrange("dt:queue:reservations", 0, -1) == [
        task_id.encode()
    ]


def test_hosted_task_returns_handle_when_enqueue_and_confirmation_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    original_enqueue = manager._commit_enqueue_job

    def enqueue_then_lose_ack(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        raise RedisConnectionError("enqueue acknowledgement lost")

    def confirmation_disconnect(_task_id: str) -> bool:
        raise RedisConnectionError("enqueue confirmation unavailable")

    monkeypatch.setattr(manager, "_commit_enqueue_job", enqueue_then_lose_ack)
    monkeypatch.setattr(
        manager,
        "_enqueue_has_authoritative_evidence",
        confirmation_disconnect,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert manager.queue.get_job_ids() == [task_id]
    assert manager.get_task(task_id) is not None
    assert manager.task_work(task_id) is not None
    assert redis_connection.lrange("dt:queue:reservations", 0, -1) == [
        task_id.encode()
    ]
    assert manager.reconcile_tasks(grace_seconds=60) == 0


def test_hosted_task_refreshes_once_before_slow_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    original_enqueue = manager.queue.enqueue_job
    refreshes_during_enqueue: list[tuple[int, int]] = []

    def slow_enqueue(*args, **kwargs):
        refresh_calls = coordinator.lease_refresh_calls
        assert refresh_calls >= 1
        sleep(0.04)
        refreshes_during_enqueue.append(
            (refresh_calls, coordinator.lease_refresh_calls)
        )
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(manager.queue, "enqueue_job", slow_enqueue)
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.session_lease_heartbeat_interval = 0.001
        coordinator.lease_refresh_calls = 0
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 202
    assert manager.queue.count == 1
    assert len(refreshes_during_enqueue) == 1
    assert refreshes_during_enqueue[0][0] == refreshes_during_enqueue[0][1]
    assert coordinator.active_leases == 0


def test_hosted_task_logs_heartbeat_failure_when_endpoint_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )

    def fail_create(*_args, **_kwargs):
        refresh_calls_before_failure = coordinator.lease_refresh_calls
        coordinator.fail_lease_refresh = True
        deadline = monotonic() + 1
        while (
            coordinator.lease_refresh_calls == refresh_calls_before_failure
            and monotonic() < deadline
        ):
            sleep(0.001)
        assert coordinator.lease_refresh_calls > refresh_calls_before_failure
        raise ValueError("endpoint rejected request")

    monkeypatch.setattr(manager, "create_video_frames_task", fail_create)
    caplog.set_level("ERROR", logger="designtoolkit.web.routes")
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.session_lease_heartbeat_interval = 0.001
        coordinator.lease_refresh_calls = 0
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "session lease heartbeat failed during endpoint error" in caplog.text
    assert "Session lease heartbeat failed" in caplog.text
    assert coordinator.active_leases == 0


def test_hosted_task_download_is_session_isolated(tmp_path: Path) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    result = tmp_path / "result.png"
    result.write_bytes(b"result")
    with _client(tmp_path, task_manager=manager) as owner:
        reference = _upload(owner, "clip.mp4", b"video").json()["reference"]
        task_id = owner.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png", "overwrite": False},
        ).json()["task_id"]
        manager.mark_finished(
            task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=f"/api/tasks/{task_id}/download",
            result_path=str(result),
        )
        download = owner.get(f"/api/tasks/{task_id}/download")

    with _client(tmp_path, task_manager=manager) as stranger:
        stranger_download = stranger.get(f"/api/tasks/{task_id}/download")

    assert download.status_code == 200
    assert download.content == b"result"
    assert stranger_download.status_code == 404


def test_hosted_download_releases_lease_after_response_body(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    coordinator.session_lease_heartbeat_interval = 0.01
    result = tmp_path / "result.png"
    result.write_bytes(b"result")
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        task_id = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        ).json()["task_id"]
        manager.mark_finished(
            task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=f"/api/tasks/{task_id}/download",
            result_path=str(result),
        )
        coordinator.lease_acquisitions = 0
        coordinator.lease_refresh_calls = 0
        coordinator.lease_release_calls = 0
        response = client.get(f"/api/tasks/{task_id}/download")
        capacity = client.app.state.stream_capacity
        assert capacity.acquire(blocking=False)
        capacity.release()
        refreshes_after_response = coordinator.lease_refresh_calls
        sleep(0.03)

    assert response.status_code == 200
    assert coordinator.lease_acquisitions == 1
    assert coordinator.lease_release_calls == 1
    assert coordinator.active_leases == 0
    assert coordinator.lease_refresh_calls == refreshes_after_response


def test_hosted_download_logs_heartbeat_failure_during_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    storage = HostedStorage(tmp_path, max_upload_bytes=1024)
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
        hosted_storage=storage,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        task_id = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        ).json()["task_id"]
        manager.mark_finished(
            task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=f"/api/tasks/{task_id}/download",
            result_path=str(tmp_path / "missing.png"),
        )
        coordinator.session_lease_heartbeat_interval = 0.001
        coordinator.lease_acquisitions = 0
        coordinator.lease_refresh_calls = 0
        coordinator.lease_release_calls = 0
        coordinator.fail_lease_refresh = True

        def fail_validation(_value: str | None) -> Path:
            deadline = monotonic() + 1
            while coordinator.lease_refresh_calls == 0 and monotonic() < deadline:
                sleep(0.001)
            assert coordinator.lease_refresh_calls > 0
            raise UploadRejectedError("Task result is not available")

        monkeypatch.setattr(storage, "validated_result_path", fail_validation)
        caplog.set_level("ERROR", logger="designtoolkit.web.routes")

        response = client.get(f"/api/tasks/{task_id}/download")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "result_not_found"
    assert (
        "download session lease heartbeat failed during result validation error"
        in caplog.text
    )
    assert "Session lease heartbeat failed" in caplog.text
    assert coordinator.lease_release_calls == 1
    assert coordinator.active_leases == 0


def test_hosted_download_releases_lease_when_response_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    coordinator.session_lease_heartbeat_interval = 0.01
    result = tmp_path / "result.png"
    result.write_bytes(b"result")
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        task_id = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        ).json()["task_id"]
        manager.mark_finished(
            task_id,
            status=TaskStatus.SUCCEEDED,
            output_path=f"/api/tasks/{task_id}/download",
            result_path=str(result),
        )
        coordinator.lease_acquisitions = 0
        coordinator.lease_refresh_calls = 0
        coordinator.lease_release_calls = 0
        coordinator.fail_lease_refresh = True
        caplog.set_level("ERROR", logger="designtoolkit.web.routes")

        async def fail_send(
            _response,
            _scope,
            _receive,
            _send,
        ) -> None:
            sleep(0.03)
            raise RuntimeError("send failed")

        monkeypatch.setattr(FileResponse, "__call__", fail_send)

        with pytest.raises(RuntimeError, match="send failed"):
            client.get(f"/api/tasks/{task_id}/download")

        refreshes_after_failure = coordinator.lease_refresh_calls
        sleep(0.03)

    assert coordinator.lease_acquisitions == 1
    assert coordinator.lease_release_calls == 1
    assert coordinator.active_leases == 0
    assert coordinator.lease_refresh_calls == refreshes_after_failure
    assert (
        "download session lease heartbeat failed during response send error"
        in caplog.text
    )
    assert "Session lease heartbeat failed" in caplog.text


@pytest.mark.parametrize(
    "path",
    [
        f"/api/tasks/{'0' * 32}/download",
        f"/api/tasks/{'0' * 32}/events",
    ],
)
def test_hosted_stream_concurrency_rejects_before_route_resources(
    tmp_path: Path,
    path: str,
) -> None:
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=1024,
        min_free_bytes=0,
    )
    with _client(
        tmp_path,
        resource_coordinator=coordinator,
        max_stream_concurrency=1,
    ) as client:
        capacity = client.app.state.stream_capacity
        assert capacity.acquire(blocking=False)
        try:
            rejected = client.get(path)
        finally:
            capacity.release()
        after_release = client.get(path)

    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "stream_concurrency"
    assert after_release.status_code == 404
    assert coordinator.lease_acquisitions == 0


@pytest.mark.anyio
async def test_stream_capacity_is_released_when_client_disconnects() -> None:
    capacity = BoundedSemaphore(1)
    assert capacity.acquire(blocking=False)

    async def stream_body():
        yield b"event"
        yield b"later"

    response = StreamingResponse(stream_body())
    app_module._release_stream_capacity_after_response(response, capacity)

    assert await anext(response.body_iterator) == b"event"
    await response.body_iterator.aclose()
    assert capacity.acquire(blocking=False)
    capacity.release()


def test_hosted_batch_resize_uses_unique_managed_output_directory(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        references = [
            _upload(client, name, _png_bytes((1, 1))).json()["reference"]
            for name in ("first.png", "second.png")
        ]
        task_id = client.post(
            "/api/tasks/media-resize",
            json={"input_paths": references, "size": "100x100"},
        ).json()["task_id"]

    job = manager.queue.fetch_job(task_id)
    assert job is not None
    payload = job.args[2]
    assert payload["output_dir"].startswith(str(tmp_path))
    assert payload["output_path"] is None


@pytest.mark.parametrize(
    ("route", "extra_payload"),
    [
        ("/api/tasks/video-frames", {"image_format": "png"}),
        ("/api/tasks/media-resize", {"size": "100x100"}),
    ],
)
def test_hosted_batch_input_paths_have_hard_request_limit(
    tmp_path: Path,
    route: str,
    extra_payload: dict[str, object],
) -> None:
    repeated_reference = f"asset:{'1' * 32}:input.bin"
    unique_references = [
        f"asset:{index:032x}:input.bin" for index in range(101)
    ]

    with _client(tmp_path) as client:
        at_limit = client.post(
            route,
            json={
                "input_paths": [repeated_reference] * 100,
                **extra_payload,
            },
        )
        over_limit = client.post(
            route,
            json={"input_paths": unique_references, **extra_payload},
        )
        repeated_over_limit = client.post(
            route,
            json={
                "input_paths": [repeated_reference] * 101,
                **extra_payload,
            },
        )
        manager = client.app.state.task_manager

    assert at_limit.status_code == 404
    assert at_limit.json()["error"]["code"] == "asset_not_found"
    assert over_limit.status_code == 422
    assert over_limit.json()["error"]["code"] == "invalid_request"
    assert repeated_over_limit.status_code == 422
    assert repeated_over_limit.json()["error"]["code"] == "invalid_request"
    assert manager.queue.count == 0


@pytest.mark.parametrize(
    ("filename", "content", "route", "payload"),
    [
        (
            "screen.png",
            _png_bytes(),
            "/api/tasks/video-frames",
            {"image_format": "png"},
        ),
        (
            "clip.mp4",
            b"video",
            "/api/tasks/chroma-keyer",
            {"samples": [[0, 255, 0]], "params": {}},
        ),
    ],
)
def test_hosted_task_rejects_wrong_input_media_type_before_enqueue(
    tmp_path: Path,
    filename: str,
    content: bytes,
    route: str,
    payload: dict[str, object],
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, filename, content).json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert manager.queue.count == 0
    assert redis_connection.llen("dt:queue:reservations") == 0
    assert list(redis_connection.scan_iter(match="dt:task:*")) == []
    assert list(tmp_path.glob("sessions/*/tasks/*")) == []


@pytest.mark.parametrize(
    ("filename", "content", "route", "payload"),
    [
        (
            "clip.mp4",
            b"video",
            "/api/tasks/video-frames",
            {"image_format": "png"},
        ),
        (
            "screen.png",
            _png_bytes(),
            "/api/tasks/chroma-keyer",
            {"samples": [[0, 255, 0]], "params": {}},
        ),
        (
            "screen.png",
            _png_bytes(),
            "/api/tasks/media-resize",
            {"size": "100x100"},
        ),
        (
            "clip.mp4",
            b"video",
            "/api/tasks/media-resize",
            {"size": "100x100"},
        ),
    ],
)
def test_hosted_task_accepts_matching_input_media_type(
    tmp_path: Path,
    filename: str,
    content: bytes,
    route: str,
    payload: dict[str, object],
) -> None:
    with _client(tmp_path) as client:
        reference = _upload(client, filename, content).json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 202


def test_hosted_queue_rejection_removes_empty_task_directory(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=0,
        retention_seconds=7200,
    )
    with _client(tmp_path, task_manager=manager) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        response = client.post(
            "/api/tasks/video-frames",
            json={
                "input_path": reference,
                "image_format": "png",
                "overwrite": False,
            },
        )

    assert response.status_code == 409
    assert list(tmp_path.glob("sessions/*/tasks/*")) == []


def test_hosted_batch_rejects_all_inputs_before_output_or_enqueue(
    tmp_path: Path,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    enqueue = Mock(side_effect=AssertionError("queue must not be called"))
    manager.queue.enqueue = enqueue
    with _client(
        tmp_path,
        task_manager=manager,
        max_image_pixels=1,
    ) as client:
        references = [
            _upload(client, "exact.png", _png_bytes((1, 1))).json()["reference"],
            _upload(client, "large.png", _png_bytes((2, 1))).json()["reference"],
        ]
        response = client.post(
            "/api/tasks/media-resize",
            json={"input_paths": references, "size": "100x100"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_limit_exceeded"
    assert "1 pixels" in response.json()["error"]["message"]
    enqueue.assert_not_called()
    assert list(tmp_path.glob("sessions/*/tasks/*")) == []


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        (
            "/api/tasks/chroma-keyer",
            {"samples": [[0, 255, 0]], "params": {}},
        ),
        (
            "/api/tasks/media-resize",
            {"size": "100x100"},
        ),
    ],
)
def test_hosted_image_tasks_reject_oversized_media_before_enqueue(
    tmp_path: Path,
    route: str,
    payload: dict[str, object],
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    enqueue = Mock(side_effect=AssertionError("queue must not be called"))
    manager.queue.enqueue = enqueue
    with _client(
        tmp_path,
        task_manager=manager,
        max_image_pixels=1,
    ) as client:
        reference = _upload(
            client,
            "large.png",
            _png_bytes((2, 1)),
        ).json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_limit_exceeded"
    enqueue.assert_not_called()
    assert list(tmp_path.glob("sessions/*/tasks/*")) == []


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        (
            "/api/tasks/video-frames",
            {"image_format": "png", "overwrite": False},
        ),
        (
            "/api/tasks/chroma-keyer-video",
            {"samples": [[0, 255, 0]], "params": {}},
        ),
    ],
)
def test_hosted_video_tasks_reject_oversized_media_before_enqueue(
    tmp_path: Path,
    route: str,
    payload: dict[str, object],
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=tmp_path,
        max_queued_tasks=20,
        retention_seconds=7200,
    )
    enqueue = Mock(side_effect=AssertionError("queue must not be called"))
    manager.queue.enqueue = enqueue
    decoder = StubHostedVideoDecoder(
        VideoMetadata(
            width=3841,
            height=2160,
            fps=30.0,
            duration_seconds=1.0,
            estimated_frame_count=30,
            codec="stub",
        )
    )
    with _client(
        tmp_path,
        task_manager=manager,
        video_decoder=decoder,
    ) as client:
        reference = _upload(client, "large.mp4", b"video").json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_limit_exceeded"
    assert decoder.decode_calls == 0
    enqueue.assert_not_called()
    assert list(tmp_path.glob("sessions/*/tasks/*")) == []


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/api/chroma-keyer/images/inspect", {}),
        (
            "/api/chroma-keyer/images/sample",
            {
                "x": 0.0,
                "y": 0.0,
                "preview_width": 2.0,
                "preview_height": 1.0,
            },
        ),
        (
            "/api/chroma-keyer/preview",
            {"samples": [[0, 255, 0]], "params": {}},
        ),
        (
            "/api/chroma-keyer/previews",
            {"samples": [[0, 255, 0]], "params": {}},
        ),
    ],
)
def test_hosted_image_preview_endpoints_reject_before_decode(
    tmp_path: Path,
    route: str,
    payload: dict[str, object],
) -> None:
    with _client(tmp_path, max_image_pixels=1) as client:
        reference = _upload(
            client,
            "large.png",
            _png_bytes((2, 1)),
        ).json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_limit_exceeded"


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/api/chroma-keyer/videos/inspect", {}),
        ("/api/chroma-keyer/videos/frame", {"timestamp_seconds": 0.0}),
        (
            "/api/chroma-keyer/videos/sample",
            {
                "timestamp_seconds": 0.0,
                "x": 0.0,
                "y": 0.0,
                "preview_width": 2.0,
                "preview_height": 2.0,
            },
        ),
        (
            "/api/chroma-keyer/videos/preview",
            {
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
        ),
    ],
)
def test_hosted_video_preview_endpoints_reject_before_decode(
    tmp_path: Path,
    route: str,
    payload: dict[str, object],
) -> None:
    decoder = StubHostedVideoDecoder(
        VideoMetadata(
            width=3840,
            height=2161,
            fps=30.0,
            duration_seconds=1.0,
            estimated_frame_count=30,
            codec="stub",
        )
    )
    with _client(tmp_path, video_decoder=decoder) as client:
        reference = _upload(client, "large.mp4", b"video").json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_limit_exceeded"
    assert decoder.decode_calls == 0


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/api/chroma-keyer/videos/inspect", {}),
        ("/api/chroma-keyer/videos/frame", {"timestamp_seconds": 0.0}),
        (
            "/api/chroma-keyer/videos/sample",
            {
                "timestamp_seconds": 0.0,
                "x": 0.0,
                "y": 0.0,
                "preview_width": 2.0,
                "preview_height": 1.0,
            },
        ),
        (
            "/api/chroma-keyer/videos/preview",
            {
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
        ),
    ],
)
def test_hosted_video_preview_endpoints_reject_static_image_with_422(
    tmp_path: Path,
    route: str,
    payload: dict[str, object],
) -> None:
    decoder = StubHostedVideoDecoder()
    with _client(tmp_path, video_decoder=decoder) as client:
        reference = _upload(client, "image.png", _png_bytes()).json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "media_limit_exceeded",
            "message": "Media type is not a video",
        }
    }
    assert decoder.probe_calls == 0
    assert decoder.decode_calls == 0


def test_hosted_unreadable_media_returns_clear_422(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        reference = _upload(client, "broken.png", b"not an image").json()["reference"]
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": reference},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "media_limit_exceeded",
            "message": "Image could not be read",
        }
    }


def test_hosted_animated_image_rejected_before_video_probe(tmp_path: Path) -> None:
    decoder = StubHostedVideoDecoder()
    with _client(tmp_path, video_decoder=decoder) as client:
        reference = _upload(
            client,
            "animated.gif",
            _animated_gif_bytes(),
        ).json()["reference"]
        response = client.post(
            "/api/chroma-keyer/videos/inspect",
            json={"input_path": reference},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_limit_exceeded"
    assert decoder.probe_calls == 0
    assert decoder.decode_calls == 0


def test_hosted_repeated_video_preview_reuses_validation_probe(
    tmp_path: Path,
) -> None:
    decoder = StubHostedVideoDecoder()
    with _client(tmp_path, video_decoder=decoder) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        first = client.post(
            "/api/chroma-keyer/videos/preview",
            json={
                "input_path": reference,
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
        )
        second = client.post(
            "/api/chroma-keyer/videos/preview",
            json={
                "input_path": reference,
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert decoder.probe_calls == 1
    assert decoder.decode_calls == 1


@pytest.mark.parametrize(
    ("route", "payload", "expected_status"),
    [
        ("/api/chroma-keyer/videos/inspect", {}, 200),
        ("/api/chroma-keyer/videos/frame", {"timestamp_seconds": 0.0}, 200),
        (
            "/api/chroma-keyer/videos/sample",
            {
                "timestamp_seconds": 0.0,
                "x": 0.0,
                "y": 0.0,
                "preview_width": 2.0,
                "preview_height": 2.0,
            },
            400,
        ),
        (
            "/api/chroma-keyer/videos/preview",
            {
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
            200,
        ),
    ],
)
def test_hosted_video_preview_routes_do_not_reprobe_after_validation(
    tmp_path: Path,
    route: str,
    payload: dict[str, object],
    expected_status: int,
) -> None:
    decoder = StubHostedVideoDecoder()
    with _client(tmp_path, video_decoder=decoder) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        response = client.post(route, json={"input_path": reference, **payload})

    assert response.status_code == expected_status
    assert decoder.probe_calls == 1
    assert decoder.decode_calls == 1


def test_hosted_video_stat_change_reprobes_and_regenerates_preview(
    tmp_path: Path,
) -> None:
    decoder = StubHostedVideoDecoder()
    with _client(tmp_path, video_decoder=decoder) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        first = client.post(
            "/api/chroma-keyer/videos/preview",
            json={
                "input_path": reference,
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
        )
        stored_path = next(tmp_path.glob("sessions/*/assets/*/clip.mp4"))
        stored_path.write_bytes(b"changed-video")
        second = client.post(
            "/api/chroma-keyer/videos/preview",
            json={
                "input_path": reference,
                "timestamp_seconds": 0.0,
                "samples": [[0, 255, 0]],
                "params": {},
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert decoder.probe_calls == 2
    assert decoder.decode_calls == 2


def test_local_mode_does_not_apply_hosted_media_limits(tmp_path: Path) -> None:
    image_path = tmp_path / "local.png"
    image_path.write_bytes(_png_bytes((2, 1)))
    settings = HostedSettings(
        enabled=False,
        data_dir=tmp_path,
        redis_url="redis://unused",
        max_image_pixels=1,
    )
    with TestClient(
        create_app(
            hosted_settings=settings,
        )
    ) as client:
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": str(image_path)},
        )
        preview = client.get(response.json()["preview_url"])

    assert response.status_code == 200
    assert response.json()["width"] == 2
    assert preview.status_code == 200


def test_local_preview_does_not_acquire_redis_asset_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "local.png"
    image_path.write_bytes(_png_bytes((2, 1)))
    acquire_pin = Mock(side_effect=AssertionError("local reads must not use Redis"))
    monkeypatch.setattr(
        RedisTaskManager,
        "acquire_asset_read_pin",
        acquire_pin,
    )
    settings = HostedSettings(
        enabled=False,
        data_dir=tmp_path,
        redis_url="redis://unused",
    )

    with TestClient(create_app(hosted_settings=settings)) as client:
        response = client.post(
            "/api/chroma-keyer/images/inspect",
            json={"input_path": str(image_path)},
        )

    assert response.status_code == 200
    acquire_pin.assert_not_called()


def test_create_app_passes_independent_task_timeout_to_redis_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_connection = fakeredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(
        app_module.Redis,
        "from_url",
        lambda _url, **_kwargs: redis_connection,
    )
    settings = HostedSettings(
        enabled=True,
        data_dir=tmp_path,
        redis_url="redis://unused",
        retention_seconds=300,
        task_timeout_seconds=9000,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=4,
        max_session_bytes=settings.max_session_bytes,
        min_free_bytes=settings.min_free_bytes,
    )

    app = create_app(
        hosted_settings=settings,
        hosted_storage=HostedStorage(
            tmp_path,
            max_upload_bytes=settings.max_upload_bytes,
        ),
        resource_coordinator=coordinator,
    )

    assert app.state.task_manager.retention_seconds == 300
    assert app.state.task_manager.task_timeout_seconds == 9000
