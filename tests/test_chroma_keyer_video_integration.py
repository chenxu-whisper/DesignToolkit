from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from designtoolkit.chroma_keyer import (
    KeyerParams,
    ScreenColorModel,
    VideoKeyOptions,
    key_video_to_png_sequence,
)
from designtoolkit.chroma_keyer.video_io import ImageioVideoDecoder
from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

WIDTH = 48
HEIGHT = 24
FPS = 4
DURATION_SECONDS = 1
FRAME_COUNT = FPS * DURATION_SECONDS
SCREEN_COLOR = (0, 255, 0)
PARAMS = KeyerParams(
    screen_gain=1.0,
    screen_balance=0.5,
    clip_black=0.0,
    clip_white=1.0,
    shrink_grow=0,
    feather=0,
    despill_amount=1.0,
)


def _ffmpeg_listing(ffmpeg_exe: str, option: str) -> str:
    completed = subprocess.run(
        [ffmpeg_exe, "-hide_banner", option],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"Unable to inspect bundled FFmpeg {option}: {completed.stderr}")
    return completed.stdout


def _require_ffmpeg_capabilities(
    ffmpeg_exe: str,
    *,
    encoder: str,
    filters: tuple[str, ...],
) -> None:
    encoders = _ffmpeg_listing(ffmpeg_exe, "-encoders")
    available_encoders = {
        columns[1]
        for line in encoders.splitlines()
        if len(columns := line.split()) >= 2
    }
    if encoder not in available_encoders:
        pytest.skip(f"Bundled FFmpeg does not provide encoder {encoder}")

    filter_listing = _ffmpeg_listing(ffmpeg_exe, "-filters")
    available_filters = {
        columns[1]
        for line in filter_listing.splitlines()
        if len(columns := line.split()) >= 2
    }
    missing_filters = sorted(set(filters) - available_filters)
    if missing_filters:
        pytest.skip(
            "Bundled FFmpeg does not provide required filter(s): "
            + ", ".join(missing_filters)
        )


def _generate_green_screen_fixture(path: Path) -> None:
    ffmpeg_exe = FfmpegBackend().get_ffmpeg_exe()
    _require_ffmpeg_capabilities(
        ffmpeg_exe,
        encoder="ffv1",
        filters=("color", "drawbox", "format"),
    )
    video_filters = ["format=rgb24"]
    for frame_index in range(FRAME_COUNT):
        moving_x = 4 + frame_index * 2
        enabled_on_frame = f"enable='eq(n,{frame_index})'"
        video_filters.extend(
            (
                (
                    f"drawbox=x={moving_x}:y=7:w=12:h=10:"
                    f"color=0x807f00:t=fill:{enabled_on_frame}"
                ),
                (
                    f"drawbox=x={moving_x + 1}:y=8:w=10:h=8:"
                    f"color=0xff0000:t=fill:{enabled_on_frame}"
                ),
            )
        )
    video_filter = ",".join(video_filters)
    completed = subprocess.run(
        [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x00ff00:s={WIDTH}x{HEIGHT}:r={FPS}:d={DURATION_SECONDS}",
            "-vf",
            video_filter,
            "-an",
            "-c:v",
            "ffv1",
            "-pix_fmt",
            "bgr0",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _screen_model() -> ScreenColorModel:
    return ScreenColorModel(
        mean_rgb=tuple(float(channel) for channel in SCREEN_COLOR),
        stddev_rgb=(0.0, 0.0, 0.0),
        samples=(SCREEN_COLOR,),
    )


def test_real_video_export_preserves_keyed_pixels_manifest_and_atomic_cleanup(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "moving-red-screen.mkv"
    output_root = tmp_path / "exports"
    output_root.mkdir()
    _generate_green_screen_fixture(input_path)

    result = key_video_to_png_sequence(
        VideoKeyOptions(input_path=input_path, output_root=output_root),
        ImageioVideoDecoder(),
        _screen_model(),
        PARAMS,
    )

    expected_names = [
        f"moving-red-screen_keyed_{index:06d}.png"
        for index in range(1, FRAME_COUNT + 1)
    ]
    png_paths = sorted(result.output_dir.glob("*.png"))
    assert result.frame_count == FRAME_COUNT
    assert result.metadata.estimated_frame_count == FRAME_COUNT
    assert [path.name for path in png_paths] == expected_names

    frames: list[np.ndarray] = []
    for png_path in png_paths:
        with Image.open(png_path) as image:
            assert image.mode == "RGBA"
            assert image.size == (WIDTH, HEIGHT)
            frames.append(np.asarray(image).copy())

    for frame_index, rgba in enumerate(frames):
        moving_x = 4 + frame_index * 2
        background = rgba[0, 0]
        foreground = rgba[11, moving_x + 5]
        blended_edge = rgba[7, moving_x]

        assert background[3] <= 5
        assert foreground[3] >= 250
        # Allow only minor per-channel rounding from fixture encoding/decoding.
        np.testing.assert_allclose(
            foreground[:3],
            [255, 0, 0],
            rtol=0,
            atol=2,
        )
        assert foreground[0] > foreground[1] + 200
        assert foreground[0] > foreground[2] + 200
        assert 32 <= blended_edge[3] <= 223
        assert blended_edge[0] > blended_edge[1]
        assert blended_edge[0] > blended_edge[2]

    manifest = json.loads(result.manifest_path.read_bytes())
    assert manifest == {
        "version": 1,
        "source_path": str(input_path),
        "width": WIDTH,
        "height": HEIGHT,
        "fps": float(FPS),
        "duration_seconds": float(DURATION_SECONDS),
        "frame_count": FRAME_COUNT,
        "pattern": "moving-red-screen_keyed_%06d.png",
        "screen_color": list(SCREEN_COLOR),
        "params": {
            "screen_gain": 1.0,
            "screen_balance": 0.5,
            "clip_black": 0.0,
            "clip_white": 1.0,
            "shrink_grow": 0,
            "feather": 0,
            "despill_amount": 1.0,
        },
    }
    assert list(output_root.glob(".moving-red-screen_keyed_frames.*.tmp")) == []
