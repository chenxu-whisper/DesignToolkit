from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from time import sleep

import fakeredis
import pytest

from designtoolkit.web.resources import (
    RedisResourceCoordinator,
    ResourceUnavailableError,
    SessionDeletingError,
)

SESSION_A = "a" * 32
SESSION_B = "b" * 32


class LuaRedis(fakeredis.FakeRedis):
    """Execute the coordinator's Lua contracts without fakeredis[lua]."""

    def __init__(self) -> None:
        super().__init__(decode_responses=False)
        self._script_lock = RLock()

    def eval(self, script, numkeys, *keys_and_args):  # noqa: ANN001, ANN201
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        operation = script.splitlines()[0].removeprefix("-- ")
        handler = getattr(self, f"_eval_{operation}")
        with self._script_lock:
            return handler(keys, args)

    def _eval_reserve_upload(self, keys, args):  # noqa: ANN001, ANN202
        state_key, index_key, token_key = keys
        (
            token,
            session_id,
            requested,
            free_bytes,
            max_uploads,
            max_session_bytes,
            min_free_bytes,
            ttl_ms,
        ) = args
        requested = int(requested)
        if self._number(state_key, "upload_count") >= int(max_uploads):
            return 1
        if (
            self._number(state_key, f"session:{session_id}:bytes") + requested
            > int(max_session_bytes)
        ):
            return 2
        if (
            int(free_bytes)
            - self._number(state_key, "reserved_bytes")
            - requested
            < int(min_free_bytes)
        ):
            return 3
        self.hset(
            token_key,
            mapping={
                "kind": "upload",
                "owner_id": session_id,
                "session_id": session_id,
                "bytes": requested,
            },
        )
        self.pexpire(token_key, int(ttl_ms))
        self.hset(index_key, token, f"upload|{session_id}|{requested}")
        self.hincrby(state_key, "reserved_bytes", requested)
        self.hincrby(state_key, "upload_count", 1)
        self.hincrby(state_key, f"session:{session_id}:bytes", requested)
        return 0

    def _eval_reserve_output(self, keys, args):  # noqa: ANN001, ANN202
        state_key, index_key, token_key = keys
        (
            token,
            task_id,
            session_id,
            requested,
            free_bytes,
            min_free_bytes,
            ttl_ms,
        ) = args
        requested = int(requested)
        if (
            int(free_bytes)
            - self._number(state_key, "reserved_bytes")
            - requested
            < int(min_free_bytes)
        ):
            return 1
        self.hset(
            token_key,
            mapping={
                "kind": "output",
                "owner_id": task_id,
                "session_id": session_id,
                "bytes": requested,
            },
        )
        self.pexpire(token_key, int(ttl_ms))
        self.hset(index_key, token, f"output|{session_id}|{requested}")
        self.hincrby(state_key, "reserved_bytes", requested)
        return 0

    def _eval_refresh_token(self, keys, args):  # noqa: ANN001, ANN202
        if not self.exists(keys[0]):
            return 0
        self.pexpire(keys[0], int(args[0]))
        return 1

    def _eval_release_reservation(self, keys, _args):  # noqa: ANN001, ANN202
        state_key, index_key, token_key = keys
        if not self.exists(token_key):
            return 0
        kind = self._text(self.hget(token_key, "kind"))
        session_id = self._text(self.hget(token_key, "session_id"))
        reserved_bytes = int(self.hget(token_key, "bytes"))
        self.delete(token_key)
        self.hdel(index_key, token_key.rsplit(":", 1)[-1])
        self._decrement(state_key, "reserved_bytes", reserved_bytes)
        if kind == "upload":
            self._decrement(state_key, "upload_count", 1)
            self._decrement(
                state_key,
                f"session:{session_id}:bytes",
                reserved_bytes,
            )
        return 1

    def _eval_reconcile_reservations(self, keys, args):  # noqa: ANN001, ANN202
        state_key, index_key = keys
        token_prefix = str(args[0])
        removed = 0
        for raw_token, raw_metadata in self.hgetall(index_key).items():
            token = self._text(raw_token)
            if self.exists(f"{token_prefix}{token}"):
                continue
            kind, session_id, byte_count = self._text(raw_metadata).split("|")
            reserved_bytes = int(byte_count)
            self.hdel(index_key, token)
            self._decrement(state_key, "reserved_bytes", reserved_bytes)
            if kind == "upload":
                self._decrement(state_key, "upload_count", 1)
                self._decrement(
                    state_key,
                    f"session:{session_id}:bytes",
                    reserved_bytes,
                )
            removed += 1
        return removed

    def _eval_acquire_session(self, keys, args):  # noqa: ANN001, ANN202
        state_key, leases_key, lease_key = keys
        token, session_id, ttl_ms, lease_prefix, cleanup_prefix = args
        self._reconcile_leases(state_key, leases_key, str(lease_prefix))
        deleting = self.hget(state_key, "deleting")
        if deleting is not None:
            deleting_token = self._text(deleting)
            if self.exists(f"{cleanup_prefix}{deleting_token}"):
                return 1
            self.hdel(state_key, "deleting")
        self.hset(lease_key, mapping={"session_id": session_id})
        self.pexpire(lease_key, int(ttl_ms))
        self.sadd(leases_key, token)
        self.hincrby(state_key, "active_count", 1)
        return 0

    def _eval_release_session(self, keys, _args):  # noqa: ANN001, ANN202
        state_key, leases_key, lease_key = keys
        if not self.exists(lease_key):
            return 0
        token = lease_key.rsplit(":", 1)[-1]
        self.delete(lease_key)
        if self.srem(leases_key, token):
            self._decrement(state_key, "active_count", 1)
        return 1

    def _eval_claim_cleanup(self, keys, args):  # noqa: ANN001, ANN202
        state_key, leases_key, cleanup_key = keys
        token, _ttl_ms, lease_prefix, cleanup_prefix = args
        self._reconcile_leases(state_key, leases_key, str(lease_prefix))
        deleting = self.hget(state_key, "deleting")
        if deleting is not None:
            deleting_token = self._text(deleting)
            if self.exists(f"{cleanup_prefix}{deleting_token}"):
                return 1
            self.hdel(state_key, "deleting")
        if self._number(state_key, "active_count") != 0:
            return 1
        self.hset(state_key, "deleting", token)
        self.set(cleanup_key, "1")
        return 0

    def _eval_release_cleanup(self, keys, args):  # noqa: ANN001, ANN202
        state_key, cleanup_key = keys
        token = str(args[0])
        deleting = self.hget(state_key, "deleting")
        if deleting is None or self._text(deleting) != token:
            return 0
        self.hdel(state_key, "deleting")
        self.delete(cleanup_key)
        return 1

    def _reconcile_leases(
        self,
        state_key: str,
        leases_key: str,
        lease_prefix: str,
    ) -> None:
        for raw_token in self.smembers(leases_key):
            token = self._text(raw_token)
            if not self.exists(f"{lease_prefix}{token}"):
                self.srem(leases_key, token)
                self._decrement(state_key, "active_count", 1)

    def _number(self, key: str, field: str) -> int:
        value = self.hget(key, field)
        return int(value) if value is not None else 0

    def _decrement(self, key: str, field: str, amount: int) -> None:
        current = self._number(key, field)
        self.hset(key, field, max(0, current - amount))

    @staticmethod
    def _text(value: bytes | str) -> str:
        return value.decode() if isinstance(value, bytes) else value


@pytest.fixture
def redis_connection() -> LuaRedis:
    return LuaRedis()


def coordinator(
    redis_connection: LuaRedis,
    *,
    max_uploads: int = 2,
    max_session_bytes: int = 10,
    min_free_bytes: int = 5,
    reservation_ttl: float = 10,
    lease_ttl: float = 10,
) -> RedisResourceCoordinator:
    return RedisResourceCoordinator(
        redis_connection=redis_connection,
        max_upload_concurrency=max_uploads,
        max_session_bytes=max_session_bytes,
        min_free_bytes=min_free_bytes,
        reservation_ttl=reservation_ttl,
        lease_ttl=lease_ttl,
    )


def test_upload_reservations_enforce_global_concurrency_atomically(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, max_uploads=2, max_session_bytes=100)

    def reserve(index: int) -> object | None:
        try:
            return resources.reserve_upload(
                f"{index:032x}",
                1,
                free_bytes=100,
            )
        except ResourceUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        reservations = list(executor.map(reserve, range(8)))

    accepted = [reservation for reservation in reservations if reservation]
    assert len(accepted) == 2
    for reservation in accepted:
        reservation.release()


def test_upload_reservations_enforce_per_session_bytes(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    first = resources.reserve_upload(SESSION_A, 6, free_bytes=100)

    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 5, free_bytes=100)

    second = resources.reserve_upload(SESSION_B, 4, free_bytes=100)
    first.release()
    second.release()


def test_upload_capacity_includes_all_active_reservations(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        max_uploads=3,
        max_session_bytes=100,
    )
    output = resources.reserve_output(
        "task-1",
        SESSION_A,
        6,
        free_bytes=20,
    )

    with pytest.raises(ResourceUnavailableError, match="disk"):
        resources.reserve_upload(SESSION_B, 10, free_bytes=20)

    output.release()


def test_output_reservations_preserve_effective_minimum_free_bytes(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, min_free_bytes=3)
    first = resources.reserve_output(
        "task-1",
        SESSION_A,
        5,
        free_bytes=15,
        min_free_bytes=5,
    )

    with pytest.raises(ResourceUnavailableError, match="disk"):
        resources.reserve_output(
            "task-2",
            SESSION_B,
            6,
            free_bytes=15,
            min_free_bytes=5,
        )

    first.release()


def test_reservation_release_is_idempotent_and_token_scoped(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, max_uploads=2, max_session_bytes=100)
    first = resources.reserve_upload(SESSION_A, 3, free_bytes=100)
    second = resources.reserve_upload(SESSION_A, 4, free_bytes=100)

    first.release()
    first.release()
    third = resources.reserve_upload(SESSION_A, 5, free_bytes=100)
    with pytest.raises(ResourceUnavailableError, match="concurrency"):
        resources.reserve_upload(SESSION_B, 1, free_bytes=100)

    second.release()
    third.release()


def test_reservation_refresh_extends_token_ttl(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, reservation_ttl=2)
    reservation = resources.reserve_upload(SESSION_A, 1, free_bytes=100)
    token_key = f"dt:resource:reservation:{reservation.token}"
    redis_connection.pexpire(token_key, 10)

    reservation.refresh()

    assert redis_connection.pttl(token_key) > 1_000


def test_reconcile_repairs_counters_after_reservation_expiry(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, max_uploads=1)
    expired = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    redis_connection.delete(f"dt:resource:reservation:{expired.token}")

    assert resources.reconcile_reservations() == 1
    replacement = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    assert resources.reconcile_reservations() == 0
    replacement.release()


def test_context_managers_release_reservation_and_session_lease(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, max_uploads=1)

    with resources.reserve_upload(SESSION_A, 1, free_bytes=100):
        pass
    with resources.acquire_session(SESSION_A):
        pass

    replacement = resources.reserve_upload(SESSION_B, 1, free_bytes=100)
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    replacement.release()
    resources.release_cleanup_claim(SESSION_A, claim)


def test_cleanup_claim_cannot_overlap_active_session_lease(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection)
    lease = resources.acquire_session(SESSION_A)

    assert resources.claim_session_cleanup(SESSION_A) is None
    lease.release()
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    resources.release_cleanup_claim(SESSION_A, claim)


def test_cleanup_claim_blocks_new_leases_until_matching_release(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection)
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None

    with pytest.raises(SessionDeletingError):
        resources.acquire_session(SESSION_A)
    resources.release_cleanup_claim(SESSION_A, "0" * 32)
    with pytest.raises(SessionDeletingError):
        resources.acquire_session(SESSION_A)

    resources.release_cleanup_claim(SESSION_A, claim)
    resources.acquire_session(SESSION_A).release()


def test_cleanup_claim_remains_until_released(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection, lease_ttl=0.01)
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    sleep(0.02)

    with pytest.raises(SessionDeletingError):
        resources.acquire_session(SESSION_A)

    resources.release_cleanup_claim(SESSION_A, claim)


def test_session_lease_release_is_idempotent_and_token_scoped(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection)
    first = resources.acquire_session(SESSION_A)
    second = resources.acquire_session(SESSION_A)

    first.release()
    first.release()
    assert resources.claim_session_cleanup(SESSION_A) is None

    second.release()
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    resources.release_cleanup_claim(SESSION_A, claim)


def test_expired_session_lease_is_reconciled_before_cleanup_claim(
    redis_connection: LuaRedis,
) -> None:
    resources = coordinator(redis_connection)
    lease = resources.acquire_session(SESSION_A)
    redis_connection.delete(f"dt:resource:lease:{lease.token}")

    claim = resources.claim_session_cleanup(SESSION_A)

    assert claim is not None
    resources.release_cleanup_claim(SESSION_A, claim)


@pytest.mark.parametrize("requested_bytes", [0, -1])
def test_reservations_require_positive_sizes(
    redis_connection: LuaRedis,
    requested_bytes: int,
) -> None:
    resources = coordinator(redis_connection)

    with pytest.raises(ValueError, match="positive"):
        resources.reserve_upload(SESSION_A, requested_bytes, free_bytes=100)
    with pytest.raises(ValueError, match="positive"):
        resources.reserve_output(
            "task-1",
            SESSION_A,
            requested_bytes,
            free_bytes=100,
        )


@pytest.mark.parametrize("session_id", ["A" * 32, "a" * 31, "../session"])
def test_public_operations_require_lowercase_32_hex_session_ids(
    redis_connection: LuaRedis,
    session_id: str,
) -> None:
    resources = coordinator(redis_connection)

    with pytest.raises(ValueError, match="session"):
        resources.reserve_upload(session_id, 1, free_bytes=100)
    with pytest.raises(ValueError, match="session"):
        resources.reserve_output("task-1", session_id, 1, free_bytes=100)
    with pytest.raises(ValueError, match="session"):
        resources.acquire_session(session_id)
    with pytest.raises(ValueError, match="session"):
        resources.claim_session_cleanup(session_id)
