from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import fakeredis
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from rq import Queue

from designtoolkit.web.app import create_app
from designtoolkit.web.hosted import HostedSettings, HostedStorage
from designtoolkit.web.redis_tasks import RedisTaskManager
from designtoolkit.web.resources import ResourceUnavailableError
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

    def release(self) -> None:
        if self.active:
            self.active = False
            self.coordinator.active_uploads -= 1


class StubLease:
    def refresh(self) -> None:
        pass

    def release(self) -> None:
        pass


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

    def reserve_upload(
        self,
        session_id: str,
        requested_bytes: int,
        *,
        free_bytes: int,
    ) -> StubReservation:
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
        return StubLease()


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 1), (0, 255, 0)).save(output, format="PNG")
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
        secure_cookies=False,
        upload_requests_per_hour=upload_requests_per_hour,
    )
    coordinator = StubCoordinator(
        max_upload_concurrency=max_upload_concurrency,
        max_session_bytes=max_session_bytes,
        min_free_bytes=min_free_bytes,
    )
    return TestClient(
        create_app(
            task_manager=task_manager,
            hosted_settings=settings,
            hosted_storage=HostedStorage(tmp_path, max_upload_bytes=max_bytes),
            resource_coordinator=coordinator,
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
            _upload(client, name, b"image").json()["reference"]
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
