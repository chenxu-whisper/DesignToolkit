from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    UploadRejectedError,
    prepare_download_artifact,
)
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
    ) -> None:
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
        self.charge_count = 0
        self.operations: list[str] = []
        self.hidden_at_commit: list[Path] = []
        self.visible_at_commit: list[Path] = []

    def refresh(self) -> None:
        self.refresh_count += 1

    def commit(self) -> None:
        self.operations.append("commit")
        if self.root is not None:
            self.hidden_at_commit = list(self.root.rglob(".uploading"))
            self.visible_at_commit = [
                path
                for path in self.root.rglob("*")
                if path.is_file() and path.name != ".uploading"
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
    assert reservation.refresh_count == 2
    assert lease.refresh_count == 2
    assert reservation.committed is True
    assert reservation.finalized is True
    assert reservation.operations == ["commit", "rename", "finalize"]
    assert len(reservation.hidden_at_commit) == 1
    assert reservation.visible_at_commit == []
    assert reservation.released is False
    with pytest.raises(UploadRejectedError, match="not available"):
        storage.resolve("b" * 32, asset.reference)


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

    with pytest.raises(UploadRejectedError, match="not available"):
        storage.delete_asset(stranger, asset.reference)

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
    assert storage.delete_asset(owner, asset.reference).size == 0


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
