from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis import Redis
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
            rate_limit = _rate_limit_for_request(request, settings)
            if (
                rate_limit is not None
                and isinstance(manager, RedisTaskManager)
                and not manager.allow_request(
                    f"{client_host}:{rate_limit[0]}",
                    limit=rate_limit[1],
                    window=rate_limit[2],
                )
            ):
                response = JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "rate_limited",
                            "message": "Too many requests; retry later",
                        }
                    },
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

    @app.exception_handler(ApiError)
    def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    def handle_request_validation_error(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return error_response(ApiError("invalid_request", "Invalid request body.", 400))

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
    return app


def _rate_limit_for_request(
    request: Request,
    settings: HostedSettings,
) -> tuple[str, int, int] | None:
    if request.method != "POST":
        return None
    if request.url.path == "/api/uploads":
        return ("upload", settings.upload_requests_per_hour, 60 * 60)
    if request.url.path.startswith("/api/chroma-keyer/"):
        return ("preview", settings.api_requests_per_minute, 60)
    if request.url.path.startswith("/api/tasks/"):
        return ("task", settings.api_requests_per_minute, 60)
    return None
