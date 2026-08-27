from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
RgbChannel = Annotated[int, Field(strict=True, ge=0, le=255)]
StrictInt = Annotated[int, Field(strict=True)]
UnitIntervalFloat = Annotated[
    float,
    Field(strict=True, ge=0, le=1, allow_inf_nan=False),
]


class TaskKind(StrEnum):
    VIDEO_FRAMES = "video_frames"
    MEDIA_RESIZE = "media_resize"
    CHROMA_KEYER = "chroma_keyer"


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


class ChromaViewMode(StrEnum):
    FINAL = "final"
    ALPHA = "alpha"


class ChromaImageInspectRequest(BaseModel):
    input_path: str

    @field_validator("input_path")
    @classmethod
    def validate_input_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_path must not be blank")
        return value


class ChromaImageInspectResponse(BaseModel):
    input_path: str
    width: PositiveInt
    height: PositiveInt
    preview_width: PositiveInt
    preview_height: PositiveInt
    preview_url: str


class ChromaSampleRequest(BaseModel):
    input_path: str
    x: float = Field(strict=True, allow_inf_nan=False)
    y: float = Field(strict=True, allow_inf_nan=False)
    preview_width: StrictInt
    preview_height: StrictInt

    @field_validator("input_path")
    @classmethod
    def validate_input_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_path must not be blank")
        return value


class ChromaSampleResponse(BaseModel):
    rgb: list[RgbChannel] = Field(min_length=3, max_length=3)
    hex: str
    source_x: Annotated[int, Field(strict=True, ge=0)]
    source_y: Annotated[int, Field(strict=True, ge=0)]


class ChromaKeyerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tolerance: UnitIntervalFloat = 0.25
    softness: UnitIntervalFloat = 0.15
    clip_black: UnitIntervalFloat = 0.0
    clip_white: UnitIntervalFloat = 1.0
    shrink_grow: Annotated[int, Field(strict=True, ge=-10, le=10)] = 0
    feather: Annotated[int, Field(strict=True, ge=0, le=10)] = 0
    despill_amount: UnitIntervalFloat = 0.5

    @model_validator(mode="after")
    def validate_clip_range(self) -> ChromaKeyerParams:
        if self.clip_black >= self.clip_white:
            raise ValueError("clip_black must be less than clip_white")
        return self


class ChromaKeyerTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_path: str
    samples: list[list[RgbChannel]] = Field(min_length=1)
    params: ChromaKeyerParams
    output_path: str | None = None

    @field_validator("input_path", "output_path")
    @classmethod
    def validate_paths(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("path must not be blank")
        return value

    @field_validator("samples")
    @classmethod
    def validate_samples(cls, value: list[list[int]]) -> list[list[int]]:
        if any(len(sample) != 3 for sample in value):
            raise ValueError("each sample must contain exactly three channels")
        return value


class ChromaPreviewRequest(BaseModel):
    input_path: str
    samples: list[list[RgbChannel]] = Field(default_factory=list)
    params: ChromaKeyerParams = Field(default_factory=ChromaKeyerParams)
    view_mode: ChromaViewMode = ChromaViewMode.FINAL

    @field_validator("input_path")
    @classmethod
    def validate_input_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_path must not be blank")
        return value

    @field_validator("samples")
    @classmethod
    def validate_samples(cls, value: list[list[int]]) -> list[list[int]]:
        if any(len(sample) != 3 for sample in value):
            raise ValueError("each sample must contain exactly three channels")
        return value


class ChromaPreviewResponse(BaseModel):
    preview_url: str


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
