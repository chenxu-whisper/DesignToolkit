from __future__ import annotations

from pathlib import Path

from designtoolkit.chroma_keyer.models import (
    KeyerParams,
    KeyImageOptions,
    ScreenColorModel,
)
from designtoolkit.chroma_keyer.sampling import screen_model_from_samples
from designtoolkit.media_resize import MediaResizeOptions
from designtoolkit.video_frames import (
    VideoFrameError,
    VideoFrameOptions,
    default_output_dir,
    parse_size,
)
from designtoolkit.web.schemas import (
    ChromaKeyerTaskRequest,
    MediaResizeTaskRequest,
    VideoFramesTaskRequest,
)


def _parse_request_size(value: str) -> tuple[int, int]:
    try:
        return parse_size(value)
    except VideoFrameError as exc:
        raise ValueError(str(exc)) from exc


def chroma_keyer_options_from_request(
    request: ChromaKeyerTaskRequest,
) -> tuple[KeyImageOptions, ScreenColorModel, KeyerParams]:
    params = request.params
    return (
        KeyImageOptions(
            input_path=Path(request.input_path),
            output_path=Path(request.output_path) if request.output_path else None,
        ),
        screen_model_from_samples(request.samples),
        KeyerParams(
            screen_gain=params.screen_gain,
            screen_balance=params.screen_balance,
            clip_black=params.clip_black,
            clip_white=params.clip_white,
            shrink_grow=params.shrink_grow,
            feather=params.feather,
            despill_amount=params.despill_amount,
        ),
    )


def video_frame_options_from_request(
    request: VideoFramesTaskRequest,
) -> VideoFrameOptions:
    options = video_frame_options_list_from_request(request)
    if len(options) != 1:
        raise ValueError("Expected a single input_path")
    return options[0]


def video_frame_options_list_from_request(
    request: VideoFramesTaskRequest,
) -> list[VideoFrameOptions]:
    input_paths = request.input_paths
    if input_paths is None:
        if request.input_path is None:
            raise ValueError("Specify either input_path or input_paths")
        input_paths = [request.input_path]
    if any(input_path == "" for input_path in input_paths):
        raise ValueError("Specify either input_path or input_paths")
    output_root = Path(request.output_dir) if request.output_dir else None
    is_batch = len(input_paths) > 1
    options: list[VideoFrameOptions] = []
    for input_path in input_paths:
        input_path_obj = Path(input_path)
        options.append(
            VideoFrameOptions(
                input_path=input_path_obj,
                output_dir=(
                    output_root / default_output_dir(input_path_obj).name
                    if output_root is not None and is_batch
                    else output_root
                ),
                image_format=request.image_format,
                size=_parse_request_size(request.size) if request.size else None,
                fps=request.fps,
                frames=request.frames,
                overwrite=request.overwrite,
            )
        )
    return options


def media_resize_options_from_request(
    request: MediaResizeTaskRequest,
) -> MediaResizeOptions:
    options = media_resize_options_list_from_request(request)
    if len(options) != 1:
        raise ValueError("Expected a single input_path")
    return options[0]


def media_resize_options_list_from_request(
    request: MediaResizeTaskRequest,
) -> list[MediaResizeOptions]:
    input_paths = request.input_paths
    if input_paths is None:
        if request.input_path is None:
            raise ValueError("Specify either input_path or input_paths")
        input_paths = [request.input_path]
    if any(input_path == "" for input_path in input_paths):
        raise ValueError("Specify either input_path or input_paths")
    size = _parse_request_size(request.size)
    return [
        MediaResizeOptions(
            input_path=Path(input_path),
            size=size,
            output_path=Path(request.output_path) if request.output_path else None,
        )
        for input_path in input_paths
    ]
