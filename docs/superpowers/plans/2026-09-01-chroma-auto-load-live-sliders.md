# Chroma Auto-Load And Live Sliders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically inspect committed image paths and provide responsive slider-based chroma controls whose latest valid values update the left preview.

**Architecture:** Extend the shared `PathInput` with an explicit commit event so text edits and usable path submissions remain distinct. Keep keying on the bounded FastAPI preview endpoint, debounce parameter changes by 150 ms, and let sequence tokens discard stale responses without locking the controls.

**Tech Stack:** React, TypeScript, Testing Library, Vitest, FastAPI preview API, CSS, Playwright.

---

### Task 1: Add A Reusable Path Commit Contract

**Files:**
- Modify: `web/frontend/src/components/PathInput.tsx`
- Modify: `web/frontend/src/components/PathInput.test.tsx`

- [ ] **Step 1: Write failing commit-event tests**

Render a controlled single-line `PathInput` with `onCommit={onCommit}` and prove:

```typescript
await user.type(input, 'D:\\art\\screen.png')
expect(onCommit).not.toHaveBeenCalled()

await user.keyboard('{Enter}')
expect(onCommit).toHaveBeenLastCalledWith('D:\\art\\screen.png')
```

Add independent tests for blur, native picker, resolved drop, and path-editor
apply. Assert multiline Enter does not commit.

- [ ] **Step 2: Run RED tests**

```bash
npm --prefix web/frontend test -- PathInput.test.tsx
```

Expected: TypeScript/test failures because `onCommit` does not exist.

- [ ] **Step 3: Implement commit sources**

Add:

```typescript
onCommit?: (value: string) => void
```

Use a helper that calls `onChange(nextValue)` and then `onCommit?.(nextValue)`
for picker, drop, and editor apply. On the single-line input, call `onCommit`
for Enter and blur without changing multiline behavior.

- [ ] **Step 4: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test -- PathInput.test.tsx
npm --prefix web/frontend run lint
git add web/frontend/src/components/PathInput.tsx web/frontend/src/components/PathInput.test.tsx
git commit -m "feat: add committed path events"
```

### Task 2: Replace Manual Image Loading With Automatic Inspection

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.test.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`

- [ ] **Step 1: Write failing form tests**

Replace `onInspect` with:

```typescript
onInputPathCommit: (path: string) => void
```

Assert the `加载图片` button is absent and a picker result both updates form
values and calls `onInputPathCommit` with the selected path.

- [ ] **Step 2: Write failing page workflow tests**

Add tests proving:

```typescript
await user.click(screen.getByRole('button', { name: '指定输入图片路径' }))
await waitFor(() =>
  expect(inspectChromaImage).toHaveBeenCalledWith({
    inputPath: 'E:\\media\\screen.png',
  }),
)
expect(screen.queryByRole('button', { name: '加载图片' })).not.toBeInTheDocument()
```

Also prove manual typing does not inspect before Enter/blur, Enter plus blur
does not duplicate inspection, same loaded path is a no-op, and a slower old
inspection cannot replace a newer committed path.

- [ ] **Step 3: Run RED tests**

```bash
npm --prefix web/frontend test -- ChromaKeyerForm.test.tsx ChromaKeyerPage.test.tsx
```

Expected: failures for the old button and missing commit handler.

- [ ] **Step 4: Implement automatic inspection**

Remove `onInspect` and the button. Pass `onCommit` from the image `PathInput`.
Change the page handler to accept the committed string directly:

```typescript
const handleInspect = async (committedPath: string) => {
  const inputPath = committedPath.trim()
  if (!inputPath) {
    setErrors((current) => ({ ...current, inputPath: '请输入图片路径' }))
    return
  }
  if (image?.inputPath === inputPath || inspectingPathRef.current === inputPath) {
    return
  }
  // sequence-guarded inspect using inputPath
}
```

Track the currently inspecting path in a ref and clear it only for its matching
request. Preserve the current path-change invalidation and latest-response
guard.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test -- ChromaKeyerForm.test.tsx ChromaKeyerPage.test.tsx PathInput.test.tsx
npm --prefix web/frontend run lint
git add web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.test.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
git commit -m "feat: auto-load committed chroma images"
```

### Task 3: Render Synchronized Parameter Sliders

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.test.tsx`
- Modify: `web/frontend/src/styles/app.css`

- [ ] **Step 1: Write failing slider tests**

For every parameter, assert one range input and one numeric input. Example:

```typescript
expect(screen.getByRole('slider', { name: 'Screen Gain' })).toHaveValue('1')
expect(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' })).toHaveValue(1)
```

Move the Screen Gain slider to `1.25` and assert `onChange` receives
`screenGain: 1.25`. Change the numeric value to `1.4` and assert the range value
becomes `1.4` after rerender through a controlled form harness.

- [ ] **Step 2: Run RED tests**

```bash
npm --prefix web/frontend test -- ChromaKeyerForm.test.tsx
```

Expected: no elements with slider roles.

- [ ] **Step 3: Implement the slider/value pairs**

For each `parameterFields` item, render:

```tsx
<input
  aria-label={field.label}
  className="chroma-parameter-slider"
  min={field.min}
  max={field.max}
  step={field.step}
  type="range"
  value={safeSliderValue}
  onChange={(event) => updateValue(field.id, Number(event.target.value))}
/>
<input
  aria-label={`${field.label} 数值`}
  className="text-input chroma-parameter-value"
  min={field.min}
  max={field.max}
  step={field.step}
  type="number"
  value={numberValue}
/>
```

Keep `NaN` representable in the numeric field while the range uses a bounded
fallback. Add stable grid tracks, focus styles, disabled styles, and responsive
single-column behavior without nested cards.

- [ ] **Step 4: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test -- ChromaKeyerForm.test.tsx
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
git add web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.test.tsx web/frontend/src/styles/app.css
git commit -m "feat: add chroma parameter sliders"
```

### Task 4: Keep Live Preview Responsive And Latest-Wins

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`
- Modify: `docs/studio_chroma_keyer_prd.md`

- [ ] **Step 1: Write failing live-preview tests**

Use fake timers or deferred promises to prove:

1. a slider change waits 150 ms before calling `createChromaPreview`;
2. the slider remains enabled while that promise is unresolved;
3. a later request resolving first wins, and the older response is ignored;
4. `Clip Black >= Clip White` sends no request;
5. changing Final/Alpha sends immediately.

- [ ] **Step 2: Run RED tests**

```bash
npm --prefix web/frontend test -- ChromaKeyerPage.test.tsx
```

Expected: the current 250 ms delay and shared pending lock fail the assertions.

- [ ] **Step 3: Separate preview pending from interaction pending**

Keep `isRequestPending` for inspect/sample actions. In `requestPreview`, remove
`setIsRequestPending(true/false)`, retain sequence checks, and leave the last
successful preview visible on failure. Change the parameter debounce to:

```typescript
const timeout = window.setTimeout(() => {
  void requestPreview(values)
}, 150)
```

Validate before scheduling. Keep immediate view-mode behavior.

- [ ] **Step 4: Update documentation**

Document committed-path auto-load, removal of the load button, synchronized
sliders, 150 ms latest-wins preview behavior, and the requirement for a valid
Screen Color before live parameter preview begins.

- [ ] **Step 5: Run complete verification and commit**

```bash
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
uv run pytest tests -q
uv run ruff check .
uv run python -m compileall src tests
git diff --check
git add web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx docs/studio_chroma_keyer_prd.md
git commit -m "feat: stream live chroma parameter previews"
```

- [ ] **Step 6: Browser verification**

Start port `8765`, load `assets/frame/test_frame_2.png` through the native
picker, sample one valid green pixel, and exercise all seven sliders in Final
and Alpha modes. Verify automatic loading, latest preview behavior, export,
console/network health, and non-overlapping layout at `1440x900` and `390x844`.
