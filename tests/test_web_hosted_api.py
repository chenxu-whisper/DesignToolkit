from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from time import sleep
from unittest.mock import Mock
from urllib.parse import quote

import fakeredis
import numpy as np
import pytest
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from PIL import Image
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue

from designtoolkit.chroma_keyer.video_models import VideoMetadata
from designtoolkit.web.app import create_app
from designtoolkit.web.hosted import HostedSettings, HostedStorage
from designtoolkit.web.redis_tasks import RedisTaskManager
from designtoolkit.web.resources import (
    AssetReleaseMismatchError,
    ResourceUnavailableError,
)
from designtoolkit.web.schemas import TaskStatus
from designtoolkit.web.tasks import TaskManager


class StubReservation:
    def __init__(
        self,
        coordinator: StubCoordinator,
        session_id: str,
        reserved_bytes: int,
    ) -> None:
        self.coordinator = coordinator
        self.session_id = session_id
        self.reserved_bytes = reserved_bytes
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

    def reserve_upload(
        self,
        session_id: str,
        requested_bytes: int,
        *,
        free_bytes: int,
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
        return StubReservation(self, session_id, requested_bytes)

    def acquire_session(self, _session_id: str) -> StubLease:
        self.active_leases += 1
        self.lease_acquisitions += 1
        return StubLease(self)

    def release_committed_asset(
        self,
        session_id: str,
        asset_id: str,
        released_bytes: int,
    ) -> None:
        self.release_calls += 1
        released = self.released_assets.get(asset_id)
        if released is not None:
            if released[0] != session_id:
                raise AssetReleaseMismatchError("Uploaded asset is not available")
            if released_bytes != 0 and released[1] != released_bytes:
                raise ResourceUnavailableError("Asset release metadata mismatch")
            return
        if released_bytes == 0:
            return
        current = self.committed.get(session_id, 0)
        if released_bytes > current:
            raise ResourceUnavailableError("Asset release bytes mismatch")
        remaining = current - released_bytes
        if remaining:
            self.committed[session_id] = remaining
        else:
            self.committed.pop(session_id, None)
        self.released_assets[asset_id] = (session_id, released_bytes)

    def is_committed_asset_released(self, session_id: str, asset_id: str) -> bool:
        self.release_query_calls += 1
        released = self.released_assets.get(asset_id)
        return released is not None and released[0] == session_id

    def finalize_asset_release(self, session_id: str, asset_id: str) -> None:
        self.finalize_release_calls += 1
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
    resource_coordinator: StubCoordinator | None = None,
    hosted_storage: HostedStorage | None = None,
    max_image_pixels: int = 100_000_000,
    max_video_width: int = 3840,
    max_video_height: int = 2160,
    max_video_duration_seconds: float = 1800.0,
    video_decoder: StubHostedVideoDecoder | None = None,
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
        )
    )


def _upload(
    client: TestClient,
    filename: str,
    content: bytes,
    *,
    content_length: int | None = None,
):
    return client.post(
        "/api/uploads",
        content=content,
        headers={
            "content-length": str(
                len(content) if content_length is None else content_length
            ),
            "content-type": "application/octet-stream",
            "x-upload-filename": quote(filename, safe=""),
        },
    )


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


def test_delete_upload_is_owner_scoped_idempotent_and_reclaims_quota(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, max_session_bytes=5) as owner:
        coordinator = owner.app.state.resource_coordinator
        uploaded = _upload(owner, "screen.png", b"image")
        reference = uploaded.json()["reference"]
        asset_id = reference.split(":", 2)[1]
        first_delete = owner.delete(f"/api/uploads/{asset_id}")
        second_delete = owner.delete(f"/api/uploads/{asset_id}")
        replacement = _upload(owner, "replacement.png", b"12345")

    assert first_delete.status_code == 204
    assert second_delete.status_code == 204
    assert replacement.status_code == 201
    assert coordinator.finalize_release_calls == 2
    assert coordinator.finalized_assets == {
        (owner.cookies["dt_session"], asset_id)
    }


def test_delete_absent_upload_best_effort_finalizes_known_release_marker(
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
        asset_id = uploaded.json()["reference"].split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        coordinator = owner.app.state.resource_coordinator
        deletion = storage.delete_asset(session_id, f"asset:{asset_id}")
        coordinator.release_committed_asset(
            session_id,
            deletion.asset_id,
            deletion.size,
        )
        storage.finalize_asset_deletion(session_id, asset_id)
        finalize_attempts = 0

        def fail_finalize(_session_id: str, _asset_id: str) -> None:
            nonlocal finalize_attempts
            finalize_attempts += 1
            raise RedisConnectionError("finalize unavailable")

        monkeypatch.setattr(
            coordinator,
            "finalize_asset_release",
            fail_finalize,
        )

        response = owner.delete(f"/api/uploads/{asset_id}")

    assert response.status_code == 204
    assert finalize_attempts == 1
    assert asset_id in coordinator.released_assets


def test_delete_upload_retries_quota_release_from_durable_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path, max_session_bytes=5) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        asset_id = uploaded.json()["reference"].split(":", 2)[1]
        session_id = owner.cookies["dt_session"]
        coordinator = owner.app.state.resource_coordinator
        original_release = coordinator.release_committed_asset
        attempts = 0

        def fail_once(
            release_session_id: str,
            release_asset_id: str,
            released_bytes: int,
        ) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RedisConnectionError("release unavailable")
            original_release(
                release_session_id,
                release_asset_id,
                released_bytes,
            )

        monkeypatch.setattr(coordinator, "release_committed_asset", fail_once)

        first_delete = owner.delete(f"/api/uploads/{asset_id}")
        tombstone = tmp_path / "sessions" / session_id / ".trash" / asset_id
        assert first_delete.status_code == 503
        assert tombstone.is_dir()
        blocked_replacement = _upload(owner, "blocked.png", b"12345")
        retry = owner.delete(f"/api/uploads/{asset_id}")
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
        asset_id = uploaded.json()["reference"].split(":", 2)[1]
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

        first_delete = owner.delete(f"/api/uploads/{asset_id}")
        assert first_delete.status_code == 503
        assert tombstone.is_dir()
        assert coordinator.finalize_release_calls == 0
        assert asset_id in coordinator.released_assets
        replacement = _upload(owner, "replacement.png", b"12345")
        retry = owner.delete(f"/api/uploads/{asset_id}")
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
        asset_id = uploaded.json()["reference"].split(":", 2)[1]
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

        first_delete = owner.delete(f"/api/uploads/{asset_id}")
        assert first_delete.status_code == 503
        assert tombstone.is_dir()
        assert not metadata.exists()

        retry = owner.delete(f"/api/uploads/{asset_id}")
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
        storage.delete_asset(session_id, reference)
        (tombstone / ".deletion.json").unlink()

        rejected = owner.delete(f"/api/uploads/{asset_id}")

    assert rejected.status_code == 503
    assert tombstone.is_dir()
    assert owner.app.state.resource_coordinator.committed[session_id] == 5


def test_concurrent_retries_recover_partial_tombstone_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(tmp_path, max_session_bytes=5) as owner:
        uploaded = _upload(owner, "screen.png", b"image")
        asset_id = uploaded.json()["reference"].split(":", 2)[1]
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
        assert owner.delete(f"/api/uploads/{asset_id}").status_code == 503

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(
                executor.map(
                    lambda _index: owner.delete(
                        f"/api/uploads/{asset_id}"
                    ).status_code,
                    range(2),
                )
            )

    assert statuses == [204, 204]
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
        asset_id = uploaded.json()["reference"].split(":", 2)[1]
        deleted = owner.delete(f"/api/uploads/{asset_id}")

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
        stranger_status = stranger.get(f"/api/tasks/{task_id}")

    assert created.status_code == 200
    assert own_status.status_code == 200
    assert own_status.json()["input_path"] == "clip.mp4"
    assert stranger_status.status_code == 404


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

    assert response.status_code == 200
    assert coordinator.lease_acquisitions == 1
    assert coordinator.active_leases == 0


def test_hosted_task_observes_session_heartbeat_failure_before_return(
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

    def slow_create(*args, **kwargs):
        sleep(0.04)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(manager, "create_video_frames_task", slow_create)
    with _client(
        tmp_path,
        task_manager=manager,
        resource_coordinator=coordinator,
    ) as client:
        reference = _upload(client, "clip.mp4", b"video").json()["reference"]
        coordinator.session_lease_heartbeat_interval = 0.01
        coordinator.fail_lease_refresh = True
        response = client.post(
            "/api/tasks/video-frames",
            json={"input_path": reference, "image_format": "png"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
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
        refreshes_after_response = coordinator.lease_refresh_calls
        sleep(0.03)

    assert response.status_code == 200
    assert coordinator.lease_acquisitions == 1
    assert coordinator.lease_release_calls == 1
    assert coordinator.active_leases == 0
    assert coordinator.lease_refresh_calls == refreshes_after_response


def test_hosted_download_releases_lease_when_response_send_fails(
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

    assert response.status_code == 200
    assert response.json()["width"] == 2
