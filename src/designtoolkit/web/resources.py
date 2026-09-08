"""Coordinate hosted resources using standalone Redis Lua scripts.

The scripts target the standalone Redis instance used by Docker Compose. Redis
Cluster support is intentionally out of scope.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import time
from typing import Literal
from uuid import uuid4

from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

_STATE_KEY = "dt:resource:state"
_RESERVATION_INDEX_KEY = "dt:resource:reservations"
_RESERVATION_PREFIX = "dt:resource:reservation:"
_RESERVATION_RECEIPT_PREFIX = "dt:resource:reservation-receipt:"
_COMMITTED_PREFIX = "dt:resource:committed:"
_COMMITTED_UPLOAD_PREFIX = "dt:resource:committed-upload:"
_RELEASED_ASSET_PREFIX = "dt:resource:asset-released:"
_PENDING_ASSET_RELEASE_PREFIX = "dt:resource:asset-release-pending:"
_PENDING_ASSET_RELEASE_INDEX_KEY = "dt:resource:asset-releases-pending"
_LEASE_PREFIX = "dt:resource:lease:"
_CLEANUP_PREFIX = "dt:resource:cleanup:"
_CLEANUP_GENERATION_PREFIX = "dt:resource:cleanup-generation:"
_LEGACY_CLEANUP_PREFIX = "dt:resource:legacy-cleanup:"
_TASK_RESOURCE_PREFIX = "dt:resource:task:"
_TASK_PIN_PREFIX = "dt:resource:task-pin:"
_ASSET_TASK_PREFIX = "dt:resource:asset-tasks:"
_UPLOAD_INFLIGHT_PREFIX = "dt:resource:upload-inflight:"
_SESSION_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_ASSET_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_DEFAULT_HEARTBEAT_INTERVAL = 5.0
_RESERVATION_RECOVERY_ATTEMPTS = 3
_REDIS_TRANSPORT_ERRORS = (RedisConnectionError, RedisTimeoutError)

_REGISTER_UPLOAD_SCRIPT = """-- register_upload
if redis.call("EXISTS", KEYS[1]) == 1 then
    if redis.call("HGET", KEYS[1], "token") ~= ARGV[1] then
        return 1
    end
    redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[2]))
    return 2
end
if redis.call("EXISTS", KEYS[2]) == 1 then
    if redis.call("TYPE", KEYS[2])["ok"] ~= "hash"
        or redis.call("HGET", KEYS[2], "session_id") ~= ARGV[3]
        or redis.call("HGET", KEYS[2], "asset_id") ~= ARGV[4] then
        return 4
    end
    if redis.call("HGET", KEYS[2], "finalized") ~= "1" then
        return 3
    end
    redis.call("DEL", KEYS[2])
    redis.call("SREM", KEYS[3], KEYS[2])
end
if redis.call("EXISTS", KEYS[4]) == 1 then
    return 3
end
redis.call(
    "HMSET",
    KEYS[1],
    "token",
    ARGV[1],
    "state",
    "inflight"
)
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[2]))
return 0
"""

_REFRESH_UPLOAD_SCRIPT = """-- refresh_upload
if redis.call("HGET", KEYS[1], "token") ~= ARGV[1] then
    return 0
end
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[2]))
if redis.call("HGET", KEYS[1], "state") == "cancelled" then
    return 2
end
return 1
"""

_CANCEL_UPLOAD_SCRIPT = """-- cancel_upload
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
redis.call("HSET", KEYS[1], "state", "cancelled")
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[1]))
return 1
"""

_FINISH_UPLOAD_SCRIPT = """-- finish_upload
if redis.call("HGET", KEYS[1], "token") ~= ARGV[1] then
    return 0
end
return redis.call("DEL", KEYS[1])
"""

_RESERVE_UPLOAD_SCRIPT = """-- reserve_upload
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
local receipt_key = KEYS[4]
local token = ARGV[1]
local session_id = ARGV[2]
local requested = tonumber(ARGV[3])
local free_bytes = tonumber(ARGV[4])
local max_uploads = tonumber(ARGV[5])
local max_session_bytes = tonumber(ARGV[6])
local min_free_bytes = tonumber(ARGV[7])
local ttl_ms = tonumber(ARGV[8])
local receipt_ttl_ms = tonumber(ARGV[9])
local index_metadata = "upload|" .. session_id .. "|" .. requested ..
    "|" .. free_bytes .. "|" .. min_free_bytes

local function metadata_matches(key)
    return redis.call("HGET", key, "kind") == "upload"
        and redis.call("HGET", key, "owner_id") == session_id
        and redis.call("HGET", key, "session_id") == session_id
        and redis.call("HGET", key, "bytes") == ARGV[3]
        and redis.call("HGET", key, "free_bytes") == ARGV[4]
        and redis.call("HGET", key, "min_free_bytes") == ARGV[7]
        and redis.call("HGET", key, "asset_id") == ARGV[10]
        and redis.call("HGET", key, "generation_id") == ARGV[11]
end

if redis.call("EXISTS", receipt_key) == 1 then
    if not metadata_matches(receipt_key) then
        return 5
    end
    if redis.call("HGET", receipt_key, "state") ~= "reserved" then
        return 6
    end
    if redis.call("HGET", index_key, token) ~= index_metadata then
        return 5
    end
    if redis.call("EXISTS", token_key) == 1 then
        if metadata_matches(token_key) then
            return 4
        end
        return 5
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
        min_free_bytes,
        "asset_id",
        ARGV[10],
        "generation_id",
        ARGV[11]
    )
    redis.call("PEXPIRE", token_key, ttl_ms)
    redis.call("PEXPIRE", receipt_key, receipt_ttl_ms)
    return 4
end

if redis.call("EXISTS", token_key) == 1
    or redis.call("HEXISTS", index_key, token) == 1 then
    return 5
end

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
    min_free_bytes,
    "asset_id",
    ARGV[10],
    "generation_id",
    ARGV[11]
)
redis.call("PEXPIRE", token_key, ttl_ms)
redis.call(
    "HMSET",
    receipt_key,
    "state",
    "reserved",
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
    min_free_bytes,
    "asset_id",
    ARGV[10],
    "generation_id",
    ARGV[11]
)
redis.call("PEXPIRE", receipt_key, receipt_ttl_ms)
redis.call(
    "HSET",
    index_key,
    token,
    index_metadata
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
local receipt_key = KEYS[5]
local token = ARGV[1]
local task_id = ARGV[2]
local session_id = ARGV[3]
local requested = tonumber(ARGV[4])
local free_bytes = tonumber(ARGV[5])
local min_free_bytes = tonumber(ARGV[6])
local ttl_ms = tonumber(ARGV[7])
local heartbeat_at = ARGV[8]
local receipt_ttl_ms = tonumber(ARGV[9])
local index_metadata = "output|" .. session_id .. "|" .. requested ..
    "|" .. free_bytes .. "|" .. min_free_bytes

local function metadata_matches(key)
    return redis.call("HGET", key, "kind") == "output"
        and redis.call("HGET", key, "owner_id") == task_id
        and redis.call("HGET", key, "session_id") == session_id
        and redis.call("HGET", key, "bytes") == ARGV[4]
        and redis.call("HGET", key, "free_bytes") == ARGV[5]
        and redis.call("HGET", key, "min_free_bytes") == ARGV[6]
end

if redis.call("EXISTS", receipt_key) == 1 then
    if not metadata_matches(receipt_key) then
        return 3
    end
    if redis.call("HGET", receipt_key, "state") ~= "reserved" then
        return 4
    end
    if redis.call("HGET", index_key, token) ~= index_metadata then
        return 3
    end
    if redis.call("EXISTS", token_key) == 1 then
        if metadata_matches(token_key) then
            return 2
        end
        return 3
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
    redis.call("PEXPIRE", receipt_key, receipt_ttl_ms)
    redis.call(
        "HMSET",
        KEYS[4],
        "session_id",
        session_id,
        "output_token",
        token,
        "heartbeat_at",
        heartbeat_at
    )
    redis.call("PEXPIRE", KEYS[4], ttl_ms)
    return 2
end

if redis.call("EXISTS", token_key) == 1
    or redis.call("HEXISTS", index_key, token) == 1 then
    return 3
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
    "HMSET",
    receipt_key,
    "state",
    "reserved",
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
redis.call("PEXPIRE", receipt_key, receipt_ttl_ms)
redis.call(
    "HSET",
    index_key,
    token,
    index_metadata
)
redis.call("HINCRBY", state_key, "reserved_bytes", requested)
redis.call("HINCRBY", state_key, "output_reserved_bytes", requested)
redis.call("HSET", state_key, "observed_free_bytes", effective_free)
redis.call("HSET", state_key, "min_free_bytes", effective_min_free)
redis.call(
    "HMSET",
    KEYS[4],
    "session_id",
    session_id,
    "output_token",
    token,
    "heartbeat_at",
    heartbeat_at
)
redis.call("PEXPIRE", KEYS[4], ttl_ms)
return 0
"""

_REFRESH_TOKEN_SCRIPT = """-- refresh_token
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[1]))
return 1
"""

_REFRESH_RESERVATION_SCRIPT = """-- refresh_reservation
if redis.call("EXISTS", KEYS[1]) == 0
    or redis.call("HGET", KEYS[2], "state") ~= "reserved" then
    return 0
end
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[1]))
redis.call("PEXPIRE", KEYS[2], tonumber(ARGV[2]))
return 1
"""

_RESERVATION_RECOVERY_STATE_SCRIPT = """-- reservation_recovery_state
local token_key = KEYS[1]
local index_key = KEYS[2]
local receipt_key = KEYS[3]
local token = ARGV[1]

local function metadata_matches(key)
    return redis.call("HGET", key, "kind") == ARGV[3]
        and redis.call("HGET", key, "owner_id") == ARGV[4]
        and redis.call("HGET", key, "session_id") == ARGV[5]
        and redis.call("HGET", key, "bytes") == ARGV[6]
        and redis.call("HGET", key, "free_bytes") == ARGV[7]
        and redis.call("HGET", key, "min_free_bytes") == ARGV[8]
        and (
            ARGV[9] == ""
            or (
                redis.call("HGET", key, "asset_id") == ARGV[9]
                and redis.call("HGET", key, "generation_id") == ARGV[10]
            )
        )
end

if redis.call("EXISTS", receipt_key) == 0 then
    return 0
end
if not metadata_matches(receipt_key) then
    return 2
end
if redis.call("HGET", receipt_key, "state") ~= "reserved" then
    return 0
end
if redis.call("HGET", index_key, token) ~= ARGV[2] then
    return 2
end
if redis.call("EXISTS", token_key) == 0 then
    return 0
end
if not metadata_matches(token_key) then
    return 2
end
return 1
"""

_RELEASE_RESERVATION_SCRIPT = """-- release_reservation
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
local task_key = KEYS[4]
local receipt_key = KEYS[5]
local receipt_ttl_ms = tonumber(ARGV[2])
if redis.call("HGET", task_key, "output_token") == ARGV[1] then
    redis.call("HDEL", task_key, "output_token")
    if redis.call("HEXISTS", task_key, "lease_token") == 0 then
        redis.call("DEL", task_key)
    end
end

local metadata_key = token_key
if redis.call("EXISTS", receipt_key) == 1 then
    local receipt_state = redis.call("HGET", receipt_key, "state")
    if receipt_state ~= "reserved" then
        return 0
    end
    metadata_key = receipt_key
elseif redis.call("EXISTS", token_key) == 0 then
    return 0
end

local kind = redis.call("HGET", metadata_key, "kind")
local session_id = redis.call("HGET", metadata_key, "session_id")
local reserved_bytes = tonumber(
    redis.call("HGET", metadata_key, "bytes") or "0"
)
local free_bytes = redis.call("HGET", metadata_key, "free_bytes")
local min_free_bytes = redis.call("HGET", metadata_key, "min_free_bytes")
local index_metadata = kind .. "|" .. session_id .. "|" .. reserved_bytes ..
    "|" .. free_bytes .. "|" .. min_free_bytes
if redis.call("HGET", index_key, ARGV[1]) ~= index_metadata then
    return 2
end
if redis.call("EXISTS", token_key) == 1 then
    for _, field in ipairs(
        {
            "kind",
            "owner_id",
            "session_id",
            "bytes",
            "free_bytes",
            "min_free_bytes",
            "asset_id",
            "generation_id"
        }
    ) do
        if redis.call("HGET", token_key, field)
            ~= redis.call("HGET", metadata_key, field) then
            return 2
        end
    end
end

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
if redis.call("EXISTS", receipt_key) == 1 then
    redis.call("HSET", receipt_key, "state", "released")
    redis.call("PEXPIRE", receipt_key, receipt_ttl_ms)
end
decrement("reserved_bytes", reserved_bytes)
if kind == "upload" then
    decrement("upload_count", 1)
    decrement(
        "session:" .. session_id .. ":reserved_bytes",
        reserved_bytes
    )
elseif kind == "output" then
    decrement("output_reserved_bytes", reserved_bytes)
end
recompute_capacity()
return 1
"""

_COMMIT_RESERVATION_SCRIPT = """-- commit_reservation
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
local committed_key = KEYS[4]
local receipt_key = KEYS[5]
local committed_upload_key = KEYS[6]
local committed_upload_index_key = KEYS[7]
if redis.call("HGET", committed_key, "state") == "committed" then
    return 2
end
if redis.call("EXISTS", token_key) == 0 then
    return 0
end
if redis.call("EXISTS", receipt_key) == 1
    and redis.call("HGET", receipt_key, "state") ~= "reserved" then
    return 0
end

local kind = redis.call("HGET", token_key, "kind")
local session_id = redis.call("HGET", token_key, "session_id")
local reserved_bytes = tonumber(redis.call("HGET", token_key, "bytes") or "0")
local asset_id = redis.call("HGET", token_key, "asset_id")
local generation_id = redis.call("HGET", token_key, "generation_id")
if kind == "upload" and asset_id and generation_id
    and asset_id ~= "" and generation_id ~= ""
    and redis.call("EXISTS", committed_upload_key) == 1 then
    return 3
end
if redis.call("EXISTS", receipt_key) == 1 then
    for _, field in ipairs(
        {"kind", "owner_id", "session_id", "bytes", "free_bytes", "min_free_bytes"}
    ) do
        if redis.call("HGET", token_key, field)
            ~= redis.call("HGET", receipt_key, field) then
            return 0
        end
    end
end

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
if redis.call("EXISTS", receipt_key) == 1 then
    redis.call("HSET", receipt_key, "state", "committed")
    redis.call("PEXPIRE", receipt_key, tonumber(ARGV[3]))
end
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
    if asset_id and generation_id
        and asset_id ~= "" and generation_id ~= "" then
        redis.call(
            "HMSET",
            committed_upload_key,
            "session_id",
            session_id,
            "asset_id",
            asset_id,
            "generation_id",
            generation_id,
            "bytes",
            reserved_bytes,
            "reservation_token",
            ARGV[1]
        )
        redis.call("SADD", committed_upload_index_key, committed_upload_key)
    end
end
redis.call(
    "HMSET",
    committed_key,
    "state",
    "committed",
    "kind",
    kind,
    "session_id",
    session_id,
    "bytes",
    reserved_bytes
)
if asset_id and generation_id
    and asset_id ~= "" and generation_id ~= "" then
    redis.call(
        "HMSET",
        committed_key,
        "asset_id",
        asset_id,
        "generation_id",
        generation_id
    )
end
redis.call("PEXPIRE", committed_key, tonumber(ARGV[2]))
recompute_capacity()
return 1
"""

_ROLLBACK_COMMIT_SCRIPT = """-- rollback_commit
local state_key = KEYS[1]
local committed_key = KEYS[2]
local ledger_key = KEYS[3]
local ledger_index_key = KEYS[4]
local receipt_key = KEYS[5]
local release_marker_key = KEYS[6]
local marker_ttl_ms = tonumber(ARGV[3])
local expected_kind = ARGV[4]
local expected_session = ARGV[5]
local expected_bytes = ARGV[6]
local expected_asset = ARGV[7]
local expected_generation = ARGV[8]
local receipt_state = redis.call("HGET", receipt_key, "state")
if redis.call("HGET", committed_key, "state") == "rolled_back"
    or receipt_state == "rolled_back" then
    return 2
end
local generation_upload = expected_kind == "upload"
    and expected_asset ~= "" and expected_generation ~= ""
if not generation_upload and redis.call("EXISTS", committed_key) == 0 then
    return 0
end
local function metadata_matches(key)
    if redis.call("HGET", key, "kind") ~= expected_kind
        or redis.call("HGET", key, "bytes") ~= expected_bytes
        or (
            expected_session ~= ""
            and redis.call("HGET", key, "session_id") ~= expected_session
        ) then
        return false
    end
    if generation_upload and (
        redis.call("HGET", key, "asset_id") ~= expected_asset
        or redis.call("HGET", key, "generation_id") ~= expected_generation
    ) then
        return false
    end
    return true
end
if redis.call("EXISTS", committed_key) == 1
    and not metadata_matches(committed_key) then
    return 3
end
if redis.call("EXISTS", receipt_key) == 1 then
    if redis.call("HGET", receipt_key, "kind") ~= expected_kind
        or redis.call("HGET", receipt_key, "bytes") ~= expected_bytes
        or (
            expected_session ~= ""
            and redis.call("HGET", receipt_key, "session_id")
                ~= expected_session
        ) then
        return 3
    end
    if generation_upload and (
        redis.call("HGET", receipt_key, "asset_id") ~= expected_asset
        or redis.call("HGET", receipt_key, "generation_id")
            ~= expected_generation
    ) then
        return 3
    end
end
if generation_upload then
    if redis.call("EXISTS", release_marker_key) == 1 then
        if redis.call("HGET", release_marker_key, "session_id")
                ~= expected_session
            or redis.call("HGET", release_marker_key, "asset_id")
                ~= expected_asset
            or redis.call("HGET", release_marker_key, "generation_id")
                ~= expected_generation then
            return 3
        end
        local marker_state = redis.call("HGET", release_marker_key, "state")
        if redis.call("EXISTS", receipt_key) == 1 then
            if marker_state == "rolled_back" then
                redis.call("HSET", receipt_key, "state", "rolled_back")
            else
                redis.call("HSET", receipt_key, "state", "released")
            end
            redis.call("PEXPIRE", receipt_key, tonumber(ARGV[2]))
        end
        redis.call("HSET", committed_key, "state", "rolled_back")
        redis.call("PEXPIRE", committed_key, tonumber(ARGV[1]))
        return 2
    end
    if redis.call("EXISTS", ledger_key) == 0 then
        if receipt_state ~= "committed" and receipt_state ~= "released" then
            return 0
        end
        redis.call("HSET", receipt_key, "state", "released")
        redis.call("PEXPIRE", receipt_key, tonumber(ARGV[2]))
        redis.call("HSET", committed_key, "state", "rolled_back")
        redis.call("PEXPIRE", committed_key, tonumber(ARGV[1]))
        return 2
    end
    if redis.call("HGET", ledger_key, "session_id") ~= expected_session
        or redis.call("HGET", ledger_key, "asset_id") ~= expected_asset
        or redis.call("HGET", ledger_key, "generation_id")
            ~= expected_generation
        or redis.call("HGET", ledger_key, "bytes") ~= expected_bytes then
        return 3
    end
end
local committed_bytes = tonumber(
    redis.call("HGET", ledger_key, "bytes") or "0"
)
if committed_bytes <= 0 then
    return 4
end
if expected_kind == "upload" then
    local committed_field = "session:" .. expected_session .. ":committed_bytes"
    local current = tonumber(redis.call("HGET", state_key, committed_field) or "0")
    if committed_bytes > current then
        return 4
    end
    local remaining = current - committed_bytes
    if remaining > 0 then
        redis.call("HSET", state_key, committed_field, remaining)
    else
        redis.call("HDEL", state_key, committed_field)
    end
end
if generation_upload then
    redis.call("DEL", ledger_key)
    redis.call("SREM", ledger_index_key, ledger_key)
    redis.call(
        "HMSET",
        release_marker_key,
        "session_id",
        expected_session,
        "asset_id",
        expected_asset,
        "generation_id",
        expected_generation,
        "bytes",
        committed_bytes,
        "state",
        "rolled_back",
        "finalized",
        "1"
    )
    redis.call("PEXPIRE", release_marker_key, marker_ttl_ms)
end
if redis.call("EXISTS", receipt_key) == 1 then
    redis.call("HSET", receipt_key, "state", "rolled_back")
    redis.call("PEXPIRE", receipt_key, tonumber(ARGV[2]))
end
redis.call("HSET", committed_key, "state", "rolled_back")
redis.call("PEXPIRE", committed_key, tonumber(ARGV[1]))
return 1
"""

_FINALIZE_COMMIT_SCRIPT = """-- finalize_commit
return redis.call("DEL", KEYS[1])
"""

_RESERVATION_STATE_SCRIPT = """-- reservation_state
if redis.call("EXISTS", KEYS[1]) == 1 then
    return 1
end
if redis.call("HGET", KEYS[2], "state") == "committed" then
    return 2
end
if ARGV[1] == "1"
    and redis.call("HGET", KEYS[3], "state") == "committed"
    and (
        redis.call("EXISTS", KEYS[4]) == 1
        or redis.call("EXISTS", KEYS[5]) == 1
    ) then
    return 2
end
return 0
"""

_RECONCILE_RESERVATIONS_SCRIPT = """-- reconcile_reservations
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_prefix = ARGV[1]
local receipt_prefix = ARGV[2]
local receipt_ttl_ms = tonumber(ARGV[3])
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
            elseif kind == "output" then
                decrement("output_reserved_bytes", reserved_bytes)
            end
            local receipt_key = receipt_prefix .. token
            if redis.call("HGET", receipt_key, "state") == "reserved" then
                redis.call("HSET", receipt_key, "state", "expired")
                redis.call("PEXPIRE", receipt_key, receipt_ttl_ms)
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
local marker_keys = redis.call("SMEMBERS", KEYS[2])
for _, marker_key in ipairs(marker_keys) do
    local marker_asset = redis.call("HGET", marker_key, "asset_id")
    local marker_generation = redis.call("HGET", marker_key, "generation_id")
    if marker_asset and marker_generation then
        local pending_key = ARGV[2] .. ARGV[1] .. ":" .. marker_asset
        if redis.call("GET", pending_key) == marker_generation then
            redis.call("DEL", pending_key)
            redis.call("SREM", KEYS[4], pending_key)
        end
    end
    redis.call("DEL", marker_key)
end
redis.call("DEL", KEYS[2])
local ledger_keys = redis.call("SMEMBERS", KEYS[3])
for _, ledger_key in ipairs(ledger_keys) do
    redis.call("DEL", ledger_key)
end
redis.call("DEL", KEYS[3])
if redis.call("SCARD", KEYS[4]) == 0 then
    redis.call("DEL", KEYS[4])
end
redis.call(
    "HDEL",
    KEYS[1],
    "session:" .. ARGV[1] .. ":committed_bytes"
)
return 1
"""

_CAPTURE_SESSION_USAGE_SCRIPT = """-- capture_session_usage
local deleting = redis.call("HGET", KEYS[2], "deleting")
if not deleting or deleting ~= ARGV[2] then
    return {0}
end
if redis.call("EXISTS", KEYS[4]) == 0 then
    return {0}
end

local result = {
    1,
    tonumber(
        redis.call(
            "HGET",
            KEYS[1],
            "session:" .. ARGV[1] .. ":committed_bytes"
        ) or "0"
    ),
    redis.call("SCARD", KEYS[5])
}
local ledger_keys = redis.call("SMEMBERS", KEYS[5])
for _, ledger_key in ipairs(ledger_keys) do
    table.insert(result, ledger_key)
end
local marker_keys = redis.call("SMEMBERS", KEYS[3])
for _, marker_key in ipairs(marker_keys) do
    table.insert(result, marker_key)
end
return result
"""

_CLEAR_SESSION_USAGE_GENERATION_SCRIPT = """-- clear_session_usage_generation
local deleting = redis.call("HGET", KEYS[2], "deleting")
if not deleting or deleting ~= ARGV[2] then
    return 3
end
if redis.call("EXISTS", KEYS[3]) == 0 then
    return 3
end
if redis.call("EXISTS", KEYS[5]) == 1 then
    local marker_type = redis.call("TYPE", KEYS[5])["ok"]
    if marker_type == "string" then
        redis.call("DEL", KEYS[5])
        redis.call(
            "HMSET",
            KEYS[5],
            "session_id",
            ARGV[1],
            "generation_id",
            ARGV[3]
        )
    elseif marker_type == "hash" then
        if redis.call("HGET", KEYS[5], "session_id") ~= ARGV[1]
            or redis.call("HGET", KEYS[5], "generation_id") ~= ARGV[3] then
            return 4
        end
        if redis.call("HGET", KEYS[5], "finalized") ~= "1" then
            redis.call("PERSIST", KEYS[5])
        end
    else
        return 4
    end
    return 0
end

local committed_field = "session:" .. ARGV[1] .. ":committed_bytes"
local current = tonumber(redis.call("HGET", KEYS[1], committed_field) or "0")
local generation_bytes = tonumber(ARGV[4])
if generation_bytes > current then
    return 2
end
local legacy_marker_count = tonumber(ARGV[8])
local generation_marker_count = tonumber(ARGV[9])
local ledger_count = tonumber(ARGV[10])
local ledger_start = 11 + legacy_marker_count + (2 * generation_marker_count)
local ledger_keys = {}
for index = 0, ledger_count - 1 do
    local asset_id = ARGV[ledger_start + (2 * index)]
    local generation_id = ARGV[ledger_start + (2 * index) + 1]
    local ledger_key = ARGV[7] .. ARGV[1] .. ":" .. asset_id ..
        ":" .. generation_id
    if redis.call("HGET", ledger_key, "session_id") ~= ARGV[1]
        or redis.call("HGET", ledger_key, "asset_id") ~= asset_id
        or redis.call("HGET", ledger_key, "generation_id") ~= generation_id then
        return 5
    end
    table.insert(ledger_keys, ledger_key)
end
local remaining = current - generation_bytes
if remaining > 0 then
    redis.call("HSET", KEYS[1], committed_field, remaining)
else
    redis.call("HDEL", KEYS[1], committed_field)
end
for _, ledger_key in ipairs(ledger_keys) do
    redis.call("DEL", ledger_key)
    redis.call("SREM", KEYS[6], ledger_key)
end

for index = 1, legacy_marker_count do
    local asset_id = ARGV[10 + index]
    local marker_key = ARGV[5] .. ARGV[1] .. ":" .. asset_id
    local marker_session = redis.call("HGET", marker_key, "session_id")
    local marker_asset = redis.call("HGET", marker_key, "asset_id")
    if marker_session == ARGV[1] and marker_asset == asset_id then
        redis.call("DEL", marker_key)
        redis.call("SREM", KEYS[4], marker_key)
    end
end
local generation_start = 11 + legacy_marker_count
for index = 0, generation_marker_count - 1 do
    local asset_id = ARGV[generation_start + (2 * index)]
    local generation_id = ARGV[generation_start + (2 * index) + 1]
    local marker_key = ARGV[5] .. ARGV[1] .. ":" .. asset_id ..
        ":" .. generation_id
    local marker_session = redis.call("HGET", marker_key, "session_id")
    local marker_asset = redis.call("HGET", marker_key, "asset_id")
    local marker_generation = redis.call("HGET", marker_key, "generation_id")
    if marker_session == ARGV[1] and marker_asset == asset_id
        and marker_generation == generation_id then
        redis.call("DEL", marker_key)
        redis.call("SREM", KEYS[4], marker_key)
        local pending_key = ARGV[6] .. ARGV[1] .. ":" .. asset_id
        if redis.call("GET", pending_key) == generation_id then
            redis.call("DEL", pending_key)
            redis.call("SREM", KEYS[7], pending_key)
        end
    end
end
if redis.call("SCARD", KEYS[7]) == 0 then
    redis.call("DEL", KEYS[7])
end
redis.call(
    "HMSET",
    KEYS[5],
    "session_id",
    ARGV[1],
    "generation_id",
    ARGV[3]
)
return 1
"""

_FINALIZE_SESSION_USAGE_GENERATION_SCRIPT = (
    """-- finalize_session_usage_generation
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
local marker_type = redis.call("TYPE", KEYS[1])["ok"]
if marker_type == "hash" then
    if redis.call("HGET", KEYS[1], "finalized") == "1" then
        return 0
    end
    redis.call("HSET", KEYS[1], "finalized", "1")
end
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[1]))
return 1
"""
)

_RECONCILE_LEGACY_SESSION_USAGE_SCRIPT = """-- reconcile_legacy_session_usage
local deleting = redis.call("HGET", KEYS[2], "deleting")
if not deleting or deleting ~= ARGV[2] then
    return {3}
end
if redis.call("EXISTS", KEYS[3]) == 0 then
    return {3}
end
if redis.call("EXISTS", KEYS[4]) == 1 then
    if redis.call("HGET", KEYS[4], "session_id") ~= ARGV[1] then
        return {2}
    end
    if redis.call("HGET", KEYS[4], "finalized") ~= "1" then
        redis.call("PERSIST", KEYS[4])
    end
    return {
        0,
        tonumber(redis.call("HGET", KEYS[4], "legacy_bytes") or "0")
    }
end

local committed_field = "session:" .. ARGV[1] .. ":committed_bytes"
local committed = tonumber(redis.call("HGET", KEYS[1], committed_field) or "0")
local current_bytes = tonumber(ARGV[3])
local legacy_capacity = tonumber(ARGV[4])
local legacy_bytes = committed - current_bytes
if legacy_bytes < 0 or legacy_bytes > legacy_capacity then
    return {2}
end

if current_bytes > 0 then
    redis.call("HSET", KEYS[1], committed_field, current_bytes)
else
    redis.call("HDEL", KEYS[1], committed_field)
end
redis.call(
    "HMSET",
    KEYS[4],
    "session_id",
    ARGV[1],
    "current_bytes",
    current_bytes,
    "legacy_bytes",
    legacy_bytes
)
return {1, legacy_bytes}
"""

_FINALIZE_LEGACY_SESSION_USAGE_SCRIPT = """-- finalize_legacy_session_usage
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
if redis.call("HGET", KEYS[1], "finalized") == "1" then
    return 0
end
redis.call("HSET", KEYS[1], "finalized", "1")
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[1]))
return 1
"""

_RELEASE_LEGACY_COMMITTED_ASSET_SCRIPT = """-- release_legacy_committed_asset
local session_id = ARGV[1]
local asset_id = ARGV[2]
local released_bytes = tonumber(ARGV[3])
local marker_ttl_ms = tonumber(ARGV[5])
local function retain_index_member()
    local marker_ttl = redis.call("PTTL", KEYS[2])
    if marker_ttl < 0 then
        marker_ttl = marker_ttl_ms
        redis.call("PEXPIRE", KEYS[2], marker_ttl)
    end
    redis.call("SADD", KEYS[3], KEYS[2])
    if redis.call("PTTL", KEYS[3]) < marker_ttl then
        redis.call("PEXPIRE", KEYS[3], marker_ttl)
    end
end
local expected_remaining = nil
if ARGV[4] ~= "" then
    expected_remaining = tonumber(ARGV[4])
end
if redis.call("EXISTS", KEYS[2]) == 1 then
    local marker_session = redis.call("HGET", KEYS[2], "session_id")
    local marker_asset = redis.call("HGET", KEYS[2], "asset_id")
    local marker_bytes = tonumber(redis.call("HGET", KEYS[2], "bytes"))
    if marker_session ~= session_id or marker_asset ~= asset_id then
        return 2
    end
    if released_bytes ~= 0 and marker_bytes ~= released_bytes then
        return 3
    end
    if redis.call("HGET", KEYS[2], "finalized") ~= "1" then
        retain_index_member()
    end
    return 0
end

local field = "session:" .. session_id .. ":committed_bytes"
local current = tonumber(redis.call("HGET", KEYS[1], field) or "0")
local remaining = nil
if expected_remaining then
    if current == expected_remaining then
        remaining = current
    elseif current >= expected_remaining + released_bytes then
        remaining = current - released_bytes
    else
        return 5
    end
else
    if released_bytes == 0 then
        return 4
    end
    if released_bytes > current then
        return 3
    end
    remaining = current - released_bytes
end
if remaining > 0 then
    redis.call("HSET", KEYS[1], field, remaining)
else
    redis.call("HDEL", KEYS[1], field)
end
redis.call(
    "HMSET",
    KEYS[2],
    "session_id",
    session_id,
    "asset_id",
    asset_id,
    "bytes",
    released_bytes
)
redis.call("PEXPIRE", KEYS[2], marker_ttl_ms)
retain_index_member()
return 1
"""

_RELEASE_COMMITTED_UPLOAD_GENERATION_SCRIPT = (
    """-- release_committed_upload_generation
local session_id = ARGV[1]
local asset_id = ARGV[2]
local generation_id = ARGV[3]
local marker_ttl_ms = tonumber(ARGV[4])
local receipt_prefix = ARGV[5]
local receipt_ttl_ms = tonumber(ARGV[6])
local function sync_receipt(reservation_token, state)
    if not reservation_token or reservation_token == "" then
        return
    end
    local receipt_key = receipt_prefix .. reservation_token
    if redis.call("EXISTS", receipt_key) == 0 then
        return
    end
    if redis.call("HGET", receipt_key, "kind") ~= "upload"
        or redis.call("HGET", receipt_key, "session_id") ~= session_id
        or redis.call("HGET", receipt_key, "asset_id") ~= asset_id
        or redis.call("HGET", receipt_key, "generation_id") ~= generation_id then
        return
    end
    redis.call("HSET", receipt_key, "state", state)
    redis.call("PEXPIRE", receipt_key, receipt_ttl_ms)
end
local function retain_recovery_state()
    local marker_ttl = redis.call("PTTL", KEYS[4])
    if marker_ttl < 0 then
        marker_ttl = marker_ttl_ms
        redis.call("PEXPIRE", KEYS[4], marker_ttl)
    end
    redis.call("SADD", KEYS[5], KEYS[4])
    if redis.call("PTTL", KEYS[5]) < marker_ttl then
        redis.call("PEXPIRE", KEYS[5], marker_ttl)
    end
    redis.call("SET", KEYS[6], generation_id, "PX", marker_ttl)
    redis.call("SADD", KEYS[7], KEYS[6])
    if redis.call("PTTL", KEYS[7]) < marker_ttl then
        redis.call("PEXPIRE", KEYS[7], marker_ttl)
    end
end
if redis.call("EXISTS", KEYS[4]) == 1 then
    if redis.call("HGET", KEYS[4], "session_id") ~= session_id
        or redis.call("HGET", KEYS[4], "asset_id") ~= asset_id
        or redis.call("HGET", KEYS[4], "generation_id") ~= generation_id then
        return 2
    end
    if redis.call("HGET", KEYS[4], "finalized") ~= "1" then
        retain_recovery_state()
    end
    local marker_state = redis.call("HGET", KEYS[4], "state")
    if marker_state ~= "rolled_back" then
        sync_receipt(
            redis.call("HGET", KEYS[4], "reservation_token"),
            "released"
        )
    end
    return 0
end
if redis.call("EXISTS", KEYS[2]) == 0 then
    return 0
end
if redis.call("HGET", KEYS[2], "session_id") ~= session_id
    or redis.call("HGET", KEYS[2], "asset_id") ~= asset_id
    or redis.call("HGET", KEYS[2], "generation_id") ~= generation_id then
    return 2
end
local committed_bytes = tonumber(redis.call("HGET", KEYS[2], "bytes") or "0")
if committed_bytes <= 0 then
    return 3
end
local reservation_token = redis.call(
    "HGET",
    KEYS[2],
    "reservation_token"
)
local field = "session:" .. session_id .. ":committed_bytes"
local current = tonumber(redis.call("HGET", KEYS[1], field) or "0")
if committed_bytes > current then
    return 3
end
local remaining = current - committed_bytes
if remaining > 0 then
    redis.call("HSET", KEYS[1], field, remaining)
else
    redis.call("HDEL", KEYS[1], field)
end
redis.call("DEL", KEYS[2])
redis.call("SREM", KEYS[3], KEYS[2])
redis.call(
    "HMSET",
    KEYS[4],
    "session_id",
    session_id,
    "asset_id",
    asset_id,
    "generation_id",
    generation_id,
    "bytes",
    committed_bytes,
    "state",
    "released",
    "reservation_token",
    reservation_token or ""
)
redis.call("PEXPIRE", KEYS[4], marker_ttl_ms)
retain_recovery_state()
sync_receipt(reservation_token, "released")
return 1
"""
)

_IS_COMMITTED_ASSET_RELEASED_SCRIPT = """-- is_committed_asset_released
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
local marker_session = redis.call("HGET", KEYS[1], "session_id")
local marker_asset = redis.call("HGET", KEYS[1], "asset_id")
if marker_session ~= ARGV[1] or marker_asset ~= ARGV[2] then
    return 0
end
return 1
"""

_IS_COMMITTED_UPLOAD_GENERATION_RELEASED_SCRIPT = (
    """-- is_committed_upload_generation_released
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
if redis.call("HGET", KEYS[1], "session_id") ~= ARGV[1]
    or redis.call("HGET", KEYS[1], "asset_id") ~= ARGV[2]
    or redis.call("HGET", KEYS[1], "generation_id") ~= ARGV[3] then
    return 0
end
return 1
"""
)

_FINALIZE_ASSET_RELEASE_SCRIPT = """-- finalize_asset_release
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
local marker_session = redis.call("HGET", KEYS[1], "session_id")
local marker_asset = redis.call("HGET", KEYS[1], "asset_id")
if marker_session ~= ARGV[1] or marker_asset ~= ARGV[2] then
    return 2
end
redis.call("SREM", KEYS[2], KEYS[1])
if redis.call("SCARD", KEYS[2]) == 0 then
    redis.call("DEL", KEYS[2])
end
if redis.call("HGET", KEYS[1], "finalized") == "1" then
    return 0
end
redis.call("HSET", KEYS[1], "finalized", "1")
if redis.call("PTTL", KEYS[1]) < 0 then
    redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[3]))
end
return 1
"""

_FINALIZE_UPLOAD_GENERATION_RELEASE_SCRIPT = (
    """-- finalize_upload_generation_release
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
if redis.call("HGET", KEYS[1], "session_id") ~= ARGV[1]
    or redis.call("HGET", KEYS[1], "asset_id") ~= ARGV[2]
    or redis.call("HGET", KEYS[1], "generation_id") ~= ARGV[3] then
    return 2
end
redis.call("SREM", KEYS[2], KEYS[1])
if redis.call("SCARD", KEYS[2]) == 0 then
    redis.call("DEL", KEYS[2])
end
if redis.call("GET", KEYS[3]) == ARGV[3] then
    redis.call("DEL", KEYS[3])
    redis.call("SREM", KEYS[4], KEYS[3])
    if redis.call("SCARD", KEYS[4]) == 0 then
        redis.call("DEL", KEYS[4])
    end
end
if redis.call("HGET", KEYS[1], "finalized") == "1" then
    return 0
end
redis.call("HSET", KEYS[1], "finalized", "1")
if redis.call("PTTL", KEYS[1]) < 0 then
    redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[4]))
end
return 1
"""
)

_ACQUIRE_SESSION_SCRIPT = """-- acquire_session
local state_key = KEYS[1]
local leases_key = KEYS[2]
local lease_key = KEYS[3]
local token = ARGV[1]
local session_id = ARGV[2]
local ttl_ms = tonumber(ARGV[3])
local lease_prefix = ARGV[4]
local cleanup_prefix = ARGV[5]
local task_id = ARGV[6]
local heartbeat_at = ARGV[7]

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
if task_id ~= "" then
    redis.call(
        "HMSET",
        KEYS[4],
        "session_id",
        session_id,
        "lease_token",
        token,
        "heartbeat_at",
        heartbeat_at
    )
    redis.call("PEXPIRE", KEYS[4], ttl_ms)
end
return 0
"""

_RELEASE_SESSION_SCRIPT = """-- release_session
local state_key = KEYS[1]
local leases_key = KEYS[2]
local lease_key = KEYS[3]
local task_key = KEYS[4]
if redis.call("HGET", task_key, "lease_token") == ARGV[1] then
    redis.call("HDEL", task_key, "lease_token")
    if redis.call("HEXISTS", task_key, "output_token") == 0 then
        redis.call("DEL", task_key)
    end
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

_RELEASE_TASK_PIN_SCRIPT = """-- release_task_pin
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
if redis.call("HGET", KEYS[1], "session_id") ~= ARGV[1] then
    return 2
end
if redis.call("HGET", KEYS[1], "task_id") ~= ARGV[2] then
    return 2
end
local identities_json = redis.call("HGET", KEYS[1], "asset_identities")
local asset_ids_json = redis.call("HGET", KEYS[1], "asset_ids")
if not identities_json and not asset_ids_json then
    return 4
end
local asset_ids = {}
if identities_json then
    if string.sub(identities_json, 1, 1) ~= "[" then
        return 3
    end
    local decoded, identities = pcall(cjson.decode, identities_json)
    if not decoded or type(identities) ~= "table" then
        return 3
    end
    for index, identity in ipairs(identities) do
        if type(identity) ~= "table"
            or #identity ~= 2
            or type(identity[1]) ~= "string"
            or (
                identity[2] ~= cjson.null
                and type(identity[2]) ~= "string"
            ) then
            return 3
        end
        if type(identity[2]) == "string"
            and (
                string.len(identity[2]) ~= 32
                or string.match(identity[2], "^[0-9a-f]+$") == nil
            ) then
            return 3
        end
        asset_ids[index] = identity[1]
    end
    if asset_ids_json then
        if string.sub(asset_ids_json, 1, 1) ~= "[" then
            return 3
        end
        local decoded_ids, legacy_asset_ids = pcall(cjson.decode, asset_ids_json)
        if not decoded_ids
            or type(legacy_asset_ids) ~= "table"
            or #legacy_asset_ids ~= #asset_ids then
            return 3
        end
        for index, asset_id in ipairs(legacy_asset_ids) do
            if type(asset_id) ~= "string" or asset_id ~= asset_ids[index] then
                return 3
            end
        end
    end
elseif asset_ids_json then
    if string.sub(asset_ids_json, 1, 1) ~= "[" then
        return 3
    end
    local decoded, legacy_asset_ids = pcall(cjson.decode, asset_ids_json)
    if not decoded or type(legacy_asset_ids) ~= "table" then
        return 3
    end
    for index, asset_id in ipairs(legacy_asset_ids) do
        if type(asset_id) ~= "string" then
            return 3
        end
        asset_ids[index] = asset_id
    end
end
for _, asset_id in ipairs(asset_ids) do
    if string.len(asset_id) ~= 32
        or string.match(asset_id, "^[0-9a-f]+$") == nil then
        return 3
    end
end
for _, asset_id in ipairs(asset_ids) do
    local asset_tasks_key = ARGV[3] .. ARGV[1] .. ":" .. asset_id
    redis.call("SREM", asset_tasks_key, ARGV[2])
    if redis.call("SCARD", asset_tasks_key) == 0 then
        redis.call("DEL", asset_tasks_key)
end
end
redis.call("DEL", KEYS[1])
redis.call("SREM", KEYS[2], ARGV[2])
if redis.call("SCARD", KEYS[2]) == 0 then
    redis.call("DEL", KEYS[2])
end
return 1
"""

_CLAIM_CLEANUP_SCRIPT = """-- claim_cleanup
local state_key = KEYS[1]
local leases_key = KEYS[2]
local cleanup_key = KEYS[3]
local task_pins_key = KEYS[4]
local token = ARGV[1]
local lease_prefix = ARGV[3]
local cleanup_prefix = ARGV[4]
local task_pin_prefix = ARGV[5]
local session_id = ARGV[6]

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

local task_ids = redis.call("SMEMBERS", task_pins_key)
for _, task_id in ipairs(task_ids) do
    local pin_key = task_pin_prefix .. task_id
    if redis.call("EXISTS", pin_key) == 0 then
        redis.call("SREM", task_pins_key, task_id)
    elseif redis.call("HGET", pin_key, "session_id") ~= session_id then
        redis.call("SREM", task_pins_key, task_id)
    end
end
if redis.call("SCARD", task_pins_key) ~= 0 then
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


class CorruptTaskPinError(ResourceUnavailableError):
    pass


class SessionDeletingError(ResourceUnavailableError):
    pass


class AssetReleaseMismatchError(ResourceUnavailableError):
    pass


class ReservationState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    FINALIZED_OR_ABSENT = "finalized_or_absent"


@dataclass(frozen=True)
class SessionUsageGeneration:
    generation_id: str
    committed_bytes: int
    released_asset_ids: tuple[str, ...]
    released_upload_generations: tuple[tuple[str, str], ...] = ()
    committed_upload_generations: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CleanupMarker:
    session_id: str
    generation_id: str
    legacy: bool


@dataclass(frozen=True)
class PendingAssetRelease:
    session_id: str
    asset_id: str
    generation_id: str


@dataclass
class RedisReservation:
    coordinator: RedisResourceCoordinator
    kind: Literal["upload", "output"]
    token: str
    owner_id: str
    reserved_bytes: int
    asset_id: str | None = None
    generation_id: str | None = None

    def refresh(self) -> None:
        self.coordinator._refresh_reservation(self)

    def commit(self) -> None:
        """Commit once; retries succeed while this token's marker exists."""
        self.coordinator._commit_reservation(self)

    def rollback(self) -> bool:
        """Roll back this token, returning whether rollback is confirmed."""
        return self.coordinator._rollback_commit(self)

    def finalize(self) -> None:
        """Make this upload commit ineligible for rollback."""
        self.coordinator._finalize_commit(self)

    def state(self) -> ReservationState:
        """Resolve token state; finalized, rolled-back, and unknown are absent."""
        return self.coordinator._reservation_state(self)

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
    task_id: str | None = None

    def refresh(self) -> None:
        self.coordinator._refresh_session(self)

    def release(self) -> None:
        self.coordinator._release_session(self)

    def __enter__(self) -> RedisSessionLease:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass
class RedisUploadRegistration:
    coordinator: RedisResourceCoordinator
    session_id: str
    upload_id: str
    token: str

    def refresh(self) -> bool:
        return self.coordinator._upload_registration_state(self)

    def finish(self) -> None:
        self.coordinator._finish_upload(self)


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
        heartbeat_stop_timeout_seconds: float = 11.0,
        release_marker_ttl: float | None = None,
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
        self.heartbeat_stop_timeout_seconds = _positive_seconds(
            heartbeat_stop_timeout_seconds,
            "heartbeat_stop_timeout_seconds",
        )
        self._reservation_ttl_ms = _ttl_milliseconds(
            max(reservation_ttl, 2 * reservation_heartbeat_interval),
            "reservation_ttl",
        )
        self._lease_ttl_ms = _ttl_milliseconds(
            max(lease_ttl, 2 * lease_heartbeat_interval),
            "lease_ttl",
        )
        self.session_lease_heartbeat_interval = lease_heartbeat_interval
        self.cleanup_claim_heartbeat_interval = lease_heartbeat_interval
        marker_ttl = (
            max(reservation_ttl, lease_ttl)
            if release_marker_ttl is None
            else _positive_seconds(release_marker_ttl, "release_marker_ttl")
        )
        self._release_marker_ttl_ms = max(
            _ttl_milliseconds(marker_ttl, "release_marker_ttl"),
            self._reservation_ttl_ms,
            self._lease_ttl_ms,
        )
        self._reservation_receipt_ttl_ms = max(
            self._reservation_ttl_ms,
            self._release_marker_ttl_ms,
            _ttl_milliseconds(
                _RESERVATION_RECOVERY_ATTEMPTS
                * self.heartbeat_stop_timeout_seconds,
                "reservation recovery window",
            ),
        )
        self.reservation_heartbeat_interval = reservation_heartbeat_interval
        self.upload_registration_ttl_seconds = self._reservation_ttl_ms / 1000
        self._register_upload_script = self._redis.register_script(
            _REGISTER_UPLOAD_SCRIPT
        )
        self._refresh_upload_script = self._redis.register_script(
            _REFRESH_UPLOAD_SCRIPT
        )
        self._cancel_upload_script = self._redis.register_script(
            _CANCEL_UPLOAD_SCRIPT
        )
        self._finish_upload_script = self._redis.register_script(
            _FINISH_UPLOAD_SCRIPT
        )
        self._reserve_upload_script = self._redis.register_script(
            _RESERVE_UPLOAD_SCRIPT
        )
        self._reserve_output_script = self._redis.register_script(
            _RESERVE_OUTPUT_SCRIPT
        )
        self._refresh_token_script = self._redis.register_script(_REFRESH_TOKEN_SCRIPT)
        self._refresh_reservation_script = self._redis.register_script(
            _REFRESH_RESERVATION_SCRIPT
        )
        self._reservation_recovery_state_script = self._redis.register_script(
            _RESERVATION_RECOVERY_STATE_SCRIPT
        )
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
        self._reservation_state_script = self._redis.register_script(
            _RESERVATION_STATE_SCRIPT
        )
        self._reconcile_reservations_script = self._redis.register_script(
            _RECONCILE_RESERVATIONS_SCRIPT
        )
        self._clear_session_usage_script = self._redis.register_script(
            _CLEAR_SESSION_USAGE_SCRIPT
        )
        self._capture_session_usage_script = self._redis.register_script(
            _CAPTURE_SESSION_USAGE_SCRIPT
        )
        self._clear_session_usage_generation_script = self._redis.register_script(
            _CLEAR_SESSION_USAGE_GENERATION_SCRIPT
        )
        self._finalize_session_usage_generation_script = self._redis.register_script(
            _FINALIZE_SESSION_USAGE_GENERATION_SCRIPT
        )
        self._reconcile_legacy_session_usage_script = self._redis.register_script(
            _RECONCILE_LEGACY_SESSION_USAGE_SCRIPT
        )
        self._finalize_legacy_session_usage_script = self._redis.register_script(
            _FINALIZE_LEGACY_SESSION_USAGE_SCRIPT
        )
        self._release_legacy_committed_asset_script = self._redis.register_script(
            _RELEASE_LEGACY_COMMITTED_ASSET_SCRIPT
        )
        self._release_committed_upload_generation_script = (
            self._redis.register_script(
                _RELEASE_COMMITTED_UPLOAD_GENERATION_SCRIPT
            )
        )
        self._is_committed_asset_released_script = self._redis.register_script(
            _IS_COMMITTED_ASSET_RELEASED_SCRIPT
        )
        self._is_committed_upload_generation_released_script = (
            self._redis.register_script(
                _IS_COMMITTED_UPLOAD_GENERATION_RELEASED_SCRIPT
            )
        )
        self._finalize_asset_release_script = self._redis.register_script(
            _FINALIZE_ASSET_RELEASE_SCRIPT
        )
        self._finalize_upload_generation_release_script = (
            self._redis.register_script(
                _FINALIZE_UPLOAD_GENERATION_RELEASE_SCRIPT
            )
        )
        self._acquire_session_script = self._redis.register_script(
            _ACQUIRE_SESSION_SCRIPT
        )
        self._release_session_script = self._redis.register_script(
            _RELEASE_SESSION_SCRIPT
        )
        self._release_task_pin_script = self._redis.register_script(
            _RELEASE_TASK_PIN_SCRIPT
        )
        self._claim_cleanup_script = self._redis.register_script(_CLAIM_CLEANUP_SCRIPT)
        self._refresh_cleanup_script = self._redis.register_script(
            _REFRESH_CLEANUP_SCRIPT
        )
        self._release_cleanup_script = self._redis.register_script(
            _RELEASE_CLEANUP_SCRIPT
        )

    def _execute_reservation_script(
        self,
        script: Callable[..., object],
        *,
        keys: list[str],
        args: list[object],
        token: str,
        expected_metadata: dict[str, str],
        index_metadata: str,
        reserved_result: int,
        conflict_result: int,
    ) -> int:
        last_transport_error: RedisConnectionError | RedisTimeoutError | None = None
        for _attempt in range(_RESERVATION_RECOVERY_ATTEMPTS):
            try:
                return int(script(keys=keys, args=args))
            except _REDIS_TRANSPORT_ERRORS as exc:
                last_transport_error = exc

        if last_transport_error is None:
            raise RuntimeError("Reservation recovery lost its transport error")
        try:
            recovery_state = int(
                self._reservation_recovery_state_script(
                    keys=[
                        _reservation_key(token),
                        _RESERVATION_INDEX_KEY,
                        _reservation_receipt_key(token),
                    ],
                    args=[
                        token,
                        index_metadata,
                        expected_metadata["kind"],
                        expected_metadata["owner_id"],
                        expected_metadata["session_id"],
                        expected_metadata["bytes"],
                        expected_metadata["free_bytes"],
                        expected_metadata["min_free_bytes"],
                        expected_metadata.get("asset_id", ""),
                        expected_metadata.get("generation_id", ""),
                    ],
                )
            )
        except _REDIS_TRANSPORT_ERRORS as query_error:
            raise last_transport_error from query_error
        if recovery_state == 1:
            return reserved_result
        if recovery_state == 2:
            return conflict_result
        raise last_transport_error

    def reserve_upload(
        self,
        session_id: str,
        requested_bytes: int,
        *,
        free_bytes: int,
        asset_id: str | None = None,
        generation_id: str | None = None,
    ) -> RedisReservation:
        _validate_session_id(session_id)
        requested_bytes = _positive_int(requested_bytes, "requested_bytes")
        free_bytes = _nonnegative_int(free_bytes, "free_bytes")
        if (asset_id is None) != (generation_id is None):
            raise ValueError(
                "asset_id and generation_id must be provided together"
            )
        if asset_id is not None:
            _validate_asset_id(asset_id)
            _validate_token(generation_id)
        token = uuid4().hex
        reservation = RedisReservation(
            coordinator=self,
            kind="upload",
            token=token,
            owner_id=session_id,
            reserved_bytes=requested_bytes,
            asset_id=asset_id,
            generation_id=generation_id,
        )
        keys = [
            _STATE_KEY,
            _RESERVATION_INDEX_KEY,
            _reservation_key(token),
            _reservation_receipt_key(token),
        ]
        args = [
            token,
            session_id,
            requested_bytes,
            free_bytes,
            self.max_upload_concurrency,
            self.max_session_bytes,
            self.min_free_bytes,
            self._reservation_ttl_ms,
            self._reservation_receipt_ttl_ms,
            asset_id or "",
            generation_id or "",
        ]
        result = self._execute_reservation_script(
            self._reserve_upload_script,
            keys=keys,
            args=args,
            token=token,
            expected_metadata={
                "kind": "upload",
                "owner_id": session_id,
                "session_id": session_id,
                "bytes": str(requested_bytes),
                "free_bytes": str(free_bytes),
                "min_free_bytes": str(self.min_free_bytes),
                "asset_id": asset_id or "",
                "generation_id": generation_id or "",
            },
            index_metadata=(
                f"upload|{session_id}|{requested_bytes}|{free_bytes}|"
                f"{self.min_free_bytes}"
            ),
            reserved_result=4,
            conflict_result=5,
        )
        if result == 1:
            raise ResourceUnavailableError("Upload concurrency limit reached")
        if result == 2:
            raise ResourceUnavailableError("Upload session byte limit reached")
        if result == 3:
            raise ResourceUnavailableError("Insufficient free disk capacity")
        if result == 5:
            raise ResourceUnavailableError(
                "Upload reservation token metadata conflict"
            )
        if result == 6:
            raise ResourceUnavailableError("Upload reservation expired")
        return reservation

    def register_upload(
        self,
        session_id: str,
        upload_id: str,
    ) -> RedisUploadRegistration:
        _validate_session_id(session_id)
        _validate_asset_id(upload_id)
        token = uuid4().hex
        registration = RedisUploadRegistration(
            coordinator=self,
            session_id=session_id,
            upload_id=upload_id,
            token=token,
        )
        last_transport_error: RedisConnectionError | RedisTimeoutError | None = None
        for _attempt in range(_RESERVATION_RECOVERY_ATTEMPTS):
            try:
                result = int(
                    self._register_upload_script(
                        keys=[
                            _upload_inflight_key(session_id, upload_id),
                            _released_asset_key(session_id, upload_id),
                            _session_released_assets_key(session_id),
                            _pending_asset_release_key(session_id, upload_id),
                        ],
                        args=[
                            token,
                            self._reservation_ttl_ms,
                            session_id,
                            upload_id,
                        ],
                    )
                )
            except _REDIS_TRANSPORT_ERRORS as exc:
                last_transport_error = exc
                continue
            if result == 1:
                raise ResourceUnavailableError("Upload is already in progress")
            if result == 3:
                raise ResourceUnavailableError("Upload deletion is still pending")
            if result == 4:
                raise ResourceUnavailableError("Upload release marker is invalid")
            return registration
        if last_transport_error is None:
            raise RuntimeError("Upload registration recovery lost its error")
        registration_key = _upload_inflight_key(session_id, upload_id)
        try:
            metadata = self._redis.hgetall(registration_key)
        except _REDIS_TRANSPORT_ERRORS as query_error:
            raise last_transport_error from query_error
        owner = _decoded_hash_value(metadata, "token")
        state = _decoded_hash_value(metadata, "state")
        if owner == token:
            if state in {"inflight", "cancelled"}:
                return registration
            raise ResourceUnavailableError(
                "Upload registration state is invalid"
            )
        if owner is not None:
            raise ResourceUnavailableError("Upload is already in progress")
        raise last_transport_error

    def cancel_upload(self, session_id: str, upload_id: str) -> bool:
        _validate_session_id(session_id)
        _validate_asset_id(upload_id)
        return bool(
            self._cancel_upload_script(
                keys=[_upload_inflight_key(session_id, upload_id)],
                args=[self._reservation_ttl_ms],
            )
        )

    def _upload_registration_state(
        self,
        registration: RedisUploadRegistration,
    ) -> bool:
        result = int(
            self._refresh_upload_script(
                keys=[
                    _upload_inflight_key(
                        registration.session_id,
                        registration.upload_id,
                    )
                ],
                args=[
                    registration.token,
                    self._reservation_ttl_ms,
                ],
            )
        )
        if result == 0:
            raise ResourceUnavailableError("Upload registration expired")
        return result == 2

    def _finish_upload(self, registration: RedisUploadRegistration) -> None:
        registration_key = _upload_inflight_key(
            registration.session_id,
            registration.upload_id,
        )
        last_transport_error: RedisConnectionError | RedisTimeoutError | None = None
        for _attempt in range(_RESERVATION_RECOVERY_ATTEMPTS):
            try:
                self._finish_upload_script(
                    keys=[registration_key],
                    args=[registration.token],
                )
            except _REDIS_TRANSPORT_ERRORS as exc:
                last_transport_error = exc
                try:
                    owner = self._redis.hget(registration_key, "token")
                except _REDIS_TRANSPORT_ERRORS:
                    continue
                owner_text = (
                    owner.decode() if isinstance(owner, bytes) else owner
                )
                if owner_text != registration.token:
                    return
                continue
            return
        if last_transport_error is None:
            raise RuntimeError("Upload registration cleanup lost its error")
        raise last_transport_error

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
        reservation = RedisReservation(
            coordinator=self,
            kind="output",
            token=token,
            owner_id=task_id,
            reserved_bytes=requested_bytes,
        )
        result = self._execute_reservation_script(
            self._reserve_output_script,
            keys=[
                _STATE_KEY,
                _RESERVATION_INDEX_KEY,
                _reservation_key(token),
                _task_resource_key(task_id),
                _reservation_receipt_key(token),
            ],
            args=[
                token,
                task_id,
                session_id,
                requested_bytes,
                free_bytes,
                min_free_bytes,
                self._reservation_ttl_ms,
                time(),
                self._reservation_receipt_ttl_ms,
            ],
            token=token,
            expected_metadata={
                "kind": "output",
                "owner_id": task_id,
                "session_id": session_id,
                "bytes": str(requested_bytes),
                "free_bytes": str(free_bytes),
                "min_free_bytes": str(min_free_bytes),
            },
            index_metadata=(
                f"output|{session_id}|{requested_bytes}|{free_bytes}|"
                f"{min_free_bytes}"
            ),
            reserved_result=2,
            conflict_result=3,
        )
        if result == 1:
            raise ResourceUnavailableError("Insufficient free disk capacity")
        if result == 3:
            raise ResourceUnavailableError(
                "Output reservation token metadata conflict"
            )
        if result == 4:
            raise ResourceUnavailableError("Output reservation expired")
        return reservation

    def acquire_session(
        self,
        session_id: str,
        *,
        task_id: str | None = None,
    ) -> RedisSessionLease:
        _validate_session_id(session_id)
        token = uuid4().hex
        task_key = (
            _task_resource_key(task_id)
            if task_id is not None
            else _lease_key(token)
        )
        result = int(
            self._acquire_session_script(
                keys=[
                    _session_state_key(session_id),
                    _session_leases_key(session_id),
                    _lease_key(token),
                    task_key,
                ],
                args=[
                    token,
                    session_id,
                    self._lease_ttl_ms,
                    _LEASE_PREFIX,
                    _CLEANUP_PREFIX,
                    task_id or "",
                    time(),
                ],
            )
        )
        if result != 0:
            raise SessionDeletingError("Session is being deleted")
        return RedisSessionLease(self, session_id, token, task_id)

    def claim_session_cleanup(self, session_id: str) -> str | None:
        _validate_session_id(session_id)
        token = uuid4().hex
        result = int(
            self._claim_cleanup_script(
                keys=[
                    _session_state_key(session_id),
                    _session_leases_key(session_id),
                    _cleanup_key(token),
                    _session_task_pins_key(session_id),
                ],
                args=[
                    token,
                    self._lease_ttl_ms,
                    _LEASE_PREFIX,
                    _CLEANUP_PREFIX,
                    _TASK_PIN_PREFIX,
                    session_id,
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
            keys=[
                _STATE_KEY,
                _session_released_assets_key(session_id),
                _session_committed_uploads_key(session_id),
                _PENDING_ASSET_RELEASE_INDEX_KEY,
            ],
            args=[
                session_id,
                _PENDING_ASSET_RELEASE_PREFIX,
            ],
        )

    def capture_session_usage(
        self,
        session_id: str,
        claim_token: str,
    ) -> SessionUsageGeneration:
        _validate_session_id(session_id)
        _validate_token(claim_token)
        result = self._capture_session_usage_script(
            keys=[
                _STATE_KEY,
                _session_state_key(session_id),
                _session_released_assets_key(session_id),
                _cleanup_key(claim_token),
                _session_committed_uploads_key(session_id),
            ],
            args=[session_id, claim_token],
        )
        if int(result[0]) != 1:
            raise ResourceUnavailableError("Cleanup claim expired")
        ledger_prefix = f"{_COMMITTED_UPLOAD_PREFIX}{session_id}:"
        ledger_count = int(result[2])
        committed_upload_generations: list[tuple[str, str]] = []
        for raw_ledger_key in result[3 : 3 + ledger_count]:
            ledger_key = (
                raw_ledger_key.decode()
                if isinstance(raw_ledger_key, bytes)
                else raw_ledger_key
            )
            if not ledger_key.startswith(ledger_prefix):
                raise ResourceUnavailableError(
                    "Session committed upload ledger is invalid"
                )
            ledger_parts = ledger_key.removeprefix(ledger_prefix).split(":")
            if len(ledger_parts) != 2:
                raise ResourceUnavailableError(
                    "Session committed upload ledger is invalid"
                )
            committed_upload_generations.append(
                (
                    _validate_asset_id(ledger_parts[0]),
                    _validate_token(ledger_parts[1]),
                )
            )
        marker_prefix = f"{_RELEASED_ASSET_PREFIX}{session_id}:"
        released_asset_ids: list[str] = []
        released_upload_generations: list[tuple[str, str]] = []
        for raw_marker_key in result[3 + ledger_count :]:
            marker_key = (
                raw_marker_key.decode()
                if isinstance(raw_marker_key, bytes)
                else raw_marker_key
            )
            if not marker_key.startswith(marker_prefix):
                raise ResourceUnavailableError(
                    "Session release marker state is invalid"
                )
            marker_suffix = marker_key.removeprefix(marker_prefix)
            marker_parts = marker_suffix.split(":")
            if len(marker_parts) == 1:
                released_asset_ids.append(
                    _validate_asset_id(marker_parts[0])
                )
            elif len(marker_parts) == 2:
                released_upload_generations.append(
                    (
                        _validate_asset_id(marker_parts[0]),
                        _validate_token(marker_parts[1]),
                    )
                )
            else:
                raise ResourceUnavailableError(
                    "Session release marker state is invalid"
                )
        return SessionUsageGeneration(
            generation_id=claim_token,
            committed_bytes=int(result[1]),
            released_asset_ids=tuple(sorted(released_asset_ids)),
            released_upload_generations=tuple(
                sorted(released_upload_generations)
            ),
            committed_upload_generations=tuple(
                sorted(committed_upload_generations)
            ),
        )

    def clear_session_usage_generation(
        self,
        session_id: str,
        claim_token: str,
        generation: SessionUsageGeneration,
    ) -> None:
        _validate_session_id(session_id)
        _validate_token(claim_token)
        _validate_token(generation.generation_id)
        committed_bytes = _nonnegative_int(
            generation.committed_bytes,
            "generation committed_bytes",
        )
        asset_ids = tuple(
            _validate_asset_id(asset_id)
            for asset_id in generation.released_asset_ids
        )
        upload_generations = tuple(
            (
                _validate_asset_id(asset_id),
                _validate_token(generation_id),
            )
            for asset_id, generation_id in (
                generation.released_upload_generations
            )
        )
        committed_upload_generations = tuple(
            (
                _validate_asset_id(asset_id),
                _validate_token(generation_id),
            )
            for asset_id, generation_id in (
                generation.committed_upload_generations
            )
        )
        result = int(
            self._clear_session_usage_generation_script(
                keys=[
                    _STATE_KEY,
                    _session_state_key(session_id),
                    _cleanup_key(claim_token),
                    _session_released_assets_key(session_id),
                    _cleanup_generation_key(generation.generation_id),
                    _session_committed_uploads_key(session_id),
                    _PENDING_ASSET_RELEASE_INDEX_KEY,
                ],
                args=[
                    session_id,
                    claim_token,
                    generation.generation_id,
                    committed_bytes,
                    _RELEASED_ASSET_PREFIX,
                    _PENDING_ASSET_RELEASE_PREFIX,
                    _COMMITTED_UPLOAD_PREFIX,
                    len(asset_ids),
                    len(upload_generations),
                    len(committed_upload_generations),
                    *asset_ids,
                    *(
                        value
                        for upload_generation in upload_generations
                        for value in upload_generation
                    ),
                    *(
                        value
                        for committed_upload_generation in (
                            committed_upload_generations
                        )
                        for value in committed_upload_generation
                    ),
                ],
            )
        )
        if result == 2:
            raise ResourceUnavailableError(
                "Session usage generation exceeds committed usage"
            )
        if result == 3:
            raise ResourceUnavailableError("Cleanup claim expired")
        if result == 4:
            raise ResourceUnavailableError(
                "Session usage generation marker is invalid"
            )
        if result == 5:
            raise ResourceUnavailableError(
                "Session committed upload ledger is invalid"
            )

    def finalize_session_usage_generation(self, generation_id: str) -> None:
        _validate_token(generation_id)
        self._finalize_session_usage_generation_script(
            keys=[_cleanup_generation_key(generation_id)],
            args=[self._release_marker_ttl_ms],
        )

    def reconcile_legacy_session_usage(
        self,
        session_id: str,
        claim_token: str,
        generation_id: str,
        *,
        current_committed_bytes: int,
        legacy_physical_bytes: int,
    ) -> int:
        _validate_session_id(session_id)
        _validate_token(claim_token)
        _validate_token(generation_id)
        current_committed_bytes = _nonnegative_int(
            current_committed_bytes,
            "current committed bytes",
        )
        legacy_physical_bytes = _nonnegative_int(
            legacy_physical_bytes,
            "legacy physical bytes",
        )
        result = self._reconcile_legacy_session_usage_script(
            keys=[
                _STATE_KEY,
                _session_state_key(session_id),
                _cleanup_key(claim_token),
                _legacy_cleanup_key(generation_id),
            ],
            args=[
                session_id,
                claim_token,
                current_committed_bytes,
                legacy_physical_bytes,
            ],
        )
        code = int(result[0])
        if code == 2:
            raise ResourceUnavailableError(
                "Legacy session usage cannot be reconciled safely"
            )
        if code == 3:
            raise ResourceUnavailableError("Cleanup claim expired")
        return int(result[1])

    def finalize_legacy_session_usage(self, generation_id: str) -> None:
        _validate_token(generation_id)
        self._finalize_legacy_session_usage_script(
            keys=[_legacy_cleanup_key(generation_id)],
            args=[self._release_marker_ttl_ms],
        )

    def pending_cleanup_markers(self) -> tuple[CleanupMarker, ...]:
        markers: list[CleanupMarker] = []
        for prefix, legacy in (
            (_CLEANUP_GENERATION_PREFIX, False),
            (_LEGACY_CLEANUP_PREFIX, True),
        ):
            for raw_key in self._redis.scan_iter(match=f"{prefix}*"):
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                generation_id = key.removeprefix(prefix)
                if not _TOKEN_PATTERN.fullmatch(generation_id):
                    continue
                marker_type = self._redis.type(key)
                if isinstance(marker_type, bytes):
                    marker_type = marker_type.decode()
                if marker_type != "hash":
                    continue
                metadata = self._redis.hgetall(key)
                if _decoded_hash_value(metadata, "finalized") == "1":
                    continue
                session_id = _decoded_hash_value(metadata, "session_id")
                if session_id is None or not _SESSION_PATTERN.fullmatch(session_id):
                    continue
                if not legacy and (
                    _decoded_hash_value(metadata, "generation_id") != generation_id
                ):
                    continue
                markers.append(
                    CleanupMarker(
                        session_id=session_id,
                        generation_id=generation_id,
                        legacy=legacy,
                    )
                )
        return tuple(
            sorted(
                markers,
                key=lambda marker: (
                    marker.session_id,
                    marker.generation_id,
                    marker.legacy,
                ),
            )
        )

    def pending_asset_releases(self) -> tuple[PendingAssetRelease, ...]:
        releases: list[PendingAssetRelease] = []
        for raw_key in self._redis.smembers(
            _PENDING_ASSET_RELEASE_INDEX_KEY
        ):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            if not key.startswith(_PENDING_ASSET_RELEASE_PREFIX):
                self._redis.srem(_PENDING_ASSET_RELEASE_INDEX_KEY, raw_key)
                continue
            parts = key.removeprefix(_PENDING_ASSET_RELEASE_PREFIX).split(":")
            if (
                len(parts) != 2
                or not _SESSION_PATTERN.fullmatch(parts[0])
                or not _ASSET_PATTERN.fullmatch(parts[1])
            ):
                self._redis.srem(_PENDING_ASSET_RELEASE_INDEX_KEY, raw_key)
                continue
            session_id, asset_id = parts
            raw_generation = self._redis.get(key)
            generation_id = (
                raw_generation.decode()
                if isinstance(raw_generation, bytes)
                else raw_generation
            )
            if (
                generation_id is None
                or not _TOKEN_PATTERN.fullmatch(generation_id)
            ):
                self._redis.srem(_PENDING_ASSET_RELEASE_INDEX_KEY, raw_key)
                continue
            marker_key = _released_upload_generation_key(
                session_id,
                asset_id,
                generation_id,
            )
            metadata = self._redis.hgetall(marker_key)
            if (
                _decoded_hash_value(metadata, "session_id") != session_id
                or _decoded_hash_value(metadata, "asset_id") != asset_id
                or _decoded_hash_value(metadata, "generation_id")
                != generation_id
            ):
                self._redis.delete(key)
                self._redis.srem(_PENDING_ASSET_RELEASE_INDEX_KEY, raw_key)
                continue
            releases.append(
                PendingAssetRelease(
                    session_id=session_id,
                    asset_id=asset_id,
                    generation_id=generation_id,
                )
            )
        if not self._redis.scard(_PENDING_ASSET_RELEASE_INDEX_KEY):
            self._redis.delete(_PENDING_ASSET_RELEASE_INDEX_KEY)
        return tuple(
            sorted(
                releases,
                key=lambda release: (
                    release.session_id,
                    release.asset_id,
                    release.generation_id,
                ),
            )
        )

    def release_committed_asset(
        self,
        session_id: str,
        asset_id: str,
        generation_id: str | int,
        *,
        expected_remaining_bytes: int | None = None,
    ) -> bool:
        if not isinstance(generation_id, str):
            return self.release_legacy_committed_asset(
                session_id,
                asset_id,
                generation_id,
                expected_remaining_bytes=expected_remaining_bytes,
            )
        if expected_remaining_bytes is not None:
            raise ValueError(
                "expected_remaining_bytes is only valid for legacy assets"
            )
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
        generation_id = _validate_token(generation_id)
        result = int(
            self._release_committed_upload_generation_script(
                keys=[
                    _STATE_KEY,
                    _committed_upload_key(
                        session_id,
                        asset_id,
                        generation_id,
                    ),
                    _session_committed_uploads_key(session_id),
                    _released_upload_generation_key(
                        session_id,
                        asset_id,
                        generation_id,
                    ),
                    _session_released_assets_key(session_id),
                    _pending_asset_release_key(session_id, asset_id),
                    _PENDING_ASSET_RELEASE_INDEX_KEY,
                ],
                args=[
                    session_id,
                    asset_id,
                    generation_id,
                    self._release_marker_ttl_ms,
                    _RESERVATION_RECEIPT_PREFIX,
                    self._reservation_receipt_ttl_ms,
                ],
            )
        )
        if result == 2:
            raise AssetReleaseMismatchError(
                "Asset release generation does not match"
            )
        if result == 3:
            raise ResourceUnavailableError(
                "Asset generation ledger is invalid"
            )
        return result == 1

    def release_legacy_committed_asset(
        self,
        session_id: str,
        asset_id: str,
        released_bytes: int,
        *,
        expected_remaining_bytes: int | None = None,
    ) -> bool:
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
        released_bytes = _nonnegative_int(released_bytes, "released_bytes")
        if expected_remaining_bytes is not None:
            expected_remaining_bytes = _nonnegative_int(
                expected_remaining_bytes,
                "expected_remaining_bytes",
            )
        result = int(
            self._release_legacy_committed_asset_script(
                keys=[
                    _STATE_KEY,
                    _released_asset_key(session_id, asset_id),
                    _session_released_assets_key(session_id),
                ],
                args=[
                    session_id,
                    asset_id,
                    released_bytes,
                    (
                        ""
                        if expected_remaining_bytes is None
                        else expected_remaining_bytes
                    ),
                    self._release_marker_ttl_ms,
                ],
            )
        )
        if result == 2:
            raise AssetReleaseMismatchError(
                "Asset release session does not match"
            )
        if result == 3:
            raise ResourceUnavailableError(
                "Asset release bytes do not match committed usage"
            )
        if result == 5:
            raise ResourceUnavailableError(
                "Asset release does not match audited committed usage"
            )
        return result == 1

    def is_committed_asset_released(
        self,
        session_id: str,
        asset_id: str,
        generation_id: str | None = None,
    ) -> bool:
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
        if generation_id is not None:
            _validate_token(generation_id)
            return bool(
                self._is_committed_upload_generation_released_script(
                    keys=[
                        _released_upload_generation_key(
                            session_id,
                            asset_id,
                            generation_id,
                        )
                    ],
                    args=[
                        session_id,
                        asset_id,
                        generation_id,
                    ],
                )
            )
        return bool(
            self._is_committed_asset_released_script(
                keys=[_released_asset_key(session_id, asset_id)],
                args=[
                    session_id,
                    asset_id,
                ],
            )
        )

    def pending_asset_release_generation(
        self,
        session_id: str,
        asset_id: str,
    ) -> str | None:
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
        value = self._redis.get(
            _pending_asset_release_key(session_id, asset_id)
        )
        if value is None:
            return None
        generation_id = value.decode() if isinstance(value, bytes) else value
        try:
            return _validate_token(generation_id)
        except (TypeError, ValueError) as exc:
            raise ResourceUnavailableError(
                "Asset release generation marker is invalid"
            ) from exc

    def finalize_asset_release(
        self,
        session_id: str,
        asset_id: str,
        generation_id: str | None = None,
    ) -> bool:
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
        if generation_id is not None:
            _validate_token(generation_id)
            result = int(
                self._finalize_upload_generation_release_script(
                    keys=[
                        _released_upload_generation_key(
                            session_id,
                            asset_id,
                            generation_id,
                        ),
                        _session_released_assets_key(session_id),
                        _pending_asset_release_key(session_id, asset_id),
                        _PENDING_ASSET_RELEASE_INDEX_KEY,
                    ],
                    args=[
                        session_id,
                        asset_id,
                        generation_id,
                        self._release_marker_ttl_ms,
                    ],
                )
            )
            if result == 2:
                raise AssetReleaseMismatchError(
                    "Asset release generation does not match"
                )
            return result == 1
        result = int(
            self._finalize_asset_release_script(
                keys=[
                    _released_asset_key(session_id, asset_id),
                    _session_released_assets_key(session_id),
                ],
                args=[
                    session_id,
                    asset_id,
                    self._release_marker_ttl_ms,
                ],
            )
        )
        if result == 2:
            raise AssetReleaseMismatchError(
                "Asset release session does not match"
            )
        return result == 1

    def reconcile_reservations(self) -> int:
        return int(
            self._reconcile_reservations_script(
                keys=[
                    _STATE_KEY,
                    _RESERVATION_INDEX_KEY,
                ],
                args=[
                    _RESERVATION_PREFIX,
                    _RESERVATION_RECEIPT_PREFIX,
                    self._reservation_receipt_ttl_ms,
                ],
            )
        )

    def release_task_resources(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
    ) -> None:
        metadata = self._redis.hgetall(_task_resource_key(task_id))
        session_id = _decoded_hash_value(metadata, "session_id")
        output_token = _decoded_hash_value(metadata, "output_token")
        lease_token = _decoded_hash_value(metadata, "lease_token")
        if output_token is not None:
            self._release_reservation(
                RedisReservation(self, "output", output_token, task_id, 0)
            )
        if lease_token is not None and session_id is not None:
            self._release_session(
                RedisSessionLease(self, session_id, lease_token, task_id),
                task_id=task_id,
            )
        self.reconcile_reservations()
        self.release_task_pin(
            task_id,
            expected_session_id=expected_session_id,
        )

    def release_task_pin(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
    ) -> bool:
        pin_key = _task_pin_key(task_id)
        metadata = self._redis.hgetall(pin_key)
        if not metadata:
            return False
        session_id = _decoded_hash_value(metadata, "session_id")
        if session_id is None or session_id != expected_session_id:
            raise CorruptTaskPinError("Task pin ownership is invalid")
        result = int(
            self._release_task_pin_script(
                keys=[
                    pin_key,
                    _session_task_pins_key(session_id),
                ],
                args=[
                    session_id,
                    task_id,
                    _ASSET_TASK_PREFIX,
                ],
            )
        )
        if result == 2:
            raise CorruptTaskPinError("Task pin ownership is invalid")
        if result in {3, 4}:
            raise CorruptTaskPinError("Task pin asset metadata is invalid")
        return result == 1

    def output_reserved_bytes(self) -> int:
        value = self._redis.hget(_STATE_KEY, "output_reserved_bytes")
        return int(value) if value is not None else 0

    def task_heartbeat_at(self, task_id: str) -> float | None:
        value = self._redis.hget(_task_resource_key(task_id), "heartbeat_at")
        return float(value) if value is not None else None

    def _refresh_reservation(self, reservation: RedisReservation) -> None:
        refreshed = self._refresh_reservation_script(
            keys=[
                _reservation_key(reservation.token),
                _reservation_receipt_key(reservation.token),
            ],
            args=[
                self._reservation_ttl_ms,
                self._reservation_receipt_ttl_ms,
            ],
        )
        if not refreshed:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} reservation expired"
            )
        if reservation.kind == "output":
            self._touch_task_resources(reservation.owner_id)

    def _release_reservation(self, reservation: RedisReservation) -> None:
        result = int(
            self._release_reservation_script(
                keys=[
                    _STATE_KEY,
                    _RESERVATION_INDEX_KEY,
                    _reservation_key(reservation.token),
                    _task_resource_key(reservation.owner_id),
                    _reservation_receipt_key(reservation.token),
                ],
                args=[
                    reservation.token,
                    self._reservation_receipt_ttl_ms,
                ],
            )
        )
        if result == 2:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} reservation metadata conflict"
            )

    def _commit_reservation(self, reservation: RedisReservation) -> None:
        committed_upload_key = (
            _committed_upload_key(
                reservation.owner_id,
                reservation.asset_id,
                reservation.generation_id,
            )
            if reservation.kind == "upload"
            and reservation.asset_id is not None
            and reservation.generation_id is not None
            else _committed_key(reservation.token)
        )
        committed_upload_index_key = (
            _session_committed_uploads_key(reservation.owner_id)
            if reservation.kind == "upload"
            else _committed_key(reservation.token)
        )
        committed = int(
            self._commit_reservation_script(
                keys=[
                    _STATE_KEY,
                    _RESERVATION_INDEX_KEY,
                    _reservation_key(reservation.token),
                    _committed_key(reservation.token),
                    _reservation_receipt_key(reservation.token),
                    committed_upload_key,
                    committed_upload_index_key,
                ],
                args=[
                    reservation.token,
                    self._reservation_ttl_ms,
                    self._reservation_receipt_ttl_ms,
                ],
            )
        )
        if not committed:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} reservation expired"
            )
        if committed == 3:
            raise ResourceUnavailableError(
                "Upload generation ledger already exists"
            )

    def _rollback_commit(self, reservation: RedisReservation) -> bool:
        generation_upload = (
            reservation.kind == "upload"
            and reservation.asset_id is not None
            and reservation.generation_id is not None
        )
        committed_upload_key = (
            _committed_upload_key(
                reservation.owner_id,
                reservation.asset_id,
                reservation.generation_id,
            )
            if generation_upload
            else _committed_key(reservation.token)
        )
        release_marker_key = (
            _released_upload_generation_key(
                reservation.owner_id,
                reservation.asset_id,
                reservation.generation_id,
            )
            if generation_upload
            else _committed_key(reservation.token)
        )
        result = int(
            self._rollback_commit_script(
                keys=[
                    _STATE_KEY,
                    _committed_key(reservation.token),
                    committed_upload_key,
                    _session_committed_uploads_key(reservation.owner_id),
                    _reservation_receipt_key(reservation.token),
                    release_marker_key,
                ],
                args=[
                    self._reservation_ttl_ms,
                    self._reservation_receipt_ttl_ms,
                    self._release_marker_ttl_ms,
                    reservation.kind,
                    reservation.owner_id if reservation.kind == "upload" else "",
                    reservation.reserved_bytes,
                    reservation.asset_id or "",
                    reservation.generation_id or "",
                ],
            )
        )
        if result == 3:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} rollback metadata conflict"
            )
        if result == 4:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} generation ledger is invalid"
            )
        return bool(result)

    def _finalize_commit(self, reservation: RedisReservation) -> None:
        self._finalize_commit_script(
            keys=[_committed_key(reservation.token)],
            args=[],
        )

    def _reservation_state(
        self,
        reservation: RedisReservation,
    ) -> ReservationState:
        generation_upload = (
            reservation.kind == "upload"
            and reservation.asset_id is not None
            and reservation.generation_id is not None
        )
        state = int(
            self._reservation_state_script(
                keys=[
                    _reservation_key(reservation.token),
                    _committed_key(reservation.token),
                    _reservation_receipt_key(reservation.token),
                    (
                        _committed_upload_key(
                            reservation.owner_id,
                            reservation.asset_id,
                            reservation.generation_id,
                        )
                        if generation_upload
                        else _committed_key(reservation.token)
                    ),
                    (
                        _released_upload_generation_key(
                            reservation.owner_id,
                            reservation.asset_id,
                            reservation.generation_id,
                        )
                        if generation_upload
                        else _committed_key(reservation.token)
                    ),
                ],
                args=["1" if generation_upload else "0"],
            )
        )
        if state == 1:
            return ReservationState.RESERVED
        if state == 2:
            return ReservationState.COMMITTED
        return ReservationState.FINALIZED_OR_ABSENT

    def _refresh_session(self, lease: RedisSessionLease) -> None:
        refreshed = self._refresh_token_script(
            keys=[_lease_key(lease.token)],
            args=[self._lease_ttl_ms],
        )
        if not refreshed:
            raise ResourceUnavailableError("Session lease expired")
        if lease.task_id is not None:
            self._touch_task_resources(lease.task_id)

    def _release_session(
        self,
        lease: RedisSessionLease,
        *,
        task_id: str | None = None,
    ) -> None:
        if task_id is None:
            task_id = lease.task_id
        self._release_session_script(
            keys=[
                _session_state_key(lease.session_id),
                _session_leases_key(lease.session_id),
                _lease_key(lease.token),
                (
                    _task_resource_key(task_id)
                    if task_id is not None
                    else _lease_key(lease.token)
                ),
            ],
            args=[lease.token],
        )

    def _touch_task_resources(self, task_id: str) -> None:
        key = _task_resource_key(task_id)
        pipeline = self._redis.pipeline()
        pipeline.hset(key, "heartbeat_at", time())
        pipeline.pexpire(
            key,
            max(self._reservation_ttl_ms, self._lease_ttl_ms),
        )
        pipeline.execute()


def _reservation_key(token: str) -> str:
    return f"{_RESERVATION_PREFIX}{token}"


def _reservation_receipt_key(token: str) -> str:
    return f"{_RESERVATION_RECEIPT_PREFIX}{token}"


def _committed_key(token: str) -> str:
    return f"{_COMMITTED_PREFIX}{token}"


def _committed_upload_key(
    session_id: str,
    asset_id: str,
    generation_id: str,
) -> str:
    return (
        f"{_COMMITTED_UPLOAD_PREFIX}{session_id}:{asset_id}:{generation_id}"
    )


def _lease_key(token: str) -> str:
    return f"{_LEASE_PREFIX}{token}"


def _cleanup_key(token: str) -> str:
    return f"{_CLEANUP_PREFIX}{token}"


def _cleanup_generation_key(token: str) -> str:
    return f"{_CLEANUP_GENERATION_PREFIX}{token}"


def _legacy_cleanup_key(token: str) -> str:
    return f"{_LEGACY_CLEANUP_PREFIX}{token}"


def _task_resource_key(task_id: str) -> str:
    return f"{_TASK_RESOURCE_PREFIX}{task_id}"


def _task_pin_key(task_id: str) -> str:
    return f"{_TASK_PIN_PREFIX}{task_id}"


def _asset_tasks_key(session_id: str, asset_id: str) -> str:
    return f"{_ASSET_TASK_PREFIX}{session_id}:{asset_id}"


def _upload_inflight_key(session_id: str, upload_id: str) -> str:
    return f"{_UPLOAD_INFLIGHT_PREFIX}{session_id}:{upload_id}"


def _decoded_hash_value(
    metadata: dict[bytes | str, bytes | str],
    field: str,
) -> str | None:
    value = metadata.get(field) or metadata.get(field.encode())
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


def _session_state_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}"


def _session_leases_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}:leases"


def _session_task_pins_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}:task-pins"


def _session_released_assets_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}:released-assets"


def _released_asset_key(session_id: str, asset_id: str) -> str:
    return f"{_RELEASED_ASSET_PREFIX}{session_id}:{asset_id}"


def _released_upload_generation_key(
    session_id: str,
    asset_id: str,
    generation_id: str,
) -> str:
    return (
        f"{_RELEASED_ASSET_PREFIX}{session_id}:{asset_id}:{generation_id}"
    )


def _pending_asset_release_key(session_id: str, asset_id: str) -> str:
    return f"{_PENDING_ASSET_RELEASE_PREFIX}{session_id}:{asset_id}"


def _session_committed_uploads_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}:committed-uploads"


def _validate_session_id(value: str) -> str:
    if not _SESSION_PATTERN.fullmatch(value):
        raise ValueError("Invalid hosted session ID")
    return value


def _validate_asset_id(value: str) -> str:
    if not _ASSET_PATTERN.fullmatch(value):
        raise ValueError("Invalid hosted asset ID")
    return value


def _validate_token(value: str) -> str:
    if not _TOKEN_PATTERN.fullmatch(value):
        raise ValueError("Invalid resource token")
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
