from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from designtoolkit.web.logs import LogStore
from designtoolkit.web.native_picker import LocalPathPicker
from designtoolkit.web.routes import ApiError, create_router, error_response
from designtoolkit.web.tasks import TaskManager


def _frontend_dist_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "web" / "frontend" / "dist"


def create_app(
    task_manager: TaskManager | None = None,
    *,
    local_path_roots: list[Path] | None = None,
    local_path_picker: LocalPathPicker | None = None,
) -> FastAPI:
    log_store = task_manager.log_store if task_manager is not None else LogStore()
    manager = task_manager or TaskManager(log_store=log_store)
    app = FastAPI(title="DesignToolkit Web GUI")

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
        ),
    )
    frontend_dist = _frontend_dist_dir()
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    app.state.task_manager = manager
    app.state.log_store = log_store
    return app
