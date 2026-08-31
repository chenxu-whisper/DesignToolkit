# Chroma Projection Matte Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace dominant-channel despill with sampled-background RGB unmixing while preserving the existing continuous projection matte.

**Architecture:** Leave the current projection matte and continuous refinement unchanged. `despill.py` solves `C = aF + (1-a)B` with the sampled RGB mean before blending from `C` to recovered `F`; preview and export continue through the shared `key_rgb` pipeline. Backend, API-schema, and frontend `despill_amount` defaults become `1.00`.

**Tech Stack:** Python 3.12, NumPy float32, OpenCV, Pillow, FastAPI/Pydantic, pytest, React, TypeScript, Vitest, Playwright.

---

## File Structure

- `src/designtoolkit/chroma_keyer/despill.py`: sampled-background RGB unmixing for fractional alpha.
- `src/designtoolkit/chroma_keyer/models.py`: backend `KeyerParams` defaults.
- `src/designtoolkit/web/schemas.py`: request-schema defaults.
- `web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts`: form defaults.
- `tests/test_chroma_keyer_despill.py`: synthetic recovery, endpoint, and amount regressions.
- `tests/test_chroma_keyer_models.py`: backend default regression.
- `tests/test_chroma_keyer_core.py`: real asset and shared-pipeline regressions.
- `tests/test_web_chroma_keyer.py`: schema defaults and preview parity.
- `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts`: frontend default regression.
- `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`: default export payload regression.
- `assets/frame/test_frame_5.png`: existing real green-background asset used read-only by the quantitative regression.

### Task 1: Recover Foreground RGB by Sampled-Background Unmixing

**Files:**
- Modify: `src/designtoolkit/chroma_keyer/despill.py`
- Modify: `tests/test_chroma_keyer_despill.py`

- [ ] **Step 1: Replace dominant-channel tests with synthetic recovery tests**

Use a pure green sampled background and white foreground. The listed observed
pixels are exact uint8 composites at alpha bytes `64`, `128`, and `191`.

```python
def test_despill_recovers_white_composited_over_green_at_fractional_alpha() -> None:
    rgb = np.array(
        [[[64, 255, 64], [128, 255, 128], [191, 255, 191]]],
        dtype=np.uint8,
    )
    alpha = np.array([[64, 128, 191]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((0.0, 255.0, 0.0)), amount=1.0)

    np.testing.assert_allclose(
        result,
        np.full_like(rgb, 255),
        atol=1,
    )


def test_despill_preserves_transparent_and_opaque_rgb_byte_identical() -> None:
    rgb = np.array([[[17, 231, 49], [203, 7, 119]]], dtype=np.uint8)
    alpha = np.array([[0, 255]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((0.0, 255.0, 0.0)), amount=1.0)

    assert np.array_equal(result, rgb)


@mark.parametrize(
    ("amount", "expected"),
    [
        (0.0, [128, 255, 128]),
        (0.5, [192, 255, 192]),
        (1.0, [255, 255, 255]),
    ],
)
def test_despill_amount_linearly_blends_observed_and_recovered_rgb(
    amount: float,
    expected: list[int],
) -> None:
    rgb = np.array([[[128, 255, 128]]], dtype=np.uint8)
    alpha = np.array([[128]], dtype=np.uint8)

    result = despill(rgb, alpha, _model((0.0, 255.0, 0.0)), amount=amount)

    assert result[0, 0].tolist() == expected
```

Retain the invalid-input tests. Delete tests that assert only one dominant
channel changes or that neutral samples move toward luminance; those contracts
are intentionally removed.

- [ ] **Step 2: Run the new tests and verify failure**

```bash
uv run pytest tests/test_chroma_keyer_despill.py -q
```

Expected: synthetic white recovery and old branch-contract replacements fail
against dominant-channel despill.

- [ ] **Step 3: Implement the exact unmixing equation**

Remove `_dominant_screen_channel`. After existing validation, use:

```python
if amount == 0:
    return rgb.copy()

rgb_float = rgb.astype(np.float32)
alpha_float = alpha.astype(np.float32) / np.float32(255.0)
background = np.asarray(model.mean_rgb, dtype=np.float32)
denominator = np.maximum(alpha_float[..., None], np.float32(1.0 / 255.0))
recovered = np.clip(
    (
        rgb_float
        - (np.float32(1.0) - alpha_float[..., None]) * background
    )
    / denominator,
    0.0,
    255.0,
)
edge = (alpha > 0) & (alpha < 255)
result = rgb_float.copy()
result[edge] = (
    rgb_float[edge]
    + np.float32(amount) * (recovered[edge] - rgb_float[edge])
)
return np.rint(np.clip(result, 0.0, 255.0)).astype(np.uint8)
```

The edge assignment is mandatory: it preserves alpha `0` and `255` RGB bytes
without relying on floating-point cancellation.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_chroma_keyer_despill.py -q
git add src/designtoolkit/chroma_keyer/despill.py tests/test_chroma_keyer_despill.py
git commit -m "feat: recover foreground from sampled background"
```

Expected: all despill tests pass before the commit.

### Task 2: Set Despill Default to 1.0 Across All Layers

**Files:**
- Modify: `src/designtoolkit/chroma_keyer/models.py`
- Modify: `src/designtoolkit/web/schemas.py`
- Modify: `tests/test_chroma_keyer_models.py`
- Modify: `tests/test_web_chroma_keyer.py`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`

- [ ] **Step 1: Change default expectations first**

Change these assertions from `0.5` to `1.0`:

- `test_params_accept_default_mvp_values` in
  `tests/test_chroma_keyer_models.py`;
- `test_chroma_keyer_task_request_accepts_the_default_export_contract` in
  `tests/test_web_chroma_keyer.py`;
- `chromaKeyerDefaultValues` in
  `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts`;
- the default export payload assertion in
  `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`.

- [ ] **Step 2: Verify the default tests fail**

```bash
uv run pytest tests/test_chroma_keyer_models.py::test_params_accept_default_mvp_values tests/test_web_chroma_keyer.py::test_chroma_keyer_task_request_accepts_the_default_export_contract -q
npm --prefix web/frontend test -- chromaKeyerValidation.test.ts ChromaKeyerPage.test.tsx
```

Expected: backend, schema, and frontend expectations report actual `0.5`.

- [ ] **Step 3: Change all three defaults**

Set:

```python
# src/designtoolkit/chroma_keyer/models.py
despill_amount: float = 1.0

# src/designtoolkit/web/schemas.py
despill_amount: UnitIntervalFloat = 1.0
```

Set `despillAmount: 1` in
`web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts`. Do not change
the `0..1` validation range or explicit non-default payload tests.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_chroma_keyer_models.py tests/test_web_chroma_keyer.py -q
npm --prefix web/frontend test -- chromaKeyerValidation.test.ts ChromaKeyerPage.test.tsx
git add src/designtoolkit/chroma_keyer/models.py src/designtoolkit/web/schemas.py tests/test_chroma_keyer_models.py tests/test_web_chroma_keyer.py web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
git commit -m "feat: enable full sampled-color recovery by default"
```

Expected: focused backend and frontend tests pass before the commit.

### Task 3: Add Real-Asset Quantitative and Pipeline Regressions

**Files:**
- Modify: `tests/test_chroma_keyer_core.py`
- Modify: `tests/test_web_chroma_keyer.py`
- Read fixture: `assets/frame/test_frame_5.png`

- [ ] **Step 1: Add a quantitative regression for the existing asset**

Add a test that reads `assets/frame/test_frame_5.png`, samples the four
background corners at `(10, 10)`, `(741, 10)`, `(10, 549)`, and `(741, 549)`,
then runs `key_rgb` with default `KeyerParams`.

```python
def test_key_rgb_reduces_sampled_background_contribution_on_real_asset() -> None:
    asset = Path(__file__).parents[1] / "assets/frame/test_frame_5.png"
    rgb = read_rgb_image(asset)
    sample_xy = ((10, 10), (741, 10), (10, 549), (741, 549))
    samples = [[int(channel) for channel in rgb[y, x]] for x, y in sample_xy]
    model = screen_model_from_samples(samples)

    keyed_rgb, alpha = key_rgb(rgb, model, KeyerParams())

    fractional = (alpha > 0) & (alpha < 255)
    assert np.count_nonzero(fractional) >= 100
    background = np.asarray(model.mean_rgb, dtype=np.float32)
    observed_distance = np.linalg.norm(
        rgb.astype(np.float32) - background,
        axis=2,
    )
    recovered_distance = np.linalg.norm(
        keyed_rgb.astype(np.float32) - background,
        axis=2,
    )
    assert recovered_distance[fractional].mean() > observed_distance[fractional].mean()
    assert np.array_equal(keyed_rgb[alpha == 0], rgb[alpha == 0])
    assert np.array_equal(keyed_rgb[alpha == 255], rgb[alpha == 255])
```

Import `key_rgb`, `read_rgb_image`, and `screen_model_from_samples` from their
existing public modules. This test uses the committed asset directly; do not
add or regenerate an image fixture.

- [ ] **Step 2: Update preview parity for RGB recovery**

In `test_final_and_alpha_previews_match_the_public_keying_pipeline`, retain the
shared-pipeline equality assertions, change the fractional synthetic source
pixel to `[128, 255, 128]`, and require its recovered RGB to equal
`[255, 255, 255]` within one uint8 value when `despill_amount = 1.0`:

```python
np.testing.assert_allclose(
    final_rgba[0, 1, :3],
    np.array([255, 255, 255], dtype=np.uint8),
    atol=1,
)
```

- [ ] **Step 3: Run focused integration tests**

```bash
uv run pytest tests/test_chroma_keyer_core.py::test_key_rgb_reduces_sampled_background_contribution_on_real_asset tests/test_web_chroma_keyer.py::test_final_and_alpha_previews_match_the_public_keying_pipeline -q
```

Expected: PASS. The real asset must contain at least 100 fractional pixels,
increase mean RGB distance from the sampled screen color over that population,
and preserve endpoint RGB.

- [ ] **Step 4: Commit**

```bash
git add tests/test_chroma_keyer_core.py tests/test_web_chroma_keyer.py
git commit -m "test: quantify sampled-color recovery"
```

### Task 4: Full Automated and Browser Verification

**Files:**
- Modify: none

- [ ] **Step 1: Run complete backend verification**

```bash
uv run pytest tests -q
uv run ruff check .
uv run python -m compileall src tests
```

Expected: pytest reports no failures, Ruff prints `All checks passed!`, and
compileall exits `0`.

- [ ] **Step 2: Run complete frontend verification**

```bash
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
npm --prefix web/frontend run test:e2e
```

Expected: Vitest and Playwright report no failures; lint and build exit `0`.

- [ ] **Step 3: Start the local application**

```powershell
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $listener | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
}
uv run designtoolkit web --host 127.0.0.1 --port 8765
```

Expected: Uvicorn listens on `http://127.0.0.1:8765`.

- [ ] **Step 4: Verify the browser workflow with the real asset**

Open Studio Chroma Keyer, load the absolute path to
`assets/frame/test_frame_5.png`, sample the four corner background points,
and verify:

1. the initial Despill Amount control reads `1.00`;
2. Final and Alpha previews both load;
3. lowering Despill Amount to `0.00` leaves Final-preview RGB unchanged while
   Alpha preview remains unchanged;
4. restoring `1.00` visibly removes more green from fractional orange/white
   edges without changing fully transparent or opaque RGB;
5. export writes a source-sized RGBA PNG to a new path; and
6. the exported alpha histogram contains values in `1..254`.

Record the four sampled RGB values, fractional-pixel count, mean observed and
recovered distances from Task 5, output path, and nonzero fractional-alpha
histogram count in the implementation handoff. Do not commit generated output.

- [ ] **Step 5: Confirm repository scope**

```bash
git status --short
git diff --check
```

Expected: no generated browser/export artifacts are tracked, and the diff has
no whitespace errors.

## Self-Review

- Spec coverage: exact RGB unmixing, alpha endpoint preservation, amount
  endpoints/linearity, cross-layer defaults, real-asset quantitative coverage,
  preview/export parity, and browser verification each have an implementation
  task. Existing projection-matte and continuous-refinement tests remain in
  the full regression suite.
- Type consistency: `despill_amount` remains a Python/API unit-interval float
  and maps to frontend `despillAmount`; `despill` keeps its current signature.
- Scope: no new controls, API fields, dependencies, trimap, AI matting, video
  behavior, or output format is introduced.
- Limitation: true foreground colors near the sampled screen color may shift;
  the existing slider is the mitigation.
