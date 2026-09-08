from __future__ import annotations

from redis import exceptions as redis_exceptions

STORAGE_UNAVAILABLE_CODE = "storage_unavailable"
STORAGE_UNAVAILABLE_MESSAGE = "Redis storage is temporarily unavailable"
STORAGE_UNAVAILABLE_STATUS = 503


class EventCursorUnavailable(redis_exceptions.RedisError):
    """Raised when an event cursor cannot be read from consistent metadata."""


_UNAVAILABLE_ERROR_NAMES = (
    "BusyLoadingError",
    "ClusterDownError",
    "MasterDownError",
    "MaxConnectionsError",
    "OutOfMemoryError",
    "ReadOnlyError",
    "TryAgainError",
)
_UNAVAILABLE_ERROR_TYPES = (
    redis_exceptions.ConnectionError,
    redis_exceptions.TimeoutError,
    *(
        error_type
        for name in _UNAVAILABLE_ERROR_NAMES
        if (
            error_type := getattr(redis_exceptions, name, None)
        ) is not None
    ),
)
_UNAVAILABLE_RESPONSE_PREFIXES = (
    "CLUSTERDOWN",
    "LOADING",
    "MASTERDOWN",
    "MISCONF",
    "NOREPLICAS",
    "OOM",
    "READONLY",
    "TRYAGAIN",
)


def is_redis_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, EventCursorUnavailable):
        return True
    if isinstance(exc, _UNAVAILABLE_ERROR_TYPES):
        return True
    if not isinstance(exc, redis_exceptions.ResponseError):
        return False
    message = str(exc).lstrip().upper()
    pipeline_marker = " CAUSED ERROR: "
    if pipeline_marker in message:
        message = message.rpartition(pipeline_marker)[2].lstrip()
    response_code = message.partition(" ")[0].rstrip(":")
    return response_code in _UNAVAILABLE_RESPONSE_PREFIXES


def storage_unavailable_payload(
    message: str = STORAGE_UNAVAILABLE_MESSAGE,
) -> dict[str, dict[str, str]]:
    return {
        "error": {
            "code": STORAGE_UNAVAILABLE_CODE,
            "message": message,
        }
    }
