# WebM Alpha PNG Frames Design

## Goal

`video-to-frames` must export PNG sequences from transparent WebM videos without losing alpha information. The test asset is `assets/video/test.webm`.

## User-Facing Behavior

- The existing command remains unchanged:

```powershell
python -m designtoolkit.main video-to-frames .\assets\video\test.webm --format png
```

- PNG output preserves alpha by default when the input is a transparent WebM.
- JPG and JPEG output remain non-transparent because those formats do not support alpha.
- Existing options keep their current meaning: `--output-dir`, `--size`, `--fps`, `--frames`, and `--overwrite`.

## Evidence From Current Behavior

Current `video-to-frames` can export `assets/video/test.webm`, but the first exported PNG decodes to alpha extrema `(255, 255)`, so all pixels are opaque. Direct FFmpeg probing shows the bundled FFmpeg preserves the same source alpha when `-c:v libvpx-vp9` is placed before `-i`, producing alpha extrema `(0, 255)` with multiple alpha levels.

## Recommended Architecture

Keep the CLI and public Python dataclasses stable. Adjust only FFmpeg command construction in `src/designtoolkit/video_frames/core.py`.

When `image_format == "png"` and `input_path.suffix.lower() == ".webm"`, insert these input decoder arguments before `-i`:

```text
-c:v libvpx-vp9
```

This makes FFmpeg use the WebM VP9 decoder path that exposes the alpha layer before PNG encoding. Non-PNG exports and non-WebM inputs use the existing command shape.

## Components

- `src/designtoolkit/video_frames/core.py`
  - Add a small helper that returns extra input decoder arguments for alpha-capable WebM-to-PNG export.
  - Call the helper while building the FFmpeg command, before `-i`.
  - Keep filter composition behavior unchanged.

- `tests/test_video_frames.py`
  - Add fake-backend command tests for WebM-to-PNG decoder arguments.
  - Add a negative command test proving JPG/JPEG does not enable alpha handling.
  - Add an integration regression test using `assets/video/test.webm` and the bundled FFmpeg backend.

- `README.md`
  - Document that transparent WebM to PNG sequence export preserves alpha by default.
  - Keep examples and existing option descriptions intact.

## Data Flow

1. CLI parses `video-to-frames` arguments into `VideoFrameOptions`.
2. `export_video_frames` normalizes the image format and prepares the output directory.
3. `_build_ffmpeg_command` adds WebM alpha decoder arguments only for PNG output.
4. FFmpeg decodes the WebM alpha layer and writes PNG frames.
5. Tests decode an exported PNG frame as RGBA raw video and assert alpha includes transparent and opaque values.

## Error Handling

- If bundled FFmpeg lacks `libvpx-vp9`, the existing backend failure path returns a `VideoFrameError` with exit code `1`.
- If the input WebM is not compatible with the decoder, FFmpeg returns a non-zero code and the existing `FFmpeg failed with exit code ...` message is preserved.
- No new CLI validation errors are added.

## Testing Strategy

- Unit test: `.webm` input plus PNG output includes `-c:v libvpx-vp9` before `-i`.
- Unit test: `.webm` input plus JPG output does not include `-c:v libvpx-vp9`.
- Integration test: export one frame from `assets/video/test.webm` to PNG with real bundled FFmpeg and verify decoded alpha extrema are `(0, 255)`.
- Regression commands:

```powershell
uv run python -m pytest tests/test_video_frames.py
uv run python -m pytest
uv run python -m ruff check .
uv run python -m compileall src tests
```

## Non-Goals

- Do not add a new CLI option such as `--preserve-alpha`.
- Do not add video metadata probing in this iteration.
- Do not attempt to preserve alpha for JPG or JPEG output.
- Do not change frame naming, default output directories, sampling options, or overwrite semantics.
