# Chroma Projection Matte Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Augment RGB-distance chroma keying with a sampled-color projection matte that preserves semi-transparent edges and suppresses background spill.

**Architecture:** Keep the public API field names and validation ranges unchanged, while setting the `KeyerParams`, API-schema, and UI defaults to `tolerance = 0.00` and `softness = 1.00`. `matte.py` will calculate continuous foreground confidence from sampled screen chroma projection plus perpendicular residual; `refine_alpha` will keep alpha continuous through morphology; `despill.py` will use a background-contribution mask. `core.py` and preview routes already share the pipeline, so focused unit tests and one route parity regression prove preview/export behavior. The defaults intentionally span the full continuous opacity range; users raise tolerance and lower softness to tighten the key.

**Tech Stack:** Python 3.12, NumPy float32, OpenCV morphology/Gaussian blur, Pillow, FastAPI, pytest.

---

## File Structure

- `src/designtoolkit/chroma_keyer/matte.py`: continuous projection-matte generation and continuous alpha refinement.
- `src/designtoolkit/chroma_keyer/despill.py`: sampled-background contribution mask and dominant-channel despill.
- `tests/test_chroma_keyer_matte.py`: alpha projection and continuous morphology regressions.
- `tests/test_chroma_keyer_despill.py`: green-spill edge and opaque-foreground regressions.
- `tests/test_chroma_keyer_core.py`: source-sized RGBA export regression using a fractional edge.
- `tests/test_web_chroma_keyer.py`: Final/Alpha preview parity against the public keying pipeline.

### Task 1: Generate a Continuous Sampled-Color Projection Matte

**Files:**
- Modify: `src/designtoolkit/chroma_keyer/matte.py:23-41`
- Modify: `tests/test_chroma_keyer_matte.py:7-43`

- [ ] **Step 1: Write failing projection-alpha regressions**

Add these tests after `_green_screen_model`:

```python
def test_generate_alpha_preserves_a_green_contaminated_fractional_edge() -> None:
    rgb = np.array([[[0, 255, 0], [128, 255, 128], [255, 0, 0]]], dtype=np.uint8)

    alpha = generate_alpha(
        rgb,
        _green_screen_model(),
        KeyerParams(tolerance=0.0, softness=1.0),
    )

    assert alpha[0, 0] == 0
    assert 0 < alpha[0, 1] < 255
    assert alpha[0, 2] == 255


def test_generate_alpha_uses_perpendicular_color_residual_as_foreground() -> None:
    rgb = np.array([[[0, 220, 255]]], dtype=np.uint8)

    alpha = generate_alpha(
        rgb,
        _green_screen_model(),
        KeyerParams(tolerance=0.0, softness=1.0),
    )

    assert alpha[0, 0] > 0
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
uv run pytest tests/test_chroma_keyer_matte.py::test_generate_alpha_preserves_a_green_contaminated_fractional_edge tests/test_chroma_keyer_matte.py::test_generate_alpha_uses_perpendicular_color_residual_as_foreground -q
```

Expected: FAIL because the Euclidean-distance implementation makes the mixed green edge opaque or does not expose the specified projection behavior.

- [ ] **Step 3: Add projection confidence to RGB-distance confidence**

In `generate_alpha`, keep RGB validation and normalize pixels and the sampled
mean to `[0, 1]`. Retain normalized RGB-distance confidence as the neutral
fallback. For chromatic sampled colors, center pixels and the sampled mean
around their per-pixel/channel averages, then calculate:

- projection-axis confidence as `1 - projection / screen_chroma`;
- perpendicular residual confidence normalized by pixel and screen chroma;
- a smooth chroma weight that blends projected alpha with RGB-distance alpha.

Map RGB-distance confidence, projection-axis confidence, and residual
confidence independently through the same variance-adjusted `tolerance` and
`softness` smoothstep before combining their alpha values:

```python
variance = float(np.mean(model.stddev_rgb)) / 255.0
lower = float(np.clip(params.tolerance + variance, 0.0, 1.0))
upper = max(lower + 0.01, min(1.0, lower + params.softness))
distance_alpha = remap_confidence(distance_confidence, lower, upper)
axis_alpha = remap_confidence(axis_confidence, lower, upper)
residual_alpha = remap_confidence(residual_confidence, lower, upper)
colored_alpha = np.maximum(axis_alpha, residual_alpha)
alpha = blend_by_screen_chroma(distance_alpha, colored_alpha)
```

Use `np.asarray(..., dtype=np.float32)` for `mean_rgb`. Do not rename API
fields or change their validation ranges. Set the backend and frontend
defaults to `tolerance = 0.00` and `softness = 1.00`.

- [ ] **Step 4: Run matte tests to verify projection behavior passes**

Run:

```bash
uv run pytest tests/test_chroma_keyer_matte.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the projection matte**

```bash
git add src/designtoolkit/chroma_keyer/matte.py tests/test_chroma_keyer_matte.py
git commit -m "feat: project chroma alpha from sampled color"
```

### Task 2: Preserve Fractional Alpha Through Morphology

**Files:**
- Modify: `src/designtoolkit/chroma_keyer/matte.py:43-67`
- Modify: `tests/test_chroma_keyer_matte.py`

- [ ] **Step 1: Write failing continuous-morphology tests**

Add:

```python
@mark.parametrize("shrink_grow", [-1, 1])
def test_refine_alpha_morphology_keeps_a_fractional_edge(shrink_grow: int) -> None:
    alpha = np.array(
        [[0, 0, 0], [0, 128, 255], [0, 0, 0]],
        dtype=np.uint8,
    )

    result = refine_alpha(alpha, KeyerParams(shrink_grow=shrink_grow))

    assert result.dtype == np.uint8
    assert 0 < result[1, 1] < 255
```

- [ ] **Step 2: Run the regression to verify it fails**

Run:

```bash
uv run pytest tests/test_chroma_keyer_matte.py::test_refine_alpha_morphology_keeps_a_fractional_edge -q
```

Expected: FAIL because the current `refined >= 0.5` conversion replaces the
center pixel with a binary-morphology result.

- [ ] **Step 3: Apply morphology directly to float alpha**

Replace the binary-mask block in `refine_alpha` with:

```python
if params.shrink_grow:
    kernel_size = 2 * abs(params.shrink_grow) + 1
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
    )
    operation = cv2.dilate if params.shrink_grow > 0 else cv2.erode
    transformed = operation(refined, kernel)
    refined = (refined + transformed) / 2.0
```

Keep the existing Gaussian blur and final uint8 conversion.

- [ ] **Step 4: Run all matte tests**

Run:

```bash
uv run pytest tests/test_chroma_keyer_matte.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit continuous morphology**

```bash
git add src/designtoolkit/chroma_keyer/matte.py tests/test_chroma_keyer_matte.py
git commit -m "fix: preserve fractional chroma edges"
```

### Task 3: Base Despill on Background Contribution

**Files:**
- Modify: `src/designtoolkit/chroma_keyer/despill.py:46-74`
- Modify: `tests/test_chroma_keyer_despill.py`

- [ ] **Step 1: Write failing edge-spill regressions**

Add:

```python
def test_despill_strongly_reduces_green_on_low_alpha_edge() -> None:
    rgb = np.array([[[180, 235, 180]]], dtype=np.uint8)
    alpha = np.array([[48]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((10.0, 200.0, 10.0)), amount=1.0)

    assert result[0, 0, 1] <= 185
    assert result[0, 0, 0] == 180
    assert result[0, 0, 2] == 180


def test_despill_keeps_opaque_foreground_byte_identical() -> None:
    rgb = np.array([[[220, 225, 210]]], dtype=np.uint8)

    result = despill(
        rgb,
        np.array([[255]], dtype=np.uint8),
        _model((10.0, 200.0, 10.0)),
        amount=1.0,
    )

    assert np.array_equal(result, rgb)
```

- [ ] **Step 2: Run the focused tests to verify the low-alpha edge test fails**

Run:

```bash
uv run pytest tests/test_chroma_keyer_despill.py::test_despill_strongly_reduces_green_on_low_alpha_edge tests/test_chroma_keyer_despill.py::test_despill_keeps_opaque_foreground_byte_identical -q
```

Expected: FAIL because `alpha * (1 - alpha)` applies only about 0.15 strength
at alpha 48.

- [ ] **Step 3: Replace the symmetric edge mask**

In `despill`, replace:

```python
effect = alpha_float * (1.0 - alpha_float) * amount
```

with:

```python
background_contribution = np.clip(1.0 - alpha_float, 0.0, 1.0)
effect = background_contribution * amount
```

Keep the dominant-channel target calculation, neutral fallback, clipping, and
opaque-alpha behavior unchanged.

- [ ] **Step 4: Run all despill tests**

Run:

```bash
uv run pytest tests/test_chroma_keyer_despill.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the spill-mask change**

```bash
git add src/designtoolkit/chroma_keyer/despill.py tests/test_chroma_keyer_despill.py
git commit -m "fix: suppress spill from fractional chroma edges"
```

### Task 4: Prove End-to-End RGBA and Preview Parity

**Files:**
- Modify: `tests/test_chroma_keyer_core.py`
- Modify: `tests/test_web_chroma_keyer.py:1090-1147`

- [ ] **Step 1: Write a failing exported-edge regression**

Add to `tests/test_chroma_keyer_core.py`:

```python
def test_key_image_preserves_fractional_edge_alpha_and_despills_green(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "screen.png"
    output_path = tmp_path / "result.png"
    _save_rgb(
        input_path,
        np.array([[[0, 255, 0], [180, 235, 180], [255, 0, 0]]], dtype=np.uint8),
    )

    key_image(
        KeyImageOptions(input_path=input_path, output_path=output_path),
        _green_screen_model(),
        KeyerParams(tolerance=0.0, softness=1.0, despill_amount=1.0),
    )

    with Image.open(output_path) as image:
        pixels = np.asarray(image.convert("RGBA"))
    assert pixels[0, 0, 3] == 0
    assert 0 < pixels[0, 1, 3] < 255
    assert pixels[0, 1, 1] < 235
    assert pixels[0, 2, 3] == 255
```

Extend `test_final_and_alpha_previews_match_the_public_keying_pipeline` in
`tests/test_web_chroma_keyer.py` with a mixed green edge in `source_rgb` and:

```python
assert 0 < final_rgba[0, 1, 3] < 255
assert final_rgba[0, 1, 1] < source_rgb[0, 1, 1]
```

- [ ] **Step 2: Run the end-to-end regressions**

Run:

```bash
uv run pytest tests/test_chroma_keyer_core.py::test_key_image_preserves_fractional_edge_alpha_and_despills_green tests/test_web_chroma_keyer.py::test_final_and_alpha_previews_match_the_public_keying_pipeline -q
```

Expected: PASS after Tasks 1-3. If either fails, correct only the relevant
matte/despill implementation before changing these assertions.

- [ ] **Step 3: Run backend regression suite**

Run:

```bash
uv run pytest tests -q
uv run ruff check .
uv run python -m compileall src tests
```

Expected: all tests pass; Ruff prints `All checks passed!`; compileall exits 0.

- [ ] **Step 4: Run frontend regression and build checks**

Run:

```bash
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
npm --prefix web/frontend run test:e2e
```

Expected: all Vitest and Playwright tests pass; lint and build exit 0.

- [ ] **Step 5: Commit end-to-end proof**

```bash
git add tests/test_chroma_keyer_core.py tests/test_web_chroma_keyer.py
git commit -m "test: cover fractional chroma edge output"
```

### Task 5: Rebuild and Manually Validate the Local GUI

**Files:**
- Modify: none

- [ ] **Step 1: Build current frontend static assets**

Run:

```bash
npm --prefix web/frontend run build
```

Expected: Vite exits 0 and writes `web/frontend/dist`.

- [ ] **Step 2: Restart the local web server**

Run:

```powershell
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listener) { $listener | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force } }
uv run designtoolkit web --host 127.0.0.1 --port 8765
```

Expected: Uvicorn listens on `http://127.0.0.1:8765`.

- [ ] **Step 3: Validate the supplied style of asset**

In Studio Chroma Keyer:

1. load a green-screen image containing a light/translucent decorative edge;
2. sample only the green background, using multiple points if it varies;
3. inspect Alpha mode for gray edge pixels rather than a binary contour;
4. inspect Final mode for reduced green fringing on the same edge;
5. export to a new PNG and verify the exported file is RGBA with nonzero,
   non-255 alpha histogram bins.

- [ ] **Step 4: Record manual validation outcome**

Record the input path, sampled RGB values, selected parameters, output path,
and alpha histogram result in the implementation handoff. Do not commit
generated user image files.
