"""Coordinate hosted resources using standalone Redis Lua scripts.

The scripts target the standalone Redis instance used by Docker Compose. Redis
Cluster support is intentionally out of scope.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from time import time
from typing import Literal
from uuid import uuid4

from redis import Redis

_STATE_KEY = "dt:resource:state"
_RESERVATION_INDEX_KEY = "dt:resource:reservations"
_RESERVATION_PREFIX = "dt:resource:reservation:"
_COMMITTED_PREFIX = "dt:resource:committed:"
_RELEASED_ASSET_PREFIX = "dt:resource:asset-released:"
_LEASE_PREFIX = "dt:resource:lease:"
_CLEANUP_PREFIX = "dt:resource:cleanup:"
_CLEANUP_GENERATION_PREFIX = "dt:resource:cleanup-generation:"
_LEGACY_CLEANUP_PREFIX = "dt:resource:legacy-cleanup:"
_TASK_RESOURCE_PREFIX = "dt:resource:task:"
_SESSION_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_ASSET_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{32}$")
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
local heartbeat_at = ARGV[8]

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

_RELEASE_RESERVATION_SCRIPT = """-- release_reservation
local state_key = KEYS[1]
local index_key = KEYS[2]
local token_key = KEYS[3]
local task_key = KEYS[4]
if redis.call("HGET", task_key, "output_token") == ARGV[1] then
    redis.call("HDEL", task_key, "output_token")
    if redis.call("HEXISTS", task_key, "lease_token") == 0 then
        redis.call("DEL", task_key)
    end
end
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
if redis.call("HGET", committed_key, "state") == "committed" then
    return 2
end
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
redis.call("PEXPIRE", committed_key, tonumber(ARGV[2]))
recompute_capacity()
return 1
"""

_ROLLBACK_COMMIT_SCRIPT = """-- rollback_commit
local state_key = KEYS[1]
local committed_key = KEYS[2]
if redis.call("EXISTS", committed_key) == 0 then
    return 0
end
if redis.call("HGET", committed_key, "state") == "rolled_back" then
    return 2
end

local kind = redis.call("HGET", committed_key, "kind")
local session_id = redis.call("HGET", committed_key, "session_id")
local committed_bytes = tonumber(redis.call("HGET", committed_key, "bytes") or "0")
if kind == "upload" then
    local committed_field = "session:" .. session_id .. ":committed_bytes"
    local current = tonumber(redis.call("HGET", state_key, committed_field) or "0")
    local remaining = current - committed_bytes
    if remaining > 0 then
        redis.call("HSET", state_key, committed_field, remaining)
    else
        redis.call("HDEL", state_key, committed_field)
    end
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
return 0
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
            elseif kind == "output" then
                decrement("output_reserved_bytes", reserved_bytes)
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
    redis.call("DEL", marker_key)
end
redis.call("DEL", KEYS[2])
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
    )
}
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
    return 0
end

local committed_field = "session:" .. ARGV[1] .. ":committed_bytes"
local current = tonumber(redis.call("HGET", KEYS[1], committed_field) or "0")
local generation_bytes = tonumber(ARGV[4])
if generation_bytes > current then
    return 2
end
local remaining = current - generation_bytes
if remaining > 0 then
    redis.call("HSET", KEYS[1], committed_field, remaining)
else
    redis.call("HDEL", KEYS[1], committed_field)
end

for index = 6, #ARGV do
    local asset_id = ARGV[index]
    local marker_key = ARGV[5] .. ARGV[1] .. ":" .. asset_id
    local marker_session = redis.call("HGET", marker_key, "session_id")
    local marker_asset = redis.call("HGET", marker_key, "asset_id")
    if marker_session == ARGV[1] and marker_asset == asset_id then
        redis.call("DEL", marker_key)
        redis.call("SREM", KEYS[4], marker_key)
    end
end
redis.call("SET", KEYS[5], "cleared")
return 1
"""

_FINALIZE_SESSION_USAGE_GENERATION_SCRIPT = (
    """-- finalize_session_usage_generation
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
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
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[1]))
return 1
"""

_RELEASE_COMMITTED_ASSET_SCRIPT = """-- release_committed_asset
local session_id = ARGV[1]
local asset_id = ARGV[2]
local released_bytes = tonumber(ARGV[3])
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
        redis.call("SADD", KEYS[3], KEYS[2])
    end
    return 0
end

local field = "session:" .. session_id .. ":committed_bytes"
local current = tonumber(redis.call("HGET", KEYS[1], field) or "0")
if released_bytes == 0 then
    return 4
end
if released_bytes > current then
    return 3
end
local remaining = current - released_bytes
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
redis.call("SADD", KEYS[3], KEYS[2])
return 1
"""

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
if redis.call("HGET", KEYS[1], "finalized") == "1" then
    return 0
end
redis.call("HSET", KEYS[1], "finalized", "1")
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[3]))
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
        self.reservation_heartbeat_interval = reservation_heartbeat_interval
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
        self._release_committed_asset_script = self._redis.register_script(
            _RELEASE_COMMITTED_ASSET_SCRIPT
        )
        self._is_committed_asset_released_script = self._redis.register_script(
            _IS_COMMITTED_ASSET_RELEASED_SCRIPT
        )
        self._finalize_asset_release_script = self._redis.register_script(
            _FINALIZE_ASSET_RELEASE_SCRIPT
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
                    _task_resource_key(task_id),
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
            keys=[
                _STATE_KEY,
                _session_released_assets_key(session_id),
            ],
            args=[session_id],
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
            ],
            args=[session_id, claim_token],
        )
        if int(result[0]) != 1:
            raise ResourceUnavailableError("Cleanup claim expired")
        marker_prefix = f"{_RELEASED_ASSET_PREFIX}{session_id}:"
        released_asset_ids: list[str] = []
        for raw_marker_key in result[2:]:
            marker_key = (
                raw_marker_key.decode()
                if isinstance(raw_marker_key, bytes)
                else raw_marker_key
            )
            if not marker_key.startswith(marker_prefix):
                raise ResourceUnavailableError(
                    "Session release marker state is invalid"
                )
            released_asset_ids.append(
                _validate_asset_id(marker_key.removeprefix(marker_prefix))
            )
        return SessionUsageGeneration(
            generation_id=claim_token,
            committed_bytes=int(result[1]),
            released_asset_ids=tuple(sorted(released_asset_ids)),
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
        result = int(
            self._clear_session_usage_generation_script(
                keys=[
                    _STATE_KEY,
                    _session_state_key(session_id),
                    _cleanup_key(claim_token),
                    _session_released_assets_key(session_id),
                    _cleanup_generation_key(generation.generation_id),
                ],
                args=[
                    session_id,
                    claim_token,
                    generation.generation_id,
                    committed_bytes,
                    _RELEASED_ASSET_PREFIX,
                    *asset_ids,
                ],
            )
        )
        if result == 2:
            raise ResourceUnavailableError(
                "Session usage generation exceeds committed usage"
            )
        if result == 3:
            raise ResourceUnavailableError("Cleanup claim expired")

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

    def release_committed_asset(
        self,
        session_id: str,
        asset_id: str,
        released_bytes: int,
    ) -> bool:
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
        released_bytes = _nonnegative_int(released_bytes, "released_bytes")
        result = int(
            self._release_committed_asset_script(
                keys=[
                    _STATE_KEY,
                    _released_asset_key(session_id, asset_id),
                    _session_released_assets_key(session_id),
                ],
                args=[
                    session_id,
                    asset_id,
                    released_bytes,
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
        return result == 1

    def is_committed_asset_released(
        self,
        session_id: str,
        asset_id: str,
    ) -> bool:
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
        return bool(
            self._is_committed_asset_released_script(
                keys=[_released_asset_key(session_id, asset_id)],
                args=[
                    session_id,
                    asset_id,
                ],
            )
        )

    def finalize_asset_release(self, session_id: str, asset_id: str) -> bool:
        _validate_session_id(session_id)
        _validate_asset_id(asset_id)
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
                ],
            )
        )

    def release_task_resources(self, task_id: str) -> None:
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

    def output_reserved_bytes(self) -> int:
        value = self._redis.hget(_STATE_KEY, "output_reserved_bytes")
        return int(value) if value is not None else 0

    def task_heartbeat_at(self, task_id: str) -> float | None:
        value = self._redis.hget(_task_resource_key(task_id), "heartbeat_at")
        return float(value) if value is not None else None

    def _refresh_reservation(self, reservation: RedisReservation) -> None:
        refreshed = self._refresh_token_script(
            keys=[_reservation_key(reservation.token)],
            args=[self._reservation_ttl_ms],
        )
        if not refreshed:
            raise ResourceUnavailableError(
                f"{reservation.kind.capitalize()} reservation expired"
            )
        if reservation.kind == "output":
            self._touch_task_resources(reservation.owner_id)

    def _release_reservation(self, reservation: RedisReservation) -> None:
        self._release_reservation_script(
            keys=[
                _STATE_KEY,
                _RESERVATION_INDEX_KEY,
                _reservation_key(reservation.token),
                _task_resource_key(reservation.owner_id),
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

    def _rollback_commit(self, reservation: RedisReservation) -> bool:
        return bool(
            self._rollback_commit_script(
                keys=[
                    _STATE_KEY,
                    _committed_key(reservation.token),
                ],
                args=[self._reservation_ttl_ms],
            )
        )

    def _finalize_commit(self, reservation: RedisReservation) -> None:
        self._finalize_commit_script(
            keys=[_committed_key(reservation.token)],
            args=[],
        )

    def _reservation_state(
        self,
        reservation: RedisReservation,
    ) -> ReservationState:
        state = int(
            self._reservation_state_script(
                keys=[
                    _reservation_key(reservation.token),
                    _committed_key(reservation.token),
                ],
                args=[],
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
        task_id = lease.task_id or self._task_id_for_lease(lease.token)
        if task_id is not None:
            self._touch_task_resources(task_id)

    def _release_session(
        self,
        lease: RedisSessionLease,
        *,
        task_id: str | None = None,
    ) -> None:
        if task_id is None:
            task_id = lease.task_id or self._task_id_for_lease(lease.token)
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

    def _task_id_for_lease(self, token: str) -> str | None:
        for key in self._redis.scan_iter(match=f"{_TASK_RESOURCE_PREFIX}*"):
            value = self._redis.hget(key, "lease_token")
            if value is None:
                continue
            value_text = value.decode() if isinstance(value, bytes) else value
            if value_text == token:
                key_text = key.decode() if isinstance(key, bytes) else key
                return key_text.removeprefix(_TASK_RESOURCE_PREFIX)
        return None


def _reservation_key(token: str) -> str:
    return f"{_RESERVATION_PREFIX}{token}"


def _committed_key(token: str) -> str:
    return f"{_COMMITTED_PREFIX}{token}"


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


def _session_released_assets_key(session_id: str) -> str:
    return f"dt:resource:session:{session_id}:released-assets"


def _released_asset_key(session_id: str, asset_id: str) -> str:
    return f"{_RELEASED_ASSET_PREFIX}{session_id}:{asset_id}"


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
