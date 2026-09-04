"""Coordinate hosted resources using standalone Redis Lua scripts.

The scripts target the standalone Redis instance used by Docker Compose. Redis
Cluster support is intentionally out of scope.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from redis import Redis

_STATE_KEY = "dt:resource:state"
_RESERVATION_INDEX_KEY = "dt:resource:reservations"
_RESERVATION_PREFIX = "dt:resource:reservation:"
_COMMITTED_PREFIX = "dt:resource:committed:"
_LEASE_PREFIX = "dt:resource:lease:"
_CLEANUP_PREFIX = "dt:resource:cleanup:"
_SESSION_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_DEFAULT_HEARTBEAT_INTERVAL = 5.0

_RESERVE_UPLOAD_SCRIPT = """-- reserve_upload
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
local token = ARGV[1]
local session_id = ARGV[2]
local requested = tonumber(ARGV[3])
local free_bytes = tonumber(ARGV[4])
local max_uploads = tonumber(ARGV[5])
local max_session_bytes = tonumber(ARGV[6])
local min_free_bytes = tonumber(ARGV[7])
local ttl_ms = tonumber(ARGV[8])

local upload_count = tonumber(redis.call("HGET", state_key, "upload_count") or "0")
if upload_count >= max_uploads then
    return 1
end

local session_reserved_field = "session:" .. session_id .. ":reserved_bytes"
local session_committed_field = "session:" .. session_id .. ":committed_bytes"
local session_reserved = tonumber(
    redis.call("HGET", state_key, session_reserved_field) or "0"
)
local session_committed = tonumber(
    redis.call("HGET", state_key, session_committed_field) or "0"
)
if session_reserved + session_committed + requested > max_session_bytes then
    return 2
end

local reserved_bytes = tonumber(
    redis.call("HGET", state_key, "reserved_bytes") or "0"
)
local observed_free = tonumber(
    redis.call("HGET", state_key, "observed_free_bytes") or free_bytes
)
local active_min_free = tonumber(
    redis.call("HGET", state_key, "min_free_bytes") or min_free_bytes
)
local effective_free = math.min(observed_free, free_bytes)
local effective_min_free = math.max(active_min_free, min_free_bytes)
if effective_free - reserved_bytes - requested < effective_min_free then
    return 3
end

redis.call(
    "HMSET",
    token_key,
    "kind",
    "upload",
    "owner_id",
    session_id,
    "session_id",
    session_id,
    "bytes",
    requested,
    "free_bytes",
    free_bytes,
    "min_free_bytes",
    min_free_bytes
)
redis.call("PEXPIRE", token_key, ttl_ms)
redis.call(
    "HSET",
    index_key,
    token,
    "upload|" .. session_id .. "|" .. requested ..
        "|" .. free_bytes .. "|" .. min_free_bytes
)
redis.call("HINCRBY", state_key, "reserved_bytes", requested)
redis.call("HINCRBY", state_key, "upload_count", 1)
redis.call("HINCRBY", state_key, session_reserved_field, requested)
redis.call("HSET", state_key, "observed_free_bytes", effective_free)
redis.call("HSET", state_key, "min_free_bytes", effective_min_free)
return 0
"""

_RESERVE_OUTPUT_SCRIPT = """-- reserve_output
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
local token = ARGV[1]
local task_id = ARGV[2]
local session_id = ARGV[3]
local requested = tonumber(ARGV[4])
local free_bytes = tonumber(ARGV[5])
local min_free_bytes = tonumber(ARGV[6])
local ttl_ms = tonumber(ARGV[7])

local reserved_bytes = tonumber(
    redis.call("HGET", state_key, "reserved_bytes") or "0"
)
local observed_free = tonumber(
    redis.call("HGET", state_key, "observed_free_bytes") or free_bytes
)
local active_min_free = tonumber(
    redis.call("HGET", state_key, "min_free_bytes") or min_free_bytes
)
local effective_free = math.min(observed_free, free_bytes)
local effective_min_free = math.max(active_min_free, min_free_bytes)
if effective_free - reserved_bytes - requested < effective_min_free then
    return 1
end

redis.call(
    "HMSET",
    token_key,
    "kind",
    "output",
    "owner_id",
    task_id,
    "session_id",
    session_id,
    "bytes",
    requested,
    "free_bytes",
    free_bytes,
    "min_free_bytes",
    min_free_bytes
)
redis.call("PEXPIRE", token_key, ttl_ms)
redis.call(
    "HSET",
    index_key,
    token,
    "output|" .. session_id .. "|" .. requested ..
        "|" .. free_bytes .. "|" .. min_free_bytes
)
redis.call("HINCRBY", state_key, "reserved_bytes", requested)
redis.call("HSET", state_key, "observed_free_bytes", effective_free)
redis.call("HSET", state_key, "min_free_bytes", effective_min_free)
return 0
"""

_REFRESH_TOKEN_SCRIPT = """-- refresh_token
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[1]))
return 1
"""

_RELEASE_RESERVATION_SCRIPT = """-- release_reservation
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
if redis.call("EXISTS", token_key) == 0 then
    return 0
end

local kind = redis.call("HGET", token_key, "kind")
local session_id = redis.call("HGET", token_key, "session_id")
local reserved_bytes = tonumber(redis.call("HGET", token_key, "bytes") or "0")

local function decrement(field, amount)
    local current = tonumber(redis.call("HGET", state_key, field) or "0")
    local remaining = current - amount
    if remaining > 0 then
        redis.call("HSET", state_key, field, remaining)
    else
        redis.call("HDEL", state_key, field)
    end
end

local function recompute_capacity()
    local metadata_entries = redis.call("HVALS", index_key)
    local observed_free = nil
    local min_free_bytes = nil
    for _, metadata in ipairs(metadata_entries) do
        local _, _, _, free_text, min_free_text = string.match(
            metadata,
            "^([^|]+)|([^|]+)|(%d+)|(%d+)|(%d+)$"
        )
        if free_text then
            local entry_free = tonumber(free_text)
            local entry_min_free = tonumber(min_free_text)
            if not observed_free or entry_free < observed_free then
                observed_free = entry_free
            end
            if not min_free_bytes or entry_min_free > min_free_bytes then
                min_free_bytes = entry_min_free
            end
        end
    end
    if observed_free then
        redis.call("HSET", state_key, "observed_free_bytes", observed_free)
        redis.call("HSET", state_key, "min_free_bytes", min_free_bytes)
    else
        redis.call(
            "HDEL",
            state_key,
            "observed_free_bytes",
            "min_free_bytes"
        )
    end
end

redis.call("DEL", token_key)
redis.call("HDEL", index_key, ARGV[1])
decrement("reserved_bytes", reserved_bytes)
if kind == "upload" then
    decrement("upload_count", 1)
    decrement(
        "session:" .. session_id .. ":reserved_bytes",
        reserved_bytes
    )
end
recompute_capacity()
return 1
"""

_COMMIT_RESERVATION_SCRIPT = """-- commit_reservation
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
local committed_key = KEYS[4]
if redis.call("EXISTS", token_key) == 0 then
    return 0
end

local kind = redis.call("HGET", token_key, "kind")
local session_id = redis.call("HGET", token_key, "session_id")
local reserved_bytes = tonumber(redis.call("HGET", token_key, "bytes") or "0")

local function decrement(field, amount)
    local current = tonumber(redis.call("HGET", state_key, field) or "0")
    local remaining = current - amount
    if remaining > 0 then
        redis.call("HSET", state_key, field, remaining)
    else
        redis.call("HDEL", state_key, field)
    end
end

local function recompute_capacity()
    local metadata_entries = redis.call("HVALS", index_key)
    local observed_free = nil
    local min_free_bytes = nil
    for _, metadata in ipairs(metadata_entries) do
        local _, _, _, free_text, min_free_text = string.match(
            metadata,
            "^([^|]+)|([^|]+)|(%d+)|(%d+)|(%d+)$"
        )
        if free_text then
            local entry_free = tonumber(free_text)
            local entry_min_free = tonumber(min_free_text)
            if not observed_free or entry_free < observed_free then
                observed_free = entry_free
            end
            if not min_free_bytes or entry_min_free > min_free_bytes then
                min_free_bytes = entry_min_free
            end
        end
    end
    if observed_free then
        redis.call("HSET", state_key, "observed_free_bytes", observed_free)
        redis.call("HSET", state_key, "min_free_bytes", min_free_bytes)
    else
        redis.call(
            "HDEL",
            state_key,
            "observed_free_bytes",
            "min_free_bytes"
        )
    end
end

redis.call("DEL", token_key)
redis.call("HDEL", index_key, ARGV[1])
decrement("reserved_bytes", reserved_bytes)
if kind == "upload" then
    decrement("upload_count", 1)
    decrement(
        "session:" .. session_id .. ":reserved_bytes",
        reserved_bytes
    )
    redis.call(
        "HINCRBY",
        state_key,
        "session:" .. session_id .. ":committed_bytes",
        reserved_bytes
    )
    redis.call(
        "HMSET",
        committed_key,
        "session_id",
        session_id,
        "bytes",
        reserved_bytes
    )
    redis.call("PEXPIRE", committed_key, tonumber(ARGV[2]))
end
recompute_capacity()
return 1
"""

_ROLLBACK_COMMIT_SCRIPT = """-- rollback_commit
local state_key = KEYS[1]
local committed_key = KEYS[2]
if redis.call("EXISTS", committed_key) == 0 then
    return 0
end

local session_id = redis.call("HGET", committed_key, "session_id")
local committed_bytes = tonumber(redis.call("HGET", committed_key, "bytes") or "0")
local committed_field = "session:" .. session_id .. ":committed_bytes"
local current = tonumber(redis.call("HGET", state_key, committed_field) or "0")
local remaining = current - committed_bytes

redis.call("DEL", committed_key)
if remaining > 0 then
    redis.call("HSET", state_key, committed_field, remaining)
else
    redis.call("HDEL", state_key, committed_field)
end
return 1
"""

_FINALIZE_COMMIT_SCRIPT = """-- finalize_commit
return redis.call("DEL", KEYS[1])
"""

_RECONCILE_RESERVATIONS_SCRIPT = """-- reconcile_reservations
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_prefix = ARGV[1]
local entries = redis.call("HGETALL", index_key)
local removed = 0

local function decrement(field, amount)
    local current = tonumber(redis.call("HGET", state_key, field) or "0")
    local remaining = current - amount
    if remaining > 0 then
        redis.call("HSET", state_key, field, remaining)
    else
        redis.call("HDEL", state_key, field)
    end
end

for index = 1, #entries, 2 do
    local token = entries[index]
    local metadata = entries[index + 1]
    if redis.call("EXISTS", token_prefix .. token) == 0 then
        local kind, session_id, byte_text = string.match(
            metadata,
            "^([^|]+)|([^|]+)|(%d+)"
        )
        if kind then
            local reserved_bytes = tonumber(byte_text)
            decrement("reserved_bytes", reserved_bytes)
            if kind == "upload" then
                decrement("upload_count", 1)
                decrement(
                    "session:" .. session_id .. ":reserved_bytes",
                    reserved_bytes
                )
            end
        end
        redis.call("HDEL", index_key, token)
        removed = removed + 1
    end
end

local metadata_entries = redis.call("HVALS", index_key)
local observed_free = nil
local min_free_bytes = nil
for _, metadata in ipairs(metadata_entries) do
    local _, _, _, free_text, min_free_text = string.match(
        metadata,
        "^([^|]+)|([^|]+)|(%d+)|(%d+)|(%d+)$"
    )
    if free_text then
        local entry_free = tonumber(free_text)
        local entry_min_free = tonumber(min_free_text)
        if not observed_free or entry_free < observed_free then
            observed_free = entry_free
        end
        if not min_free_bytes or entry_min_free > min_free_bytes then
            min_free_bytes = entry_min_free
        end
    end
end
if observed_free then
    redis.call("HSET", state_key, "observed_free_bytes", observed_free)
    redis.call("HSET", state_key, "min_free_bytes", min_free_bytes)
else
    redis.call(
        "HDEL",
        state_key,
        "observed_free_bytes",
        "min_free_bytes"
    )
end
return removed
"""

_CLEAR_SESSION_USAGE_SCRIPT = """-- clear_session_usage
redis.call(
    "HDEL",
    KEYS[1],
    "session:" .. ARGV[1] .. ":committed_bytes"
)
return 1
"""

_ACQUIRE_SESSION_SCRIPT = """-- acquire_session
local state_key = KEYS[1]
local leases_key = KEYS[2]
local lease_key = KEYS[3]
local token = ARGV[1]
local session_id = ARGV[2]
local ttl_ms = tonumber(ARGV[3])
local lease_prefix = ARGV[4]
local cleanup_prefix = ARGV[5]

local lease_tokens = redis.call("SMEMBERS", leases_key)
for _, lease_token in ipairs(lease_tokens) do
    if redis.call("EXISTS", lease_prefix .. lease_token) == 0 then
        redis.call("SREM", leases_key, lease_token)
        local active = tonumber(
            redis.call("HGET", state_key, "active_count") or "0"
        )
        if active > 1 then
            redis.call("HSET", state_key, "active_count", active - 1)
        else
            redis.call("HDEL", state_key, "active_count")
        end
    end
end

local deleting = redis.call("HGET", state_key, "deleting")
if deleting then
    if redis.call("EXISTS", cleanup_prefix .. deleting) == 1 then
        return 1
    end
    redis.call("HDEL", state_key, "deleting")
end

redis.call("HSET", lease_key, "session_id", session_id)
redis.call("PEXPIRE", lease_key, ttl_ms)
redis.call("SADD", leases_key, token)
redis.call("HINCRBY", state_key, "active_count", 1)
return 0
"""

_RELEASE_SESSION_SCRIPT = """-- release_session
local state_key = KEYS[1]
local leases_key = KEYS[2]
local lease_key = KEYS[3]
if redis.call("EXISTS", lease_key) == 0 then
    return 0
end

redis.call("DEL", lease_key)
if redis.call("SREM", leases_key, ARGV[1]) == 1 then
    local active = tonumber(
        redis.call("HGET", state_key, "active_count") or "0"
    )
    if active > 1 then
        redis.call("HSET", state_key, "active_count", active - 1)
    else
        redis.call("HDEL", state_key, "active_count")
    end
end
return 1
"""

_CLAIM_CLEANUP_SCRIPT = """-- claim_cleanup
local state_key = KEYS[1]
local leases_key = KEYS[2]
local cleanup_key = KEYS[3]
local token = ARGV[1]
local lease_prefix = ARGV[3]
local cleanup_prefix = ARGV[4]

local lease_tokens = redis.call("SMEMBERS", leases_key)
for _, lease_token in ipairs(lease_tokens) do
    if redis.call("EXISTS", lease_prefix .. lease_token) == 0 then
        redis.call("SREM", leases_key, lease_token)
        local active = tonumber(
            redis.call("HGET", state_key, "active_count") or "0"
        )
        if active > 1 then
            redis.call("HSET", state_key, "active_count", active - 1)
        else
            redis.call("HDEL", state_key, "active_count")
        end
    end
end

local deleting = redis.call("HGET", state_key, "deleting")
if deleting then
    if redis.call("EXISTS", cleanup_prefix .. deleting) == 1 then
        return 1
    end
    redis.call("HDEL", state_key, "deleting")
end
if tonumber(redis.call("HGET", state_key, "active_count") or "0") ~= 0 then
    return 1
end

redis.call("HSET", state_key, "deleting", token)
redis.call("SET", cleanup_key, "1", "PX", tonumber(ARGV[2]))
return 0
"""

_REFRESH_CLEANUP_SCRIPT = """-- refresh_cleanup
local state_key = KEYS[1]
local cleanup_key = KEYS[2]
local deleting = redis.call("HGET", state_key, "deleting")
if not deleting or deleting ~= ARGV[1] then
    return 0
end
if redis.call("EXISTS", cleanup_key) == 0 then
    return 0
end
redis.call("PEXPIRE", cleanup_key, tonumber(ARGV[2]))
return 1
"""

_RELEASE_CLEANUP_SCRIPT = """-- release_cleanup
local state_key = KEYS[1]
local cleanup_key = KEYS[2]
local deleting = redis.call("HGET", state_key, "deleting")
if not deleting or deleting ~= ARGV[1] then
    return 0
end
redis.call("HDEL", state_key, "deleting")
redis.call("DEL", cleanup_key)
return 1
"""


class ResourceUnavailableError(RuntimeError):
    pass


class SessionDeletingError(ResourceUnavailableError):
    pass


@dataclass
class RedisReservation:
    coordinator: RedisResourceCoordinator
    kind: Literal["upload", "output"]
    token: str
    owner_id: str
    reserved_bytes: int

    def refresh(self) -> None:
        self.coordinator._refresh_reservation(self)

    def commit(self) -> None:
        """Consume this reservation once; repeated or expired commits fail."""
        self.coordinator._commit_reservation(self)

    def rollback(self) -> None:
        """Roll back this upload commit while its marker still exists."""
        self.coordinator._rollback_commit(self)

    def finalize(self) -> None:
        """Make this upload commit ineligible for rollback."""
        self.coordinator._finalize_commit(self)

    def release(self) -> None:
        self.coordinator._release_reservation(self)

    def __enter__(self) -> RedisReservation:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass
class RedisSessionLease:
    coordinator: RedisResourceCoordinator
    session_id: str
    token: str

    def refresh(self) -> None:
        self.coordinator._refresh_session(self)

    def release(self) -> None:
        self.coordinator._release_session(self)

    def __enter__(self) -> RedisSessionLease:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class RedisResourceCoordinator:
    def __init__(
        self,
        *,
        redis_connection: Redis,
        max_upload_concurrency: int,
        max_session_bytes: int,
        min_free_bytes: int,
        reservation_ttl: float,
        lease_ttl: float,
        reservation_heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL,
        lease_heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._redis = redis_connection
        self.max_upload_concurrency = _positive_int(
            max_upload_concurrency,
            "max_upload_concurrency",
        )
        self.max_session_bytes = _positive_int(
            max_session_bytes,
            "max_session_bytes",
        )
        self.min_free_bytes = _nonnegative_int(min_free_bytes, "min_free_bytes")
        reservation_ttl = _positive_seconds(reservation_ttl, "reservation_ttl")
        lease_ttl = _positive_seconds(lease_ttl, "lease_ttl")
        reservation_heartbeat_interval = _positive_seconds(
            reservation_heartbeat_interval,
            "reservation_heartbeat_interval",
        )
        lease_heartbeat_interval = _positive_seconds(
            lease_heartbeat_interval,
            "lease_heartbeat_interval",
        )
        self._reservation_ttl_ms = _ttl_milliseconds(
            max(reservation_ttl, 2 * reservation_heartbeat_interval),
            "reservation_ttl",
        )
        self._lease_ttl_ms = _ttl_milliseconds(
            max(lease_ttl, 2 * lease_heartbeat_interval),
            "lease_ttl",
        )
        self._reserve_upload_script = self._redis.register_script(
            _RESERVE_UPLOAD_SCRIPT
        )
        self._reserve_output_script = self._redis.register_script(
            _RESERVE_OUTPUT_SCRIPT
        )
        self._refresh_token_script = self._redis.register_script(_REFRESH_TOKEN_SCRIPT)
        self._release_reservation_script = self._redis.register_script(
            _RELEASE_RESERVATION_SCRIPT
        )
        self._commit_reservation_script = self._redis.register_script(
            _COMMIT_RESERVATION_SCRIPT
        )
        self._rollback_commit_script = self._redis.register_script(
            _ROLLBACK_COMMIT_SCRIPT
        )
        self._finalize_commit_script = self._redis.register_script(
            _FINALIZE_COMMIT_SCRIPT
        )
        self._reconcile_reservations_script = self._redis.register_script(
            _RECONCILE_RESERVATIONS_SCRIPT
        )
        self._clear_session_usage_script = self._redis.register_script(
            _CLEAR_SESSION_USAGE_SCRIPT
        )
        self._acquire_session_script = self._redis.register_script(
            _ACQUIRE_SESSION_SCRIPT
        )
        self._release_session_script = self._redis.register_script(
            _RELEASE_SESSION_SCRIPT
        )
        self._claim_cleanup_script = self._redis.register_script(_CLAIM_CLEANUP_SCRIPT)
        self._refresh_cleanup_script = self._redis.register_script(
            _REFRESH_CLEANUP_SCRIPT
        )
        self._release_cleanup_script = self._redis.register_script(
            _RELEASE_CLEANUP_SCRIPT
        )

    def reserve_upload(
        self,
        session_id: str,
        requested_bytes: int,
        *,
        free_bytes: int,
    ) -> RedisReservation:
        _validate_session_id(session_id)
        requested_bytes = _positive_int(requested_bytes, "requested_bytes")
        free_bytes = _nonnegative_int(free_bytes, "free_bytes")
        token = uuid4().hex
        result = int(
            self._reserve_upload_script(
                keys=[
                    _STATE_KEY,
                    _RESERVATION_INDEX_KEY,
                    _reservation_key(token),
                ],
                args=[
                    token,
                    session_id,
                    requested_bytes,
                    free_bytes,
                    self.max_upload_concurrency,
                    self.max_session_bytes,
                    self.min_free_bytes,
                    self._reservation_ttl_ms,
                ],
            )
        )
        if result == 1:
            raise ResourceUnavailableError("Upload concurrency limit reached")
        if result == 2:
            raise ResourceUnavailableError("Upload session byte limit reached")
        if result == 3:
            raise ResourceUnavailableError("Insufficient free disk capacity")
        return RedisReservation(
            coordinator=self,
            kind="upload",
            token=token,
            owner_id=session_id,
            reserved_bytes=requested_bytes,
        )

    def reserve_output(
        self,
        task_id: str,
        session_id: str,
        requested_bytes: int,
        *,
        free_bytes: int,
        min_free_bytes: int = 0,
    ) -> RedisReservation:
        _validate_session_id(session_id)
        requested_bytes = _positive_int(requested_bytes, "requested_bytes")
        free_bytes = _nonnegative_int(free_bytes, "free_bytes")
        min_free_bytes = max(
            self.min_free_bytes,
            _nonnegative_int(min_free_bytes, "min_free_bytes"),
        )
        token = uuid4().hex
        result = int(
            self._reserve_output_script(
                keys=[
                    _STATE_KEY,
                    _RESERVATION_INDEX_KEY,
                    _reservation_key(token),
                ],
                args=[
                    token,
                    task_id,
                    session_id,
                    requested_bytes,
                    free_bytes,
                    min_free_bytes,
                    self._reservation_ttl_ms,
                ],
            )
        )
        if result != 0:
            raise ResourceUnavailableError("Insufficient free disk capacity")
        return RedisReservation(
            coordinator=self,
            kind="output",
            token=token,
            owner_id=task_id,
            reserved_bytes=requested_bytes,
        )

    def acquire_session(self, session_id: str) -> RedisSessionLease:
        _validate_session_id(session_id)
        token = uuid4().hex
        result = int(
            self._acquire_session_script(
                keys=[
                    _session_state_key(session_id),
                    _session_leases_key(session_id),
                    _lease_key(token),
                ],
                args=[
                    token,
                    session_id,
                    self._lease_ttl_ms,
                    _LEASE_PREFIX,
                    _CLEANUP_PREFIX,
                ],
            )
        )
        if result != 0:
            raise SessionDeletingError("Session is being deleted")
        return RedisSessionLease(self, session_id, token)

    def claim_session_cleanup(self, session_id: str) -> str | None:
        _validate_session_id(session_id)
        token = uuid4().hex
        result = int(
            self._claim_cleanup_script(
                keys=[
                    _session_state_key(session_id),
                    _session_leases_key(session_id),
                    _cleanup_key(token),
                ],
                args=[
                    token,
                    self._lease_ttl_ms,
                    _LEASE_PREFIX,
                    _CLEANUP_PREFIX,
                ],
            )
        )
        return token if result == 0 else None

    def refresh_cleanup_claim(self, session_id: str, token: str) -> None:
        _validate_session_id(session_id)
        refreshed = self._refresh_cleanup_script(
            keys=[
                _session_state_key(session_id),
                _cleanup_key(token),
            ],
            args=[
                token,
                self._lease_ttl_ms,
            ],
        )
        if not refreshed:
            raise ResourceUnavailableError("Cleanup claim expired")

    def release_cleanup_claim(self, session_id: str, token: str) -> None:
        _validate_session_id(session_id)
        self._release_cleanup_script(
            keys=[
                _session_state_key(session_id),
                _cleanup_key(token),
            ],
            args=[token],
        )

    def clear_session_usage(self, session_id: str) -> None:
        _validate_session_id(session_id)
        self._clear_session_usage_script(
            keys=[_STATE_KEY],
            args=[session_id],
        )

    def reconcile_reservations(self) -> int:
        return int(
            self._reconcile_reservations_script(
                keys=[
                    _STATE_KEY,
                    _RESERVATION_INDEX_KEY,
                ],
                args=[
                    _RESERVATION_PREFIX,
                ],
            )
        )

    def _refresh_reservation(self, reservation: RedisReservation) -> None:
        refreshed = self._refresh_token_script(
            keys=[_reservation_key(reservation.token)],
            args=[self._reservation_ttl_ms],
        )
        if not refreshed:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} reservation expired"
            )

    def _release_reservation(self, reservation: RedisReservation) -> None:
        self._release_reservation_script(
            keys=[
                _STATE_KEY,
                _RESERVATION_INDEX_KEY,
                _reservation_key(reservation.token),
            ],
            args=[reservation.token],
        )

    def _commit_reservation(self, reservation: RedisReservation) -> None:
        committed = int(
            self._commit_reservation_script(
                keys=[
                    _STATE_KEY,
                    _RESERVATION_INDEX_KEY,
                    _reservation_key(reservation.token),
                    _committed_key(reservation.token),
                ],
                args=[
                    reservation.token,
                    self._reservation_ttl_ms,
                ],
            )
        )
        if not committed:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} reservation expired"
            )

    def _rollback_commit(self, reservation: RedisReservation) -> None:
        self._rollback_commit_script(
            keys=[
                _STATE_KEY,
                _committed_key(reservation.token),
            ],
            args=[],
        )

    def _finalize_commit(self, reservation: RedisReservation) -> None:
        self._finalize_commit_script(
            keys=[_committed_key(reservation.token)],
            args=[],
        )

    def _refresh_session(self, lease: RedisSessionLease) -> None:
        refreshed = self._refresh_token_script(
            keys=[_lease_key(lease.token)],
            args=[self._lease_ttl_ms],
        )
        if not refreshed:
            raise ResourceUnavailableError("Session lease expired")

    def _release_session(self, lease: RedisSessionLease) -> None:
        self._release_session_script(
            keys=[
                _session_state_key(lease.session_id),
                _session_leases_key(lease.session_id),
                _lease_key(lease.token),
            ],
            args=[lease.token],
        )


def _reservation_key(token: str) -> str:
    return f"{_RESERVATION_PREFIX}{token}"


def _committed_key(token: str) -> str:
    return f"{_COMMITTED_PREFIX}{token}"


def _lease_key(token: str) -> str:
    return f"{_LEASE_PREFIX}{token}"


def _cleanup_key(token: str) -> str:
    return f"{_CLEANUP_PREFIX}{token}"


def _session_state_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}"


def _session_leases_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}:leases"


def _validate_session_id(value: str) -> str:
    if not _SESSION_PATTERN.fullmatch(value):
        raise ValueError("Invalid hosted session ID")
    return value


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _ttl_milliseconds(value: float, name: str) -> int:
    value = _positive_seconds(value, name)
    return max(1, math.ceil(value * 1000))


def _positive_seconds(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)
