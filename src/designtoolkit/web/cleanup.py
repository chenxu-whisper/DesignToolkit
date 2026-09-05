from __future__ import annotations

import logging
from argparse import ArgumentParser
from time import sleep

from redis import Redis
from rq import Queue

from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    redis_connection_options,
)
from designtoolkit.web.redis_tasks import RedisTaskManager
from designtoolkit.web.resources import RedisResourceCoordinator

_LOGGER = logging.getLogger(__name__)


def cleanup_once(settings: HostedSettings) -> int:
    redis_connection = Redis.from_url(
        settings.redis_url,
        **redis_connection_options(settings),
    )
    coordinator = RedisResourceCoordinator(
        redis_connection=redis_connection,
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
    manager = RedisTaskManager(
        redis_connection=redis_connection,
        queue=Queue("designtoolkit", connection=redis_connection),
        data_dir=settings.data_dir,
        max_queued_tasks=settings.max_queued_tasks,
        retention_seconds=settings.retention_seconds,
        task_timeout_seconds=settings.task_timeout_seconds,
        resource_coordinator=coordinator,
        heartbeat_stop_timeout_seconds=settings.heartbeat_stop_timeout_seconds,
    )
    storage = HostedStorage(
        settings.data_dir,
        max_upload_bytes=settings.max_upload_bytes,
    )
    manager.reconcile_tasks()
    manager.reconcile_queue_slots()
    coordinator.reconcile_reservations()
    return storage.cleanup_expired(
        retention_seconds=settings.retention_seconds,
        coordinator=coordinator,
    )


def run_cleanup_loop(settings: HostedSettings, *, interval: int) -> None:
    while True:
        try:
            cleanup_once(settings)
        except Exception:
            _LOGGER.exception("Hosted cleanup iteration failed")
        sleep(max(1, interval))


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    settings = HostedSettings.from_env()
    if args.once:
        cleanup_once(settings)
        return 0
    run_cleanup_loop(settings, interval=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
