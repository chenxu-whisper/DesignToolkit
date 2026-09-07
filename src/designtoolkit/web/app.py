from __future__ import annotations

from functools import partial
from pathlib import Path
from threading import BoundedSemaphore

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue

from designtoolkit.chroma_keyer.video_io import ImageioVideoDecoder
from designtoolkit.chroma_keyer.video_models import VideoDecoder
from designtoolkit.web.hosted import (
    HostedSettings,
    HostedStorage,
    is_valid_session_id,
    new_session_id,
    redis_connection_options,
)
from designtoolkit.web.logs import LogStore
from designtoolkit.web.native_picker import LocalPathPicker
from designtoolkit.web.redis_errors import (
    STORAGE_UNAVAILABLE_STATUS,
    is_redis_unavailable,
    storage_unavailable_payload,
)
from designtoolkit.web.redis_tasks import RedisTaskManager
from designtoolkit.web.resources import RedisResourceCoordinator
from designtoolkit.web.routes import ApiError, create_router, error_response
from designtoolkit.web.tasks import TaskManager


def _frontend_dist_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "web" / "frontend" / "dist"


def create_app(
    task_manager: TaskManager | RedisTaskManager | None = None,
    *,
    local_path_roots: list[Path] | None = None,
    local_path_picker: LocalPathPicker | None = None,
    video_decoder: VideoDecoder | None = None,
    hosted_settings: HostedSettings | None = None,
    hosted_storage: HostedStorage | None = None,
    resource_coordinator: RedisResourceCoordinator | None = None,
) -> FastAPI:
    settings = hosted_settings or HostedSettings.from_env()
    storage = hosted_storage
    redis_connection = None
    if settings.enabled and storage is None:
        storage = HostedStorage(
            settings.data_dir,
            max_upload_bytes=settings.max_upload_bytes,
            max_session_bytes=settings.max_session_bytes,
            min_free_bytes=settings.min_free_bytes,
        )
    if task_manager is not None:
        manager = task_manager
        if isinstance(manager, RedisTaskManager):
            redis_connection = manager.queue.connection
    elif settings.enabled:
        redis_connection = Redis.from_url(
            settings.redis_url,
            **redis_connection_options(settings),
        )
        manager = RedisTaskManager(
            redis_connection=redis_connection,
            queue=Queue("designtoolkit", connection=redis_connection),
            data_dir=settings.data_dir,
            max_queued_tasks=settings.max_queued_tasks,
            retention_seconds=settings.retention_seconds,
            task_timeout_seconds=settings.task_timeout_seconds,
            heartbeat_stop_timeout_seconds=settings.heartbeat_stop_timeout_seconds,
        )
    else:
        manager = TaskManager(log_store=LogStore())
    if settings.enabled and resource_coordinator is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if redis_connection is None:
            redis_connection = Redis.from_url(
                settings.redis_url,
                **redis_connection_options(settings),
            )
        resource_coordinator = RedisResourceCoordinator(
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
    log_store = manager.log_store
    decoder = video_decoder if video_decoder is not None else ImageioVideoDecoder()
    app = FastAPI(title="DesignToolkit Web GUI")
    stream_capacity = BoundedSemaphore(settings.max_stream_concurrency)

    if settings.enabled:

        @app.middleware("http")
        async def anonymous_session(request: Request, call_next):
            session_id = request.cookies.get("dt_session")
            new_session = not is_valid_session_id(session_id)
            if new_session:
                session_id = new_session_id()
            request.state.session_id = session_id
            forwarded_for = request.headers.get("x-forwarded-for", "")
            client_host = (
                forwarded_for.split(",", 1)[0].strip()
                or (request.client.host if request.client is not None else "unknown")
            )
            rate_limits = _rate_limits_for_request(request, settings)
            try:
                rate_limited = False
                if isinstance(manager, RedisTaskManager) and rate_limits:
                    rate_limited = await anyio.to_thread.run_sync(
                        partial(
                            _is_rate_limited,
                            manager,
                            client_host=client_host,
                            session_id=session_id,
                            rate_limits=rate_limits,
                        )
                    )
            except RedisError as exc:
                if not is_redis_unavailable(exc):
                    raise
                response = _middleware_error_response(
                    "storage_unavailable",
                    "Rate limit storage is temporarily unavailable",
                    503,
                )
            else:
                if rate_limited:
                    response = _middleware_error_response(
                        "rate_limited",
                        "Too many requests; retry later",
                        429,
                    )
                elif _uses_stream_capacity(request):
                    if not stream_capacity.acquire(blocking=False):
                        response = _middleware_error_response(
                            "stream_concurrency",
                            "Download and event stream capacity is busy; retry shortly",
                            429,
                        )
                    else:
                        try:
                            response = await call_next(request)
                        except BaseException:
                            stream_capacity.release()
                            raise
                        _release_stream_capacity_after_response(
                            response,
                            stream_capacity,
                        )
                else:
                    response = await call_next(request)
            response.set_cookie(
                "dt_session",
                session_id,
                httponly=True,
                max_age=settings.retention_seconds,
                samesite="lax",
                secure=settings.secure_cookies,
            )
            return response

    @app.exception_handler(RedisError)
    def handle_redis_error(_request: Request, exc: RedisError) -> JSONResponse:
        if not is_redis_unavailable(exc):
            raise exc
        return JSONResponse(
            status_code=STORAGE_UNAVAILABLE_STATUS,
            content=storage_unavailable_payload(),
        )

    @app.exception_handler(ApiError)
    def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    def handle_request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        status_code = (
            422
            if any(
                error["type"] == "too_long"
                and error["loc"][-1:] == ("input_paths",)
                for error in exc.errors()
            )
            else 400
        )
        return error_response(
            ApiError("invalid_request", "Invalid request body.", status_code)
        )

    app.include_router(
        create_router(
            manager,
            local_path_roots=local_path_roots,
            local_path_picker=local_path_picker,
            video_decoder=decoder,
            hosted_settings=settings,
            hosted_storage=storage,
            resource_coordinator=resource_coordinator,
        ),
    )
    frontend_dist = _frontend_dist_dir()
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    app.state.task_manager = manager
    app.state.log_store = log_store
    app.state.video_decoder = decoder
    app.state.resource_coordinator = resource_coordinator
    app.state.stream_capacity = stream_capacity
    return app


def _rate_limits_for_request(
    request: Request,
    settings: HostedSettings,
) -> tuple[tuple[str, int, int], ...]:
    if not request.url.path.startswith("/api/"):
        return ()
    if request.url.path == "/api/health":
        return ()

    rate_limits = [
        ("api", settings.api_requests_per_minute, 60),
    ]
    if request.method == "POST" and request.url.path == "/api/uploads":
        rate_limits.append(("upload", settings.upload_requests_per_hour, 60 * 60))
    return tuple(rate_limits)


def _is_rate_limited(
    manager: RedisTaskManager,
    *,
    client_host: str,
    session_id: str,
    rate_limits: tuple[tuple[str, int, int], ...],
) -> bool:
    for bucket, limit, window in rate_limits:
        for identity in (f"ip:{client_host}", f"session:{session_id}"):
            if not manager.allow_request(
                f"{identity}:{bucket}",
                limit=limit,
                window=window,
            ):
                return True
    return False


def _middleware_error_response(
    code: str,
    message: str,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _uses_stream_capacity(request: Request) -> bool:
    if request.method != "GET":
        return False
    path_parts = request.url.path.split("/")
    return (
        len(path_parts) == 5
        and path_parts[1:3] == ["api", "tasks"]
        and path_parts[4] in {"download", "events"}
    )


def _release_stream_capacity_after_response(
    response: Response,
    capacity: BoundedSemaphore,
) -> None:
    body_iterator = response.body_iterator

    async def body_with_release():
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            capacity.release()

    response.body_iterator = body_with_release()
