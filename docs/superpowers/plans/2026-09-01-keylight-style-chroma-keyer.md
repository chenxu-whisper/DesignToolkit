# Keylight-Style Chroma Keyer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace projection-matte Tolerance/Softness controls and additive sampling with a single-screen-color, Screen Gain/Balance color-difference keyer.

**Architecture:** Keep the existing `samples` wire field but enforce exactly one RGB sample. Build the matte from the sampled color's dominant-channel saturation ratio, then reuse the existing continuous Clip, Shrink/Grow, Feather, sampled-background RGB recovery, shared preview, and export pipeline.

**Tech Stack:** Python 3.12, NumPy, OpenCV, FastAPI/Pydantic, React, TypeScript, Vitest, pytest, Playwright.

---

### Task 1: Replace Matte Parameters And Implement Color-Difference Alpha

**Files:**
- Modify: `src/designtoolkit/chroma_keyer/models.py`
- Modify: `src/designtoolkit/chroma_keyer/matte.py`
- Modify: `tests/test_chroma_keyer_models.py`
- Modify: `tests/test_chroma_keyer_matte.py`

- [ ] **Step 1: Write failing parameter-contract tests**

Replace Tolerance/Softness expectations with:

```python
def test_params_accept_keylight_style_defaults() -> None:
    assert KeyerParams() == KeyerParams(
        screen_gain=1.0,
        screen_balance=0.5,
        clip_black=0.0,
        clip_white=1.0,
        shrink_grow=0,
        feather=0,
        despill_amount=1.0,
    )
```

Add finite range cases proving `screen_gain` accepts `0..2`,
`screen_balance` accepts `0..1`, and values outside those ranges fail.

- [ ] **Step 2: Write failing matte tests**

Use green, blue, and red screen models. Assert:

```python
green = np.array(
    [[[0, 255, 0], [64, 255, 64], [128, 255, 128],
      [191, 255, 191], [255, 255, 255]]],
    dtype=np.uint8,
)
assert generate_alpha(green, green_model, KeyerParams()).tolist() == [
    [0, 64, 128, 191, 255]
]
```

Add tests showing `screen_gain=2` lowers a fractional alpha relative to gain
`1`, and balance `0`/`1` select the stronger/weaker alternate component.

- [ ] **Step 3: Run RED tests**

Run:

```bash
uv run pytest tests/test_chroma_keyer_models.py tests/test_chroma_keyer_matte.py -q
```

Expected: failures because `screen_gain` and `screen_balance` do not exist and
the projection matte does not implement the saturation-ratio formula.

- [ ] **Step 4: Implement parameter validation**

Define:

```python
@dataclass(frozen=True)
class KeyerParams:
    screen_gain: float = 1.0
    screen_balance: float = 0.5
    clip_black: float = 0.0
    clip_white: float = 1.0
    shrink_grow: int = 0
    feather: int = 0
    despill_amount: float = 1.0
```

Validate gain as a finite real in `[0, 2]`, balance as a unit-interval float,
and remove Tolerance/Softness.

- [ ] **Step 5: Implement the color-difference matte**

Use:

```python
dominant = int(np.argmax(screen))
other = [channel for channel in range(3) if channel != dominant]

def balanced_other(values: np.ndarray) -> np.ndarray:
    high = np.max(values[..., other], axis=-1)
    low = np.min(values[..., other], axis=-1)
    return (1.0 - balance) * high + balance * low

screen_saturation = screen[dominant] - balanced_other(screen)
pixel_saturation = rgb_float[..., dominant] - balanced_other(rgb_float)
coverage = np.clip(gain * pixel_saturation / screen_saturation, 0.0, 1.0)
alpha = 1.0 - coverage
```

Do not alter `refine_alpha`.

- [ ] **Step 6: Run GREEN tests and commit**

```bash
uv run pytest tests/test_chroma_keyer_models.py tests/test_chroma_keyer_matte.py -q
uv run ruff check src/designtoolkit/chroma_keyer tests/test_chroma_keyer_models.py tests/test_chroma_keyer_matte.py
git add src/designtoolkit/chroma_keyer/models.py src/designtoolkit/chroma_keyer/matte.py tests/test_chroma_keyer_models.py tests/test_chroma_keyer_matte.py
git commit -m "feat: add keylight-style screen matte"
```

### Task 2: Enforce One Valid Screen Color In Backend Requests

**Files:**
- Modify: `src/designtoolkit/chroma_keyer/sampling.py`
- Modify: `src/designtoolkit/web/schemas.py`
- Modify: `src/designtoolkit/web/adapters.py`
- Modify: `src/designtoolkit/web/routes.py`
- Modify: `tests/test_chroma_keyer_sampling.py`
- Modify: `tests/test_web_chroma_keyer.py`

- [ ] **Step 1: Write failing single-color and validation tests**

Add tests proving:

```python
with raises(KeyerError, match="请选择具有明显主色的纯色幕背景"):
    screen_model_from_samples(((128, 128, 128),))

with raises(KeyerError, match="exactly one"):
    screen_model_from_samples(((0, 255, 0), (0, 240, 0)))
```

Task and preview schemas must reject zero or multiple samples. The sample route
must reject a gray sampled pixel using the structured `invalid_screen_color`
error and accept a saturated green pixel.

- [ ] **Step 2: Write failing schema and adapter tests**

Replace `tolerance`/`softness` payload fields with:

```json
{
  "screen_gain": 1.0,
  "screen_balance": 0.5
}
```

Assert removed fields are rejected as extras, gain above `2` fails, and adapter
output is `KeyerParams(screen_gain=..., screen_balance=...)`.

- [ ] **Step 3: Run RED tests**

```bash
uv run pytest tests/test_chroma_keyer_sampling.py tests/test_web_chroma_keyer.py -q
```

Expected: exact-one, low-saturation, and new parameter assertions fail.

- [ ] **Step 4: Implement one-color validation and API mapping**

`screen_model_from_samples` must validate one sample, calculate the dominant
channel, and reject saturation below `0.05 * 255`. On success return a model
whose mean equals the sample and standard deviation is `(0, 0, 0)`.

Set schema fields to:

```python
screen_gain: Annotated[float, Field(strict=True, ge=0, le=2, allow_inf_nan=False)] = 1.0
screen_balance: UnitIntervalFloat = 0.5
samples: list[list[RgbChannel]] = Field(min_length=1, max_length=1)
```

Update adapters and route error mapping. Validate the sampled pixel before
returning it so an invalid replacement never reaches frontend state.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
uv run pytest tests/test_chroma_keyer_sampling.py tests/test_web_chroma_keyer.py -q
uv run ruff check src/designtoolkit tests/test_chroma_keyer_sampling.py tests/test_web_chroma_keyer.py
git add src/designtoolkit/chroma_keyer/sampling.py src/designtoolkit/web/schemas.py src/designtoolkit/web/adapters.py src/designtoolkit/web/routes.py tests/test_chroma_keyer_sampling.py tests/test_web_chroma_keyer.py
git commit -m "feat: enforce one chroma screen color"
```

### Task 3: Replace Frontend Controls And Sampling Interaction

**Files:**
- Modify: `web/frontend/src/services/apiClient.ts`
- Modify: `web/frontend/src/services/apiClient.test.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.test.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`

- [ ] **Step 1: Write failing frontend contract tests**

Assert defaults include `screenGain: 1`, `screenBalance: 0.5`, and omit
Tolerance/Softness. Assert gain accepts `0..2`, balance accepts `0..1`, and the
wire payload uses `screen_gain`/`screen_balance` only.

- [ ] **Step 2: Write failing interaction tests**

After inspect and first valid sample, click a second location and assert:

```typescript
expect(values.samples).toEqual([[secondRed, secondGreen, secondBlue]])
expect(screen.queryByText('#1')).not.toBeInTheDocument()
expect(screen.getAllByRole('button', { name: '删除 Screen Color' })).toHaveLength(1)
```

Make the second sample request reject with
`请选择具有明显主色的纯色幕背景`; assert the first color remains visible and the
error appears.

- [ ] **Step 3: Run RED tests**

```bash
npm --prefix web/frontend test -- chromaKeyerValidation.test.ts ChromaKeyerForm.test.tsx ChromaKeyerPage.test.tsx apiClient.test.ts
```

Expected: new field, wire payload, replacement, and invalid-replacement
assertions fail.

- [ ] **Step 4: Implement frontend state and UI**

Replace fields in `ChromaKeyerValues`, defaults, validation, form controls, and
API serialization. In `handleSample`, replace arrays instead of appending:

```typescript
setValues((current) => ({ ...current, samples: [[...response.rgb]] }))
setSampleDetails([{ ...response metadata... }])
```

Render one Screen Color row without ordinal numbering. Keep the previous state
when sampling rejects. Ensure preview-effect dependencies use Screen Gain and
Screen Balance.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
git add web/frontend/src/services/apiClient.ts web/frontend/src/services/apiClient.test.ts web/frontend/src/modules/chroma-keyer
git commit -m "feat: add single-color keylight controls"
```

### Task 4: Real-Asset Regression And GUI Verification

**Files:**
- Modify: `tests/test_chroma_keyer_core.py`
- Modify: `tests/test_web_chroma_keyer.py`
- Modify: `docs/studio_chroma_keyer_prd.md`
- Read: `assets/frame/test_frame_2.png`

- [ ] **Step 1: Add the real-asset regression**

Read `assets/frame/test_frame_2.png`, sample `(10, 10)`, and call `key_rgb` with
defaults. Assert:

```python
fractional = (alpha > 0) & (alpha < 255)
spill = keyed[..., 1].astype(np.int16) - np.maximum(
    keyed[..., 0], keyed[..., 2]
).astype(np.int16)

assert np.count_nonzero(fractional) > 100_000
assert np.count_nonzero((spill > 0) & fractional) < 50_000
assert alpha.shape == rgb.shape[:2]
```

Also retain exact preview/export shared-pipeline assertions using the new
parameter names.

- [ ] **Step 2: Update the PRD**

Document one-color replacement sampling, Screen Gain `0..2`, Screen Balance
`0..1`, low-saturation rejection, and removal of Tolerance/Softness.

- [ ] **Step 3: Run full automated verification**

```bash
uv run pytest tests -q
uv run ruff check .
uv run python -m compileall src tests
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 4: Commit regression and documentation**

```bash
git add tests/test_chroma_keyer_core.py tests/test_web_chroma_keyer.py docs/studio_chroma_keyer_prd.md
git commit -m "test: validate keylight matte on production artwork"
```

- [ ] **Step 5: Verify the running GUI**

Start the server on port `8765`, load `assets/frame/test_frame_2.png`, sample a
clean green background pixel, and verify:

1. a second click replaces the first Screen Color;
2. gray sampling is rejected while preserving the previous color;
3. Screen Gain and Screen Balance update Final and Alpha previews;
4. title and wing edges have substantially less green contamination;
5. the Alpha view retains gray edge pixels;
6. export produces a source-sized RGBA PNG.
