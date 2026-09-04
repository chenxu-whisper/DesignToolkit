from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from threading import Event
from time import sleep
from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    UploadRejectedError,
    _CleanupClaimHeartbeat,
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


class RecordingCleanupCoordinator:
    def __init__(
        self,
        *,
        active_sessions: set[str] | None = None,
        committed_bytes: dict[str, int] | None = None,
    ) -> None:
        self.cleanup_claim_heartbeat_interval = 0.01
        self.active_sessions = active_sessions or set()
        self.committed_bytes = committed_bytes if committed_bytes is not None else {}
        self.claims: dict[str, str] = {}
        self.claim_calls: list[str] = []
        self.refresh_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_generations: set[str] = set()
        self.reconciled_legacy: dict[str, int] = {}

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
        storage.cleanup_expired(
            retention_seconds=2_000,
            coordinator=coordinator,
        )

    assert rename_called is False
    assert session_dir.is_dir()
    assert coordinator.claims == {}


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
        storage.cleanup_expired(
            retention_seconds=2_000,
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


def test_cleanup_preserves_orphaned_quarantine_while_new_lease_is_active(
    tmp_path: Path,
) -> None:
    storage = HostedStorage(tmp_path, max_upload_bytes=16)
    session_id = "a" * 32
    orphan = tmp_path / ".trash" / f"{session_id}-{'1' * 32}"
    orphan.mkdir(parents=True)
    (orphan / "asset.bin").write_bytes(b"old")
    coordinator = RecordingCleanupCoordinator(active_sessions={session_id})

    removed = storage.cleanup_expired(
        retention_seconds=2_000,
        coordinator=coordinator,
    )

    assert removed == 0
    assert orphan.is_dir()
    assert coordinator.claim_calls == [session_id]
    assert coordinator.cleared_sessions == []


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
        storage.cleanup_expired(
            retention_seconds=2_000,
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
        def __init__(self, path: Path, *_args, **_kwargs) -> None:
            self.path = path

        def __enter__(self):
            self.path.write_bytes(b"partial zip")
            return self

        def write(self, _path: Path, *, arcname: str) -> None:
            del arcname
            if failure_mode == "write":
                raise OSError("zip write failed")

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            if failure_mode == "close":
                raise OSError("zip close failed")

    monkeypatch.setattr("designtoolkit.web.hosted.ZipFile", FailingZipFile)

    with pytest.raises(OSError, match=f"zip {failure_mode} failed"):
        prepare_download_artifact("task-1", str(output_dir))

    assert not archive_path.exists()
    assert source.exists() is (failure_mode == "write")


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
