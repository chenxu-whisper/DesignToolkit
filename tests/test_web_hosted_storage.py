from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    UploadRejectedError,
    prepare_download_artifact,
)
from designtoolkit.web.resources import ResourceUnavailableError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingReservation:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.committed = False
        self.released = False
        self.refresh_count = 0
        self.fail_commit = fail_commit

    def refresh(self) -> None:
        self.refresh_count += 1

    def commit(self) -> None:
        if self.fail_commit:
            raise ResourceUnavailableError("Upload reservation expired")
        self.committed = True

    def release(self) -> None:
        self.released = True


class RecordingLease:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh(self) -> None:
        self.refresh_count += 1


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


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
    assert settings.reservation_ttl_seconds >= (
        2 * settings.reservation_heartbeat_seconds
    )
    assert settings.lease_ttl_seconds >= 2 * settings.lease_heartbeat_seconds
    assert settings.max_queued_tasks == 20
    assert settings.retention_seconds == 2 * 60 * 60


@pytest.mark.anyio
async def test_upload_is_streamed_to_an_opaque_session_asset(tmp_path: Path) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    reservation = RecordingReservation()
    lease = RecordingLease()

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
    assert reservation.refresh_count == 2
    assert lease.refresh_count == 2
    assert reservation.committed is True
    assert reservation.released is False
    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve("b" * 32, asset.reference)


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
    reservation = RecordingReservation(fail_commit=True)

    with pytest.raises(ResourceUnavailableError, match="expired"):
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
    expired = tmp_path / "sessions" / "expired"
    current = tmp_path / "sessions" / "current"
    expired.mkdir(parents=True)
    current.mkdir(parents=True)
    (expired / "asset.bin").write_bytes(b"old")
    (current / "asset.bin").write_bytes(b"new")
    monkeypatch.setattr(
        "designtoolkit.web.hosted.time",
        lambda: 10_000.0,
    )
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda path: 1_000.0 if path.name == "expired" else 9_500.0,
    )

    removed = storage.cleanup_expired(retention_seconds=2_000)

    assert removed == 1
    assert not expired.exists()
    assert current.is_dir()


def test_cleanup_preserves_expired_session_with_active_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    active = tmp_path / "sessions" / ("a" * 32)
    active.mkdir(parents=True)
    monkeypatch.setattr("designtoolkit.web.hosted.time", lambda: 10_000.0)
    monkeypatch.setattr(
        "designtoolkit.web.hosted._directory_modified_at",
        lambda _path: 1_000.0,
    )

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        protected_sessions={"a" * 32},
    )

    assert removed == 0
    assert active.is_dir()


def test_directory_result_is_packaged_as_a_zip(tmp_path: Path) -> None:
    output_dir = tmp_path / "frames"
    output_dir.mkdir()
    (output_dir / "frame_000001.png").write_bytes(b"png")

    artifact = prepare_download_artifact("task-1", str(output_dir))

    assert artifact == tmp_path / "task-1.zip"
    assert artifact.is_file()
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
