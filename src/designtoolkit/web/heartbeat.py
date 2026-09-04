from __future__ import annotations

import logging
from threading import Event, Thread

from designtoolkit.web.resources import ResourceUnavailableError

DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS = 11.0
_LOGGER = logging.getLogger(__name__)


class HeartbeatStopTimeoutError(ResourceUnavailableError):
    pass


def stop_heartbeat_thread(
    stopped: Event,
    thread: Thread,
    *,
    timeout: float,
    heartbeat_name: str,
) -> None:
    stopped.set()
    thread.join(timeout=timeout)
    if not thread.is_alive():
        return
    _LOGGER.error(
        "heartbeat_stop_timeout",
        extra={
            "heartbeat_name": heartbeat_name,
            "timeout_seconds": timeout,
        },
    )
    raise HeartbeatStopTimeoutError(
        f"{heartbeat_name} did not stop within {timeout:g} seconds"
    )
