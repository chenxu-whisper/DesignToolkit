from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskKind(StrEnum):
    VIDEO_FRAMES = "video_frames"
    MEDIA_RESIZE = "media_resize"


class TaskStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LogLevel(StrEnum):
    INFO = "info"
    PROGRESS = "progress"
    SUCCESS = "success"
    ERROR = "error"


class LogPhase(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    PREPARING = "preparing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class VideoFramesTaskRequest(BaseModel):
    input_path: str | None = Field(default=None, min_length=1)
    input_paths: list[str] | None = None
    output_dir: str | None = None
    image_format: Literal["png", "jpg", "jpeg"] = "png"
    size: str | None = None
    fps: float | None = None
    frames: int | None = None
    overwrite: bool = False

    @field_validator("input_paths")
    @classmethod
    def validate_input_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("input_paths must contain at least one path")
        normalized = [path.strip() for path in value]
        if any(path == "" for path in normalized):
            raise ValueError("input_paths cannot contain blank paths")
        return normalized

    @model_validator(mode="after")
    def validate_sampling(self) -> VideoFramesTaskRequest:
        has_input_path = self.input_path is not None
        has_input_paths = self.input_paths is not None
        if has_input_path == has_input_paths:
            raise ValueError("Specify either input_path or input_paths")
        if self.fps is not None and self.frames is not None:
            raise ValueError("fps and frames cannot both be set")
        return self


class MediaResizeTaskRequest(BaseModel):
    input_path: str | None = Field(default=None, min_length=1)
    input_paths: list[str] | None = None
    output_path: str | None = None
    size: str = Field(min_length=1)

    @field_validator("input_paths")
    @classmethod
    def validate_input_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("input_paths must contain at least one path")
        normalized = [path.strip() for path in value]
        if any(path == "" for path in normalized):
            raise ValueError("input_paths cannot contain blank paths")
        return normalized

    @model_validator(mode="after")
    def validate_input_selection(self) -> MediaResizeTaskRequest:
        has_input_path = self.input_path is not None
        has_input_paths = self.input_paths is not None
        if has_input_path == has_input_paths:
            raise ValueError("Specify either input_path or input_paths")
        if has_input_paths and self.output_path:
            raise ValueError("output_path cannot be used with input_paths")
        return self


class DroppedFilePathResolveRequest(BaseModel):
    names: list[str]

    @field_validator("names")
    @classmethod
    def validate_names(cls, value: list[str]) -> list[str]:
        normalized = [name.strip() for name in value]
        if len(normalized) == 0:
            raise ValueError("names must contain at least one file name")
        if any(name == "" for name in normalized):
            raise ValueError("names cannot contain blank file names")
        return normalized


class DroppedFilePathResolveResponse(BaseModel):
    paths: list[str]
    unresolved: list[str]


class LocalFilePickRequest(BaseModel):
    multiple: bool = False
    file_kind: Literal["video", "media", "any"] = "any"
    title: str = "Select file"


class LocalDirectoryPickRequest(BaseModel):
    title: str = "Select directory"


class LocalSaveFilePickRequest(BaseModel):
    file_kind: Literal["media", "any"] = "any"
    title: str = "Select output path"


class LocalPathPickResponse(BaseModel):
    paths: list[str]
    cancelled: bool


class TaskCreateResponse(BaseModel):
    task_id: str
    status: TaskStatus


class TaskResponse(BaseModel):
    task_id: str
    kind: TaskKind
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input_path: str
    output_path: str | None = None
    error: str | None = None


class LogEventResponse(BaseModel):
    task_id: str
    timestamp: datetime
    level: LogLevel
    phase: LogPhase
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
