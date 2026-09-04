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
_LEASE_PREFIX = "dt:resource:lease:"
_CLEANUP_PREFIX = "dt:resource:cleanup:"
_SESSION_PATTERN = re.compile(r"^[a-f0-9]{32}$")

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

local session_field = "session:" .. session_id .. ":bytes"
local session_bytes = tonumber(redis.call("HGET", state_key, session_field) or "0")
if session_bytes + requested > max_session_bytes then
    return 2
end

local reserved_bytes = tonumber(
    redis.call("HGET", state_key, "reserved_bytes") or "0"
)
if free_bytes - reserved_bytes - requested < min_free_bytes then
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
    requested
)
redis.call("PEXPIRE", token_key, ttl_ms)
redis.call(
    "HSET",
    index_key,
    token,
    "upload|" .. session_id .. "|" .. requested
)
redis.call("HINCRBY", state_key, "reserved_bytes", requested)
redis.call("HINCRBY", state_key, "upload_count", 1)
redis.call("HINCRBY", state_key, session_field, requested)
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
if free_bytes - reserved_bytes - requested < min_free_bytes then
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
    requested
)
redis.call("PEXPIRE", token_key, ttl_ms)
redis.call(
    "HSET",
    index_key,
    token,
    "output|" .. session_id .. "|" .. requested
)
redis.call("HINCRBY", state_key, "reserved_bytes", requested)
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

redis.call("DEL", token_key)
redis.call("HDEL", index_key, ARGV[1])
decrement("reserved_bytes", reserved_bytes)
if kind == "upload" then
    decrement("upload_count", 1)
    decrement("session:" .. session_id .. ":bytes", reserved_bytes)
end
return 1
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
            "^([^|]+)|([^|]+)|(%d+)$"
        )
        if kind then
            local reserved_bytes = tonumber(byte_text)
            decrement("reserved_bytes", reserved_bytes)
            if kind == "upload" then
                decrement("upload_count", 1)
                decrement(
                    "session:" .. session_id .. ":bytes",
                    reserved_bytes
                )
            end
        end
        redis.call("HDEL", index_key, token)
        removed = removed + 1
    end
end
return removed
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
redis.call("SET", cleanup_key, "1")
return 0
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
        self._reservation_ttl_ms = _ttl_milliseconds(
            reservation_ttl,
            "reservation_ttl",
        )
        self._lease_ttl_ms = _ttl_milliseconds(lease_ttl, "lease_ttl")

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
            self._redis.eval(
                _RESERVE_UPLOAD_SCRIPT,
                3,
                _STATE_KEY,
                _RESERVATION_INDEX_KEY,
                _reservation_key(token),
                token,
                session_id,
                requested_bytes,
                free_bytes,
                self.max_upload_concurrency,
                self.max_session_bytes,
                self.min_free_bytes,
                self._reservation_ttl_ms,
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
            self._redis.eval(
                _RESERVE_OUTPUT_SCRIPT,
                3,
                _STATE_KEY,
                _RESERVATION_INDEX_KEY,
                _reservation_key(token),
                token,
                task_id,
                session_id,
                requested_bytes,
                free_bytes,
                min_free_bytes,
                self._reservation_ttl_ms,
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
            self._redis.eval(
                _ACQUIRE_SESSION_SCRIPT,
                3,
                _session_state_key(session_id),
                _session_leases_key(session_id),
                _lease_key(token),
                token,
                session_id,
                self._lease_ttl_ms,
                _LEASE_PREFIX,
                _CLEANUP_PREFIX,
            )
        )
        if result != 0:
            raise SessionDeletingError("Session is being deleted")
        return RedisSessionLease(self, session_id, token)

    def claim_session_cleanup(self, session_id: str) -> str | None:
        _validate_session_id(session_id)
        token = uuid4().hex
        result = int(
            self._redis.eval(
                _CLAIM_CLEANUP_SCRIPT,
                3,
                _session_state_key(session_id),
                _session_leases_key(session_id),
                _cleanup_key(token),
                token,
                self._lease_ttl_ms,
                _LEASE_PREFIX,
                _CLEANUP_PREFIX,
            )
        )
        return token if result == 0 else None

    def release_cleanup_claim(self, session_id: str, token: str) -> None:
        _validate_session_id(session_id)
        self._redis.eval(
            _RELEASE_CLEANUP_SCRIPT,
            2,
            _session_state_key(session_id),
            _cleanup_key(token),
            token,
        )

    def reconcile_reservations(self) -> int:
        return int(
            self._redis.eval(
                _RECONCILE_RESERVATIONS_SCRIPT,
                2,
                _STATE_KEY,
                _RESERVATION_INDEX_KEY,
                _RESERVATION_PREFIX,
            )
        )

    def _refresh_reservation(self, reservation: RedisReservation) -> None:
        refreshed = self._redis.eval(
            _REFRESH_TOKEN_SCRIPT,
            1,
            _reservation_key(reservation.token),
            self._reservation_ttl_ms,
        )
        if not refreshed:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} reservation expired"
            )

    def _release_reservation(self, reservation: RedisReservation) -> None:
        self._redis.eval(
            _RELEASE_RESERVATION_SCRIPT,
            3,
            _STATE_KEY,
            _RESERVATION_INDEX_KEY,
            _reservation_key(reservation.token),
            reservation.token,
        )

    def _refresh_session(self, lease: RedisSessionLease) -> None:
        refreshed = self._redis.eval(
            _REFRESH_TOKEN_SCRIPT,
            1,
            _lease_key(lease.token),
            self._lease_ttl_ms,
        )
        if not refreshed:
            raise ResourceUnavailableError("Session lease expired")

    def _release_session(self, lease: RedisSessionLease) -> None:
        self._redis.eval(
            _RELEASE_SESSION_SCRIPT,
            3,
            _session_state_key(lease.session_id),
            _session_leases_key(lease.session_id),
            _lease_key(lease.token),
            lease.token,
        )


def _reservation_key(token: str) -> str:
    return f"{_RESERVATION_PREFIX}{token}"


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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return max(1, math.ceil(value * 1000))
