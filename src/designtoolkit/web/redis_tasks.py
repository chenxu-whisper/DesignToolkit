from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from queue import Empty
from threading import Event, Thread
from time import monotonic, sleep, time
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from redis import Redis
from redis.exceptions import RedisError, ResponseError, WatchError
from rq import Queue
from rq.job import Callback, Job, JobStatus
from rq.registry import FailedJobRegistry, StartedJobRegistry

from designtoolkit.chroma_keyer.image_io import (
    default_output_path,
    validate_output_path,
)
from designtoolkit.chroma_keyer.models import KeyerError
from designtoolkit.video_frames import VideoFrameOptions
from designtoolkit.web.adapters import (
    chroma_keyer_options_from_request,
    chroma_video_keyer_options_from_request,
    media_resize_options_list_from_request,
    video_frame_options_list_from_request,
)
from designtoolkit.web.heartbeat import (
    DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
    HeartbeatStopTimeoutError,
    stop_heartbeat_thread,
)
from designtoolkit.web.redis_errors import EventCursorUnavailable
from designtoolkit.web.resources import (
    _ASSET_TASK_PREFIX,
    _CLEANUP_PREFIX,
    _RELEASE_TASK_PIN_SCRIPT,
    _TASK_PIN_PREFIX,
    CorruptTaskPinError,
    SessionDeletingError,
    _asset_tasks_key,
    _session_state_key,
    _session_task_pins_key,
    _task_pin_key,
    _validate_asset_id,
    _validate_session_id,
    _validate_token,
)
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    ChromaVideoKeyerTaskRequest,
    LogEventResponse,
    LogLevel,
    LogPhase,
    MediaResizeTaskRequest,
    TaskKind,
    TaskStatus,
    VideoFramesTaskRequest,
)
from designtoolkit.web.tasks import TaskConflictError, TaskRecord

if TYPE_CHECKING:
    from designtoolkit.web.resources import RedisResourceCoordinator

_QUEUE_NAME = "designtoolkit"
_UNCERTAIN_ENQUEUE_ERROR = "Task enqueue could not be confirmed"
_ENQUEUE_INTENT_TTL_SECONDS = 60.0
_ENQUEUE_INTENT_HEARTBEAT_SECONDS = 10.0
_ASSET_DELETE_PREFIX = "dt:resource:asset-delete:"
_ASSET_DELETE_TTL_MS = 60_000
_ASSET_READ_PIN_PREFIX = "dt:resource:asset-read-pin:"
_ASSET_READERS_PREFIX = "dt:resource:asset-readers:"
_ASSET_READ_PIN_TTL_SECONDS = 60.0
_ASSET_READ_PIN_HEARTBEAT_SECONDS = 10.0
_CORRUPT_PIN_SCAN_COUNT = 100
_MAX_REVERSE_TASK_ID_BYTES = 128
_SESSION_TASK_PINS_PREFIX = "dt:resource:session:"
_SESSION_TASK_PINS_SUFFIX = ":task-pins"
type AssetIdentity = tuple[str, str | None]
_ALLOW_REQUEST_SCRIPT = """-- allow_request
local count = redis.call("INCR", KEYS[1])
if count == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end
return count
"""
_APPEND_EVENT_SCRIPT = """-- append_task_event
local event_exists = redis.call("EXISTS", KEYS[1]) == 1
local generation = redis.call("GET", KEYS[2])
local event_count = redis.call("LLEN", KEYS[1])
local indexed_sequence = tonumber(
    redis.call("HGET", KEYS[3], "next_sequence")
)
local index_consistent = indexed_sequence == event_count
    and redis.call("HLEN", KEYS[3]) == event_count + 1
if not event_exists or not generation then
    generation = ARGV[3]
    redis.call("SET", KEYS[2], generation)
    redis.call("DEL", KEYS[3])
    redis.call("HSET", KEYS[3], "next_sequence", event_count)
elseif not index_consistent then
    redis.call("DEL", KEYS[3])
    redis.call("HSET", KEYS[3], "next_sequence", event_count)
end
local sequence = redis.call("HINCRBY", KEYS[3], "next_sequence", 1)
redis.call("HSET", KEYS[3], tostring(sequence), ARGV[1])
redis.call("RPUSH", KEYS[1], ARGV[1])
redis.call("EXPIRE", KEYS[1], ARGV[2])
redis.call("EXPIRE", KEYS[2], ARGV[2])
redis.call("EXPIRE", KEYS[3], ARGV[2])
return {generation, tostring(sequence)}
"""
_READ_EVENT_LOG_SCRIPT = """-- read_task_event_log
if redis.call("EXISTS", KEYS[1]) == 0 then
    return {"", {}, "1"}
end

local generation = redis.call("GET", KEYS[2])
if not generation then
    generation = ARGV[1]
    redis.call("SET", KEYS[2], generation)
end
local history = redis.call("LRANGE", KEYS[1], 0, -1)
local event_count = #history
local indexed_sequence = tonumber(
    redis.call("HGET", KEYS[3], "next_sequence")
)
local index_consistent = indexed_sequence == event_count
    and redis.call("HLEN", KEYS[3]) == event_count + 1
if index_consistent then
    for sequence, event in ipairs(history) do
        if redis.call("HGET", KEYS[3], tostring(sequence)) ~= event then
            index_consistent = false
            break
        end
    end
end
if not index_consistent then
    redis.call("DEL", KEYS[3])
    redis.call("HSET", KEYS[3], "next_sequence", event_count)
    for sequence, event in ipairs(history) do
        redis.call("HSET", KEYS[3], tostring(sequence), event)
    end
end
local ttl_ms = redis.call("PTTL", KEYS[1])
if ttl_ms > 0 then
    redis.call("PEXPIRE", KEYS[1], ttl_ms)
    redis.call("PEXPIRE", KEYS[2], ttl_ms)
    redis.call("PEXPIRE", KEYS[3], ttl_ms)
else
    redis.call("EXPIRE", KEYS[1], ARGV[2])
    redis.call("EXPIRE", KEYS[2], ARGV[2])
    redis.call("EXPIRE", KEYS[3], ARGV[2])
end
return {generation, history, tostring(event_count + 1)}
"""
_READ_EVENT_AT_CURSOR_SCRIPT = """-- read_task_event_at_cursor
local expected_generation = ARGV[1]
local expected_sequence = tonumber(ARGV[2])
local generation = redis.call("GET", KEYS[2])
local indexed_sequence_value = redis.call("HGET", KEYS[3], "next_sequence")
local indexed_sequence = tonumber(indexed_sequence_value)

if not generation then
    if expected_generation ~= "" or indexed_sequence_value then
        return {"unavailable", "", false, ARGV[2]}
    end
    return {"empty", "", false, ARGV[2]}
end
if expected_generation ~= "" and generation ~= expected_generation then
    return {"unavailable", generation, false, ARGV[2]}
end
if not indexed_sequence or not expected_sequence
    or indexed_sequence < 0 or expected_sequence < 1
    or expected_sequence > indexed_sequence + 1 then
    return {"unavailable", generation, false, ARGV[2]}
end

local event = false
if expected_sequence <= indexed_sequence then
    event = redis.call("HGET", KEYS[3], tostring(expected_sequence))
    if not event then
        return {"unavailable", generation, false, ARGV[2]}
    end
end

if not event then
    return {"ok", generation, false, tostring(expected_sequence)}
end
return {"ok", generation, event, tostring(expected_sequence + 1)}
"""
_RESERVE_ENQUEUE_SCRIPT = """-- reserve_enqueue_intent
local queue_key = KEYS[1]
local intent_key = KEYS[2]
local owner_key = KEYS[3]
local session_state_key = KEYS[4]
local session_task_pins_key = KEYS[5]
local task_pin_key = KEYS[6]
local task_id = ARGV[1]
local max_queued_tasks = tonumber(ARGV[2])
local ttl_seconds = tonumber(ARGV[3])
local owner_id = ARGV[4]
local created_at = ARGV[5]
local cleanup_prefix = ARGV[6]
local owner_token = ARGV[7]
local owner_ttl_ms = tonumber(ARGV[8])
local asset_identities = cjson.decode(ARGV[9])
local asset_task_prefix = ARGV[11]
local asset_delete_prefix = ARGV[12]
local asset_read_pin_token = ARGV[13]
local asset_readers_prefix = ARGV[14]

if owner_id ~= "" then
    local deleting = redis.call("HGET", session_state_key, "deleting")
    if deleting then
        if redis.call("EXISTS", cleanup_prefix .. deleting) == 1 then
            return 2
        end
        redis.call("HDEL", session_state_key, "deleting")
    end
end
if redis.call("LLEN", queue_key) >= max_queued_tasks then
    return 1
end
for _, identity in ipairs(asset_identities) do
    local asset_id = identity[1]
    local delete_key = asset_delete_prefix .. owner_id .. ":" .. asset_id
    local redis_time = redis.call("TIME")
    local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)
    redis.call("ZREMRANGEBYSCORE", delete_key, "-inf", now_ms)
    if redis.call("ZCARD", delete_key) ~= 0 then
        return 3
    end
end
if asset_read_pin_token ~= "" then
    if redis.call("HGET", KEYS[7], "token") ~= asset_read_pin_token
        or redis.call("HGET", KEYS[7], "session_id") ~= owner_id
        or redis.call("HGET", KEYS[7], "asset_identities") ~= ARGV[9] then
        return 4
    end
end

redis.call("RPUSH", queue_key, task_id)
redis.call(
    "HMSET",
    intent_key,
    "task_id",
    task_id,
    "job_id",
    task_id,
    "slot_reserved",
    "1",
    "state",
    "creating",
    "owner_id",
    owner_id,
    "created_at",
    created_at,
    "owner_token",
    owner_token
)
redis.call("EXPIRE", intent_key, ttl_seconds)
redis.call("SET", owner_key, owner_token, "PX", owner_ttl_ms)
if owner_id ~= "" then
    redis.call(
        "HMSET",
        task_pin_key,
        "task_id",
        task_id,
        "session_id",
        owner_id,
        "asset_ids",
        ARGV[10],
        "asset_identities",
        ARGV[9]
    )
    redis.call("SADD", session_task_pins_key, task_id)
    for _, identity in ipairs(asset_identities) do
        local asset_id = identity[1]
        redis.call(
            "SADD",
            asset_task_prefix .. owner_id .. ":" .. asset_id,
            task_id
        )
    end
end
if asset_read_pin_token ~= "" then
    for _, identity in ipairs(asset_identities) do
        local asset_id = identity[1]
        local readers_key = asset_readers_prefix .. owner_id .. ":" .. asset_id
        redis.call("ZREM", readers_key, asset_read_pin_token)
        if redis.call("ZCARD", readers_key) == 0 then
            redis.call("DEL", readers_key)
        end
    end
    redis.call("DEL", KEYS[7])
end
return 0
"""
_ROLLBACK_ENQUEUE_SCRIPT = """-- rollback_enqueue_intent
local task_id = ARGV[1]
local owner_id = ARGV[2]
local owner_token = ARGV[3]
local expected_identities_json = ARGV[5]
local expected_asset_ids_json = ARGV[6]
if redis.call("HGET", KEYS[5], "owner_token") ~= owner_token then
    return 0
end
local release_pin = false
local repair_pin = (
    redis.call("HGET", KEYS[5], "task_id") ~= task_id
    or redis.call("HGET", KEYS[5], "owner_id") ~= owner_id
)
local asset_ids = {}
if owner_id ~= "" then
    release_pin = true
    local pin_session = redis.call("HGET", KEYS[6], "session_id")
    local pin_task = redis.call("HGET", KEYS[6], "task_id")
    if pin_session ~= owner_id
        or pin_task ~= task_id
        or expected_identities_json == ""
        or redis.call(
            "HGET",
            KEYS[6],
            "asset_identities"
        ) ~= expected_identities_json
        or redis.call("HGET", KEYS[6], "asset_ids") ~= expected_asset_ids_json
    then
        repair_pin = true
    else
        local identities_json = redis.call(
            "HGET",
            KEYS[6],
            "asset_identities"
        )
        local asset_ids_json = redis.call("HGET", KEYS[6], "asset_ids")
        if not identities_json and not asset_ids_json then
            repair_pin = true
        end
        if identities_json and not repair_pin then
            if string.sub(identities_json, 1, 1) ~= "[" then
                repair_pin = true
            else
                local decoded, identities = pcall(cjson.decode, identities_json)
                if not decoded or type(identities) ~= "table" then
                    repair_pin = true
                else
                    for index, identity in ipairs(identities) do
                        if type(identity) ~= "table"
                            or #identity ~= 2
                            or type(identity[1]) ~= "string"
                            or (
                                identity[2] ~= cjson.null
                                and type(identity[2]) ~= "string"
                            )
                            or (
                                type(identity[2]) == "string"
                                and (
                                    string.len(identity[2]) ~= 32
                                    or string.match(
                                        identity[2],
                                        "^[0-9a-f]+$"
                                    ) == nil
                                )
                            ) then
                            repair_pin = true
                            break
                        end
                        asset_ids[index] = identity[1]
                    end
                end
            end
            if asset_ids_json and not repair_pin then
                if string.sub(asset_ids_json, 1, 1) ~= "[" then
                    repair_pin = true
                else
                    local decoded_ids, legacy_asset_ids = pcall(
                        cjson.decode,
                        asset_ids_json
                    )
                    if not decoded_ids
                        or type(legacy_asset_ids) ~= "table"
                        or #legacy_asset_ids ~= #asset_ids then
                        repair_pin = true
                    end
                    if not repair_pin then
                        for index, asset_id in ipairs(legacy_asset_ids) do
                            if type(asset_id) ~= "string"
                                or asset_id ~= asset_ids[index] then
                                repair_pin = true
                                break
                            end
                        end
                    end
                end
            end
        elseif asset_ids_json and not repair_pin then
            if string.sub(asset_ids_json, 1, 1) ~= "[" then
                repair_pin = true
            else
                local decoded, legacy_asset_ids = pcall(
                    cjson.decode,
                    asset_ids_json
                )
                if not decoded or type(legacy_asset_ids) ~= "table" then
                    repair_pin = true
                else
                    for index, asset_id in ipairs(legacy_asset_ids) do
                        if type(asset_id) ~= "string" then
                            repair_pin = true
                            break
                        end
                        asset_ids[index] = asset_id
                    end
                end
            end
        end
        if not repair_pin then
            for _, asset_id in ipairs(asset_ids) do
                if string.len(asset_id) ~= 32
                    or string.match(asset_id, "^[0-9a-f]+$") == nil then
                    repair_pin = true
                    break
                end
            end
        end
    end
end
redis.call("LREM", KEYS[1], 0, task_id)
redis.call(
    "DEL",
    KEYS[2],
    KEYS[3],
    KEYS[4],
    KEYS[5],
    KEYS[8],
    KEYS[9],
    KEYS[10]
)
if release_pin and not repair_pin then
    for _, asset_id in ipairs(asset_ids) do
        local asset_tasks_key = ARGV[4] .. owner_id .. ":" .. asset_id
        redis.call("SREM", asset_tasks_key, task_id)
        if redis.call("SCARD", asset_tasks_key) == 0 then
            redis.call("DEL", asset_tasks_key)
        end
    end
    redis.call("DEL", KEYS[6])
    redis.call("SREM", KEYS[7], task_id)
    if redis.call("SCARD", KEYS[7]) == 0 then
        redis.call("DEL", KEYS[7])
    end
end
if repair_pin then
    return 4
end
return 1
"""
_CLAIM_ASSET_DELETION_SCRIPT = """-- claim_asset_deletion
if redis.call("SCARD", KEYS[1]) ~= 0 then
    return 1
end
local redis_time = redis.call("TIME")
local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)
redis.call("ZREMRANGEBYSCORE", KEYS[2], "-inf", now_ms)
redis.call("ZREMRANGEBYSCORE", KEYS[3], "-inf", now_ms)
if redis.call("ZCARD", KEYS[3]) ~= 0 then
    return 1
end
redis.call("ZADD", KEYS[2], now_ms + tonumber(ARGV[2]), ARGV[1])
redis.call("PEXPIRE", KEYS[2], ARGV[2])
return 0
"""
_RELEASE_ASSET_DELETION_SCRIPT = """-- release_asset_deletion
local removed = redis.call("ZREM", KEYS[1], ARGV[1])
if redis.call("ZCARD", KEYS[1]) == 0 then
    redis.call("DEL", KEYS[1])
end
return removed
"""
_ACQUIRE_ASSET_READ_PIN_SCRIPT = """-- acquire_asset_read_pin
local token = ARGV[1]
local session_id = ARGV[2]
local ttl_ms = tonumber(ARGV[3])
local asset_identities_json = ARGV[4]
local asset_count = tonumber(ARGV[5])
local redis_time = redis.call("TIME")
local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)
local expires_at = now_ms + ttl_ms

for index = 1, asset_count do
    local delete_key = KEYS[1 + index]
    redis.call("ZREMRANGEBYSCORE", delete_key, "-inf", now_ms)
    if redis.call("ZCARD", delete_key) ~= 0 then
        return 1
    end
end

redis.call(
    "HMSET",
    KEYS[1],
    "token",
    token,
    "session_id",
    session_id,
    "asset_identities",
    asset_identities_json
)
redis.call("PEXPIRE", KEYS[1], ttl_ms)
for index = 1, asset_count do
    local readers_key = KEYS[1 + asset_count + index]
    redis.call("ZREMRANGEBYSCORE", readers_key, "-inf", now_ms)
    redis.call("ZADD", readers_key, expires_at, token)
end
return 0
"""
_REFRESH_ASSET_READ_PIN_SCRIPT = """-- refresh_asset_read_pin
if redis.call("HGET", KEYS[1], "token") ~= ARGV[1]
    or redis.call("HGET", KEYS[1], "session_id") ~= ARGV[2]
    or redis.call("HGET", KEYS[1], "asset_identities") ~= ARGV[4] then
    return 0
end
local redis_time = redis.call("TIME")
local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)
local expires_at = now_ms + tonumber(ARGV[3])
redis.call("PEXPIRE", KEYS[1], ARGV[3])
for index = 2, #KEYS do
    redis.call("ZREMRANGEBYSCORE", KEYS[index], "-inf", now_ms)
    redis.call("ZADD", KEYS[index], expires_at, ARGV[1])
end
return 1
"""
_RELEASE_ASSET_READ_PIN_SCRIPT = """-- release_asset_read_pin
if redis.call("EXISTS", KEYS[1]) == 0 then
    return 0
end
if redis.call("HGET", KEYS[1], "token") ~= ARGV[1]
    or redis.call("HGET", KEYS[1], "session_id") ~= ARGV[2]
    or redis.call("HGET", KEYS[1], "asset_identities") ~= ARGV[3] then
    return 2
end
for index = 2, #KEYS do
    redis.call("ZREM", KEYS[index], ARGV[1])
    if redis.call("ZCARD", KEYS[index]) == 0 then
        redis.call("DEL", KEYS[index])
    end
end
redis.call("DEL", KEYS[1])
return 1
"""
_REFRESH_ENQUEUE_OWNER_SCRIPT = """-- refresh_enqueue_owner
local owner_token = ARGV[1]
if redis.call("HGET", KEYS[1], "owner_token") ~= owner_token then
    return 0
end
local state = redis.call("HGET", KEYS[1], "state")
if state ~= "creating" and state ~= "uncertain" then
    return 0
end
if redis.call("GET", KEYS[2]) ~= owner_token then
    return 0
end
redis.call("PEXPIRE", KEYS[2], ARGV[2])
return 1
"""
_CLAIM_EXPIRED_ENQUEUE_INTENT_SCRIPT = """-- claim_expired_enqueue_intent
local task_id = ARGV[1]
local reconciler_token = ARGV[2]
local ttl_seconds = tonumber(ARGV[3])
local owner_ttl_ms = tonumber(ARGV[4])
local stored_task_id = redis.call("HGET", KEYS[1], "task_id")
local job_id = redis.call("HGET", KEYS[1], "job_id")
local state = redis.call("HGET", KEYS[1], "state")
local owner_token = redis.call("HGET", KEYS[1], "owner_token")

if stored_task_id ~= task_id or job_id ~= task_id then
    return 0
end
if state ~= "creating"
    and state ~= "uncertain"
    and state ~= "reconciling" then
    return 0
end
if owner_token and redis.call("GET", KEYS[2]) == owner_token then
    return 1
end

redis.call(
    "HSET",
    KEYS[1],
    "state",
    "reconciling",
    "owner_token",
    reconciler_token
)
redis.call("EXPIRE", KEYS[1], ttl_seconds)
redis.call("SET", KEYS[2], reconciler_token, "PX", owner_ttl_ms)
return 2
"""
_MARK_RUNNING_SCRIPT = """-- mark_task_running
local value = redis.call("GET", KEYS[1])
if not value then
    return {0, ""}
end

local task = cjson.decode(value)
if task.status ~= "queued" then
    return {2, value}
end

task.status = "running"
task.started_at = ARGV[2]
local encoded = cjson.encode(task)
redis.call("SET", KEYS[1], encoded, "EX", ARGV[3])
redis.call("LREM", KEYS[2], 0, ARGV[1])
local event_exists = redis.call("EXISTS", KEYS[3]) == 1
local generation = redis.call("GET", KEYS[4])
local event_count = redis.call("LLEN", KEYS[3])
local indexed_sequence = tonumber(
    redis.call("HGET", KEYS[5], "next_sequence")
)
local index_consistent = indexed_sequence == event_count
    and redis.call("HLEN", KEYS[5]) == event_count + 1
if not event_exists or not generation then
    redis.call("SET", KEYS[4], ARGV[5])
    redis.call("DEL", KEYS[5])
    redis.call("HSET", KEYS[5], "next_sequence", event_count)
elseif not index_consistent then
    redis.call("DEL", KEYS[5])
    redis.call("HSET", KEYS[5], "next_sequence", event_count)
end
local sequence = redis.call("HINCRBY", KEYS[5], "next_sequence", 1)
redis.call("HSET", KEYS[5], tostring(sequence), ARGV[4])
redis.call("RPUSH", KEYS[3], ARGV[4])
redis.call("EXPIRE", KEYS[3], ARGV[3])
redis.call("EXPIRE", KEYS[4], ARGV[3])
redis.call("EXPIRE", KEYS[5], ARGV[3])
return {1, encoded}
"""
_TERMINAL_TRANSITION_SCRIPT = """-- transition_task_terminal
local value = redis.call("GET", KEYS[1])
if not value then
    return {0, ""}
end

local task = cjson.decode(value)
if task.status == "succeeded" or task.status == "failed" then
    return {2, value}
end

task.status = ARGV[1]
if ARGV[2] == "1" then
    task.output_path = ARGV[3]
end
if ARGV[4] == "1" then
    task.error = ARGV[5]
else
    task.error = cjson.null
end
if ARGV[6] == "1" then
    task.result_path = ARGV[7]
end
task.finished_at = ARGV[8]

local encoded = cjson.encode(task)
redis.call("SET", KEYS[1], encoded, "EX", ARGV[9])
if redis.call("EXISTS", KEYS[2]) == 1 then
    redis.call("EXPIRE", KEYS[2], ARGV[9])
end
if ARGV[10] == "1" then
    local event_exists = redis.call("EXISTS", KEYS[3]) == 1
    local generation = redis.call("GET", KEYS[4])
    local event_count = redis.call("LLEN", KEYS[3])
    local indexed_sequence = tonumber(
        redis.call("HGET", KEYS[5], "next_sequence")
    )
    local index_consistent = indexed_sequence == event_count
        and redis.call("HLEN", KEYS[5]) == event_count + 1
    if not event_exists or not generation then
        redis.call("SET", KEYS[4], ARGV[12])
        redis.call("DEL", KEYS[5])
        redis.call(
            "HSET",
            KEYS[5],
            "next_sequence",
            event_count
        )
    elseif not index_consistent then
        redis.call("DEL", KEYS[5])
        redis.call(
            "HSET",
            KEYS[5],
            "next_sequence",
            event_count
        )
    end
    local sequence = redis.call("HINCRBY", KEYS[5], "next_sequence", 1)
    redis.call("HSET", KEYS[5], tostring(sequence), ARGV[11])
    redis.call("RPUSH", KEYS[3], ARGV[11])
end
if redis.call("EXISTS", KEYS[3]) == 1 then
    if redis.call("EXISTS", KEYS[4]) == 0 then
        redis.call("SET", KEYS[4], ARGV[12])
    end
    if ARGV[10] ~= "1" then
        local event_count = redis.call("LLEN", KEYS[3])
        local indexed_sequence = tonumber(
            redis.call("HGET", KEYS[5], "next_sequence")
        )
        if indexed_sequence ~= event_count
            or redis.call("HLEN", KEYS[5]) ~= event_count + 1 then
            redis.call("DEL", KEYS[5])
            redis.call(
                "HSET",
                KEYS[5],
                "next_sequence",
                event_count
            )
        end
    end
    redis.call("EXPIRE", KEYS[3], ARGV[9])
    redis.call("EXPIRE", KEYS[4], ARGV[9])
    redis.call("EXPIRE", KEYS[5], ARGV[9])
else
    redis.call("DEL", KEYS[4])
    redis.call("DEL", KEYS[5])
end
return {1, encoded}
"""


class RunningTransitionResult(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"


class TerminalTransitionResult(StrEnum):
    APPLIED = "applied"
    ALREADY_TERMINAL = "already_terminal"


class _EnqueueIntentHeartbeat:
    def __init__(
        self,
        refresh: Callable[[], bool],
        interval: float,
        stop_timeout: float = DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._refresh = refresh
        self._interval = interval
        self._stop_timeout = stop_timeout
        self._stopped = Event()
        self._failure: BaseException | None = None
        self._thread = Thread(target=self._run, daemon=True)
        self._started = False

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        if self._started:
            try:
                stop_heartbeat_thread(
                    self._stopped,
                    self._thread,
                    timeout=self._stop_timeout,
                    heartbeat_name="Enqueue intent heartbeat",
                )
            except HeartbeatStopTimeoutError as exc:
                self._failure = exc
                raise
        else:
            self._stopped.set()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                if not self._refresh():
                    raise TaskConflictError("Task enqueue reservation expired")
            except BaseException as exc:
                self._failure = exc
                return


class RedisEventSubscriber:
    def __init__(
        self,
        store: RedisLogStore,
        task_id: str,
        generation: str | None,
        next_sequence: int,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._generation = generation
        self._next_sequence = next_sequence

    def get(self, timeout: float) -> LogEventResponse:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            generation, event, next_sequence = self._store._event_at_cursor(
                self._task_id,
                self._generation,
                self._next_sequence,
            )
            self._generation = generation
            self._next_sequence = next_sequence
            if event is not None:
                return event
            sleep(min(0.05, max(0.0, deadline - monotonic())))
        raise Empty


class RedisLogStore:
    def __init__(
        self,
        redis_connection: Redis,
        *,
        retention_seconds: int,
    ) -> None:
        self._redis = redis_connection
        self._retention_seconds = retention_seconds
        self._append_event_script = self._redis.register_script(
            _APPEND_EVENT_SCRIPT
        )
        self._read_event_log_script = self._redis.register_script(
            _READ_EVENT_LOG_SCRIPT
        )
        self._read_event_at_cursor_script = self._redis.register_script(
            _READ_EVENT_AT_CURSOR_SCRIPT
        )

    def append(
        self,
        *,
        task_id: str,
        level: LogLevel,
        phase: LogPhase,
        message: str,
        details: dict[str, object] | None = None,
    ) -> LogEventResponse:
        event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=level,
            phase=phase,
            message=message,
            details=dict(details or {}),
        )
        event_json = event.model_dump_json()
        try:
            self._append_event_script(
                keys=[
                    _event_key(task_id),
                    _event_generation_key(task_id),
                    _event_index_key(task_id),
                ],
                args=[
                    event_json,
                    self._retention_seconds,
                    uuid4().hex,
                ],
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            self._append_without_lua(task_id, event_json)
        return event

    def history(self, task_id: str) -> list[LogEventResponse]:
        _generation, events, _next_sequence = self._snapshot(task_id)
        return events

    def history_and_subscribe(
        self,
        task_id: str,
    ) -> tuple[list[LogEventResponse], RedisEventSubscriber]:
        generation, history, next_sequence = self._snapshot(task_id)
        return history, RedisEventSubscriber(
            self,
            task_id,
            generation,
            next_sequence,
        )

    def unsubscribe(
        self,
        task_id: str,
        subscriber: RedisEventSubscriber,
    ) -> None:
        del task_id, subscriber

    def _snapshot(
        self,
        task_id: str,
    ) -> tuple[str | None, list[LogEventResponse], int]:
        try:
            generation, values, next_sequence = self._read_event_log_script(
                keys=[
                    _event_key(task_id),
                    _event_generation_key(task_id),
                    _event_index_key(task_id),
                ],
                args=[uuid4().hex, self._retention_seconds],
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            generation, values, next_sequence = self._snapshot_without_lua(
                task_id
            )
        generation_text = _redis_text(generation) or None
        events = [
            LogEventResponse.model_validate_json(value) for value in values
        ]
        return generation_text, events, int(next_sequence)

    def _event_at_cursor(
        self,
        task_id: str,
        expected_generation: str | None,
        expected_sequence: int,
    ) -> tuple[str | None, LogEventResponse | None, int]:
        try:
            status, generation, value, next_sequence = (
                self._read_event_at_cursor_script(
                    keys=[
                        _event_key(task_id),
                        _event_generation_key(task_id),
                        _event_index_key(task_id),
                    ],
                    args=[
                        expected_generation or "",
                        expected_sequence,
                    ],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            status, generation, value, next_sequence = (
                self._event_at_cursor_without_lua(
                    task_id,
                    expected_generation,
                    expected_sequence,
                )
            )
        if _redis_text(status) == "unavailable":
            raise EventCursorUnavailable(
                "Task event cursor state is unavailable"
            )
        generation_text = _redis_text(generation) or None
        try:
            event_json = value.decode() if isinstance(value, bytes) else value
            event = (
                LogEventResponse.model_validate_json(event_json)
                if event_json is not None
                else None
            )
        except ValueError as exc:
            raise EventCursorUnavailable(
                "Task cursor event is unavailable"
            ) from exc
        return generation_text, event, int(next_sequence)

    def _append_without_lua(self, task_id: str, event_json: str) -> None:
        event_key = _event_key(task_id)
        generation_key = _event_generation_key(task_id)
        event_index_key = _event_index_key(task_id)
        candidate = uuid4().hex
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(
                        event_key,
                        generation_key,
                        event_index_key,
                    )
                    generation = pipeline.get(generation_key)
                    event_log_exists = bool(pipeline.exists(event_key))
                    event_count = int(pipeline.llen(event_key))
                    indexed_sequence = pipeline.hget(
                        event_index_key,
                        "next_sequence",
                    )
                    reset_index = (
                        not event_log_exists
                        or generation is None
                        or not _event_index_is_consistent(
                            indexed_sequence,
                            int(pipeline.hlen(event_index_key)),
                            event_count,
                        )
                    )
                    sequence = event_count + 1
                    pipeline.multi()
                    if not event_log_exists or generation is None:
                        pipeline.set(generation_key, candidate)
                    if reset_index:
                        pipeline.delete(event_index_key)
                        pipeline.hset(
                            event_index_key,
                            "next_sequence",
                            event_count,
                        )
                    pipeline.hincrby(
                        event_index_key,
                        "next_sequence",
                        1,
                    )
                    pipeline.hset(event_index_key, str(sequence), event_json)
                    pipeline.rpush(event_key, event_json)
                    pipeline.expire(event_key, self._retention_seconds)
                    pipeline.expire(generation_key, self._retention_seconds)
                    pipeline.expire(
                        event_index_key,
                        self._retention_seconds,
                    )
                    pipeline.execute()
                    return
                except WatchError:
                    continue

    def _event_at_cursor_without_lua(
        self,
        task_id: str,
        expected_generation: str | None,
        expected_sequence: int,
    ) -> tuple[str, bytes | str | None, bytes | str | None, int]:
        event_key = _event_key(task_id)
        generation_key = _event_generation_key(task_id)
        event_index_key = _event_index_key(task_id)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(
                        event_key,
                        generation_key,
                        event_index_key,
                    )
                    generation = pipeline.get(generation_key)
                    indexed_sequence_value = pipeline.hget(
                        event_index_key,
                        "next_sequence",
                    )
                    if generation is None:
                        pipeline.unwatch()
                        status = (
                            "unavailable"
                            if expected_generation is not None
                            or indexed_sequence_value is not None
                            else "empty"
                        )
                        return status, None, None, expected_sequence
                    generation_text = _redis_text(generation)
                    if (
                        expected_generation is not None
                        and generation_text != expected_generation
                    ):
                        pipeline.unwatch()
                        return (
                            "unavailable",
                            generation,
                            None,
                            expected_sequence,
                        )
                    try:
                        indexed_sequence = int(indexed_sequence_value)
                    except (TypeError, ValueError):
                        pipeline.unwatch()
                        return (
                            "unavailable",
                            generation,
                            None,
                            expected_sequence,
                        )
                    if (
                        indexed_sequence < 0
                        or expected_sequence < 1
                        or expected_sequence > indexed_sequence + 1
                    ):
                        pipeline.unwatch()
                        return (
                            "unavailable",
                            generation,
                            None,
                            expected_sequence,
                        )
                    indexed_event = (
                        pipeline.hget(event_index_key, str(expected_sequence))
                        if expected_sequence <= indexed_sequence
                        else None
                    )
                    if (
                        indexed_event is None
                        and expected_sequence <= indexed_sequence
                    ):
                        pipeline.unwatch()
                        return (
                            "unavailable",
                            generation,
                            None,
                            expected_sequence,
                        )
                    pipeline.multi()
                    pipeline.execute()
                    next_sequence = expected_sequence + (
                        indexed_event is not None
                    )
                    return "ok", generation, indexed_event, next_sequence
                except WatchError:
                    continue

    def _snapshot_without_lua(
        self,
        task_id: str,
    ) -> tuple[bytes | str | None, list[bytes | str], int]:
        event_key = _event_key(task_id)
        generation_key = _event_generation_key(task_id)
        event_index_key = _event_index_key(task_id)
        candidate = uuid4().hex
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(
                        event_key,
                        generation_key,
                        event_index_key,
                    )
                    if not pipeline.exists(event_key):
                        pipeline.unwatch()
                        return None, [], 1
                    generation = pipeline.get(generation_key)
                    values = pipeline.lrange(event_key, 0, -1)
                    event_count = len(values)
                    indexed_sequence = pipeline.hget(
                        event_index_key,
                        "next_sequence",
                    )
                    index_is_consistent = _event_index_is_consistent(
                        indexed_sequence,
                        int(pipeline.hlen(event_index_key)),
                        event_count,
                    )
                    if index_is_consistent:
                        indexed_values = pipeline.hmget(
                            event_index_key,
                            [
                                str(sequence)
                                for sequence in range(1, event_count + 1)
                            ],
                        )
                        index_is_consistent = indexed_values == values
                    ttl_ms = pipeline.pttl(event_key)
                    pipeline.multi()
                    if generation is None:
                        generation = candidate
                        pipeline.set(generation_key, generation)
                    if not index_is_consistent:
                        pipeline.delete(event_index_key)
                        pipeline.hset(
                            event_index_key,
                            mapping={
                                "next_sequence": event_count,
                                **{
                                    str(sequence): value
                                    for sequence, value in enumerate(
                                        values,
                                        start=1,
                                    )
                                },
                            },
                        )
                    if ttl_ms > 0:
                        for key in (
                            event_key,
                            generation_key,
                            event_index_key,
                        ):
                            pipeline.pexpire(key, ttl_ms)
                    else:
                        for key in (
                            event_key,
                            generation_key,
                            event_index_key,
                        ):
                            pipeline.expire(
                                key,
                                self._retention_seconds,
                            )
                    pipeline.execute()
                    return generation, values, event_count + 1
                except WatchError:
                    continue


class RedisAssetReadPin:
    def __init__(
        self,
        manager: RedisTaskManager,
        session_id: str,
        asset_identities: tuple[AssetIdentity, ...],
        token: str,
    ) -> None:
        self._manager = manager
        self.session_id = session_id
        self.asset_identities = asset_identities
        self.token = token

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return _asset_ids(self.asset_identities)

    def refresh(self) -> None:
        if not self._manager.refresh_asset_read_pin(
            self.session_id,
            self.asset_identities,
            self.token,
        ):
            raise TaskConflictError("Asset read pin expired")

    def release(self) -> bool:
        return self._manager.release_asset_read_pin(
            self.session_id,
            self.asset_identities,
            self.token,
        )


class RedisTaskManager:
    def __init__(
        self,
        *,
        redis_connection: Redis,
        queue: Queue | None = None,
        data_dir: Path,
        max_queued_tasks: int,
        retention_seconds: int,
        task_timeout_seconds: int = 2 * 60 * 60,
        resource_coordinator: RedisResourceCoordinator | None = None,
        enqueue_intent_ttl: float = _ENQUEUE_INTENT_TTL_SECONDS,
        enqueue_intent_heartbeat_interval: float = (
            _ENQUEUE_INTENT_HEARTBEAT_SECONDS
        ),
        asset_read_pin_ttl: float = _ASSET_READ_PIN_TTL_SECONDS,
        asset_read_pin_heartbeat_interval: float = (
            _ASSET_READ_PIN_HEARTBEAT_SECONDS
        ),
        heartbeat_stop_timeout_seconds: float = (
            DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS
        ),
    ) -> None:
        if enqueue_intent_ttl <= 0:
            raise ValueError("enqueue_intent_ttl must be positive")
        if enqueue_intent_heartbeat_interval <= 0:
            raise ValueError(
                "enqueue_intent_heartbeat_interval must be positive"
            )
        if asset_read_pin_ttl <= 0:
            raise ValueError("asset_read_pin_ttl must be positive")
        if asset_read_pin_heartbeat_interval <= 0:
            raise ValueError(
                "asset_read_pin_heartbeat_interval must be positive"
            )
        if heartbeat_stop_timeout_seconds <= 0:
            raise ValueError("heartbeat_stop_timeout_seconds must be positive")
        if task_timeout_seconds <= 0:
            raise ValueError("task_timeout_seconds must be positive")
        self._redis = redis_connection
        self.queue = queue or Queue(_QUEUE_NAME, connection=redis_connection)
        self.data_dir = data_dir
        self.max_queued_tasks = max_queued_tasks
        self.retention_seconds = retention_seconds
        self.task_timeout_seconds = task_timeout_seconds
        self.resource_coordinator = resource_coordinator
        self.live_task_ttl = max(retention_seconds * 2, 24 * 60 * 60)
        self.enqueue_intent_heartbeat_interval = (
            enqueue_intent_heartbeat_interval
        )
        self.asset_read_pin_heartbeat_interval = (
            asset_read_pin_heartbeat_interval
        )
        self.heartbeat_stop_timeout_seconds = heartbeat_stop_timeout_seconds
        self._enqueue_intent_ttl_ms = max(
            1,
            round(
                max(
                    enqueue_intent_ttl,
                    enqueue_intent_heartbeat_interval * 2,
                )
                * 1000
            ),
        )
        self._asset_read_pin_ttl_ms = max(
            1,
            round(
                max(
                    asset_read_pin_ttl,
                    asset_read_pin_heartbeat_interval * 2,
                )
                * 1000
            ),
        )
        self._allow_request_script = self._redis.register_script(
            _ALLOW_REQUEST_SCRIPT
        )
        self._reserve_enqueue_script = self._redis.register_script(
            _RESERVE_ENQUEUE_SCRIPT
        )
        self._rollback_enqueue_script = self._redis.register_script(
            _ROLLBACK_ENQUEUE_SCRIPT
        )
        self._refresh_enqueue_owner_script = self._redis.register_script(
            _REFRESH_ENQUEUE_OWNER_SCRIPT
        )
        self._claim_expired_enqueue_intent_script = (
            self._redis.register_script(
                _CLAIM_EXPIRED_ENQUEUE_INTENT_SCRIPT
            )
        )
        self._mark_running_script = self._redis.register_script(
            _MARK_RUNNING_SCRIPT
        )
        self._terminal_transition_script = self._redis.register_script(
            _TERMINAL_TRANSITION_SCRIPT
        )
        self._release_task_pin_script = self._redis.register_script(
            _RELEASE_TASK_PIN_SCRIPT
        )
        self._claim_asset_deletion_script = self._redis.register_script(
            _CLAIM_ASSET_DELETION_SCRIPT
        )
        self._release_asset_deletion_script = self._redis.register_script(
            _RELEASE_ASSET_DELETION_SCRIPT
        )
        self._acquire_asset_read_pin_script = self._redis.register_script(
            _ACQUIRE_ASSET_READ_PIN_SCRIPT
        )
        self._refresh_asset_read_pin_script = self._redis.register_script(
            _REFRESH_ASSET_READ_PIN_SCRIPT
        )
        self._release_asset_read_pin_script = self._redis.register_script(
            _RELEASE_ASSET_READ_PIN_SCRIPT
        )
        self.log_store = RedisLogStore(
            redis_connection,
            retention_seconds=retention_seconds,
        )

    def create_video_frames_task(
        self,
        request: VideoFramesTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
        asset_ids: tuple[str, ...] = (),
        asset_identities: tuple[AssetIdentity, ...] = (),
        asset_read_pin_token: str | None = None,
    ) -> TaskRecord:
        options = video_frame_options_list_from_request(request)
        return self._enqueue(
            TaskKind.VIDEO_FRAMES,
            request,
            job_kind="video_frames",
            owner_id=owner_id,
            input_path=_joined_input_names(options),
            commit=commit,
            asset_ids=asset_ids,
            asset_identities=asset_identities,
            asset_read_pin_token=asset_read_pin_token,
        )

    def create_media_resize_task(
        self,
        request: MediaResizeTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
        asset_ids: tuple[str, ...] = (),
        asset_identities: tuple[AssetIdentity, ...] = (),
        asset_read_pin_token: str | None = None,
    ) -> TaskRecord:
        options = media_resize_options_list_from_request(request)
        return self._enqueue(
            TaskKind.MEDIA_RESIZE,
            request,
            job_kind="media_resize",
            owner_id=owner_id,
            input_path="\n".join(option.input_path.name for option in options),
            commit=commit,
            asset_ids=asset_ids,
            asset_identities=asset_identities,
            asset_read_pin_token=asset_read_pin_token,
        )

    def create_chroma_keyer_task(
        self,
        request: ChromaKeyerTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
        asset_ids: tuple[str, ...] = (),
        asset_identities: tuple[AssetIdentity, ...] = (),
        asset_read_pin_token: str | None = None,
    ) -> TaskRecord:
        options, _model, _params = chroma_keyer_options_from_request(request)
        output_path = options.output_path or default_output_path(options.input_path)
        validate_output_path(options.input_path, output_path)
        return self._enqueue(
            TaskKind.CHROMA_KEYER,
            request,
            job_kind="chroma_keyer",
            owner_id=owner_id,
            input_path=options.input_path.name,
            commit=commit,
            asset_ids=asset_ids,
            asset_identities=asset_identities,
            asset_read_pin_token=asset_read_pin_token,
        )

    def create_chroma_video_keyer_task(
        self,
        request: ChromaVideoKeyerTaskRequest,
        *,
        owner_id: str | None = None,
        commit: Callable[[Callable[[], Job]], Job] | None = None,
        asset_ids: tuple[str, ...] = (),
        asset_identities: tuple[AssetIdentity, ...] = (),
        asset_read_pin_token: str | None = None,
    ) -> TaskRecord:
        options, _model, _params = chroma_video_keyer_options_from_request(request)
        output_root = options.output_root or options.input_path.parent
        if not output_root.exists() or not output_root.is_dir():
            raise KeyerError("Output root must exist and be a directory")
        output_path = output_root / f"{options.input_path.stem}_keyed_frames"
        if output_path.exists():
            raise KeyerError("Final output directory already exists")
        return self._enqueue(
            TaskKind.CHROMA_KEYER,
            request,
            job_kind="chroma_video_keyer",
            owner_id=owner_id,
            input_path=options.input_path.name,
            commit=commit,
            asset_ids=asset_ids,
            asset_identities=asset_identities,
            asset_read_pin_token=asset_read_pin_token,
        )

    def get_task(
        self,
        task_id: str,
        *,
        owner_id: str | None = None,
    ) -> TaskRecord | None:
        value = self._redis.get(_task_key(task_id))
        if value is None:
            return None
        task = _decode_task(value)
        if owner_id is not None and task.owner_id != owner_id:
            return None
        return task

    def mark_running(
        self,
        task_id: str,
    ) -> tuple[TaskRecord, RunningTransitionResult]:
        started_at = datetime.now(UTC)
        event = LogEventResponse(
            task_id=task_id,
            timestamp=started_at,
            level=LogLevel.PROGRESS,
            phase=LogPhase.RUNNING,
            message="Task is running",
            details={},
        )
        try:
            result = self._mark_running_script(
                keys=[
                    _task_key(task_id),
                    _queue_reservations_key(),
                    _event_key(task_id),
                    _event_generation_key(task_id),
                    _event_index_key(task_id),
                ],
                args=[
                    task_id,
                    started_at.isoformat(),
                    self.live_task_ttl,
                    event.model_dump_json(),
                    uuid4().hex,
                ],
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            return self._mark_running_without_lua(
                task_id,
                started_at=started_at,
                event=event,
            )
        code = int(result[0])
        if code == 0:
            raise KeyError(task_id)
        transition = (
            RunningTransitionResult.APPLIED
            if code == 1
            else RunningTransitionResult.REJECTED
        )
        return _decode_task(result[1]), transition

    def mark_finished(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None = None,
        error: str | None = None,
        result_path: str | None = None,
    ) -> TaskRecord:
        task, _transitioned = self._transition_finished(
            task_id,
            status=status,
            output_path=output_path,
            error=error,
            result_path=result_path,
        )
        return task

    def record_heartbeat(self, task_id: str) -> None:
        key = _task_resources_key(task_id)
        pipeline = self._redis.pipeline()
        pipeline.hset(key, "heartbeat_at", time())
        pipeline.expire(key, self.live_task_ttl)
        pipeline.execute()

    def reconcile_tasks(self, *, grace_seconds: float = 60) -> int:
        now = datetime.now(UTC)
        started_ids = set(
            StartedJobRegistry(
                self.queue.name,
                connection=self._redis,
                queue=self.queue,
            ).get_job_ids()
        )
        failed_ids = set(
            FailedJobRegistry(
                self.queue.name,
                connection=self._redis,
                queue=self.queue,
            ).get_job_ids()
        )
        reconciled = self._reconcile_asset_read_pins()
        for task in self._tasks_requiring_failure_cleanup():
            message = task.error or "Task failed"
            if self._finalize_external_failure(task.task_id, message):
                reconciled += 1
        for task_id in self._enqueue_intent_task_ids():
            if self._reconcile_enqueue_intent(task_id):
                reconciled += 1
        for task in self._active_tasks():
            if task.status is TaskStatus.QUEUED:
                if self._enqueue_intent_exists(task.task_id):
                    continue
                job = self.queue.fetch_job(task.task_id)
                status = (
                    job.get_status(refresh=True) if job is not None else None
                )
                stale = (now - task.created_at).total_seconds() > grace_seconds
                abandoned = job is None or status in {
                    JobStatus.FAILED,
                    JobStatus.STOPPED,
                    JobStatus.CANCELED,
                    JobStatus.FINISHED,
                }
                if not stale or not abandoned:
                    continue
                message = (
                    "Task finished without publishing a terminal result"
                    if status is JobStatus.FINISHED
                    else "Queued task was abandoned before execution"
                )
            else:
                job = self.queue.fetch_job(task.task_id)
                status = (
                    job.get_status(refresh=True) if job is not None else None
                )
                if status is JobStatus.STARTED or task.task_id in started_ids:
                    continue
                if not self._heartbeat_is_stale(task, now, grace_seconds):
                    continue
                terminal_evidence = (
                    job is None
                    or task.task_id in failed_ids
                    or status
                    in {
                        JobStatus.FAILED,
                        JobStatus.STOPPED,
                        JobStatus.CANCELED,
                        JobStatus.FINISHED,
                    }
                )
                if not terminal_evidence:
                    continue
                message = (
                    "Task finished without publishing a terminal result"
                    if status is JobStatus.FINISHED
                    else "Running task was abandoned by its worker"
                )
            if self._finalize_external_failure(task.task_id, message):
                reconciled += 1
        reconciled += self._reconcile_stale_task_pins(started_ids)
        return reconciled

    def active_owner_ids(self) -> set[str]:
        owners: set[str] = set()
        for key in self._redis.scan_iter(match="dt:task:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            if _is_task_event_key(key_text):
                continue
            value = self._redis.get(key)
            if value is None:
                continue
            task = _decode_task(value)
            if (
                task.owner_id is not None
                and task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
            ):
                owners.add(task.owner_id)
        return owners

    def reconcile_queue_slots(self) -> int:
        for value in self._redis.lrange(_queue_reservations_key(), 0, -1):
            task_id = value.decode() if isinstance(value, bytes) else value
            task = self.get_task(task_id)
            intent_exists = self._enqueue_intent_exists(task_id)
            if task is None:
                if intent_exists:
                    continue
                self._release_queue_slot(task_id)
                continue
            stale_missing_job = (
                task.status is TaskStatus.QUEUED
                and (datetime.now(UTC) - task.created_at).total_seconds() > 60
                and self.queue.fetch_job(task_id) is None
                and not intent_exists
            )
            if task.status is not TaskStatus.QUEUED or stale_missing_job:
                self._release_queue_slot(task_id)
                if stale_missing_job:
                    self.mark_finished(
                        task_id,
                        status=TaskStatus.FAILED,
                        error="Queued task was abandoned before execution",
                    )
        return self._redis.llen(_queue_reservations_key())

    def allow_request(self, bucket: str, *, limit: int, window: int) -> bool:
        window_id = int(time() // window)
        key = f"dt:rate:{bucket}:{window_id}"
        try:
            count = int(
                self._allow_request_script(
                    keys=[key],
                    args=[window + 1],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            count = self._allow_request_without_lua(key, window)
        return count <= limit

    def _allow_request_without_lua(self, key: str, window: int) -> int:
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(key)
                    current = pipeline.get(key)
                    count = int(current or 0) + 1
                    pipeline.multi()
                    pipeline.incr(key)
                    if current is None:
                        pipeline.expire(key, window + 1)
                    pipeline.execute()
                    return count
                except WatchError:
                    continue

    def acquire_asset_read_pin(
        self,
        session_id: str,
        asset_identities: tuple[str | AssetIdentity, ...],
    ) -> RedisAssetReadPin:
        session_id = _validate_session_id(session_id)
        identities = _normalize_asset_identities(asset_identities)
        if not identities:
            raise ValueError("Asset read pin requires at least one asset")
        asset_ids = _asset_ids(identities)
        token = uuid4().hex
        identities_json = _asset_identities_json(identities)
        keys = [
            _asset_read_pin_key(token),
            *(
                _asset_delete_key(session_id, asset_id)
                for asset_id in asset_ids
            ),
            *(
                _asset_readers_key(session_id, asset_id)
                for asset_id in asset_ids
            ),
        ]
        try:
            try:
                result = int(
                    self._acquire_asset_read_pin_script(
                        keys=keys,
                        args=[
                            token,
                            session_id,
                            self._asset_read_pin_ttl_ms,
                            identities_json,
                            len(asset_ids),
                        ],
                    )
                )
            except ResponseError as error_response:
                if "unknown command" not in str(error_response).lower():
                    raise
                result = self._acquire_asset_read_pin_without_lua(
                    session_id,
                    identities,
                    token,
                )
        except RedisError as acquire_error:
            try:
                acquired = self._asset_read_pin_matches(
                    session_id,
                    identities,
                    token,
                )
            except RedisError as confirmation_error:
                raise acquire_error from confirmation_error
            if not acquired:
                raise acquire_error
            result = 0
        if result == 1:
            raise TaskConflictError("Asset deletion is already in progress")
        return RedisAssetReadPin(self, session_id, identities, token)

    def refresh_asset_read_pin(
        self,
        session_id: str,
        asset_identities: tuple[str | AssetIdentity, ...],
        token: str,
    ) -> bool:
        session_id = _validate_session_id(session_id)
        identities = _normalize_asset_identities(asset_identities)
        _validate_token(token)
        asset_ids = _asset_ids(identities)
        keys = [
            _asset_read_pin_key(token),
            *(
                _asset_readers_key(session_id, asset_id)
                for asset_id in asset_ids
            ),
        ]
        try:
            try:
                return bool(
                    self._refresh_asset_read_pin_script(
                        keys=keys,
                        args=[
                            token,
                            session_id,
                            self._asset_read_pin_ttl_ms,
                            _asset_identities_json(identities),
                        ],
                    )
                )
            except ResponseError as error_response:
                if "unknown command" not in str(error_response).lower():
                    raise
                return self._refresh_asset_read_pin_without_lua(
                    session_id,
                    identities,
                    token,
                )
        except RedisError as refresh_error:
            try:
                return self._asset_read_pin_matches(
                    session_id,
                    identities,
                    token,
                )
            except RedisError as confirmation_error:
                raise refresh_error from confirmation_error

    def release_asset_read_pin(
        self,
        session_id: str,
        asset_identities: tuple[str | AssetIdentity, ...],
        token: str,
    ) -> bool:
        session_id = _validate_session_id(session_id)
        identities = _normalize_asset_identities(asset_identities)
        _validate_token(token)
        asset_ids = _asset_ids(identities)
        keys = [
            _asset_read_pin_key(token),
            *(
                _asset_readers_key(session_id, asset_id)
                for asset_id in asset_ids
            ),
        ]
        try:
            try:
                result = int(
                    self._release_asset_read_pin_script(
                        keys=keys,
                        args=[
                            token,
                            session_id,
                            _asset_identities_json(identities),
                        ],
                    )
                )
            except ResponseError as error_response:
                if "unknown command" not in str(error_response).lower():
                    raise
                result = self._release_asset_read_pin_without_lua(
                    session_id,
                    identities,
                    token,
                )
        except RedisError as release_error:
            try:
                if not self._asset_read_pin_matches(
                    session_id,
                    identities,
                    token,
                ):
                    return True
            except RedisError:
                pass
            raise release_error
        if result == 2:
            raise TaskConflictError("Asset read pin ownership is invalid")
        return result == 1

    def _acquire_asset_read_pin_without_lua(
        self,
        session_id: str,
        asset_identities: tuple[AssetIdentity, ...],
        token: str,
    ) -> int:
        asset_ids = _asset_ids(asset_identities)
        claim_keys = [
            _asset_delete_key(session_id, asset_id) for asset_id in asset_ids
        ]
        reader_keys = [
            _asset_readers_key(session_id, asset_id) for asset_id in asset_ids
        ]
        pin_key = _asset_read_pin_key(token)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(pin_key, *claim_keys, *reader_keys)
                    now_ms = round(time() * 1000)
                    if any(
                        pipeline.zcount(key, now_ms, "+inf")
                        for key in claim_keys
                    ):
                        pipeline.unwatch()
                        return 1
                    expires_at = now_ms + self._asset_read_pin_ttl_ms
                    pipeline.multi()
                    for key in claim_keys:
                        pipeline.zremrangebyscore(key, "-inf", now_ms)
                    pipeline.hset(
                        pin_key,
                        mapping={
                            "token": token,
                            "session_id": session_id,
                            "asset_identities": _asset_identities_json(
                                asset_identities
                            ),
                        },
                    )
                    pipeline.pexpire(pin_key, self._asset_read_pin_ttl_ms)
                    for key in reader_keys:
                        pipeline.zremrangebyscore(key, "-inf", now_ms)
                        pipeline.zadd(key, {token: expires_at})
                    pipeline.execute()
                    return 0
                except WatchError:
                    continue

    def _refresh_asset_read_pin_without_lua(
        self,
        session_id: str,
        asset_identities: tuple[AssetIdentity, ...],
        token: str,
    ) -> bool:
        asset_ids = _asset_ids(asset_identities)
        pin_key = _asset_read_pin_key(token)
        reader_keys = [
            _asset_readers_key(session_id, asset_id) for asset_id in asset_ids
        ]
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(pin_key, *reader_keys)
                    if not _asset_read_pin_metadata_matches(
                        pipeline.hgetall(pin_key),
                        session_id,
                        asset_identities,
                        token,
                    ):
                        pipeline.unwatch()
                        return False
                    now_ms = round(time() * 1000)
                    expires_at = now_ms + self._asset_read_pin_ttl_ms
                    pipeline.multi()
                    pipeline.pexpire(pin_key, self._asset_read_pin_ttl_ms)
                    for key in reader_keys:
                        pipeline.zremrangebyscore(key, "-inf", now_ms)
                        pipeline.zadd(key, {token: expires_at})
                    pipeline.execute()
                    return True
                except WatchError:
                    continue

    def _release_asset_read_pin_without_lua(
        self,
        session_id: str,
        asset_identities: tuple[AssetIdentity, ...],
        token: str,
    ) -> int:
        asset_ids = _asset_ids(asset_identities)
        pin_key = _asset_read_pin_key(token)
        reader_keys = [
            _asset_readers_key(session_id, asset_id) for asset_id in asset_ids
        ]
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(pin_key, *reader_keys)
                    metadata = pipeline.hgetall(pin_key)
                    if not metadata:
                        pipeline.unwatch()
                        return 0
                    if not _asset_read_pin_metadata_matches(
                        metadata,
                        session_id,
                        asset_identities,
                        token,
                    ):
                        pipeline.unwatch()
                        return 2
                    pipeline.multi()
                    for key in reader_keys:
                        pipeline.zrem(key, token)
                    pipeline.delete(pin_key)
                    pipeline.execute()
                    return 1
                except WatchError:
                    continue

    def _asset_read_pin_matches(
        self,
        session_id: str,
        asset_identities: tuple[AssetIdentity, ...],
        token: str,
    ) -> bool:
        return _asset_read_pin_metadata_matches(
            self._redis.hgetall(_asset_read_pin_key(token)),
            session_id,
            asset_identities,
            token,
        )

    def claim_asset_deletion(self, session_id: str, asset_id: str) -> str:
        session_id = _validate_session_id(session_id)
        asset_id = _validate_asset_id(asset_id)
        token = uuid4().hex
        asset_tasks_key = _asset_tasks_key(session_id, asset_id)
        claim_key = _asset_delete_key(session_id, asset_id)
        readers_key = _asset_readers_key(session_id, asset_id)
        try:
            result = int(
                self._claim_asset_deletion_script(
                    keys=[asset_tasks_key, claim_key, readers_key],
                    args=[token, _ASSET_DELETE_TTL_MS],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            while True:
                with self._redis.pipeline() as pipeline:
                    try:
                        pipeline.watch(asset_tasks_key, claim_key, readers_key)
                        now_ms = round(time() * 1000)
                        if pipeline.scard(asset_tasks_key) or pipeline.zcount(
                            readers_key,
                            now_ms,
                            "+inf",
                        ):
                            pipeline.unwatch()
                            result = 1
                            break
                        pipeline.multi()
                        pipeline.zremrangebyscore(claim_key, "-inf", now_ms)
                        pipeline.zremrangebyscore(readers_key, "-inf", now_ms)
                        pipeline.zadd(
                            claim_key,
                            {token: now_ms + _ASSET_DELETE_TTL_MS},
                        )
                        pipeline.pexpire(claim_key, _ASSET_DELETE_TTL_MS)
                        pipeline.execute()
                        result = 0
                        break
                    except WatchError:
                        continue
        if result == 1:
            raise TaskConflictError("Asset is referenced by an active task")
        return token

    def release_asset_deletion(
        self,
        session_id: str,
        asset_id: str,
        token: str,
    ) -> bool:
        session_id = _validate_session_id(session_id)
        asset_id = _validate_asset_id(asset_id)
        claim_key = _asset_delete_key(session_id, asset_id)
        try:
            return bool(
                self._release_asset_deletion_script(
                    keys=[claim_key],
                    args=[token],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(claim_key)
                    claimed = pipeline.zscore(claim_key, token) is not None
                    claim_count = pipeline.zcard(claim_key)
                    pipeline.multi()
                    pipeline.zrem(claim_key, token)
                    if claimed and claim_count == 1:
                        pipeline.delete(claim_key)
                    results = pipeline.execute()
                    return bool(results[0])
                except WatchError:
                    continue

    def _enqueue(
        self,
        kind: TaskKind,
        request,
        *,
        job_kind: str,
        owner_id: str | None,
        input_path: str,
        commit: Callable[[Callable[[], Job]], Job] | None,
        asset_ids: tuple[str, ...],
        asset_identities: tuple[AssetIdentity, ...],
        asset_read_pin_token: str | None,
    ) -> TaskRecord:
        if asset_ids and asset_identities:
            raise ValueError(
                "Use either asset_ids or asset_identities, not both"
            )
        identities = _normalize_asset_identities(
            asset_identities or asset_ids
        )
        if identities and owner_id is None:
            raise ValueError("Hosted asset references require a task owner")
        if asset_read_pin_token is not None:
            _validate_token(asset_read_pin_token)
            if not identities:
                raise ValueError("Asset read pin requires hosted asset references")
        task_id = str(uuid4())
        task = TaskRecord(
            task_id=task_id,
            kind=kind,
            status=TaskStatus.QUEUED,
            created_at=datetime.now(UTC),
            started_at=None,
            finished_at=None,
            input_path=input_path,
            output_path=None,
            error=None,
            owner_id=owner_id,
            asset_identities=identities,
            has_canonical_asset_identities=True,
        )
        owner_token = uuid4().hex
        self._reserve_enqueue_intent(
            task,
            owner_token,
            identities,
            asset_read_pin_token,
        )
        heartbeat = _EnqueueIntentHeartbeat(
            lambda: self._refresh_enqueue_intent(task.task_id, owner_token),
            self.enqueue_intent_heartbeat_interval,
            self.heartbeat_stop_timeout_seconds,
        )
        heartbeat.start()
        try:
            self._save_task(task)
            self._save_work(
                task.task_id,
                job_kind,
                request.model_dump(mode="json"),
            )
            self.log_store.append(
                task_id=task.task_id,
                level=LogLevel.INFO,
                phase=LogPhase.CREATED,
                message=f"Created {kind.value} task",
                details={"input_path": input_path},
            )
            job = self.queue.create_job(
                "designtoolkit.web.worker.run_task",
                args=(
                    task.task_id,
                    job_kind,
                    request.model_dump(mode="json"),
                ),
                job_id=task.task_id,
                timeout=self.task_timeout_seconds,
                result_ttl=self.retention_seconds,
                failure_ttl=self.retention_seconds,
                meta={"owner_id": owner_id},
                on_failure=Callback(handle_job_failure),
                on_stopped=Callback(handle_job_stopped),
            )
        except Exception:
            heartbeat.stop()
            self._rollback_enqueue(
                task.task_id,
                owner_id=owner_id,
                owner_token=owner_token,
                asset_identities=task.asset_identities,
            )
            raise
        except BaseException:
            heartbeat.stop()
            raise

        def enqueue_job() -> Job:
            heartbeat.stop()
            heartbeat.raise_if_failed()
            if not self._refresh_enqueue_intent(task.task_id, owner_token):
                raise TaskConflictError("Task enqueue reservation expired")
            return self._commit_enqueue_job(
                job,
                task_id=task.task_id,
                owner_token=owner_token,
            )

        try:
            if commit is None:
                enqueue_job()
            else:
                commit(enqueue_job)
        except TaskConflictError:
            heartbeat.stop()
            self._rollback_enqueue(
                task.task_id,
                owner_id=owner_id,
                owner_token=owner_token,
                asset_identities=task.asset_identities,
            )
            raise
        except Exception as enqueue_error:
            heartbeat.stop()
            try:
                enqueue_committed = self._enqueue_has_authoritative_evidence(
                    task.task_id
                )
            except Exception as recovery_error:
                enqueue_error.add_note(
                    "Enqueue recovery could not confirm whether the RQ job committed"
                )
                enqueue_error.add_note(
                    f"Enqueue recovery failed with: {recovery_error}"
                )
                try:
                    self._mark_enqueue_uncertain(task.task_id, owner_token)
                except Exception as intent_error:
                    enqueue_error.add_note(
                        f"Enqueue intent could not be persisted: {intent_error}"
                    )
                    raise enqueue_error from intent_error
                return task
            if enqueue_committed:
                self._clear_enqueue_intent(task.task_id, owner_token)
                return task
            self._rollback_enqueue(
                task.task_id,
                owner_id=owner_id,
                owner_token=owner_token,
                asset_identities=task.asset_identities,
            )
            raise
        except BaseException:
            heartbeat.stop()
            raise
        heartbeat.stop()
        heartbeat.raise_if_failed()
        return task

    def _reserve_enqueue_intent(
        self,
        task: TaskRecord,
        owner_token: str,
        asset_identities: tuple[AssetIdentity, ...],
        asset_read_pin_token: str | None,
    ) -> None:
        owner_id = task.owner_id or ""
        session_state_key = (
            _session_state_key(owner_id)
            if owner_id
            else _enqueue_intent_key(task.task_id)
        )
        session_task_pins_key = (
            _session_task_pins_key(owner_id)
            if owner_id
            else _enqueue_intent_key(task.task_id)
        )
        try:
            result = int(
                self._reserve_enqueue_script(
                    keys=[
                        _queue_reservations_key(),
                        _enqueue_intent_key(task.task_id),
                        _enqueue_owner_key(task.task_id),
                        session_state_key,
                        session_task_pins_key,
                        _task_pin_key(task.task_id),
                        _asset_read_pin_key(asset_read_pin_token)
                        if asset_read_pin_token is not None
                        else _enqueue_intent_key(task.task_id),
                    ],
                    args=[
                        task.task_id,
                        self.max_queued_tasks,
                        self.live_task_ttl,
                        owner_id,
                        task.created_at.timestamp(),
                        _CLEANUP_PREFIX,
                        owner_token,
                        self._enqueue_intent_ttl_ms,
                        _asset_identities_json(asset_identities),
                        _asset_ids_json(asset_identities),
                        _ASSET_TASK_PREFIX,
                        _ASSET_DELETE_PREFIX,
                        asset_read_pin_token or "",
                        _ASSET_READERS_PREFIX,
                    ],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            result = self._reserve_enqueue_intent_without_lua(
                task,
                owner_token,
                asset_identities,
                asset_read_pin_token,
            )
        if result == 1:
            raise TaskConflictError("The task queue is full")
        if result == 2:
            raise SessionDeletingError("Session is being deleted")
        if result == 3:
            raise TaskConflictError("Asset deletion is already in progress")
        if result == 4:
            raise TaskConflictError("Asset read pin expired")

    def _reserve_enqueue_intent_without_lua(
        self,
        task: TaskRecord,
        owner_token: str,
        asset_identities: tuple[AssetIdentity, ...],
        asset_read_pin_token: str | None,
    ) -> int:
        owner_id = task.owner_id or ""
        asset_ids = _asset_ids(asset_identities)
        state_key = (
            _session_state_key(owner_id)
            if owner_id
            else _enqueue_intent_key(task.task_id)
        )
        asset_claim_keys = [
            _asset_delete_key(owner_id, asset_id) for asset_id in asset_ids
        ]
        asset_reader_keys = [
            _asset_readers_key(owner_id, asset_id) for asset_id in asset_ids
        ]
        read_pin_key = (
            _asset_read_pin_key(asset_read_pin_token)
            if asset_read_pin_token is not None
            else _enqueue_intent_key(task.task_id)
        )
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(
                        _queue_reservations_key(),
                        state_key,
                        *asset_claim_keys,
                        *asset_reader_keys,
                        read_pin_key,
                    )
                    deleting = pipeline.hget(state_key, "deleting")
                    if owner_id and deleting is not None:
                        deleting_text = _redis_text(deleting)
                        if self._redis.exists(f"{_CLEANUP_PREFIX}{deleting_text}"):
                            pipeline.unwatch()
                            return 2
                    if (
                        int(pipeline.llen(_queue_reservations_key()))
                        >= self.max_queued_tasks
                    ):
                        pipeline.unwatch()
                        return 1
                    now_ms = round(time() * 1000)
                    if any(
                        pipeline.zcount(key, now_ms, "+inf")
                        for key in asset_claim_keys
                    ):
                        pipeline.unwatch()
                        return 3
                    if asset_read_pin_token is not None:
                        read_pin_metadata = pipeline.hgetall(read_pin_key)
                        if not _asset_read_pin_metadata_matches(
                            read_pin_metadata,
                            owner_id,
                            asset_identities,
                            asset_read_pin_token,
                        ):
                            pipeline.unwatch()
                            return 4
                    pipeline.multi()
                    for key in asset_claim_keys:
                        pipeline.zremrangebyscore(key, "-inf", now_ms)
                    if owner_id and deleting is not None:
                        pipeline.hdel(state_key, "deleting")
                    pipeline.rpush(_queue_reservations_key(), task.task_id)
                    pipeline.hset(
                        _enqueue_intent_key(task.task_id),
                        mapping=self._enqueue_intent_mapping(
                            task,
                            owner_token,
                        ),
                    )
                    pipeline.expire(
                        _enqueue_intent_key(task.task_id),
                        self.live_task_ttl,
                    )
                    pipeline.psetex(
                        _enqueue_owner_key(task.task_id),
                        self._enqueue_intent_ttl_ms,
                        owner_token,
                    )
                    if owner_id:
                        pipeline.hset(
                            _task_pin_key(task.task_id),
                            mapping={
                                "task_id": task.task_id,
                                "session_id": owner_id,
                                "asset_ids": _asset_ids_json(
                                    asset_identities
                                ),
                                "asset_identities": _asset_identities_json(
                                    asset_identities
                                ),
                            },
                        )
                        pipeline.sadd(
                            _session_task_pins_key(owner_id),
                            task.task_id,
                        )
                        for asset_id in asset_ids:
                            pipeline.sadd(
                                _asset_tasks_key(owner_id, asset_id),
                                task.task_id,
                            )
                    if asset_read_pin_token is not None:
                        for asset_reader_key in asset_reader_keys:
                            pipeline.zrem(
                                asset_reader_key,
                                asset_read_pin_token,
                            )
                        pipeline.delete(read_pin_key)
                    pipeline.execute()
                    return 0
                except WatchError:
                    continue

    @staticmethod
    def _enqueue_intent_mapping(
        task: TaskRecord,
        owner_token: str,
    ) -> dict[str, str]:
        return {
            "task_id": task.task_id,
            "job_id": task.task_id,
            "slot_reserved": "1",
            "state": "creating",
            "owner_id": task.owner_id or "",
            "created_at": str(task.created_at.timestamp()),
            "owner_token": owner_token,
        }

    def _rollback_enqueue(
        self,
        task_id: str,
        *,
        owner_id: str | None,
        owner_token: str,
        asset_identities: tuple[AssetIdentity, ...] | None = None,
    ) -> bool:
        owner = owner_id or ""
        if asset_identities is None:
            task = self.get_task(task_id)
            asset_identities = (
                task.asset_identities
                if (
                    task is not None
                    and task.has_canonical_asset_identities
                )
                else None
            )
        identities_json = (
            _asset_identities_json(asset_identities)
            if asset_identities is not None
            else ""
        )
        asset_ids_json = (
            _asset_ids_json(asset_identities)
            if asset_identities is not None
            else ""
        )
        session_task_pins_key = (
            _session_task_pins_key(owner)
            if owner
            else _enqueue_intent_key(task_id)
        )
        try:
            result = int(
                self._rollback_enqueue_script(
                    keys=[
                        _queue_reservations_key(),
                        _task_key(task_id),
                        _event_key(task_id),
                        _task_work_key(task_id),
                        _enqueue_intent_key(task_id),
                        _task_pin_key(task_id),
                        session_task_pins_key,
                        _enqueue_owner_key(task_id),
                        _event_generation_key(task_id),
                        _event_index_key(task_id),
                    ],
                    args=[
                        task_id,
                        owner,
                        owner_token,
                        _ASSET_TASK_PREFIX,
                        identities_json,
                        asset_ids_json,
                    ],
                )
            )
            if result in {0, 4} and self._repair_rolled_back_enqueue_indexes(
                task_id
            ):
                return True
            return result == 1
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    watched_keys = [
                        _enqueue_intent_key(task_id),
                        _event_key(task_id),
                        _event_generation_key(task_id),
                        _event_index_key(task_id),
                    ]
                    if owner:
                        watched_keys.extend(
                            (
                                _task_pin_key(task_id),
                                _session_task_pins_key(owner),
                            )
                        )
                    pipeline.watch(*watched_keys)
                    intent_metadata = pipeline.hgetall(
                        _enqueue_intent_key(task_id)
                    )
                    if (
                        _redis_hash_text(intent_metadata, "owner_token")
                        != owner_token
                    ):
                        pipeline.unwatch()
                        return self._repair_rolled_back_enqueue_indexes(
                            task_id
                        )
                    context_matches = (
                        _redis_hash_text(intent_metadata, "task_id") == task_id
                        and _redis_hash_text(intent_metadata, "owner_id")
                        == owner
                    )
                    pin_metadata = pipeline.hgetall(_task_pin_key(task_id))
                    release_pin = bool(owner) and (
                        _redis_hash_text(pin_metadata, "session_id") == owner
                        and _redis_hash_text(pin_metadata, "task_id") == task_id
                    )
                    repair_pin = not context_matches or (
                        bool(owner)
                        and (
                            not release_pin
                            or not _task_pin_metadata_matches(
                                pin_metadata,
                                task_id=task_id,
                                session_id=owner,
                                asset_identities=asset_identities,
                            )
                        )
                    )
                    asset_ids = (
                        ()
                        if not release_pin or repair_pin
                        else _asset_ids_from_pin_metadata(pin_metadata)
                    )
                    asset_task_keys = [
                        _asset_tasks_key(owner, asset_id)
                        for asset_id in asset_ids
                    ]
                    if asset_task_keys:
                        pipeline.watch(*asset_task_keys)
                    pipeline.multi()
                    pipeline.lrem(_queue_reservations_key(), 0, task_id)
                    pipeline.delete(
                        _task_key(task_id),
                        _event_key(task_id),
                        _event_index_key(task_id),
                        _event_generation_key(task_id),
                        _task_work_key(task_id),
                        _enqueue_intent_key(task_id),
                        _enqueue_owner_key(task_id),
                    )
                    if release_pin and not repair_pin:
                        for asset_task_key in asset_task_keys:
                            pipeline.srem(asset_task_key, task_id)
                        pipeline.delete(_task_pin_key(task_id))
                        pipeline.srem(_session_task_pins_key(owner), task_id)
                    pipeline.execute()
                    if repair_pin:
                        return self._repair_rolled_back_enqueue_indexes(
                            task_id
                        )
                    return True
                except WatchError:
                    continue

    def _repair_rolled_back_enqueue_indexes(
        self,
        task_id: str,
    ) -> bool:
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.get(_task_key(task_id))
        pipeline.hgetall(_enqueue_intent_key(task_id))
        task_value, intent_metadata = pipeline.execute()
        if task_value is not None or intent_metadata:
            return False
        return self._repair_task_reverse_indexes(task_id)

    def _clear_enqueue_intent(
        self,
        task_id: str,
        owner_token: str,
    ) -> None:
        try:
            while True:
                with self._redis.pipeline() as pipeline:
                    try:
                        pipeline.watch(_enqueue_intent_key(task_id))
                        token = pipeline.hget(
                            _enqueue_intent_key(task_id),
                            "owner_token",
                        )
                        if _redis_text(token) != owner_token:
                            pipeline.unwatch()
                            return
                        pipeline.multi()
                        pipeline.delete(
                            _enqueue_intent_key(task_id),
                            _enqueue_owner_key(task_id),
                        )
                        pipeline.execute()
                        return
                    except WatchError:
                        continue
        except Exception:
            # The durable intent remains safe for reconciliation after ack loss.
            pass

    def _refresh_enqueue_intent(
        self,
        task_id: str,
        owner_token: str,
    ) -> bool:
        try:
            return bool(
                self._refresh_enqueue_owner_script(
                    keys=[
                        _enqueue_intent_key(task_id),
                        _enqueue_owner_key(task_id),
                    ],
                    args=[owner_token, self._enqueue_intent_ttl_ms],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(
                        _enqueue_intent_key(task_id),
                        _enqueue_owner_key(task_id),
                    )
                    intent_token = pipeline.hget(
                        _enqueue_intent_key(task_id),
                        "owner_token",
                    )
                    intent_state = pipeline.hget(
                        _enqueue_intent_key(task_id),
                        "state",
                    )
                    lease_token = pipeline.get(_enqueue_owner_key(task_id))
                    if (
                        _redis_text(intent_token) != owner_token
                        or _redis_text(intent_state)
                        not in {"creating", "uncertain"}
                        or _redis_text(lease_token) != owner_token
                    ):
                        pipeline.unwatch()
                        return False
                    pipeline.multi()
                    pipeline.pexpire(
                        _enqueue_owner_key(task_id),
                        self._enqueue_intent_ttl_ms,
                    )
                    pipeline.execute()
                    return True
                except WatchError:
                    continue

    def _commit_enqueue_job(
        self,
        job: Job,
        *,
        task_id: str,
        owner_token: str,
    ) -> Job:
        intent_key = _enqueue_intent_key(task_id)
        owner_key = _enqueue_owner_key(task_id)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(intent_key, owner_key)
                    intent_token = pipeline.hget(intent_key, "owner_token")
                    intent_state = pipeline.hget(intent_key, "state")
                    lease_token = pipeline.get(owner_key)
                    if (
                        _redis_text(intent_token) != owner_token
                        or _redis_text(intent_state)
                        not in {"creating", "uncertain"}
                        or _redis_text(lease_token) != owner_token
                    ):
                        pipeline.unwatch()
                        raise TaskConflictError(
                            "Task enqueue reservation expired"
                        )
                    pipeline.multi()
                    self.queue.enqueue_job(job, pipeline=pipeline)
                    pipeline.delete(intent_key, owner_key)
                    pipeline.execute()
                    return job
                except WatchError:
                    continue

    def _enqueue_has_authoritative_evidence(self, task_id: str) -> bool:
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.exists(Job.key_for(task_id))
        pipeline.get(_task_key(task_id))
        job_exists, task_value = pipeline.execute()
        if job_exists:
            return True
        if task_value is None:
            return False
        return _decode_task(task_value).status is not TaskStatus.QUEUED

    def _mark_enqueue_uncertain(
        self,
        task_id: str,
        owner_token: str,
    ) -> None:
        intent_key = _enqueue_intent_key(task_id)
        owner_key = _enqueue_owner_key(task_id)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(intent_key, owner_key)
                    token = pipeline.hget(intent_key, "owner_token")
                    if _redis_text(token) != owner_token:
                        pipeline.unwatch()
                        return
                    pipeline.multi()
                    pipeline.hset(intent_key, "state", "uncertain")
                    pipeline.expire(intent_key, self.live_task_ttl)
                    pipeline.delete(owner_key)
                    pipeline.execute()
                    return
                except WatchError:
                    continue

    def _enqueue_intent_task_ids(self) -> list[str]:
        task_ids: list[str] = []
        for key in self._redis.scan_iter(match="dt:enqueue-intent:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            task_id = key_text.removeprefix("dt:enqueue-intent:")
            metadata = self._redis.hgetall(key)
            state = metadata.get(b"state", metadata.get("state"))
            stored_task_id = metadata.get(b"task_id", metadata.get("task_id"))
            job_id = metadata.get(b"job_id", metadata.get("job_id"))
            if (
                _redis_text(state)
                in {"creating", "uncertain", "reconciling"}
                and _redis_text(stored_task_id) == task_id
                and _redis_text(job_id) == task_id
            ):
                task_ids.append(task_id)
        return task_ids

    def _enqueue_intent_exists(self, task_id: str) -> bool:
        metadata = self._redis.hgetall(_enqueue_intent_key(task_id))
        return (
            _redis_text(metadata.get(b"task_id", metadata.get("task_id")))
            == task_id
            and _redis_text(metadata.get(b"job_id", metadata.get("job_id")))
            == task_id
            and _redis_text(metadata.get(b"state", metadata.get("state")))
            in {"creating", "uncertain", "reconciling"}
            and _redis_text(
                metadata.get(b"owner_token", metadata.get("owner_token"))
            )
            is not None
        )

    def _claim_expired_enqueue_intent(
        self,
        task_id: str,
        reconciler_token: str,
    ) -> int:
        intent_key = _enqueue_intent_key(task_id)
        owner_key = _enqueue_owner_key(task_id)
        try:
            return int(
                self._claim_expired_enqueue_intent_script(
                    keys=[intent_key, owner_key],
                    args=[
                        task_id,
                        reconciler_token,
                        self.live_task_ttl,
                        self._enqueue_intent_ttl_ms,
                    ],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(intent_key, owner_key)
                    metadata = pipeline.hgetall(intent_key)
                    state = _redis_text(
                        metadata.get(b"state", metadata.get("state"))
                    )
                    owner_token = _redis_text(
                        metadata.get(
                            b"owner_token",
                            metadata.get("owner_token"),
                        )
                    )
                    if (
                        _redis_text(
                            metadata.get(
                                b"task_id",
                                metadata.get("task_id"),
                            )
                        )
                        != task_id
                        or _redis_text(
                            metadata.get(b"job_id", metadata.get("job_id"))
                        )
                        != task_id
                        or state
                        not in {"creating", "uncertain", "reconciling"}
                    ):
                        pipeline.unwatch()
                        return 0
                    if (
                        owner_token is not None
                        and _redis_text(pipeline.get(owner_key)) == owner_token
                    ):
                        pipeline.unwatch()
                        return 1
                    pipeline.multi()
                    pipeline.hset(
                        intent_key,
                        mapping={
                            "state": "reconciling",
                            "owner_token": reconciler_token,
                        },
                    )
                    pipeline.expire(intent_key, self.live_task_ttl)
                    pipeline.psetex(
                        owner_key,
                        self._enqueue_intent_ttl_ms,
                        reconciler_token,
                    )
                    pipeline.execute()
                    return 2
                except WatchError:
                    continue

    def _reconcile_enqueue_intent(
        self,
        task_id: str,
    ) -> bool:
        task_key = _task_key(task_id)
        intent_key = _enqueue_intent_key(task_id)
        owner_key = _enqueue_owner_key(task_id)
        job_key = Job.key_for(task_id)
        event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=LogLevel.ERROR,
            phase=LogPhase.FAILED,
            message=_UNCERTAIN_ENQUEUE_ERROR,
            details={},
        )
        reconciler_token = uuid4().hex
        if (
            self._claim_expired_enqueue_intent(
                task_id,
                reconciler_token,
            )
            != 2
        ):
            return False
        transitioned = False
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    event_key = _event_key(task_id)
                    generation_key = _event_generation_key(task_id)
                    event_index_key = _event_index_key(task_id)
                    pipeline.watch(
                        task_key,
                        intent_key,
                        owner_key,
                        job_key,
                        event_key,
                        generation_key,
                        event_index_key,
                    )
                    metadata = pipeline.hgetall(intent_key)
                    if not metadata:
                        pipeline.unwatch()
                        return False
                    state = _redis_text(
                        metadata.get(b"state", metadata.get("state"))
                    )
                    owner_id = _redis_text(
                        metadata.get(b"owner_id", metadata.get("owner_id"))
                    )
                    owner_token = _redis_text(
                        metadata.get(
                            b"owner_token",
                            metadata.get("owner_token"),
                        )
                    )
                    if (
                        state != "reconciling"
                        or owner_token != reconciler_token
                    ):
                        pipeline.unwatch()
                        return False
                    if pipeline.exists(job_key):
                        pipeline.multi()
                        pipeline.delete(intent_key, owner_key)
                        pipeline.execute()
                        return False
                    if owner_id:
                        pipeline.watch(
                            _task_pin_key(task_id),
                            _session_task_pins_key(owner_id),
                        )
                    value = pipeline.get(task_key)
                    if value is None:
                        pipeline.multi()
                        pipeline.lrem(_queue_reservations_key(), 0, task_id)
                        pipeline.delete(intent_key, owner_key)
                        pipeline.execute()
                        self.release_task_pin(task_id)
                        return True
                    task = _decode_task(value)
                    if task.status is not TaskStatus.QUEUED:
                        pipeline.multi()
                        pipeline.lrem(_queue_reservations_key(), 0, task_id)
                        pipeline.delete(intent_key, owner_key)
                        pipeline.execute()
                        if task.status in {
                            TaskStatus.SUCCEEDED,
                            TaskStatus.FAILED,
                        }:
                            self.release_task_pin(task_id)
                        if (
                            task.status is TaskStatus.FAILED
                            and self._cleanup_failed_task(task_id)
                        ):
                            return True
                        return False
                    task.status = TaskStatus.FAILED
                    task.error = _UNCERTAIN_ENQUEUE_ERROR
                    task.finished_at = event.timestamp
                    event_log_exists = bool(pipeline.exists(event_key))
                    generation = pipeline.get(generation_key)
                    event_count = int(pipeline.llen(event_key))
                    indexed_sequence = pipeline.hget(
                        event_index_key,
                        "next_sequence",
                    )
                    reset_index = (
                        not event_log_exists
                        or generation is None
                        or not _event_index_is_consistent(
                            indexed_sequence,
                            int(pipeline.hlen(event_index_key)),
                            event_count,
                        )
                    )
                    sequence = event_count + 1
                    pipeline.multi()
                    pipeline.set(
                        task_key,
                        _encode_task(task),
                        ex=self.retention_seconds,
                    )
                    pipeline.expire(
                        _task_work_key(task_id),
                        self.retention_seconds,
                    )
                    if not event_log_exists or generation is None:
                        pipeline.set(generation_key, uuid4().hex)
                    if reset_index:
                        pipeline.delete(event_index_key)
                        pipeline.hset(
                            event_index_key,
                            "next_sequence",
                            event_count,
                        )
                    pipeline.hincrby(
                        event_index_key,
                        "next_sequence",
                        1,
                    )
                    pipeline.hset(
                        event_index_key,
                        str(sequence),
                        event.model_dump_json(),
                    )
                    pipeline.rpush(event_key, event.model_dump_json())
                    pipeline.expire(event_key, self.retention_seconds)
                    pipeline.expire(generation_key, self.retention_seconds)
                    pipeline.expire(
                        event_index_key,
                        self.retention_seconds,
                    )
                    pipeline.lrem(_queue_reservations_key(), 0, task_id)
                    pipeline.delete(intent_key, owner_key)
                    pipeline.execute()
                    self.release_task_pin(task_id)
                    transitioned = True
                    break
                except WatchError:
                    continue
        cleaned = self._cleanup_failed_task(task_id)
        return transitioned or cleaned

    def _reconcile_asset_read_pins(self) -> int:
        now_ms = round(time() * 1000)
        reconciled = 0
        for key in self._redis.scan_iter(match=f"{_ASSET_READERS_PREFIX}*"):
            reconciled += int(
                self._redis.zremrangebyscore(key, "-inf", now_ms)
            )
            if not self._redis.zcard(key):
                self._redis.delete(key)
        return reconciled

    def _reconcile_stale_task_pins(self, started_ids: set[str]) -> int:
        coordinator = self.resource_coordinator
        reconciled = 0
        pin_keys: list[tuple[bytes | str, str]] = []
        reverse_index_keys: list[bytes | str] = []
        repair_members: set[bytes] = set()
        for key in self._redis.scan_iter(
            match="dt:resource:*",
            count=_CORRUPT_PIN_SCAN_COUNT,
        ):
            pin_suffix = _task_pin_key_suffix(key)
            if pin_suffix is not None:
                task_id = _valid_task_id_from_index_member(pin_suffix)
                if task_id is None:
                    reconciled += int(bool(self._redis.delete(key)))
                    if len(pin_suffix) <= _MAX_REVERSE_TASK_ID_BYTES:
                        repair_members.add(pin_suffix)
                    continue
                pin_keys.append((key, task_id))
                continue
            if _is_asset_tasks_key(key) or _is_session_task_pins_key(key):
                reverse_index_keys.append(key)

        for key, task_id in pin_keys:
            if self._enqueue_intent_exists(task_id):
                continue
            task = self.get_task(task_id)
            if task is not None and task.status in {
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
            }:
                continue
            if task is None:
                job = self.queue.fetch_job(task_id)
                status = job.get_status(refresh=True) if job is not None else None
                if status in {JobStatus.QUEUED, JobStatus.STARTED}:
                    continue
                if task_id in started_ids:
                    continue
            repair_pin = task is None
            if coordinator is not None:
                try:
                    coordinator.release_task_resources(
                        task_id,
                        expected_session_id=(
                            task.owner_id if task is not None else None
                        ),
                        expected_asset_identities=(
                            task.asset_identities
                            if (
                                task is not None
                                and task.has_canonical_asset_identities
                            )
                            else None
                        ),
                    )
                except CorruptTaskPinError:
                    repair_pin = True
            else:
                metadata = self._redis.hgetall(key)
                if (
                    task is None
                    or task.owner_id is None
                    or not _task_pin_metadata_matches(
                        metadata,
                        task_id=task_id,
                        session_id=task.owner_id,
                        asset_identities=(
                            task.asset_identities
                            if task.has_canonical_asset_identities
                            else None
                        ),
                    )
                ):
                    repair_pin = True
                else:
                    self.release_task_pin(task_id)
            if repair_pin:
                repair_members.add(task_id.encode("ascii"))
                self._redis.delete(key)
            if not self._redis.exists(key):
                reconciled += 1
        self._reconcile_reverse_indexes(
            reverse_index_keys,
            repair_members,
        )
        return reconciled

    def _reconcile_reverse_indexes(
        self,
        keys: list[bytes | str],
        repair_members: set[bytes],
    ) -> None:
        for key in keys:
            for member in self._redis.smembers(key):
                raw_member = (
                    member.encode("utf-8")
                    if isinstance(member, str)
                    else member
                )
                task_id = _valid_task_id_from_index_member(raw_member)
                if (
                    raw_member in repair_members
                    or task_id is None
                    or not self._redis.exists(_task_pin_key(task_id))
                ):
                    self._remove_task_from_index(raw_member, key)

    def release_task_pin(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return self._repair_task_reverse_indexes(task_id)
        pin_key = _task_pin_key(task_id)
        metadata = self._redis.hgetall(pin_key)
        if not metadata:
            if task.owner_id is None:
                return False
            return self._repair_task_reverse_indexes(task_id)
        session_id = _redis_hash_text(metadata, "session_id")
        if session_id is None or not _task_pin_metadata_matches(
            metadata,
            task_id=task_id,
            session_id=session_id,
            asset_identities=(
                task.asset_identities
                if task.has_canonical_asset_identities
                else None
            ),
        ):
            return self._repair_task_reverse_indexes(task_id)
        try:
            _validate_session_id(session_id)
        except ValueError:
            return self._repair_task_reverse_indexes(task_id)
        if task.owner_id != session_id:
            return self._repair_task_reverse_indexes(task_id)

        session_task_pins_key = _session_task_pins_key(session_id)
        try:
            result = int(
                self._release_task_pin_script(
                    keys=[
                        pin_key,
                        session_task_pins_key,
                    ],
                    args=[
                        session_id,
                        task_id,
                        _ASSET_TASK_PREFIX,
                        _asset_identities_json(task.asset_identities),
                        _asset_ids_json(task.asset_identities),
                    ],
                )
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            while True:
                with self._redis.pipeline() as pipeline:
                    try:
                        pipeline.watch(pin_key, session_task_pins_key)
                        metadata = pipeline.hgetall(pin_key)
                        if not metadata:
                            pipeline.unwatch()
                            result = 4
                            break
                        if (
                            not _task_pin_metadata_matches(
                                metadata,
                                task_id=task_id,
                                session_id=session_id,
                                asset_identities=(
                                    task.asset_identities
                                    if task.has_canonical_asset_identities
                                    else None
                                ),
                            )
                        ):
                            pipeline.unwatch()
                            result = 4
                            break
                        asset_task_keys = [
                            _asset_tasks_key(session_id, asset_id)
                            for asset_id in _asset_ids_from_pin_metadata(metadata)
                        ]
                        if asset_task_keys:
                            pipeline.watch(*asset_task_keys)
                        pipeline.multi()
                        for asset_task_key in asset_task_keys:
                            pipeline.srem(asset_task_key, task_id)
                        pipeline.delete(pin_key)
                        pipeline.srem(session_task_pins_key, task_id)
                        pipeline.execute()
                        result = 1
                        break
                    except WatchError:
                        continue
        if result in {0, 2, 3, 4}:
            return self._repair_task_reverse_indexes(task_id)
        return result == 1

    def _repair_task_reverse_indexes(self, task_id: str) -> bool:
        _validate_task_id(task_id)
        self._remove_task_from_index_scan(
            task_id,
            match=f"{_ASSET_TASK_PREFIX}*",
            validator=_is_asset_tasks_key,
        )
        self._remove_task_from_index_scan(
            task_id,
            match=(
                f"{_SESSION_TASK_PINS_PREFIX}*"
                f"{_SESSION_TASK_PINS_SUFFIX}"
            ),
            validator=_is_session_task_pins_key,
        )
        self._redis.delete(_task_pin_key(task_id))
        return True

    def _remove_task_from_index_scan(
        self,
        task_id: str,
        *,
        match: str,
        validator: Callable[[bytes | str], bool],
    ) -> None:
        for keys in _validated_index_key_batches(
            self._redis.scan_iter(
                match=match,
                count=_CORRUPT_PIN_SCAN_COUNT,
            ),
            validator,
        ):
            for key in keys:
                self._remove_task_from_index(task_id, key)

    def _remove_task_from_index(
        self,
        task_id: bytes | str,
        key: bytes | str,
    ) -> None:
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    pipeline.watch(key)
                    cardinality = pipeline.scard(key)
                    contains_task = pipeline.sismember(key, task_id)
                    pipeline.multi()
                    pipeline.srem(key, task_id)
                    if cardinality == 0 or (
                        cardinality == 1 and contains_task
                    ):
                        pipeline.delete(key)
                    pipeline.execute()
                    return
                except WatchError:
                    continue

    def _release_queue_slot(self, task_id: str) -> None:
        self._redis.lrem(_queue_reservations_key(), 0, task_id)

    def _required_task(self, task_id: str) -> TaskRecord:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _mark_running_without_lua(
        self,
        task_id: str,
        *,
        started_at: datetime,
        event: LogEventResponse,
    ) -> tuple[TaskRecord, RunningTransitionResult]:
        key = _task_key(task_id)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    event_key = _event_key(task_id)
                    generation_key = _event_generation_key(task_id)
                    event_index_key = _event_index_key(task_id)
                    pipeline.watch(
                        key,
                        event_key,
                        generation_key,
                        event_index_key,
                    )
                    value = pipeline.get(key)
                    if value is None:
                        raise KeyError(task_id)
                    task = _decode_task(value)
                    if task.status is not TaskStatus.QUEUED:
                        pipeline.unwatch()
                        return task, RunningTransitionResult.REJECTED
                    task.status = TaskStatus.RUNNING
                    task.started_at = started_at
                    event_log_exists = bool(pipeline.exists(event_key))
                    generation = pipeline.get(generation_key)
                    event_count = int(pipeline.llen(event_key))
                    indexed_sequence = pipeline.hget(
                        event_index_key,
                        "next_sequence",
                    )
                    reset_index = (
                        not event_log_exists
                        or generation is None
                        or not _event_index_is_consistent(
                            indexed_sequence,
                            int(pipeline.hlen(event_index_key)),
                            event_count,
                        )
                    )
                    sequence = event_count + 1
                    pipeline.multi()
                    pipeline.set(
                        key,
                        _encode_task(task),
                        ex=self.live_task_ttl,
                    )
                    pipeline.lrem(_queue_reservations_key(), 0, task_id)
                    if not event_log_exists or generation is None:
                        pipeline.set(generation_key, uuid4().hex)
                    if reset_index:
                        pipeline.delete(event_index_key)
                        pipeline.hset(
                            event_index_key,
                            "next_sequence",
                            event_count,
                        )
                    pipeline.hincrby(
                        event_index_key,
                        "next_sequence",
                        1,
                    )
                    pipeline.hset(
                        event_index_key,
                        str(sequence),
                        event.model_dump_json(),
                    )
                    pipeline.rpush(event_key, event.model_dump_json())
                    pipeline.expire(event_key, self.live_task_ttl)
                    pipeline.expire(generation_key, self.live_task_ttl)
                    pipeline.expire(event_index_key, self.live_task_ttl)
                    pipeline.execute()
                    return task, RunningTransitionResult.APPLIED
                except WatchError:
                    continue

    def _transition_finished(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None = None,
        error: str | None = None,
        result_path: str | None = None,
        terminal_event: LogEventResponse | None = None,
    ) -> tuple[TaskRecord, TerminalTransitionResult]:
        finished_at = datetime.now(UTC)
        event_json = (
            terminal_event.model_dump_json() if terminal_event is not None else ""
        )
        try:
            result = self._terminal_transition_script(
                keys=[
                    _task_key(task_id),
                    _task_work_key(task_id),
                    _event_key(task_id),
                    _event_generation_key(task_id),
                    _event_index_key(task_id),
                ],
                args=[
                    status.value,
                    "1" if output_path is not None else "0",
                    output_path or "",
                    "1" if error is not None else "0",
                    error or "",
                    "1" if result_path is not None else "0",
                    result_path or "",
                    finished_at.isoformat(),
                    self.retention_seconds,
                    "1" if terminal_event is not None else "0",
                    event_json,
                    uuid4().hex,
                ],
            )
        except ResponseError as error_response:
            if "unknown command" not in str(error_response).lower():
                raise
            return self._transition_finished_without_lua(
                task_id,
                status=status,
                output_path=output_path,
                error=error,
                result_path=result_path,
                terminal_event=terminal_event,
                finished_at=finished_at,
            )
        code = int(result[0])
        if code == 0:
            raise KeyError(task_id)
        task = _decode_task(result[1])
        transition = (
            TerminalTransitionResult.APPLIED
            if code == 1
            else TerminalTransitionResult.ALREADY_TERMINAL
        )
        self.release_task_pin(task_id)
        return task, transition

    def _transition_finished_without_lua(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output_path: str | None,
        error: str | None,
        result_path: str | None,
        terminal_event: LogEventResponse | None,
        finished_at: datetime,
    ) -> tuple[TaskRecord, TerminalTransitionResult]:
        key = _task_key(task_id)
        while True:
            with self._redis.pipeline() as pipeline:
                try:
                    event_key = _event_key(task_id)
                    generation_key = _event_generation_key(task_id)
                    event_index_key = _event_index_key(task_id)
                    pipeline.watch(
                        key,
                        event_key,
                        generation_key,
                        event_index_key,
                    )
                    value = pipeline.get(key)
                    if value is None:
                        raise KeyError(task_id)
                    task = _decode_task(value)
                    if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
                        pipeline.unwatch()
                        self.release_task_pin(task_id)
                        return task, TerminalTransitionResult.ALREADY_TERMINAL
                    task.status = status
                    if output_path is not None:
                        task.output_path = output_path
                    task.error = error
                    if result_path is not None:
                        task.result_path = result_path
                    task.finished_at = finished_at
                    event_log_exists = bool(pipeline.exists(event_key))
                    generation = pipeline.get(generation_key)
                    event_count = int(pipeline.llen(event_key))
                    indexed_sequence = pipeline.hget(
                        event_index_key,
                        "next_sequence",
                    )
                    index_is_consistent = _event_index_is_consistent(
                        indexed_sequence,
                        int(pipeline.hlen(event_index_key)),
                        event_count,
                    )
                    pipeline.multi()
                    pipeline.set(
                        key,
                        _encode_task(task),
                        ex=self.retention_seconds,
                    )
                    pipeline.expire(
                        _task_work_key(task_id),
                        self.retention_seconds,
                    )
                    if terminal_event is not None:
                        reset_index = (
                            not event_log_exists
                            or generation is None
                            or not index_is_consistent
                        )
                        sequence = event_count + 1
                        if not event_log_exists or generation is None:
                            pipeline.set(generation_key, uuid4().hex)
                        if reset_index:
                            pipeline.delete(event_index_key)
                            pipeline.hset(
                                event_index_key,
                                "next_sequence",
                                event_count,
                            )
                        pipeline.hincrby(
                            event_index_key,
                            "next_sequence",
                            1,
                        )
                        pipeline.hset(
                            event_index_key,
                            str(sequence),
                            terminal_event.model_dump_json(),
                        )
                        pipeline.rpush(
                            event_key,
                            terminal_event.model_dump_json(),
                        )
                        event_log_exists = True
                    if event_log_exists:
                        if generation is None and terminal_event is None:
                            pipeline.set(generation_key, uuid4().hex)
                        if (
                            not index_is_consistent
                            and terminal_event is None
                        ):
                            pipeline.delete(event_index_key)
                            pipeline.hset(
                                event_index_key,
                                "next_sequence",
                                event_count,
                            )
                        pipeline.expire(event_key, self.retention_seconds)
                        pipeline.expire(
                            generation_key,
                            self.retention_seconds,
                        )
                        pipeline.expire(
                            event_index_key,
                            self.retention_seconds,
                        )
                    else:
                        pipeline.delete(generation_key, event_index_key)
                    pipeline.execute()
                    self.release_task_pin(task_id)
                    return task, TerminalTransitionResult.APPLIED
                except WatchError:
                    continue

    def _finalize_external_failure(self, task_id: str, message: str) -> bool:
        terminal_event = LogEventResponse(
            task_id=task_id,
            timestamp=datetime.now(UTC),
            level=LogLevel.ERROR,
            phase=LogPhase.FAILED,
            message=message,
            details={},
        )
        try:
            task, transition = self._transition_finished(
                task_id,
                status=TaskStatus.FAILED,
                error=message,
                terminal_event=terminal_event,
            )
        except KeyError:
            return False
        if task.status is TaskStatus.SUCCEEDED:
            return False
        cleaned = self._cleanup_failed_task(task_id)
        return transition is TerminalTransitionResult.APPLIED or cleaned

    def _cleanup_failed_task(self, task_id: str) -> bool:
        cleanup_key = _failure_cleanup_key(task_id)
        if self._redis.exists(cleanup_key):
            return False
        task = self.get_task(task_id)
        self._release_queue_slot(task_id)
        from designtoolkit.web.worker import discard_failed_task_output

        try:
            discard_failed_task_output(task_id, self)
        finally:
            if self.resource_coordinator is not None:
                self.resource_coordinator.release_task_resources(
                    task_id,
                    expected_session_id=(
                        task.owner_id if task is not None else None
                    ),
                    expected_asset_identities=(
                        task.asset_identities
                        if (
                            task is not None
                            and task.has_canonical_asset_identities
                        )
                        else None
                    ),
                )
        self._redis.set(cleanup_key, "1", ex=self.retention_seconds)
        return True

    def _tasks_requiring_failure_cleanup(self) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        for key in self._redis.scan_iter(match="dt:task:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            if _is_task_event_key(key_text):
                continue
            value = self._redis.get(key)
            if value is None:
                continue
            task = _decode_task(value)
            if (
                task.status is TaskStatus.FAILED
                and not self._redis.exists(_failure_cleanup_key(task.task_id))
            ):
                tasks.append(task)
        return tasks

    def _active_tasks(self) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        for key in self._redis.scan_iter(match="dt:task:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            if _is_task_event_key(key_text):
                continue
            value = self._redis.get(key)
            if value is None:
                continue
            task = _decode_task(value)
            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                tasks.append(task)
        return tasks

    def _heartbeat_is_stale(
        self,
        task: TaskRecord,
        now: datetime,
        grace_seconds: float,
    ) -> bool:
        value = self._redis.hget(_task_resources_key(task.task_id), "heartbeat_at")
        heartbeat_at = float(value) if value is not None else None
        fallback = task.started_at or task.created_at
        last_seen = (
            datetime.fromtimestamp(heartbeat_at, UTC)
            if heartbeat_at is not None
            else fallback
        )
        return (now - last_seen).total_seconds() > grace_seconds

    def _save_task(self, task: TaskRecord) -> None:
        key = _task_key(task.task_id)
        value = _encode_task(task)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            self._redis.set(key, value, ex=self.retention_seconds)
        else:
            self._redis.set(key, value, ex=self.live_task_ttl)

    def _save_work(
        self,
        task_id: str,
        job_kind: str,
        payload: dict[str, object],
    ) -> None:
        value = json.dumps(
            {"job_kind": job_kind, "payload": payload},
            separators=(",", ":"),
        )
        self._redis.set(
            _task_work_key(task_id),
            value,
            ex=self.live_task_ttl,
        )

    def task_work(self, task_id: str) -> tuple[str, dict[str, object]] | None:
        value = self._redis.get(_task_work_key(task_id))
        if value is None:
            return None
        data = json.loads(value)
        return data["job_kind"], data["payload"]


def _joined_input_names(options: list[VideoFrameOptions]) -> str:
    return "\n".join(option.input_path.name for option in options)


def _validate_task_id(task_id: str) -> str:
    try:
        parsed = UUID(task_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Invalid task id") from exc
    if str(parsed) != task_id:
        raise ValueError("Invalid task id")
    return task_id


def _task_key(task_id: str) -> str:
    return f"dt:task:{task_id}"


def _event_key(task_id: str) -> str:
    return f"dt:task:{task_id}:events"


def _event_index_key(task_id: str) -> str:
    return f"dt:task:{task_id}:event-index"


def _is_task_event_key(key: str) -> bool:
    return key.endswith((":events", ":event-index"))


def _event_index_is_consistent(
    indexed_sequence: bytes | str | None,
    index_size: int,
    event_count: int,
) -> bool:
    try:
        return (
            indexed_sequence is not None
            and int(indexed_sequence) == event_count
            and index_size == event_count + 1
        )
    except ValueError:
        return False


def _event_generation_key(task_id: str) -> str:
    return f"dt:event-generation:{task_id}"


def _queue_reservations_key() -> str:
    return "dt:queue:reservations"


def _enqueue_intent_key(task_id: str) -> str:
    return f"dt:enqueue-intent:{task_id}"


def _enqueue_owner_key(task_id: str) -> str:
    return f"dt:enqueue-owner:{task_id}"


def _task_resources_key(task_id: str) -> str:
    return f"dt:resource:task:{task_id}"


def _task_work_key(task_id: str) -> str:
    return f"dt:task-work:{task_id}"


def _asset_delete_key(session_id: str, asset_id: str) -> str:
    return f"{_ASSET_DELETE_PREFIX}{session_id}:{asset_id}"


def _asset_read_pin_key(token: str) -> str:
    return f"{_ASSET_READ_PIN_PREFIX}{token}"


def _asset_readers_key(session_id: str, asset_id: str) -> str:
    return f"{_ASSET_READERS_PREFIX}{session_id}:{asset_id}"


def _normalize_asset_identities(
    values: tuple[str | AssetIdentity, ...],
) -> tuple[AssetIdentity, ...]:
    identities: list[AssetIdentity] = []
    generations_by_asset: dict[str, str | None] = {}
    for value in values:
        if isinstance(value, str):
            asset_id, generation_id = value, None
        elif (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], str)
            and (value[1] is None or isinstance(value[1], str))
        ):
            asset_id, generation_id = value
        else:
            raise ValueError("Invalid hosted asset identity")
        _validate_asset_id(asset_id)
        if generation_id is not None:
            _validate_token(generation_id)
        previous = generations_by_asset.get(asset_id, generation_id)
        if asset_id in generations_by_asset and previous != generation_id:
            raise ValueError("Conflicting hosted asset generations")
        if asset_id not in generations_by_asset:
            generations_by_asset[asset_id] = generation_id
            identities.append((asset_id, generation_id))
    return tuple(identities)


def _asset_ids(
    asset_identities: tuple[AssetIdentity, ...],
) -> tuple[str, ...]:
    return tuple(identity[0] for identity in asset_identities)


def _asset_identities_json(
    asset_identities: tuple[AssetIdentity, ...],
) -> str:
    return json.dumps(asset_identities, separators=(",", ":"))


def _asset_ids_json(
    asset_identities: tuple[AssetIdentity, ...],
) -> str:
    return json.dumps(_asset_ids(asset_identities), separators=(",", ":"))


def _task_pin_metadata_matches(
    metadata: dict[bytes | str, bytes | str],
    *,
    task_id: str,
    session_id: str,
    asset_identities: tuple[AssetIdentity, ...] | None,
) -> bool:
    if asset_identities is None:
        return False
    return (
        _redis_hash_text(metadata, "task_id") == task_id
        and _redis_hash_text(metadata, "session_id") == session_id
        and _redis_hash_text(metadata, "asset_identities")
        == _asset_identities_json(asset_identities)
        and _redis_hash_text(metadata, "asset_ids")
        == _asset_ids_json(asset_identities)
    )


def _asset_read_pin_metadata_matches(
    metadata: dict[bytes | str, bytes | str],
    session_id: str,
    asset_identities: tuple[AssetIdentity, ...],
    token: str,
) -> bool:
    return (
        _redis_hash_text(metadata, "token") == token
        and _redis_hash_text(metadata, "session_id") == session_id
        and _redis_hash_text(metadata, "asset_identities")
        == _asset_identities_json(asset_identities)
    )


def _asset_ids_from_pin_metadata(
    metadata: dict[bytes | str, bytes | str],
) -> tuple[str, ...]:
    identities_value = _redis_hash_text(metadata, "asset_identities")
    legacy_value = _redis_hash_text(metadata, "asset_ids")
    if identities_value is not None:
        try:
            decoded = json.loads(identities_value)
            if not isinstance(decoded, list):
                raise ValueError
            identities = tuple(
                (identity[0], identity[1])
                for identity in decoded
                if (
                    isinstance(identity, list)
                    and len(identity) == 2
                    and isinstance(identity[0], str)
                    and (
                        identity[1] is None
                        or isinstance(identity[1], str)
                    )
                )
            )
            if len(identities) != len(decoded):
                raise ValueError
            asset_ids = _asset_ids(_normalize_asset_identities(identities))
            if legacy_value is not None:
                legacy_asset_ids = _decode_legacy_asset_ids(legacy_value)
                if legacy_asset_ids != asset_ids:
                    raise ValueError
            return asset_ids
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskConflictError(
                "Task pin asset metadata is invalid"
            ) from exc

    if legacy_value is None:
        return ()
    try:
        return _decode_legacy_asset_ids(legacy_value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TaskConflictError("Task pin asset metadata is invalid") from exc


def _task_pin_asset_metadata_missing(
    metadata: dict[bytes | str, bytes | str],
) -> bool:
    return (
        _redis_hash_text(metadata, "asset_identities") is None
        and _redis_hash_text(metadata, "asset_ids") is None
    )


def _task_pin_asset_metadata_corrupt(
    metadata: dict[bytes | str, bytes | str],
) -> bool:
    if _task_pin_asset_metadata_missing(metadata):
        return True
    try:
        _asset_ids_from_pin_metadata(metadata)
    except TaskConflictError:
        return True
    return False


def _validated_index_key_batches(
    keys: Iterable[bytes | str],
    validator: Callable[[bytes | str], bool],
) -> Iterator[list[bytes | str]]:
    batch: list[bytes | str] = []
    for key in keys:
        if not validator(key):
            continue
        batch.append(key)
        if len(batch) == _CORRUPT_PIN_SCAN_COUNT:
            yield batch
            batch = []
    if batch:
        yield batch


def _task_pin_key_suffix(key: bytes | str) -> bytes | None:
    raw_key = key.encode("utf-8") if isinstance(key, str) else key
    prefix = _TASK_PIN_PREFIX.encode("ascii")
    if not raw_key.startswith(prefix):
        return None
    return raw_key[len(prefix) :]


def _valid_task_id_from_index_member(member: bytes | str) -> str | None:
    raw_member = member.encode("utf-8") if isinstance(member, str) else member
    if not raw_member or len(raw_member) > _MAX_REVERSE_TASK_ID_BYTES:
        return None
    try:
        task_id = raw_member.decode("ascii")
        return _validate_task_id(task_id)
    except (UnicodeDecodeError, ValueError):
        return None


def _is_asset_tasks_key(key: bytes | str) -> bool:
    key_text = _decode_redis_key(key)
    if key_text is None or not key_text.startswith(_ASSET_TASK_PREFIX):
        return False
    parts = key_text.removeprefix(_ASSET_TASK_PREFIX).split(":")
    if len(parts) != 2:
        return False
    try:
        _validate_session_id(parts[0])
        _validate_asset_id(parts[1])
    except ValueError:
        return False
    return True


def _is_session_task_pins_key(key: bytes | str) -> bool:
    key_text = _decode_redis_key(key)
    if (
        key_text is None
        or not key_text.startswith(_SESSION_TASK_PINS_PREFIX)
        or not key_text.endswith(_SESSION_TASK_PINS_SUFFIX)
    ):
        return False
    session_id = key_text[
        len(_SESSION_TASK_PINS_PREFIX) : -len(_SESSION_TASK_PINS_SUFFIX)
    ]
    try:
        _validate_session_id(session_id)
    except ValueError:
        return False
    return True


def _decode_redis_key(key: bytes | str) -> str | None:
    if isinstance(key, str):
        return key
    try:
        return key.decode("ascii")
    except UnicodeDecodeError:
        return None


def _decode_legacy_asset_ids(value: str) -> tuple[str, ...]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(
        isinstance(asset_id, str) for asset_id in decoded
    ):
        raise ValueError
    return _asset_ids(_normalize_asset_identities(tuple(decoded)))


def _redis_hash_text(
    metadata: dict[bytes | str, bytes | str],
    field: str,
) -> str | None:
    return _redis_text(metadata.get(field) or metadata.get(field.encode()))


def _failure_cleanup_key(task_id: str) -> str:
    return f"dt:failure-cleanup:{task_id}"


def _redis_text(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode()
    return value


def handle_job_failure(
    job: Job,
    connection: Redis,
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: object,
) -> None:
    del exc_type, traceback
    _handle_external_job_failure(job, connection, str(exc_value))


def handle_job_stopped(job: Job, connection: Redis) -> None:
    _handle_external_job_failure(job, connection, "Task was stopped")


def _handle_external_job_failure(
    job: Job,
    connection: Redis,
    message: str,
) -> None:
    manager = _callback_manager(connection)
    manager._finalize_external_failure(job.id, message)


def _callback_manager(connection: Redis) -> RedisTaskManager:
    from designtoolkit.web.hosted import HostedSettings
    from designtoolkit.web.resources import RedisResourceCoordinator

    settings = HostedSettings.from_env()
    resources = RedisResourceCoordinator(
        redis_connection=connection,
        max_upload_concurrency=settings.max_upload_concurrency,
        max_session_bytes=settings.max_session_bytes,
        min_free_bytes=settings.min_free_bytes,
        reservation_ttl=settings.reservation_ttl_seconds,
        lease_ttl=settings.lease_ttl_seconds,
        reservation_heartbeat_interval=settings.reservation_heartbeat_seconds,
        lease_heartbeat_interval=settings.lease_heartbeat_seconds,
        heartbeat_stop_timeout_seconds=settings.heartbeat_stop_timeout_seconds,
        release_marker_ttl=max(
            settings.retention_seconds * 2,
            settings.reservation_ttl_seconds,
            settings.lease_ttl_seconds,
        ),
    )
    return RedisTaskManager(
        redis_connection=connection,
        queue=Queue(_QUEUE_NAME, connection=connection),
        data_dir=settings.data_dir,
        max_queued_tasks=settings.max_queued_tasks,
        retention_seconds=settings.retention_seconds,
        task_timeout_seconds=settings.task_timeout_seconds,
        resource_coordinator=resources,
        heartbeat_stop_timeout_seconds=settings.heartbeat_stop_timeout_seconds,
    )


def _encode_task(task: TaskRecord) -> str:
    data: dict[str, Any] = {
        "task_id": task.task_id,
        "kind": task.kind.value,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "input_path": task.input_path,
        "output_path": task.output_path,
        "error": task.error,
        "owner_id": task.owner_id,
        "result_path": task.result_path,
    }
    if task.has_canonical_asset_identities:
        data["asset_identities"] = task.asset_identities
    return json.dumps(data, separators=(",", ":"))


def _decode_task(value: bytes | str) -> TaskRecord:
    data = json.loads(value)
    has_canonical_asset_identities = "asset_identities" in data
    return TaskRecord(
        task_id=data["task_id"],
        kind=TaskKind(data["kind"]),
        status=TaskStatus(data["status"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        started_at=(
            datetime.fromisoformat(data["started_at"])
            if data["started_at"]
            else None
        ),
        finished_at=(
            datetime.fromisoformat(data["finished_at"])
            if data["finished_at"]
            else None
        ),
        input_path=data["input_path"],
        output_path=data["output_path"],
        error=data["error"],
        owner_id=data["owner_id"],
        result_path=data["result_path"],
        asset_identities=tuple(
            (identity[0], identity[1])
            for identity in data.get("asset_identities", ())
        ),
        has_canonical_asset_identities=has_canonical_asset_identities,
    )
