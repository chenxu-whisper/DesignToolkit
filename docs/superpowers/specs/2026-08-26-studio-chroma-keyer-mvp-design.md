# Studio Chroma Keyer MVP Design

## Scope

This design implements one local image at a time. The user selects a PNG, JPG, or JPEG from a local path, samples one or more background colors on a preview canvas, adjusts a compact chroma key parameter set, previews the result, and exports an RGBA PNG.

Video, image sequences, uploads, background replacement, automatic sampling, GPU acceleration, and advanced edge recovery are explicitly out of scope.

## Existing-System Fit

The implementation extends the existing DesignToolkit Web GUI rather than creating a separate application:

- FastAPI is mounted under `/api` by `src/designtoolkit/web/app.py`.
- `src/designtoolkit/web/routes.py` owns route definitions and structured `ApiError` responses.
- `TaskManager` owns asynchronous work, status and SSE logs.
- `PathInput` and `/api/local-paths/pick-files` provide local file selection.
- `toolModules` adds a new navigation module, and `TaskEventController` remains the single global SSE subscriber.

The feature must not use browser uploads or attempt to derive an absolute path from a browser `File` object.

## Backend Design

Create a focused `designtoolkit.chroma_keyer` package:

- `models.py`: immutable request/result dataclasses and validated `KeyerParams`.
- `image_io.py`: read supported images as `uint8` RGB/RGBA arrays and write RGBA PNG without altering source dimensions.
- `sampling.py`: map preview coordinates to source pixels and aggregate a `ScreenColorModel`.
- `matte.py`: calculate normalized RGB distance and continuous alpha; apply clip, morphology and feather operations.
- `despill.py`: neutralize the sample model's dominant channel only where alpha is fractional.
- `core.py`: compose the stages into `key_image`.

`ScreenColorModel` contains:

```python
@dataclass(frozen=True)
class ScreenColorModel:
    mean_rgb: tuple[float, float, float]
    stddev_rgb: tuple[float, float, float]
    samples: tuple[tuple[int, int, int], ...]
```

The aggregate sample variance is `mean(stddev_rgb) / 255`. It expands the effective lower threshold:

```text
lower = clamp(tolerance + sample_variance, 0, 1)
upper = clamp(lower + softness, lower + 0.01, 1)
alpha = smoothstep(lower, upper, normalized_rgb_distance)
```

`clip_black` and `clip_white` remap only the endpoints of alpha. Invalid settings where `clip_black >= clip_white` are rejected. Shrink/grow uses a binary-alpha morphological kernel and blends the transformed matte with the continuous source matte. Feather applies a Gaussian blur to alpha only.

Despill determines the dominant color channel from `mean_rgb`. If that channel exceeds the other two by a minimum configurable margin, only that channel is reduced toward the maximum of the two other channels. Otherwise each RGB channel is mixed toward the sample model's neutral luminance. The effect mask is `alpha * (1 - alpha)`, so fully transparent background and opaque foreground are not altered.

## API Contract

All request and response models reside in `src/designtoolkit/web/schemas.py`.

### Inspect Image

`POST /api/chroma-keyer/images/inspect`

Request:

```json
{ "input_path": "E:\\media\\screen.png" }
```

Response:

```json
{
  "input_path": "E:\\media\\screen.png",
  "width": 1920,
  "height": 1080,
  "preview_url": "/api/chroma-keyer/previews/<id>"
}
```

The backend validates that the path exists, is a file, and has a supported suffix. It creates a bounded preview cache keyed by image path and modification time. The preview endpoint returns PNG bytes and never exposes an arbitrary filesystem path.

### Sample Color

`POST /api/chroma-keyer/images/sample`

Request:

```json
{
  "input_path": "E:\\media\\screen.png",
  "x": 320,
  "y": 180,
  "preview_width": 960,
  "preview_height": 540
}
```

Response:

```json
{
  "source_x": 640,
  "source_y": 360,
  "rgb": [42, 180, 72],
  "hex": "#2AB448"
}
```

The server maps coordinates using:

```python
source_x = min(source_width - 1, max(0, floor(x * source_width / preview_width)))
source_y = min(source_height - 1, max(0, floor(y * source_height / preview_height)))
```

`preview_width` and `preview_height` must be positive. `x` and `y` must be within the submitted preview rectangle.

### Generate Preview

`POST /api/chroma-keyer/preview`

Request:

```json
{
  "input_path": "E:\\media\\screen.png",
  "samples": [[42, 180, 72], [40, 176, 70]],
  "params": {
    "tolerance": 0.25,
    "softness": 0.15,
    "clip_black": 0.0,
    "clip_white": 1.0,
    "shrink_grow": 0,
    "feather": 0,
    "despill_amount": 0.5
  },
  "view_mode": "final"
}
```

Response:

```json
{ "preview_url": "/api/chroma-keyer/previews/<id>" }
```

At least one sample is required. The preview uses a fixed maximum dimension and is cached by input fingerprint, sample set, parameter object and view mode.

### Export

`POST /api/tasks/chroma-keyer`

Request:

```json
{
  "input_path": "E:\\media\\screen.png",
  "output_path": "E:\\media\\screen_keyed.png",
  "samples": [[42, 180, 72]],
  "params": {
    "tolerance": 0.25,
    "softness": 0.15,
    "clip_black": 0.0,
    "clip_white": 1.0,
    "shrink_grow": 0,
    "feather": 0,
    "despill_amount": 0.5
  }
}
```

The endpoint creates a `TaskManager` task. The default output path is computed by the server when `output_path` is omitted. It rejects existing outputs and unsupported output suffixes. The task emits queued, running, processing, success or failure events compatible with the existing UI.

## Frontend Design

Create `web/frontend/src/modules/chroma-keyer/` with:

- `ChromaKeyerPage.tsx`: owns image session state, samples, parameter state and submission handlers.
- `ChromaKeyerCanvas.tsx`: renders the returned preview, records click coordinates relative to the rendered image, draws numbered sample markers, and reports exact element pixel dimensions.
- `ChromaKeyerControls.tsx`: local path input, sample list, range controls, Final/Alpha segmented control and output action.
- `chromaKeyerValidation.ts`: validates path, sample presence and parameter order before any request.
- tests next to each component.

The page begins with an input `PathInput` configured for one image. Selecting an image calls inspect. Canvas clicks call sample, append the returned original RGB sample, and schedule a debounced preview refresh. Delete and clear actions update the same sample state, so the server receives the complete sample set on every preview/export request.

Canvas display size may differ from natural preview size due to responsive layout. The click handler uses `getBoundingClientRect()` and converts client coordinates to integer CSS pixels before calling the API. It submits the displayed image element's width and height, not window size or natural image dimensions.

The editor has Final and Alpha modes. Both are rendered by the backend, avoiding duplicated keyer arithmetic in TypeScript.

## Error Handling

- Input does not exist, is a directory, or has an unsupported extension: return 400 `invalid_request`.
- Image decoder failure: return 400 `invalid_image`.
- No samples for preview/export: return 400 `missing_samples`.
- Invalid sampling coordinates: return 400 `invalid_sample_coordinates`.
- Output exists: return 409 `output_exists`.
- Task slot is already occupied: retain the existing 409 `task_conflict` behavior.

The frontend shows route error messages near the relevant control and must not change the active task state after a rejected request.

## Testing Strategy

Backend unit tests use synthetic NumPy arrays to prove:

1. Preview-to-source coordinate mapping at normal, edge and bounds-clamped coordinates.
2. Single and multi-point screen models compute RGB mean and standard deviation.
3. Pixels matching a green, blue, and red sample become transparent while distant pixels stay opaque.
4. Softness produces intermediate alpha values.
5. Clip validation rejects inverted thresholds.
6. Despill affects only partial-alpha edge pixels.
7. Exported RGBA PNG has source dimensions and a four-channel alpha image.

Route tests verify structured validation errors, inspect and sample response contracts, preview PNG response, task creation and output collision rejection.

Frontend tests verify local path inspection, canvas coordinate payloads, sample list mutations, disabled preview/export without samples, view mode requests, and export payload construction.

## Acceptance Mapping

- PRD local import: inspect endpoint plus `PathInput`.
- PRD canvas sampling: `ChromaKeyerCanvas` plus `/images/sample`.
- PRD any-color chroma key: `ScreenColorModel` without a fixed screen-type enum.
- PRD green/blue behavior: dominant-channel despill from the sampling model.
- PRD preview: backend Final/Alpha preview endpoint.
- PRD original-size PNG: `key_image` export task with dimensional and alpha tests.
