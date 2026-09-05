from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, RLock
from time import monotonic, sleep
from types import SimpleNamespace

import fakeredis
import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from designtoolkit.web.resources import (
    CleanupMarker,
    RedisResourceCoordinator,
    ReservationState,
    ResourceUnavailableError,
    SessionDeletingError,
)

SESSION_A = "a" * 32
SESSION_B = "b" * 32


class DeterministicRedis(fakeredis.FakeRedis):
    """Deterministically model coordinator behavior without executing Lua."""

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

    def register_script(self, script):  # noqa: ANN001, ANN201
        def execute(*, keys, args):
            return self.eval(script, len(keys), *keys, *args)

        return execute

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
        free_bytes = int(free_bytes)
        min_free_bytes = int(min_free_bytes)
        if self.exists(token_key):
            expected = {
                b"kind": b"upload",
                b"owner_id": str(session_id).encode(),
                b"session_id": str(session_id).encode(),
                b"bytes": str(requested).encode(),
                b"free_bytes": str(free_bytes).encode(),
                b"min_free_bytes": str(min_free_bytes).encode(),
            }
            return 4 if self.hgetall(token_key) == expected else 5
        if self._number(state_key, "upload_count") >= int(max_uploads):
            return 1
        if self._number(
            state_key,
            f"session:{session_id}:reserved_bytes",
        ) + self._number(
            state_key,
            f"session:{session_id}:committed_bytes",
        ) + requested > int(max_session_bytes):
            return 2
        observed_free = self._optional_number(
            state_key,
            "observed_free_bytes",
            free_bytes,
        )
        active_min_free = self._optional_number(
            state_key,
            "min_free_bytes",
            min_free_bytes,
        )
        effective_free = min(observed_free, free_bytes)
        effective_min_free = max(active_min_free, min_free_bytes)
        if (
            effective_free - self._number(state_key, "reserved_bytes") - requested
            < effective_min_free
        ):
            return 3
        self.hset(
            token_key,
            mapping={
                "kind": "upload",
                "owner_id": session_id,
                "session_id": session_id,
                "bytes": requested,
                "free_bytes": free_bytes,
                "min_free_bytes": min_free_bytes,
            },
        )
        self.pexpire(token_key, int(ttl_ms))
        self.hset(
            index_key,
            token,
            f"upload|{session_id}|{requested}|{free_bytes}|{min_free_bytes}",
        )
        self.hincrby(state_key, "reserved_bytes", requested)
        self.hincrby(state_key, "upload_count", 1)
        self.hincrby(
            state_key,
            f"session:{session_id}:reserved_bytes",
            requested,
        )
        self.hset(state_key, "observed_free_bytes", effective_free)
        self.hset(state_key, "min_free_bytes", effective_min_free)
        return 0

    def _eval_reserve_output(self, keys, args):  # noqa: ANN001, ANN202
        state_key, index_key, token_key, task_key = keys
        (
            token,
            task_id,
            session_id,
            requested,
            free_bytes,
            min_free_bytes,
            ttl_ms,
            heartbeat_at,
        ) = args
        requested = int(requested)
        free_bytes = int(free_bytes)
        min_free_bytes = int(min_free_bytes)
        observed_free = self._optional_number(
            state_key,
            "observed_free_bytes",
            free_bytes,
        )
        active_min_free = self._optional_number(
            state_key,
            "min_free_bytes",
            min_free_bytes,
        )
        effective_free = min(observed_free, free_bytes)
        effective_min_free = max(active_min_free, min_free_bytes)
        if (
            effective_free - self._number(state_key, "reserved_bytes") - requested
            < effective_min_free
        ):
            return 1
        self.hset(
            token_key,
            mapping={
                "kind": "output",
                "owner_id": task_id,
                "session_id": session_id,
                "bytes": requested,
                "free_bytes": free_bytes,
                "min_free_bytes": min_free_bytes,
            },
        )
        self.pexpire(token_key, int(ttl_ms))
        self.hset(
            index_key,
            token,
            f"output|{session_id}|{requested}|{free_bytes}|{min_free_bytes}",
        )
        self.hincrby(state_key, "reserved_bytes", requested)
        self.hincrby(state_key, "output_reserved_bytes", requested)
        self.hset(state_key, "observed_free_bytes", effective_free)
        self.hset(state_key, "min_free_bytes", effective_min_free)
        self.hset(
            task_key,
            mapping={
                "session_id": session_id,
                "output_token": token,
                "heartbeat_at": heartbeat_at,
            },
        )
        self.pexpire(task_key, int(ttl_ms))
        return 0

    def _eval_refresh_token(self, keys, args):  # noqa: ANN001, ANN202
        if not self.exists(keys[0]):
            return 0
        self.pexpire(keys[0], int(args[0]))
        return 1

    def _eval_release_reservation(self, keys, _args):  # noqa: ANN001, ANN202
        state_key, index_key, token_key, task_key = keys
        token = token_key.rsplit(":", 1)[-1]
        if self.hget(task_key, "output_token") == token.encode():
            self.hdel(task_key, "output_token")
            if not self.hexists(task_key, "lease_token"):
                self.delete(task_key)
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
                f"session:{session_id}:reserved_bytes",
                reserved_bytes,
            )
        elif kind == "output":
            self._decrement(
                state_key,
                "output_reserved_bytes",
                reserved_bytes,
            )
        self._recompute_capacity(state_key, index_key)
        return 1

    def _eval_commit_reservation(self, keys, args):  # noqa: ANN001, ANN202
        state_key, index_key, token_key, committed_key = keys
        if self.hget(committed_key, "state") == b"committed":
            return 2
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
                f"session:{session_id}:reserved_bytes",
                reserved_bytes,
            )
            self.hincrby(
                state_key,
                f"session:{session_id}:committed_bytes",
                reserved_bytes,
            )
        self.hset(
            committed_key,
            mapping={
                "state": "committed",
                "kind": kind,
                "session_id": session_id,
                "bytes": reserved_bytes,
            },
        )
        self.pexpire(committed_key, int(args[1]))
        self._recompute_capacity(state_key, index_key)
        return 1

    def _eval_rollback_commit(self, keys, args):  # noqa: ANN001, ANN202
        state_key, committed_key = keys
        if not self.exists(committed_key):
            return 0
        if self.hget(committed_key, "state") == b"rolled_back":
            return 2
        kind = self._text(self.hget(committed_key, "kind"))
        session_id = self._text(self.hget(committed_key, "session_id"))
        committed_bytes = int(self.hget(committed_key, "bytes"))
        if kind == "upload":
            self._decrement(
                state_key,
                f"session:{session_id}:committed_bytes",
                committed_bytes,
            )
        self.hset(committed_key, "state", "rolled_back")
        self.pexpire(committed_key, int(args[0]))
        return 1

    def _eval_finalize_commit(self, keys, _args):  # noqa: ANN001, ANN202
        return self.delete(keys[0])

    def _eval_reservation_state(self, keys, _args):  # noqa: ANN001, ANN202
        token_key, committed_key = keys
        if self.exists(token_key):
            return 1
        if self.hget(committed_key, "state") == b"committed":
            return 2
        return 0

    def _eval_reconcile_reservations(self, keys, args):  # noqa: ANN001, ANN202
        state_key, index_key = keys
        token_prefix = str(args[0])
        removed = 0
        for raw_token, raw_metadata in self.hgetall(index_key).items():
            token = self._text(raw_token)
            if self.exists(f"{token_prefix}{token}"):
                continue
            kind, session_id, byte_count, *_capacity = self._text(raw_metadata).split(
                "|"
            )
            reserved_bytes = int(byte_count)
            self.hdel(index_key, token)
            self._decrement(state_key, "reserved_bytes", reserved_bytes)
            if kind == "upload":
                self._decrement(state_key, "upload_count", 1)
                self._decrement(
                    state_key,
                    f"session:{session_id}:reserved_bytes",
                    reserved_bytes,
                )
            elif kind == "output":
                self._decrement(
                    state_key,
                    "output_reserved_bytes",
                    reserved_bytes,
                )
            removed += 1
        self._recompute_capacity(state_key, index_key)
        return removed

    def _eval_clear_session_usage(self, keys, args):  # noqa: ANN001, ANN202
        state_key, marker_index_key = keys
        session_id = str(args[0])
        marker_keys = self.smembers(marker_index_key)
        if marker_keys:
            self.delete(*marker_keys)
        self.delete(marker_index_key)
        self.hdel(
            state_key,
            f"session:{session_id}:committed_bytes",
        )
        return 1

    def _eval_capture_session_usage(self, keys, args):  # noqa: ANN001, ANN202
        state_key, session_state_key, marker_index_key, cleanup_key = keys
        session_id, claim_token = (str(value) for value in args)
        deleting = self.hget(session_state_key, "deleting")
        if (
            deleting is None
            or self._text(deleting) != claim_token
            or not self.exists(cleanup_key)
        ):
            return [0]
        marker_prefix = f"dt:resource:asset-released:{session_id}:"
        marker_keys = sorted(
            self._text(marker_key)
            for marker_key in self.smembers(marker_index_key)
            if self._text(marker_key).startswith(marker_prefix)
        )
        return [
            1,
            self._number(
                state_key,
                f"session:{session_id}:committed_bytes",
            ),
            *marker_keys,
        ]

    def _eval_clear_session_usage_generation(  # noqa: ANN001, ANN202
        self,
        keys,
        args,
    ):
        (
            state_key,
            session_state_key,
            cleanup_key,
            marker_index_key,
            generation_key,
        ) = keys
        (
            session_id,
            claim_token,
            _generation_id,
            committed_bytes,
            marker_prefix,
            *asset_ids,
        ) = (str(value) for value in args)
        deleting = self.hget(session_state_key, "deleting")
        if (
            deleting is None
            or self._text(deleting) != claim_token
            or not self.exists(cleanup_key)
        ):
            return 3
        if self.exists(generation_key):
            marker_type = self.type(generation_key)
            if marker_type == b"string":
                self.delete(generation_key)
                self.hset(
                    generation_key,
                    mapping={
                        "session_id": session_id,
                        "generation_id": _generation_id,
                    },
                )
            elif marker_type == b"hash":
                if (
                    self._text(self.hget(generation_key, "session_id"))
                    != session_id
                    or self._text(self.hget(generation_key, "generation_id"))
                    != _generation_id
                ):
                    return 4
                if self.hget(generation_key, "finalized") != b"1":
                    self.persist(generation_key)
            else:
                return 4
            return 0
        committed_bytes = int(committed_bytes)
        field = f"session:{session_id}:committed_bytes"
        current = self._number(state_key, field)
        if committed_bytes > current:
            return 2
        self._decrement(state_key, field, committed_bytes)
        for asset_id in asset_ids:
            marker_key = f"{marker_prefix}{session_id}:{asset_id}"
            self.delete(marker_key)
            self.srem(marker_index_key, marker_key)
        self.hset(
            generation_key,
            mapping={
                "session_id": session_id,
                "generation_id": _generation_id,
            },
        )
        return 1

    def _eval_finalize_session_usage_generation(  # noqa: ANN001, ANN202
        self,
        keys,
        args,
    ):
        if not self.exists(keys[0]):
            return 0
        if self.hget(keys[0], "finalized") == b"1":
            return 0
        self.hset(keys[0], "finalized", 1)
        self.pexpire(keys[0], int(args[0]))
        return 1

    def _eval_reconcile_legacy_session_usage(  # noqa: ANN001, ANN202
        self,
        keys,
        args,
    ):
        state_key, session_state_key, cleanup_key, tombstone_key = keys
        (
            session_id,
            claim_token,
            current_committed_bytes,
            legacy_physical_bytes,
        ) = (str(value) for value in args)
        deleting = self.hget(session_state_key, "deleting")
        if (
            deleting is None
            or self._text(deleting) != claim_token
            or not self.exists(cleanup_key)
        ):
            return [3]
        if self.exists(tombstone_key):
            if self.hget(tombstone_key, "session_id") != session_id.encode():
                return [2]
            if self.hget(tombstone_key, "finalized") != b"1":
                self.persist(tombstone_key)
            return [0, int(self.hget(tombstone_key, "legacy_bytes"))]
        current_committed_bytes = int(current_committed_bytes)
        legacy_physical_bytes = int(legacy_physical_bytes)
        field = f"session:{session_id}:committed_bytes"
        committed = self._number(state_key, field)
        legacy_bytes = committed - current_committed_bytes
        if legacy_bytes < 0 or legacy_bytes > legacy_physical_bytes:
            return [2]
        if current_committed_bytes:
            self.hset(state_key, field, current_committed_bytes)
        else:
            self.hdel(state_key, field)
        self.hset(
            tombstone_key,
            mapping={
                "session_id": session_id,
                "current_bytes": current_committed_bytes,
                "legacy_bytes": legacy_bytes,
            },
        )
        return [1, legacy_bytes]

    def _eval_finalize_legacy_session_usage(self, keys, args):  # noqa: ANN001, ANN202
        if not self.exists(keys[0]):
            return 0
        if self.hget(keys[0], "finalized") == b"1":
            return 0
        self.hset(keys[0], "finalized", 1)
        self.pexpire(keys[0], int(args[0]))
        return 1

    def _eval_release_committed_asset(self, keys, args):  # noqa: ANN001, ANN202
        state_key, marker_key, marker_index_key = keys
        session_id, asset_id, released_bytes = args
        released_bytes = int(released_bytes)
        if self.exists(marker_key):
            existing_session = self._text(self.hget(marker_key, "session_id"))
            existing_asset = self._text(self.hget(marker_key, "asset_id"))
            existing_bytes = int(self.hget(marker_key, "bytes"))
            if existing_session != session_id:
                return 2
            if existing_asset != asset_id:
                return 2
            if released_bytes != 0 and int(existing_bytes) != released_bytes:
                return 3
            if self.hget(marker_key, "finalized") != b"1":
                self.sadd(marker_index_key, marker_key)
            return 0
        field = f"session:{session_id}:committed_bytes"
        current = self._number(state_key, field)
        if released_bytes == 0:
            return 4
        if released_bytes > current:
            return 3
        remaining = current - released_bytes
        if remaining:
            self.hset(state_key, field, remaining)
        else:
            self.hdel(state_key, field)
        self.hset(
            marker_key,
            mapping={
                "session_id": session_id,
                "asset_id": asset_id,
                "bytes": released_bytes,
            },
        )
        self.sadd(marker_index_key, marker_key)
        return 1

    def _eval_is_committed_asset_released(  # noqa: ANN001, ANN202
        self,
        keys,
        args,
    ):
        marker_key = keys[0]
        if not self.exists(marker_key):
            return 0
        existing_session = self._text(self.hget(marker_key, "session_id"))
        existing_asset = self._text(self.hget(marker_key, "asset_id"))
        return int(existing_session == args[0] and existing_asset == args[1])

    def _eval_finalize_asset_release(self, keys, args):  # noqa: ANN001, ANN202
        marker_key, marker_index_key = keys
        session_id, asset_id, marker_ttl_ms = args
        if not self.exists(marker_key):
            return 0
        existing_session = self._text(self.hget(marker_key, "session_id"))
        existing_asset = self._text(self.hget(marker_key, "asset_id"))
        if existing_session != session_id or existing_asset != asset_id:
            return 2
        self.srem(marker_index_key, marker_key)
        if self.hget(marker_key, "finalized") == b"1":
            return 0
        self.hset(marker_key, "finalized", 1)
        self.pexpire(marker_key, int(marker_ttl_ms))
        return 1

    def _eval_acquire_session(self, keys, args):  # noqa: ANN001, ANN202
        state_key, leases_key, lease_key, task_key = keys
        (
            token,
            session_id,
            ttl_ms,
            lease_prefix,
            cleanup_prefix,
            task_id,
            heartbeat_at,
        ) = args
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
        if task_id:
            self.hset(
                task_key,
                mapping={
                    "session_id": session_id,
                    "lease_token": token,
                    "heartbeat_at": heartbeat_at,
                },
            )
            self.pexpire(task_key, int(ttl_ms))
        return 0

    def _eval_release_session(self, keys, _args):  # noqa: ANN001, ANN202
        state_key, leases_key, lease_key, task_key = keys
        token = lease_key.rsplit(":", 1)[-1]
        if self.hget(task_key, "lease_token") == token.encode():
            self.hdel(task_key, "lease_token")
            if not self.hexists(task_key, "output_token"):
                self.delete(task_key)
        self.delete(lease_key)
        if self.srem(leases_key, token):
            self._decrement(state_key, "active_count", 1)
        return 1

    def _eval_release_task_pin(self, keys, args):  # noqa: ANN001, ANN202
        pin_key, task_pins_key = keys
        session_id, task_id = args
        if not self.exists(pin_key):
            return 0
        if self._text(self.hget(pin_key, "session_id")) != session_id:
            return 2
        if self._text(self.hget(pin_key, "task_id")) != task_id:
            return 2
        self.delete(pin_key)
        self.srem(task_pins_key, task_id)
        if not self.scard(task_pins_key):
            self.delete(task_pins_key)
        return 1

    def _eval_claim_cleanup(self, keys, args):  # noqa: ANN001, ANN202
        state_key, leases_key, cleanup_key, task_pins_key = keys
        (
            token,
            ttl_ms,
            lease_prefix,
            cleanup_prefix,
            task_pin_prefix,
            session_id,
        ) = args
        self._reconcile_leases(state_key, leases_key, str(lease_prefix))
        deleting = self.hget(state_key, "deleting")
        if deleting is not None:
            deleting_token = self._text(deleting)
            if self.exists(f"{cleanup_prefix}{deleting_token}"):
                return 1
            self.hdel(state_key, "deleting")
        if self._number(state_key, "active_count") != 0:
            return 1
        for raw_task_id in self.smembers(task_pins_key):
            task_id = self._text(raw_task_id)
            pin_key = f"{task_pin_prefix}{task_id}"
            pin_session = self.hget(pin_key, "session_id")
            if (
                not self.exists(pin_key)
                or self._text(pin_session) != session_id
            ):
                self.srem(task_pins_key, task_id)
        if self.scard(task_pins_key):
            return 1
        self.hset(state_key, "deleting", token)
        self.set(cleanup_key, "1")
        self.pexpire(cleanup_key, int(ttl_ms))
        return 0

    def _eval_refresh_cleanup(self, keys, args):  # noqa: ANN001, ANN202
        state_key, cleanup_key = keys
        token, ttl_ms = args
        deleting = self.hget(state_key, "deleting")
        if (
            deleting is None
            or self._text(deleting) != str(token)
            or not self.exists(cleanup_key)
        ):
            return 0
        self.pexpire(cleanup_key, int(ttl_ms))
        return 1

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

    def _optional_number(self, key: str, field: str, default: int) -> int:
        value = self.hget(key, field)
        return int(value) if value is not None else default

    def _recompute_capacity(self, state_key: str, index_key: str) -> None:
        observations: list[int] = []
        floors: list[int] = []
        for raw_metadata in self.hvals(index_key):
            parts = self._text(raw_metadata).split("|")
            if len(parts) == 5:
                observations.append(int(parts[3]))
                floors.append(int(parts[4]))
        if observations:
            self.hset(state_key, "observed_free_bytes", min(observations))
            self.hset(state_key, "min_free_bytes", max(floors))
        else:
            self.hdel(state_key, "observed_free_bytes", "min_free_bytes")

    def _decrement(self, key: str, field: str, amount: int) -> None:
        current = self._number(key, field)
        remaining = current - amount
        if remaining > 0:
            self.hset(key, field, remaining)
        else:
            self.hdel(key, field)

    @staticmethod
    def _text(value: bytes | str) -> str:
        return value.decode() if isinstance(value, bytes) else value


class ExecuteThenRaiseRedis:
    def __init__(
        self,
        operation: str,
        redis_connection: Redis | None = None,
        *,
        execute_before_raise: bool = True,
        raise_once: bool = True,
    ) -> None:
        self._redis = redis_connection or DeterministicRedis()
        self.operation = operation
        self.execute_before_raise = execute_before_raise
        self.raise_once = raise_once
        self.raised = False
        self.attempt_tokens: list[str] = []

    def register_script(self, script):  # noqa: ANN001, ANN201
        execute = self._redis.register_script(script)
        operation = script.splitlines()[0].removeprefix("-- ")

        def execute_then_raise(*, keys, args):
            if operation != self.operation:
                return execute(keys=keys, args=args)
            if args:
                self.attempt_tokens.append(str(args[0]))
            should_raise = not self.raised or not self.raise_once
            if not should_raise:
                return execute(keys=keys, args=args)
            self.raised = True
            if self.execute_before_raise:
                execute(keys=keys, args=args)
            raise RedisConnectionError(f"{operation} acknowledgement lost")

        return execute_then_raise

    def __getattr__(self, name: str):
        return getattr(self._redis, name)


@pytest.fixture
def redis_connection() -> DeterministicRedis:
    return DeterministicRedis()


def coordinator(
    redis_connection: Redis,
    *,
    max_uploads: int = 2,
    max_session_bytes: int = 10,
    min_free_bytes: int = 5,
    reservation_ttl: float = 10,
    lease_ttl: float = 10,
    reservation_heartbeat_interval: float = 5,
    lease_heartbeat_interval: float = 5,
    release_marker_ttl: float | None = None,
) -> RedisResourceCoordinator:
    return RedisResourceCoordinator(
        redis_connection=redis_connection,
        max_upload_concurrency=max_uploads,
        max_session_bytes=max_session_bytes,
        min_free_bytes=min_free_bytes,
        reservation_ttl=reservation_ttl,
        lease_ttl=lease_ttl,
        reservation_heartbeat_interval=reservation_heartbeat_interval,
        lease_heartbeat_interval=lease_heartbeat_interval,
        release_marker_ttl=release_marker_ttl,
    )


def _clear_resource_keys(redis_connection: Redis) -> None:
    keys = list(redis_connection.scan_iter(match="dt:resource:*"))
    if keys:
        redis_connection.delete(*keys)


@pytest.fixture
def real_redis_connection() -> Redis:
    url = os.environ.get(
        "DESIGNTOOLKIT_TEST_REDIS_URL",
        "redis://localhost:6379/15",
    )
    connection = Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )
    try:
        connection.ping()
    except RedisError as error:
        pytest.skip(f"real Redis unavailable at {url}: {error}")
    _clear_resource_keys(connection)
    try:
        yield connection
    finally:
        _clear_resource_keys(connection)
        connection.close()


def test_upload_reservations_enforce_global_concurrency_atomically(
    redis_connection: DeterministicRedis,
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
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    first = resources.reserve_upload(SESSION_A, 6, free_bytes=100)

    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 5, free_bytes=100)

    second = resources.reserve_upload(SESSION_B, 4, free_bytes=100)
    first.release()
    second.release()


def test_committed_uploads_retain_session_usage_until_cleanup(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    first = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    second = resources.reserve_upload(SESSION_A, 4, free_bytes=100)

    first.commit()
    second.commit()

    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 3, free_bytes=100)

    resources.clear_session_usage(SESSION_A)
    replacement = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    replacement.release()


def test_release_committed_asset_is_idempotent_and_session_scoped(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    first = resources.reserve_upload(SESSION_A, 7, free_bytes=100)
    second = resources.reserve_upload(SESSION_B, 6, free_bytes=100)
    first.commit()
    second.commit()
    asset_id = "1" * 32

    resources.release_committed_asset(SESSION_A, asset_id, 4)
    resources.release_committed_asset(SESSION_A, asset_id, 4)
    assert resources.is_committed_asset_released(SESSION_A, asset_id) is True
    assert resources.is_committed_asset_released(SESSION_B, asset_id) is False
    assert resources.is_committed_asset_released(SESSION_A, "2" * 32) is False
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 8, free_bytes=100)
    resources.reserve_upload(SESSION_A, 7, free_bytes=100).release()

    marker_key = f"dt:resource:asset-released:{SESSION_A}:{asset_id}"
    redis_connection.hset(marker_key, "session_id", SESSION_B)
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.release_committed_asset(SESSION_A, asset_id, 4)
    redis_connection.hset(marker_key, "session_id", SESSION_A)
    with pytest.raises(ResourceUnavailableError, match="bytes"):
        resources.release_committed_asset(SESSION_A, asset_id, 3)
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_B, 5, free_bytes=100)


def test_asset_release_marker_remains_persistent_until_finalized(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        reservation_ttl=1,
        lease_ttl=2,
        reservation_heartbeat_interval=0.25,
        lease_heartbeat_interval=0.25,
        release_marker_ttl=20,
    )
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    reservation.commit()

    resources.release_committed_asset(SESSION_A, "1" * 32, 4)

    marker_key = f"dt:resource:asset-released:{SESSION_A}:{'1' * 32}"
    assert redis_connection.pttl(marker_key) == -1

    resources.is_committed_asset_released(SESSION_A, "1" * 32)
    resources.release_committed_asset(SESSION_A, "1" * 32, 4)

    assert redis_connection.pttl(marker_key) == -1


def test_finalized_asset_release_marker_expires_without_ttl_refresh(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        max_session_bytes=10,
        reservation_ttl=0.02,
        lease_ttl=0.02,
        reservation_heartbeat_interval=0.005,
        lease_heartbeat_interval=0.005,
        release_marker_ttl=0.15,
    )
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    reservation.commit()
    asset_id = "1" * 32
    marker_key = f"dt:resource:asset-released:{SESSION_A}:{asset_id}"
    marker_index_key = f"dt:resource:session:{SESSION_A}:released-assets"

    resources.release_committed_asset(SESSION_A, asset_id, 4)
    resources.finalize_asset_release(SESSION_A, asset_id)
    initial_ttl = redis_connection.pttl(marker_key)
    assert not redis_connection.exists(marker_index_key)
    sleep(0.06)
    ttl_before_retry = redis_connection.pttl(marker_key)
    resources.finalize_asset_release(SESSION_A, asset_id)

    assert 0 < ttl_before_retry < initial_ttl <= 150
    assert 0 < redis_connection.pttl(marker_key) <= ttl_before_retry
    deadline = monotonic() + 0.2
    while redis_connection.exists(marker_key) and monotonic() < deadline:
        sleep(0.005)

    assert not redis_connection.exists(marker_key)


def test_concurrent_asset_release_decrements_committed_usage_once(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    first = resources.reserve_upload(SESSION_A, 5, free_bytes=100)
    second = resources.reserve_upload(SESSION_A, 3, free_bytes=100)
    first.commit()
    second.commit()
    start = Barrier(8)

    def release() -> None:
        start.wait()
        resources.release_committed_asset(SESSION_A, "1" * 32, 5)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(release) for _ in range(8)]
        for future in futures:
            future.result()

    resources.reserve_upload(SESSION_A, 7, free_bytes=100).release()
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 8, free_bytes=100)


def test_asset_release_acknowledgement_loss_can_be_retried_without_double_release(
) -> None:
    redis_connection = ExecuteThenRaiseRedis("release_committed_asset")
    resources = coordinator(redis_connection, max_session_bytes=10)
    first = resources.reserve_upload(SESSION_A, 5, free_bytes=100)
    second = resources.reserve_upload(SESSION_A, 3, free_bytes=100)
    first.commit()
    second.commit()

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        resources.release_committed_asset(SESSION_A, "1" * 32, 5)
    resources.release_committed_asset(SESSION_A, "1" * 32, 5)

    resources.reserve_upload(SESSION_A, 7, free_bytes=100).release()
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 8, free_bytes=100)


def test_asset_release_finalization_acknowledgement_loss_does_not_refresh_ttl(
) -> None:
    redis_connection = ExecuteThenRaiseRedis("finalize_asset_release")
    resources = coordinator(
        redis_connection,
        max_session_bytes=10,
        reservation_ttl=0.02,
        lease_ttl=0.02,
        reservation_heartbeat_interval=0.005,
        lease_heartbeat_interval=0.005,
        release_marker_ttl=0.15,
    )
    reservation = resources.reserve_upload(SESSION_A, 5, free_bytes=100)
    reservation.commit()
    asset_id = "1" * 32
    marker_key = f"dt:resource:asset-released:{SESSION_A}:{asset_id}"
    resources.release_committed_asset(SESSION_A, asset_id, 5)

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        resources.finalize_asset_release(SESSION_A, asset_id)
    sleep(0.06)
    ttl_before_retry = redis_connection.pttl(marker_key)
    resources.finalize_asset_release(SESSION_A, asset_id)

    assert 0 < redis_connection.pttl(marker_key) <= ttl_before_retry


def test_clear_session_usage_removes_persistent_asset_release_markers(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        max_session_bytes=10,
        release_marker_ttl=20,
    )
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    reservation.commit()
    asset_id = "1" * 32
    resources.release_committed_asset(SESSION_A, asset_id, 4)
    marker_key = f"dt:resource:asset-released:{SESSION_A}:{asset_id}"
    marker_index_key = f"dt:resource:session:{SESSION_A}:released-assets"

    assert redis_connection.hgetall(marker_key) == {
        b"session_id": SESSION_A.encode(),
        b"asset_id": asset_id.encode(),
        b"bytes": b"4",
    }
    assert redis_connection.pttl(marker_key) == -1
    assert redis_connection.smembers(marker_index_key) == {marker_key.encode()}

    resources.clear_session_usage(SESSION_A)

    assert not redis_connection.exists(marker_key)
    assert not redis_connection.exists(marker_index_key)


def test_generation_cleanup_is_idempotent_and_preserves_new_session_usage(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        max_session_bytes=10,
        reservation_ttl=0.02,
        lease_ttl=0.02,
        reservation_heartbeat_interval=0.005,
        lease_heartbeat_interval=0.005,
        release_marker_ttl=0.05,
    )
    old_upload = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    old_upload.commit()
    old_asset_id = "1" * 32
    resources.release_committed_asset(SESSION_A, old_asset_id, 1)
    old_claim = resources.claim_session_cleanup(SESSION_A)
    assert old_claim is not None
    generation = resources.capture_session_usage(SESSION_A, old_claim)
    resources.clear_session_usage_generation(
        SESSION_A,
        old_claim,
        generation,
    )
    generation_key = f"dt:resource:cleanup-generation:{generation.generation_id}"
    assert redis_connection.pttl(generation_key) == -1
    resources.release_cleanup_claim(SESSION_A, old_claim)
    sleep(0.08)

    new_upload = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    new_upload.commit()
    new_asset_id = "2" * 32
    resources.release_committed_asset(SESSION_A, new_asset_id, 1)
    recovery_claim = resources.claim_session_cleanup(SESSION_A)
    assert recovery_claim is not None

    resources.clear_session_usage_generation(
        SESSION_A,
        recovery_claim,
        generation,
    )
    resources.clear_session_usage_generation(
        SESSION_A,
        recovery_claim,
        generation,
    )

    assert resources.is_committed_asset_released(SESSION_A, old_asset_id) is False
    assert resources.is_committed_asset_released(SESSION_A, new_asset_id) is True
    assert redis_connection.pttl(generation_key) == -1
    resources.reserve_upload(SESSION_A, 7, free_bytes=100).release()
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 8, free_bytes=100)
    resources.finalize_session_usage_generation(generation.generation_id)
    assert 0 < redis_connection.pttl(generation_key) <= 50
    resources.release_cleanup_claim(SESSION_A, recovery_claim)


def test_generation_cleanup_migrates_expiring_marker_without_releasing_again(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    old_upload = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    old_upload.commit()
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    generation = resources.capture_session_usage(SESSION_A, claim)
    generation_key = f"dt:resource:cleanup-generation:{generation.generation_id}"
    committed_field = f"session:{SESSION_A}:committed_bytes"
    redis_connection.hdel("dt:resource:state", committed_field)
    redis_connection.hset("dt:resource:state", committed_field, 3)
    redis_connection.set(generation_key, "cleared", px=50)

    resources.clear_session_usage_generation(SESSION_A, claim, generation)

    assert redis_connection.hgetall(generation_key) == {
        b"session_id": SESSION_A.encode(),
        b"generation_id": generation.generation_id.encode(),
    }
    assert redis_connection.pttl(generation_key) == -1
    assert int(redis_connection.hget("dt:resource:state", committed_field)) == 3
    resources.release_cleanup_claim(SESSION_A, claim)


@pytest.mark.parametrize(
    ("asset_id", "released_bytes"),
    [
        ("not-an-asset", 1),
        ("1" * 32, -1),
    ],
)
def test_release_committed_asset_rejects_malformed_input(
    redis_connection: DeterministicRedis,
    asset_id: str,
    released_bytes: int,
) -> None:
    resources = coordinator(redis_connection)

    with pytest.raises(ValueError):
        resources.release_committed_asset(SESSION_A, asset_id, released_bytes)


def test_upload_commit_creates_bounded_marker_with_accounting_metadata(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        reservation_ttl=1,
        reservation_heartbeat_interval=3,
    )
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)

    reservation.commit()

    marker_key = f"dt:resource:committed:{reservation.token}"
    assert redis_connection.hgetall(marker_key) == {
        b"state": b"committed",
        b"kind": b"upload",
        b"session_id": SESSION_A.encode(),
        b"bytes": b"4",
    }
    assert 0 < redis_connection.pttl(marker_key) <= 6_000


def test_rollback_only_removes_its_committed_upload_usage(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    first = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    second = resources.reserve_upload(SESSION_A, 3, free_bytes=100)
    first.commit()
    second.commit()

    first.rollback()

    replacement = resources.reserve_upload(SESSION_A, 7, free_bytes=100)
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 8, free_bytes=100)
    replacement.release()


def test_committed_upload_rollback_is_idempotent_and_session_isolated(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=5)
    first = resources.reserve_upload(SESSION_A, 5, free_bytes=100)
    second = resources.reserve_upload(SESSION_B, 5, free_bytes=100)
    first.commit()
    second.commit()

    first.rollback()
    first.rollback()

    resources.reserve_upload(SESSION_A, 5, free_bytes=100).release()
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_B, 1, free_bytes=100)


def test_finalize_discards_marker_without_releasing_committed_usage(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=5)
    reservation = resources.reserve_upload(SESSION_A, 5, free_bytes=100)
    reservation.commit()

    reservation.finalize()
    reservation.finalize()
    reservation.rollback()

    assert not redis_connection.exists(
        f"dt:resource:committed:{reservation.token}"
    )
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 1, free_bytes=100)


def test_expired_committed_marker_preserves_usage(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=5)
    reservation = resources.reserve_upload(SESSION_A, 5, free_bytes=100)
    reservation.commit()
    redis_connection.delete(f"dt:resource:committed:{reservation.token}")

    reservation.rollback()
    reservation.finalize()

    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 1, free_bytes=100)


def test_output_commit_uses_idempotent_marker_without_session_charging(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    reservation = resources.reserve_output(
        "task-1",
        SESSION_A,
        4,
        free_bytes=100,
    )

    reservation.commit()
    reservation.commit()
    assert reservation.state() is ReservationState.COMMITTED
    reservation.rollback()
    assert reservation.rollback() is True
    reservation.finalize()

    assert not redis_connection.exists(
        f"dt:resource:committed:{reservation.token}"
    )


def test_failed_upload_release_frees_reserved_session_capacity(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=10)
    committed = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    committed.commit()
    failed = resources.reserve_upload(SESSION_A, 5, free_bytes=100)

    failed.release()

    replacement = resources.reserve_upload(SESSION_A, 6, free_bytes=100)
    replacement.release()
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 7, free_bytes=100)


def test_upload_capacity_includes_all_active_reservations(
    redis_connection: DeterministicRedis,
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
    redis_connection: DeterministicRedis,
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


def test_two_workers_cannot_concurrently_overreserve_output_capacity(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, min_free_bytes=5)
    start = Barrier(2)

    def reserve(index: int):
        start.wait()
        try:
            return resources.reserve_output(
                f"task-{index}",
                f"{index:032x}",
                6,
                free_bytes=15,
            )
        except ResourceUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(executor.map(reserve, range(2)))

    accepted = [reservation for reservation in reservations if reservation]
    assert len(accepted) == 1
    accepted[0].release()


def test_task_resources_can_be_released_by_task_idempotently(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    resources.acquire_session(SESSION_A, task_id="task-1")
    resources.reserve_output(
        "task-1",
        SESSION_A,
        5,
        free_bytes=100,
    )
    assert resources.output_reserved_bytes() == 5

    resources.release_task_resources("task-1")
    resources.release_task_resources("task-1")

    assert resources.output_reserved_bytes() == 0
    assert redis_connection.hget("dt:resource:state", "reserved_bytes") is None
    assert (
        redis_connection.hget(
            f"dt:resource:session:{SESSION_A}",
            "active_count",
        )
        is None
    )
    assert not redis_connection.exists("dt:resource:task:task-1")


def test_mixed_reservations_preserve_strictest_capacity_observation(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        max_uploads=3,
        max_session_bytes=100,
        min_free_bytes=5,
    )
    output = resources.reserve_output(
        "task-1",
        SESSION_A,
        10,
        free_bytes=100,
        min_free_bytes=30,
    )
    upload = resources.reserve_upload(SESSION_B, 10, free_bytes=200)

    with pytest.raises(ResourceUnavailableError, match="disk"):
        resources.reserve_output(
            "task-2",
            SESSION_A,
            55,
            free_bytes=200,
            min_free_bytes=5,
        )

    output.release()
    relaxed = resources.reserve_output(
        "task-3",
        SESSION_A,
        170,
        free_bytes=200,
        min_free_bytes=5,
    )
    relaxed.release()
    upload.release()


def test_reservation_release_is_idempotent_and_token_scoped(
    redis_connection: DeterministicRedis,
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
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, reservation_ttl=2)
    reservation = resources.reserve_upload(SESSION_A, 1, free_bytes=100)
    token_key = f"dt:resource:reservation:{reservation.token}"
    redis_connection.pexpire(token_key, 10)

    reservation.refresh()

    assert redis_connection.pttl(token_key) > 1_000


def test_expired_reservation_commit_fails_explicitly(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    reservation = resources.reserve_upload(SESSION_A, 1, free_bytes=100)
    redis_connection.delete(f"dt:resource:reservation:{reservation.token}")

    with pytest.raises(ResourceUnavailableError, match="expired"):
        reservation.commit()


def test_upload_commit_is_idempotent_after_reservation_conversion(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=2)
    reservation = resources.reserve_upload(SESSION_A, 2, free_bytes=100)

    reservation.commit()
    reservation.commit()

    assert reservation.state() is ReservationState.COMMITTED
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 1, free_bytes=100)


def test_missing_reservation_without_commit_marker_still_fails(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    reservation = resources.reserve_upload(SESSION_A, 1, free_bytes=100)
    redis_connection.delete(f"dt:resource:reservation:{reservation.token}")

    assert reservation.state() is ReservationState.FINALIZED_OR_ABSENT
    with pytest.raises(ResourceUnavailableError, match="expired"):
        reservation.commit()


def test_commit_execute_then_raise_can_be_retried_without_double_charging() -> None:
    redis_connection = ExecuteThenRaiseRedis("commit_reservation")
    resources = coordinator(redis_connection, max_session_bytes=4)
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        reservation.commit()
    reservation.commit()

    assert reservation.state() is ReservationState.COMMITTED
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 1, free_bytes=100)


def test_upload_reserve_recovers_lost_ack_without_double_reserving(
    real_redis_connection: Redis,
) -> None:
    redis_connection = ExecuteThenRaiseRedis(
        "reserve_upload",
        real_redis_connection,
    )
    resources = coordinator(
        redis_connection,
        max_uploads=2,
        max_session_bytes=10,
    )

    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)

    assert redis_connection.attempt_tokens == [
        reservation.token,
        reservation.token,
    ]
    assert real_redis_connection.hget("dt:resource:state", "reserved_bytes") == b"4"
    assert real_redis_connection.hget("dt:resource:state", "upload_count") == b"1"
    assert (
        real_redis_connection.hget(
            "dt:resource:state",
            f"session:{SESSION_A}:reserved_bytes",
        )
        == b"4"
    )
    reservation.release()

    assert real_redis_connection.hget("dt:resource:state", "upload_count") is None
    assert real_redis_connection.hget("dt:resource:state", "reserved_bytes") is None
    assert (
        real_redis_connection.hget(
            "dt:resource:state",
            f"session:{SESSION_A}:reserved_bytes",
        )
        is None
    )


def test_upload_reserve_rejects_conflicting_existing_token_metadata(
    real_redis_connection: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "1" * 32
    monkeypatch.setattr(
        "designtoolkit.web.resources.uuid4",
        lambda: SimpleNamespace(hex=token),
    )
    resources = coordinator(
        real_redis_connection,
        max_uploads=3,
        max_session_bytes=10,
    )
    reservation = resources.reserve_upload(SESSION_A, 3, free_bytes=100)

    with pytest.raises(ResourceUnavailableError, match="metadata conflict"):
        resources.reserve_upload(SESSION_A, 4, free_bytes=100)

    assert real_redis_connection.hget("dt:resource:state", "reserved_bytes") == b"3"
    assert real_redis_connection.hget("dt:resource:state", "upload_count") == b"1"
    reservation.release()


def test_rollback_execute_then_raise_is_idempotently_resolved() -> None:
    redis_connection = ExecuteThenRaiseRedis("rollback_commit")
    resources = coordinator(redis_connection, max_session_bytes=4)
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    reservation.commit()

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        reservation.rollback()
    assert reservation.rollback() is True

    replacement = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    replacement.release()


def test_finalize_execute_then_raise_never_changes_committed_usage() -> None:
    redis_connection = ExecuteThenRaiseRedis("finalize_commit")
    resources = coordinator(redis_connection, max_session_bytes=4)
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    reservation.commit()

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        reservation.finalize()
    reservation.finalize()

    assert reservation.state() is ReservationState.FINALIZED_OR_ABSENT
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 1, free_bytes=100)


def test_commit_release_race_has_single_accounting_winner(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=4)
    reservation = resources.reserve_upload(SESSION_A, 4, free_bytes=100)
    start = Barrier(2)

    def commit() -> str:
        start.wait()
        try:
            reservation.commit()
        except ResourceUnavailableError:
            return "released"
        return "committed"

    def release() -> None:
        start.wait()
        reservation.release()

    with ThreadPoolExecutor(max_workers=2) as executor:
        commit_result = executor.submit(commit)
        release_result = executor.submit(release)
        result = commit_result.result()
        release_result.result()

    if result == "committed":
        with pytest.raises(ResourceUnavailableError, match="session"):
            resources.reserve_upload(SESSION_A, 1, free_bytes=100)
        resources.clear_session_usage(SESSION_A)
    replacement = resources.reserve_upload(
        SESSION_A,
        4,
        free_bytes=100,
    )
    replacement.release()


def test_ttls_are_clamped_to_twice_their_heartbeat_intervals(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        reservation_ttl=1,
        lease_ttl=2,
        reservation_heartbeat_interval=3,
        lease_heartbeat_interval=4,
    )

    assert resources._reservation_ttl_ms == 6_000
    assert resources._lease_ttl_ms == 8_000


@pytest.mark.parametrize(
    ("interval_name", "interval"),
    [
        ("reservation_heartbeat_interval", 0),
        ("reservation_heartbeat_interval", -1),
        ("lease_heartbeat_interval", 0),
        ("lease_heartbeat_interval", -1),
    ],
)
def test_heartbeat_intervals_must_be_positive(
    redis_connection: DeterministicRedis,
    interval_name: str,
    interval: float,
) -> None:
    with pytest.raises(ValueError, match=interval_name):
        coordinator(redis_connection, **{interval_name: interval})


def test_reconcile_repairs_counters_after_reservation_expiry(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_uploads=1)
    expired = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    redis_connection.delete(f"dt:resource:reservation:{expired.token}")

    assert resources.reconcile_reservations() == 1
    replacement = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    assert resources.reconcile_reservations() == 0
    replacement.release()


def test_real_redis_executes_reservation_scripts(
    real_redis_connection: Redis,
) -> None:
    resources = coordinator(
        real_redis_connection,
        max_uploads=3,
        max_session_bytes=10,
        min_free_bytes=5,
    )
    upload = resources.reserve_upload(SESSION_A, 6, free_bytes=100)
    upload.commit()

    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 5, free_bytes=100)

    upload.rollback()
    finalized = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    finalized.commit()
    finalized.finalize()
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 1, free_bytes=100)

    resources.release_committed_asset(SESSION_A, "1" * 32, 10)
    resources.release_committed_asset(SESSION_A, "1" * 32, 0)
    replacement = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    replacement.release()

    resources.clear_session_usage(SESSION_A)
    released = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    released.refresh()
    released.release()

    capacity_resources = coordinator(
        real_redis_connection,
        max_uploads=3,
        max_session_bytes=100,
        min_free_bytes=5,
    )
    output = capacity_resources.reserve_output(
        "task-1",
        SESSION_A,
        10,
        free_bytes=100,
        min_free_bytes=30,
    )
    active_upload = capacity_resources.reserve_upload(
        SESSION_B,
        10,
        free_bytes=200,
    )
    with pytest.raises(ResourceUnavailableError, match="disk"):
        capacity_resources.reserve_output(
            "task-2",
            SESSION_A,
            55,
            free_bytes=200,
            min_free_bytes=5,
        )
    output.release()
    relaxed = capacity_resources.reserve_output(
        "task-3",
        SESSION_A,
        170,
        free_bytes=200,
        min_free_bytes=5,
    )
    relaxed.release()
    active_upload.release()

    lease = resources.acquire_session(SESSION_A)
    lease.refresh()
    lease.release()
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    generation = resources.capture_session_usage(SESSION_A, claim)
    resources.clear_session_usage_generation(SESSION_A, claim, generation)
    generation_key = (
        f"dt:resource:cleanup-generation:{generation.generation_id}"
    )
    assert real_redis_connection.pttl(generation_key) == -1
    assert resources.pending_cleanup_markers() == (
        CleanupMarker(
            session_id=SESSION_A,
            generation_id=generation.generation_id,
            legacy=False,
        ),
    )
    resources.finalize_session_usage_generation(generation.generation_id)
    assert real_redis_connection.pttl(generation_key) > 0
    assert resources.pending_cleanup_markers() == ()
    resources.release_cleanup_claim(SESSION_A, claim)


def test_real_redis_upload_reservation_failure_before_execution_creates_no_state(
    real_redis_connection: Redis,
) -> None:
    failing_redis = ExecuteThenRaiseRedis(
        "reserve_upload",
        real_redis_connection,
        execute_before_raise=False,
        raise_once=False,
    )
    resources = coordinator(
        failing_redis,
        max_uploads=1,
        max_session_bytes=4,
    )

    with pytest.raises(RedisConnectionError, match="acknowledgement lost"):
        resources.reserve_upload(SESSION_A, 4, free_bytes=100)

    assert len(failing_redis.attempt_tokens) >= 2
    assert len(set(failing_redis.attempt_tokens)) == 1
    assert list(
        real_redis_connection.scan_iter(match="dt:resource:reservation:*")
    ) == []
    assert real_redis_connection.hlen("dt:resource:reservations") == 0
    assert real_redis_connection.hget("dt:resource:state", "upload_count") is None
    assert real_redis_connection.hget("dt:resource:state", "reserved_bytes") is None
    assert (
        real_redis_connection.hget(
            "dt:resource:state",
            f"session:{SESSION_A}:reserved_bytes",
        )
        is None
    )


def test_real_redis_ttl_expiry_is_reconciled(
    real_redis_connection: Redis,
) -> None:
    resources = coordinator(
        real_redis_connection,
        max_uploads=1,
        max_session_bytes=10,
        reservation_ttl=0.02,
        reservation_heartbeat_interval=0.005,
    )
    expired = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    token_key = f"dt:resource:reservation:{expired.token}"

    deadline = monotonic() + 1
    while real_redis_connection.exists(token_key) and monotonic() < deadline:
        sleep(0.005)

    assert not real_redis_connection.exists(token_key)
    assert resources.reconcile_reservations() == 1
    replacement = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    replacement.release()

    committed = resources.reserve_upload(SESSION_A, 10, free_bytes=100)
    committed.commit()
    marker_key = f"dt:resource:committed:{committed.token}"
    deadline = monotonic() + 1
    while real_redis_connection.exists(marker_key) and monotonic() < deadline:
        sleep(0.005)

    assert not real_redis_connection.exists(marker_key)
    committed.rollback()
    with pytest.raises(ResourceUnavailableError, match="session"):
        resources.reserve_upload(SESSION_A, 1, free_bytes=100)


def test_context_managers_release_reservation_and_session_lease(
    redis_connection: DeterministicRedis,
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
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    lease = resources.acquire_session(SESSION_A)

    assert resources.claim_session_cleanup(SESSION_A) is None
    lease.release()
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    resources.release_cleanup_claim(SESSION_A, claim)


def test_cleanup_claim_blocks_new_leases_until_matching_release(
    redis_connection: DeterministicRedis,
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


def test_cleanup_claim_has_bounded_ttl_and_can_be_refreshed(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        lease_ttl=2,
        lease_heartbeat_interval=0.5,
    )
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    claim_key = f"dt:resource:cleanup:{claim}"
    assert 0 < redis_connection.pttl(claim_key) <= 2_000
    redis_connection.pexpire(claim_key, 10)

    resources.refresh_cleanup_claim(SESSION_A, claim)

    assert redis_connection.pttl(claim_key) > 1_000


def test_abandoned_cleanup_claim_expires_and_allows_recovery(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    redis_connection.delete(f"dt:resource:cleanup:{claim}")

    resources.acquire_session(SESSION_A).release()
    replacement = resources.claim_session_cleanup(SESSION_A)

    assert replacement is not None
    resources.release_cleanup_claim(SESSION_A, replacement)


def test_cleanup_claim_refresh_is_token_scoped(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None

    with pytest.raises(ResourceUnavailableError, match="expired"):
        resources.refresh_cleanup_claim(SESSION_A, "0" * 32)

    resources.release_cleanup_claim(SESSION_A, claim)


@pytest.mark.parametrize(
    ("committed_bytes", "current_bytes", "legacy_bytes"),
    [(4, 0, 4), (7, 3, 4)],
)
def test_legacy_session_usage_reconcile_is_atomic_and_preserves_current_usage(
    redis_connection: DeterministicRedis,
    committed_bytes: int,
    current_bytes: int,
    legacy_bytes: int,
) -> None:
    resources = coordinator(
        redis_connection,
        max_session_bytes=20,
        release_marker_ttl=20,
    )
    redis_connection.hset(
        "dt:resource:state",
        f"session:{SESSION_A}:committed_bytes",
        committed_bytes,
    )
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    generation_id = "1" * 32

    reconciled = resources.reconcile_legacy_session_usage(
        SESSION_A,
        claim,
        generation_id,
        current_committed_bytes=current_bytes,
        legacy_physical_bytes=legacy_bytes,
    )
    repeated = resources.reconcile_legacy_session_usage(
        SESSION_A,
        claim,
        generation_id,
        current_committed_bytes=current_bytes,
        legacy_physical_bytes=legacy_bytes,
    )

    assert reconciled == legacy_bytes
    assert repeated == legacy_bytes
    assert int(
        redis_connection.hget(
            "dt:resource:state",
            f"session:{SESSION_A}:committed_bytes",
        )
        or 0
    ) == current_bytes
    tombstone_key = f"dt:resource:legacy-cleanup:{generation_id}"
    assert redis_connection.hget(tombstone_key, "session_id") == SESSION_A.encode()
    assert redis_connection.pttl(tombstone_key) == -1
    resources.finalize_legacy_session_usage(generation_id)
    assert redis_connection.pttl(tombstone_key) > 0
    resources.release_cleanup_claim(SESSION_A, claim)


def test_legacy_cleanup_marker_survives_old_ttl_and_preserves_rebuilt_usage(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(
        redis_connection,
        max_session_bytes=20,
        reservation_ttl=0.02,
        lease_ttl=0.02,
        reservation_heartbeat_interval=0.005,
        lease_heartbeat_interval=0.005,
        release_marker_ttl=0.05,
    )
    state_key = "dt:resource:state"
    committed_field = f"session:{SESSION_A}:committed_bytes"
    generation_id = "1" * 32
    tombstone_key = f"dt:resource:legacy-cleanup:{generation_id}"
    redis_connection.hset(state_key, committed_field, 4)
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None

    assert resources.reconcile_legacy_session_usage(
        SESSION_A,
        claim,
        generation_id,
        current_committed_bytes=0,
        legacy_physical_bytes=4,
    ) == 4
    assert redis_connection.pttl(tombstone_key) == -1
    resources.release_cleanup_claim(SESSION_A, claim)
    sleep(0.08)

    redis_connection.hset(state_key, committed_field, 3)
    recovery_claim = resources.claim_session_cleanup(SESSION_A)
    assert recovery_claim is not None
    assert resources.reconcile_legacy_session_usage(
        SESSION_A,
        recovery_claim,
        generation_id,
        current_committed_bytes=3,
        legacy_physical_bytes=4,
    ) == 4
    assert int(redis_connection.hget(state_key, committed_field)) == 3
    assert redis_connection.pttl(tombstone_key) == -1

    resources.finalize_legacy_session_usage(generation_id)
    assert 0 < redis_connection.pttl(tombstone_key) <= 50
    resources.release_cleanup_claim(SESSION_A, recovery_claim)


def test_legacy_cleanup_persists_existing_unfinalized_marker(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection, max_session_bytes=20)
    state_key = "dt:resource:state"
    committed_field = f"session:{SESSION_A}:committed_bytes"
    generation_id = "1" * 32
    tombstone_key = f"dt:resource:legacy-cleanup:{generation_id}"
    redis_connection.hset(state_key, committed_field, 3)
    redis_connection.hset(
        tombstone_key,
        mapping={
            "session_id": SESSION_A,
            "current_bytes": 0,
            "legacy_bytes": 4,
        },
    )
    redis_connection.pexpire(tombstone_key, 50)
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None

    assert resources.reconcile_legacy_session_usage(
        SESSION_A,
        claim,
        generation_id,
        current_committed_bytes=3,
        legacy_physical_bytes=4,
    ) == 4

    assert redis_connection.pttl(tombstone_key) == -1
    assert int(redis_connection.hget(state_key, committed_field)) == 3
    resources.release_cleanup_claim(SESSION_A, claim)


def test_legacy_session_usage_reconcile_rejects_unproven_counter_delta(
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    redis_connection.hset(
        "dt:resource:state",
        f"session:{SESSION_A}:committed_bytes",
        9,
    )
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None

    with pytest.raises(ResourceUnavailableError, match="cannot be reconciled"):
        resources.reconcile_legacy_session_usage(
            SESSION_A,
            claim,
            "1" * 32,
            current_committed_bytes=3,
            legacy_physical_bytes=4,
        )

    assert int(
        redis_connection.hget(
            "dt:resource:state",
            f"session:{SESSION_A}:committed_bytes",
        )
    ) == 9
    resources.release_cleanup_claim(SESSION_A, claim)


def test_real_redis_reconciles_legacy_session_usage(
    real_redis_connection: Redis,
) -> None:
    resources = coordinator(real_redis_connection, max_session_bytes=20)
    real_redis_connection.hset(
        "dt:resource:state",
        f"session:{SESSION_A}:committed_bytes",
        7,
    )
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    generation_id = "1" * 32

    assert resources.reconcile_legacy_session_usage(
        SESSION_A,
        claim,
        generation_id,
        current_committed_bytes=3,
        legacy_physical_bytes=4,
    ) == 4
    assert int(
        real_redis_connection.hget(
            "dt:resource:state",
            f"session:{SESSION_A}:committed_bytes",
        )
    ) == 3
    tombstone_key = f"dt:resource:legacy-cleanup:{generation_id}"
    assert real_redis_connection.pttl(tombstone_key) == -1
    assert resources.pending_cleanup_markers() == (
        CleanupMarker(
            session_id=SESSION_A,
            generation_id=generation_id,
            legacy=True,
        ),
    )
    resources.finalize_legacy_session_usage(generation_id)
    assert real_redis_connection.pttl(tombstone_key) > 0
    assert resources.pending_cleanup_markers() == ()
    resources.release_cleanup_claim(SESSION_A, claim)


def test_session_lease_release_is_idempotent_and_token_scoped(
    redis_connection: DeterministicRedis,
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
    redis_connection: DeterministicRedis,
) -> None:
    resources = coordinator(redis_connection)
    lease = resources.acquire_session(SESSION_A)
    redis_connection.delete(f"dt:resource:lease:{lease.token}")

    claim = resources.claim_session_cleanup(SESSION_A)

    assert claim is not None
    resources.release_cleanup_claim(SESSION_A, claim)


def test_real_redis_cleanup_claim_ttl_expires_and_recovers(
    real_redis_connection: Redis,
) -> None:
    resources = coordinator(
        real_redis_connection,
        lease_ttl=0.02,
        lease_heartbeat_interval=0.005,
    )
    claim = resources.claim_session_cleanup(SESSION_A)
    assert claim is not None
    claim_key = f"dt:resource:cleanup:{claim}"

    deadline = monotonic() + 1
    while real_redis_connection.exists(claim_key) and monotonic() < deadline:
        sleep(0.005)

    assert not real_redis_connection.exists(claim_key)
    resources.acquire_session(SESSION_A).release()
    replacement = resources.claim_session_cleanup(SESSION_A)
    assert replacement is not None
    resources.release_cleanup_claim(SESSION_A, replacement)


@pytest.mark.parametrize("requested_bytes", [0, -1])
def test_reservations_require_positive_sizes(
    redis_connection: DeterministicRedis,
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
    redis_connection: DeterministicRedis,
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
    with pytest.raises(ValueError, match="session"):
        resources.clear_session_usage(session_id)
