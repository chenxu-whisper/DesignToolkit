# Studio Chroma Keyer MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local single-image chroma key editor that samples arbitrary background colors on a canvas, previews Final/Alpha results, and exports a full-resolution RGBA PNG.

**Architecture:** A new pure-Python `designtoolkit.chroma_keyer` package owns image validation, coordinate mapping, sampling, alpha generation and PNG export. The existing FastAPI layer exposes inspection, sample and preview routes, while the current task manager runs full-resolution export and emits existing SSE events. A new React module owns one image session and delegates all image computation to the backend.

**Tech Stack:** Python 3.12, NumPy, OpenCV headless, Pillow, FastAPI, Pydantic, pytest, React 19, TypeScript, Zustand, Vitest, Testing Library.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `pyproject.toml` / `uv.lock` | Runtime dependencies: NumPy, OpenCV headless and Pillow. |
| `src/designtoolkit/chroma_keyer/models.py` | Immutable parameter, sample model and result contracts. |
| `src/designtoolkit/chroma_keyer/sampling.py` | Preview-to-source coordinate mapping and sample aggregation. |
| `src/designtoolkit/chroma_keyer/matte.py` | Continuous alpha, clip, shrink/grow and feather operations. |
| `src/designtoolkit/chroma_keyer/despill.py` | Sample-driven edge-only color spill suppression. |
| `src/designtoolkit/chroma_keyer/image_io.py` | Supported-image read, preview PNG encoding and RGBA PNG export. |
| `src/designtoolkit/chroma_keyer/core.py` | One public image-keying pipeline. |
| `src/designtoolkit/web/chroma_previews.py` | Bounded in-memory preview cache keyed by input fingerprint and request state. |
| `src/designtoolkit/web/schemas.py` | Chroma inspect/sample/preview/export Pydantic contracts and task enum. |
| `src/designtoolkit/web/adapters.py` | Conversion from web export requests to chroma options. |
| `src/designtoolkit/web/tasks.py` | Chroma export task creation and runner. |
| `src/designtoolkit/web/routes.py` | Inspect, sample, preview, preview-byte and task routes. |
| `tests/test_chroma_keyer_*.py` | Algorithm, image IO and cache regression tests. |
| `tests/test_web_chroma_keyer.py` | Route and task integration tests. |
| `web/frontend/src/modules/chroma-keyer/*` | Editor state, validation, canvas, controls and page tests. |
| `web/frontend/src/services/apiClient.ts` | Typed chroma API calls. |
| `web/frontend/src/app/types.ts`, `modules.ts` | New navigation module identifier and entry. |
| `web/frontend/src/styles/app.css` | Responsive editor canvas, parameter panel and sample-list styles. |

### Task 1: Add Dependencies and Domain Contracts

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/designtoolkit/chroma_keyer/__init__.py`
- Create: `src/designtoolkit/chroma_keyer/models.py`
- Create: `tests/test_chroma_keyer_models.py`

- [ ] **Step 1: Write failing parameter validation tests**

```python
from pytest import raises

from designtoolkit.chroma_keyer.models import KeyerError, KeyerParams


def test_params_reject_clip_black_not_below_clip_white() -> None:
    with raises(KeyerError, match="clip_black must be less than clip_white"):
        KeyerParams(clip_black=0.8, clip_white=0.8)


def test_params_accept_default_mvp_values() -> None:
    assert KeyerParams() == KeyerParams(
        tolerance=0.25,
        softness=0.15,
        clip_black=0.0,
        clip_white=1.0,
        shrink_grow=0,
        feather=0,
        despill_amount=0.5,
    )
```

- [ ] **Step 2: Run the focused test and verify it fails because the module is missing**

Run: `uv run pytest tests/test_chroma_keyer_models.py -v`

Expected: collection failure for `designtoolkit.chroma_keyer`.

- [ ] **Step 3: Add runtime dependencies and minimal contracts**

```toml
dependencies = [
    "fastapi>=0.116.0",
    "imageio-ffmpeg>=0.6.0",
    "numpy>=2.0.0",
    "opencv-python-headless>=4.10.0",
    "pillow>=10.0.0",
    "uvicorn>=0.35.0",
]
```

```python
@dataclass(frozen=True)
class KeyerParams:
    tolerance: float = 0.25
    softness: float = 0.15
    clip_black: float = 0.0
    clip_white: float = 1.0
    shrink_grow: int = 0
    feather: int = 0
    despill_amount: float = 0.5

    def __post_init__(self) -> None:
        if not 0 <= self.clip_black < self.clip_white <= 1:
            raise KeyerError("clip_black must be less than clip_white")
```

Also define `KeyerError`, `ScreenColorModel`, `KeyImageOptions`, `KeyImageResult`, and export them from `__init__.py`. Validate all normalized float values are finite and within their documented ranges.

- [ ] **Step 4: Refresh the lockfile and run the focused test**

Run: `uv lock && uv sync --link-mode=copy && uv run pytest tests/test_chroma_keyer_models.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit the dependency and contract slice**

```powershell
git add pyproject.toml uv.lock src/designtoolkit/chroma_keyer tests/test_chroma_keyer_models.py
git commit -m "feat: add chroma keyer contracts"
```

### Task 2: Implement Sampling and Alpha-Matte Pipeline

**Files:**
- Create: `src/designtoolkit/chroma_keyer/sampling.py`
- Create: `src/designtoolkit/chroma_keyer/matte.py`
- Create: `src/designtoolkit/chroma_keyer/despill.py`
- Create: `tests/test_chroma_keyer_sampling.py`
- Create: `tests/test_chroma_keyer_matte.py`
- Create: `tests/test_chroma_keyer_despill.py`

- [ ] **Step 1: Write failing coordinate and aggregation tests**

```python
def test_maps_preview_click_to_original_image_pixel() -> None:
    assert map_preview_point(
        x=320, y=180, preview_width=960, preview_height=540,
        source_width=1920, source_height=1080,
    ) == (640, 360)


def test_aggregates_multiple_samples_into_mean_and_stddev() -> None:
    model = screen_model_from_samples([(40, 180, 70), (44, 184, 74)])
    assert model.mean_rgb == (42.0, 182.0, 72.0)
    assert model.stddev_rgb == (2.0, 2.0, 2.0)
```

- [ ] **Step 2: Run sampling tests and verify expected missing-symbol failures**

Run: `uv run pytest tests/test_chroma_keyer_sampling.py -v`

Expected: import failure for `map_preview_point` and `screen_model_from_samples`.

- [ ] **Step 3: Implement exact coordinate validation and sample aggregation**

```python
def map_preview_point(...) -> tuple[int, int]:
    if preview_width <= 0 or preview_height <= 0:
        raise KeyerError("preview dimensions must be greater than 0")
    if not 0 <= x < preview_width or not 0 <= y < preview_height:
        raise KeyerError("sample coordinates must be inside the preview")
    source_x = min(source_width - 1, floor(x * source_width / preview_width))
    source_y = min(source_height - 1, floor(y * source_height / preview_height))
    return source_x, source_y
```

Implement `screen_model_from_samples` with `numpy.asarray(samples, dtype=numpy.float32)`, `mean(axis=0)`, and `std(axis=0)`. Reject an empty sample list and channel values outside `0..255`.

- [ ] **Step 4: Write failing alpha and despill behavior tests**

```python
def test_matching_screen_pixel_is_transparent_and_distant_pixel_is_opaque() -> None:
    rgb = numpy.array([[[42, 180, 72], [220, 40, 40]]], dtype=numpy.uint8)
    alpha = generate_alpha(rgb, screen_model_from_samples([(42, 180, 72)]), KeyerParams())
    assert alpha[0, 0] == 0
    assert alpha[0, 1] == 255


def test_despill_changes_only_fractional_alpha_edge_pixels() -> None:
    rgb = numpy.array([[[30, 180, 30], [30, 180, 30]]], dtype=numpy.uint8)
    alpha = numpy.array([[0, 128]], dtype=numpy.uint8)
    result = despill(rgb, alpha, screen_model_from_samples([(42, 180, 72)]), amount=1.0)
    assert result[0, 0].tolist() == [30, 180, 30]
    assert result[0, 1, 1] < 180
```

- [ ] **Step 5: Run matte and despill tests and verify expected failures**

Run: `uv run pytest tests/test_chroma_keyer_matte.py tests/test_chroma_keyer_despill.py -v`

Expected: import failures for `generate_alpha` and `despill`.

- [ ] **Step 6: Implement the minimal continuous-matte and edge-only-despill behavior**

```python
distance = numpy.linalg.norm(rgb.astype(numpy.float32) - mean_rgb, axis=2)
distance /= numpy.sqrt(3.0 * 255.0**2)
lower = numpy.clip(params.tolerance + model.variance, 0.0, 1.0)
upper = max(lower + 0.01, min(1.0, lower + params.softness))
t = numpy.clip((distance - lower) / (upper - lower), 0.0, 1.0)
alpha = t * t * (3.0 - 2.0 * t)
```

Apply clip remapping, use OpenCV ellipse kernels for non-zero `shrink_grow`, and `cv2.GaussianBlur` only when `feather > 0`. In `despill`, use `alpha / 255 * (1 - alpha / 255)` as the mask and select the dominant sample channel only when it exceeds both other mean channels by at least `0.05 * 255`.

- [ ] **Step 7: Run all algorithm tests**

Run: `uv run pytest tests/test_chroma_keyer_models.py tests/test_chroma_keyer_sampling.py tests/test_chroma_keyer_matte.py tests/test_chroma_keyer_despill.py -v`

Expected: all tests pass.

- [ ] **Step 8: Commit the sampling and matte slice**

```powershell
git add src/designtoolkit/chroma_keyer tests/test_chroma_keyer_sampling.py tests/test_chroma_keyer_matte.py tests/test_chroma_keyer_despill.py
git commit -m "feat: add chroma matte generation"
```

### Task 3: Add Image IO, Preview Encoding, and Full-Resolution PNG Export

**Files:**
- Create: `src/designtoolkit/chroma_keyer/image_io.py`
- Create: `src/designtoolkit/chroma_keyer/core.py`
- Create: `tests/test_chroma_keyer_core.py`

- [ ] **Step 1: Write failing export behavior tests using a generated image**

```python
def test_key_image_exports_source_sized_rgba_png(tmp_path: Path) -> None:
    input_path = tmp_path / "screen.png"
    output_path = tmp_path / "screen_keyed.png"
    Image.fromarray(numpy.array([[[42, 180, 72], [220, 40, 40]]], dtype=numpy.uint8)).save(input_path)

    result = key_image(
        KeyImageOptions(input_path=input_path, output_path=output_path),
        screen_model_from_samples([(42, 180, 72)]),
        KeyerParams(),
    )

    image = Image.open(result.output_path)
    assert image.mode == "RGBA"
    assert image.size == (2, 1)
    assert image.getchannel("A").getpixel((0, 0)) == 0


def test_key_image_refuses_existing_output(tmp_path: Path) -> None:
    ...
    with raises(KeyerError, match="Output path already exists"):
        key_image(...)
```

- [ ] **Step 2: Run the test and verify it fails because `key_image` does not exist**

Run: `uv run pytest tests/test_chroma_keyer_core.py -v`

Expected: import failure for `key_image`.

- [ ] **Step 3: Implement supported image IO and the composition pipeline**

```python
def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_keyed.png")


def key_image(options: KeyImageOptions, model: ScreenColorModel, params: KeyerParams) -> KeyImageResult:
    rgb = read_rgb_image(options.input_path)
    output_path = options.output_path or default_output_path(options.input_path)
    validate_output_path(input_path=options.input_path, output_path=output_path)
    alpha = refine_alpha(generate_alpha(rgb, model, params), params)
    keyed_rgb = despill(rgb, alpha, model, amount=params.despill_amount)
    write_rgba_png(output_path, keyed_rgb, alpha)
    return KeyImageResult(output_path=output_path)
```

Accept only `.png`, `.jpg`, and `.jpeg`; normalize source images to RGB with Pillow; preserve exact source width/height; always write a PNG output. Build `make_preview_png` that downsizes with Pillow while preserving aspect ratio, then encodes to PNG bytes.

- [ ] **Step 4: Run the core test and verify it passes**

Run: `uv run pytest tests/test_chroma_keyer_core.py -v`

Expected: all core export tests pass.

- [ ] **Step 5: Commit the image IO slice**

```powershell
git add src/designtoolkit/chroma_keyer/image_io.py src/designtoolkit/chroma_keyer/core.py tests/test_chroma_keyer_core.py
git commit -m "feat: export keyed png images"
```

### Task 4: Add Cached Image Inspection, Sampling, and Preview API

**Files:**
- Create: `src/designtoolkit/web/chroma_previews.py`
- Modify: `src/designtoolkit/web/schemas.py`
- Modify: `src/designtoolkit/web/routes.py`
- Create: `tests/test_web_chroma_keyer.py`

- [ ] **Step 1: Write failing route tests against `create_app`**

```python
def test_inspect_returns_dimensions_and_preview_url(client, input_png: Path) -> None:
    response = client.post("/api/chroma-keyer/images/inspect", json={"input_path": str(input_png)})
    assert response.status_code == 200
    assert response.json()["width"] == 2
    assert response.json()["height"] == 1
    assert response.json()["preview_url"].startswith("/api/chroma-keyer/previews/")


def test_sample_maps_preview_coordinates_to_source_pixel(client, input_png: Path) -> None:
    response = client.post("/api/chroma-keyer/images/sample", json={
        "input_path": str(input_png), "x": 1, "y": 0,
        "preview_width": 2, "preview_height": 1,
    })
    assert response.status_code == 200
    assert response.json()["rgb"] == [220, 40, 40]
```

- [ ] **Step 2: Run the route tests and verify 404 failures**

Run: `uv run pytest tests/test_web_chroma_keyer.py -v`

Expected: failures because the chroma routes are not registered.

- [ ] **Step 3: Add Pydantic schemas and a bounded preview cache**

Define `ChromaImageInspectRequest/Response`, `ChromaSampleRequest/Response`, `ChromaPreviewRequest/Response`, `ChromaViewMode`, and `ChromaKeyerTaskRequest`. `samples` is a non-empty list of exactly-three-integer RGB tuples for preview and export. Store at most 32 entries in `ChromaPreviewStore`; preview IDs are opaque UUIDs and its lookup returns PNG bytes or `None`.

- [ ] **Step 4: Add routes and structured failures**

```python
@router.post("/chroma-keyer/images/inspect", response_model=ChromaImageInspectResponse)
def inspect_chroma_image(request: ChromaImageInspectRequest) -> ChromaImageInspectResponse:
    image = inspect_image(Path(request.input_path))
    preview_id = preview_store.put(make_preview_png(image.rgb))
    return ChromaImageInspectResponse(..., preview_url=f"/api/chroma-keyer/previews/{preview_id}")
```

Add sample, preview and `GET /chroma-keyer/previews/{preview_id}` routes before the API catch-all. Translate `KeyerError` to `ApiError` with `invalid_request` and return `preview_not_found` with HTTP 404 for an evicted/unknown ID.

- [ ] **Step 5: Run route tests and add negative cases**

Run: `uv run pytest tests/test_web_chroma_keyer.py -v`

Expected: inspect, sample, Final/Alpha preview byte responses and invalid path/coordinate/missing sample cases pass.

- [ ] **Step 6: Commit the read-only API slice**

```powershell
git add src/designtoolkit/web/chroma_previews.py src/designtoolkit/web/schemas.py src/designtoolkit/web/routes.py tests/test_web_chroma_keyer.py
git commit -m "feat: add chroma image preview api"
```

### Task 5: Run Keyed PNG Export Through the Existing Task Manager

**Files:**
- Modify: `src/designtoolkit/web/schemas.py`
- Modify: `src/designtoolkit/web/adapters.py`
- Modify: `src/designtoolkit/web/tasks.py`
- Modify: `src/designtoolkit/web/routes.py`
- Modify: `tests/test_web_chroma_keyer.py`

- [ ] **Step 1: Write a failing synchronous task test**

```python
def test_chroma_export_task_writes_default_keyed_png(tmp_path: Path) -> None:
    manager = TaskManager(log_store=LogStore(), run_async=False)
    task = manager.create_chroma_keyer_task(
        ChromaKeyerTaskRequest(input_path=str(input_png), samples=[[42, 180, 72]])
    )
    assert task.status is TaskStatus.SUCCEEDED
    assert task.output_path == str(tmp_path / "screen_keyed.png")
    assert Path(task.output_path).exists()
```

- [ ] **Step 2: Run the test and verify it fails because the task method is missing**

Run: `uv run pytest tests/test_web_chroma_keyer.py::test_chroma_export_task_writes_default_keyed_png -v`

Expected: `AttributeError` for `create_chroma_keyer_task`.

- [ ] **Step 3: Extend task types, adapters and runner**

Add `CHROMA_KEYER = "chroma_keyer"` to `TaskKind`. Implement `chroma_keyer_options_from_request`, `TaskManager.create_chroma_keyer_task`, and `_run_chroma_keyer`. Mirror `_run_media_resize`: reserve, mark running, call `key_image`, record elapsed seconds, and emit the existing terminal status event. Do not add a second SSE mechanism.

- [ ] **Step 4: Register export route and collision test**

```python
@router.post("/tasks/chroma-keyer", response_model=TaskCreateResponse)
def create_chroma_keyer(request: ChromaKeyerTaskRequest) -> TaskCreateResponse:
    try:
        task = task_manager.create_chroma_keyer_task(request)
    except TaskConflictError as exc:
        raise ApiError("task_conflict", str(exc), 409) from exc
    except KeyerError as exc:
        raise ApiError("invalid_request", str(exc), 400) from exc
    return TaskCreateResponse(task_id=task.task_id, status=task.status)
```

Add a test that an existing output yields the documented conflict response and no file overwrite.

- [ ] **Step 5: Run backend tests**

Run: `uv run pytest tests/test_chroma_keyer_*.py tests/test_web_chroma_keyer.py -v`

Expected: all algorithm, route and task tests pass.

- [ ] **Step 6: Commit the export-task slice**

```powershell
git add src/designtoolkit/web/schemas.py src/designtoolkit/web/adapters.py src/designtoolkit/web/tasks.py src/designtoolkit/web/routes.py tests/test_web_chroma_keyer.py
git commit -m "feat: add chroma key export task"
```

### Task 6: Add Typed Frontend Client and Validation

**Files:**
- Modify: `web/frontend/src/services/apiClient.ts`
- Modify: `web/frontend/src/services/taskTypes.ts`
- Create: `web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts`
- Create: `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.ts`
- Create: `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts`
- Modify: `web/frontend/src/services/apiClient.test.ts`

- [ ] **Step 1: Write failing frontend validation tests**

```typescript
it('requires one sample before preview or export', () => {
  expect(validateChromaKeyerValues(createChromaKeyerDefaultValues())).toEqual({
    samples: '请先在画布上吸取需要抠除的背景颜色',
  })
})

it('rejects clip thresholds in the wrong order', () => {
  expect(validateChromaKeyerValues({
    ...createChromaKeyerDefaultValues(),
    samples: [[42, 180, 72]],
    clipBlack: 0.8,
    clipWhite: 0.8,
  })).toEqual({ clipBlack: 'Clip Black 必须小于 Clip White' })
})
```

- [ ] **Step 2: Run the validation test and verify missing-module failure**

Run: `npm --prefix web/frontend test -- chromaKeyerValidation.test.ts`

Expected: failed module resolution.

- [ ] **Step 3: Add client contracts and validation**

Add `LocalFileKind = 'video' | 'media' | 'image' | 'any'`, then add typed `inspectChromaImage`, `sampleChromaImage`, `createChromaPreview`, and `createChromaKeyerTask` wrappers. Define `ChromaKeyerPayload` with exactly one input path, a non-empty RGB sample list and validated parameters. Keep request errors flowing through the existing `request` helper.

- [ ] **Step 4: Add request-shape tests and run frontend unit tests**

Run: `npm --prefix web/frontend test -- apiClient.test.ts chromaKeyerValidation.test.ts`

Expected: all selected tests pass and assert the exact API paths and JSON bodies.

- [ ] **Step 5: Commit frontend client and validation**

```powershell
git add web/frontend/src/services web/frontend/src/modules/chroma-keyer
git commit -m "feat: add chroma keyer frontend api"
```

### Task 7: Build the Canvas Editor and Integrate Global Task State

**Files:**
- Modify: `web/frontend/src/app/types.ts`
- Modify: `web/frontend/src/app/modules.ts`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.test.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`
- Modify: `web/frontend/src/app/AppShell.test.tsx`
- Modify: `web/frontend/src/styles/app.css`

- [ ] **Step 1: Write a failing canvas coordinate-payload test**

```tsx
it('submits click coordinates relative to the rendered preview', async () => {
  render(<ChromaKeyerCanvas previewUrl="/preview.png" onSample={onSample} samples={[]} />)
  const image = await screen.findByRole('img', { name: '抠像预览' })
  vi.spyOn(image, 'getBoundingClientRect').mockReturnValue({
    left: 10, top: 20, width: 400, height: 200, right: 410, bottom: 220, x: 10, y: 20, toJSON: () => ({}),
  })
  await userEvent.click(image, { clientX: 210, clientY: 120 })
  expect(onSample).toHaveBeenCalledWith({ x: 200, y: 100, previewWidth: 400, previewHeight: 200 })
})
```

- [ ] **Step 2: Run the canvas test and verify missing-component failure**

Run: `npm --prefix web/frontend test -- ChromaKeyerCanvas.test.tsx`

Expected: failed module resolution.

- [ ] **Step 3: Implement the canvas and sample-state UI**

Use a real `<img>` element for the backend PNG response. On click, compute `clientX - rect.left` and `clientY - rect.top`, floor each value, and submit `rect.width`/`rect.height`. Render fixed-size numbered markers with absolute positioning based on returned original coordinates only after converting them to the preview's display ratio. Provide icon buttons with accessible labels for deleting one sample and clearing all samples.

- [ ] **Step 4: Write failing page tests for the local image workflow**

```tsx
it('inspects a selected local image then appends sampled colors', async () => {
  render(<ChromaKeyerPage />)
  await user.type(screen.getByLabelText('输入图片路径'), 'E:\\media\\screen.png')
  await user.click(screen.getByRole('button', { name: '加载图片' }))
  expect(inspectChromaImage).toHaveBeenCalledWith({ input_path: 'E:\\media\\screen.png' })
  await user.click(await screen.findByRole('img', { name: '抠像预览' }))
  await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
})
```

- [ ] **Step 5: Implement the page and form**

The form reuses `PathInput` with `fileKind: 'image'`, one path only, and the existing native picker fallback. It disables preview/export while any existing global task is queued/running or a local request is pending. It must:

1. Inspect the image on explicit `加载图片`.
2. Call sample on canvas click, append the returned RGB triple, and request a debounced Final preview.
3. Allow Final/Alpha mode selection and re-request preview when the mode or valid parameter state changes.
4. Clear sample state and preview URL when the input path changes.
5. Validate before preview/export; submit `createChromaKeyerTask`, then call existing `startTask` with `kind: 'chroma_keyer'`.

- [ ] **Step 6: Add module navigation and editor CSS**

Add `'chroma_keyer'` to `ModuleId` and `TaskKind`, then insert `{ id: 'chroma_keyer', label: 'Studio Chroma Keyer', component: ChromaKeyerPage }` in `toolModules`. Add a responsive two-column editor layout within `.module-surface`; retain the existing app shell and do not nest cards. Use a bounded canvas image area, compact parameter sections, visible sample swatches, and 40px icon controls.

- [ ] **Step 7: Run focused frontend tests**

Run: `npm --prefix web/frontend test -- ChromaKeyerCanvas.test.tsx ChromaKeyerForm.test.tsx ChromaKeyerPage.test.tsx AppShell.test.tsx`

Expected: all selected tests pass, including global task disable behavior.

- [ ] **Step 8: Commit the editor slice**

```powershell
git add web/frontend/src/app web/frontend/src/modules/chroma-keyer web/frontend/src/styles/app.css
git commit -m "feat: add chroma keyer editor"
```

### Task 8: Execute End-to-End Regression and Update Documentation

**Files:**
- Modify: `README.md`
- Modify: `web/frontend/e2e/app.spec.ts`
- Modify: `docs/studio_chroma_keyer_prd.md` only if implementation revealed an approved contract correction.

- [ ] **Step 1: Write an E2E smoke test for module availability**

```typescript
test('opens Studio Chroma Keyer from the navigation', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Studio Chroma Keyer' }).click()
  await expect(page.getByRole('heading', { name: 'Studio Chroma Keyer' })).toBeVisible()
  await expect(page.getByLabel('输入图片路径')).toBeVisible()
})
```

- [ ] **Step 2: Run the E2E test before implementation is complete and verify it catches missing navigation**

Run: `npm --prefix web/frontend run test:e2e -- --grep "Studio Chroma Keyer"`

Expected before Task 7 is complete: failure to find the navigation entry. Expected after Task 7: pass.

- [ ] **Step 3: Document launch and feature boundaries**

Add a `Studio Chroma Keyer` subsection under `README.md` Web GUI. State that it accepts one local PNG/JPG/JPEG image, uses canvas sampling of the actual background color, exports RGBA PNG, and does not yet support video or image sequences.

- [ ] **Step 4: Run all verification commands**

Run:

```powershell
uv run pytest
uv run ruff check .
uv run python -m compileall src tests
npm --prefix web/frontend test
npm --prefix web/frontend run build
npm --prefix web/frontend run test:e2e
```

Expected: all commands exit 0. `pytest` must collect the new chroma tests; `0 items` is a failure, not a passing verification result.

- [ ] **Step 5: Start the GUI and browser-verify the local page**

Run:

```powershell
uv run designtoolkit web --host 127.0.0.1 --port 8765
```

Use a browser to open `http://127.0.0.1:8765/`, select the module, load a local sample image via the folder button, click a background point, switch Final/Alpha preview, and export to a temporary path. Verify output PNG dimensions and alpha histogram using Pillow.

- [ ] **Step 6: Commit documentation and regression coverage**

```powershell
git add README.md web/frontend/e2e/app.spec.ts docs/studio_chroma_keyer_prd.md
git commit -m "docs: document chroma keyer workflow"
```

## Plan Self-Review

- Scope coverage: Tasks 1-3 implement arbitrary-color sampling, alpha, despill and RGBA output. Tasks 4-5 expose inspection, sampling, previews and task export. Tasks 6-7 implement local-path UI and canvas interactions. Task 8 covers documentation and full verification.
- No placeholders: all tests, API paths, model names, commands and integration points are specified.
- Type consistency: `KeyerParams`, `ScreenColorModel`, `ChromaKeyerTaskRequest`, `TaskKind.CHROMA_KEYER`, `ChromaKeyerPage`, and `createChromaKeyerTask` use the same names across tasks.
- Constraint coverage: the plan uses local paths and the native picker, maps clicks from preview to original pixels, avoids a screen-type selector, limits despill to edge pixels, and defers video/sequence support.
