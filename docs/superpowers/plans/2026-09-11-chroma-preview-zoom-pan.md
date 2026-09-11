# Chroma Preview Zoom And Pan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add precise 25%-800% zoom, pointer-anchored wheel zoom, and Space-drag panning to the chroma preview without changing sampling coordinates or preview APIs.

**Architecture:** Keep geometry in pure functions, interaction and measurement state in a reusable `useImageViewport` hook, and controls in a stateless toolbar. `ChromaKeyerCanvas` composes these units around the existing image and marker layer, while `ChromaKeyerPage` owns a numeric media revision so preview URL refreshes preserve the viewport and successful source inspections reset it.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, Lucide React, CSS, Playwright

---

## File Map

- Create `web/frontend/src/modules/chroma-keyer/imageViewportMath.ts`: scale, wheel, anchor, and boundary calculations with no React dependency.
- Create `web/frontend/src/modules/chroma-keyer/imageViewportMath.test.ts`: exhaustive geometry tests.
- Create `web/frontend/src/modules/chroma-keyer/useImageViewport.ts`: viewport measurement, zoom mode, keyboard state, pan lifecycle, and cleanup.
- Create `web/frontend/src/modules/chroma-keyer/useImageViewport.test.tsx`: hook lifecycle and reset tests.
- Create `web/frontend/src/modules/chroma-keyer/ImageViewportToolbar.tsx`: accessible stateless zoom controls.
- Create `web/frontend/src/modules/chroma-keyer/ImageViewportToolbar.test.tsx`: labels, status, callbacks, and disabled limits.
- Modify `web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.tsx`: fixed viewport, shared transform layer, sampling, wheel, keyboard, and pan composition.
- Modify `web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx`: integration tests for zoom, pan suppression, sampling, and marker alignment.
- Modify `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`: own and increment `viewportKey` only after successful source inspection.
- Modify `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`: verify reset and preview-refresh preservation.
- Modify `web/frontend/src/styles/modules.css`: viewport dimensions, clipping, toolbar, cursors, responsive rules, and reduced motion.
- Modify `web/frontend/e2e/app.spec.ts`: browser-level image/video zoom, wheel, pan, sampling, alignment, and overflow checks.

## Execution Preconditions

- [ ] **Step 1: Create an isolated implementation worktree**

Invoke `using-git-worktrees`, then create a branch from `main` named
`feature/chroma-preview-zoom-pan`. Keep worktree directories outside the
repository root or under an already ignored worktree directory.

- [ ] **Step 2: Confirm the baseline in the worktree**

Run:

```powershell
cd web/frontend
npm test -- src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
npm run lint
```

Expected: both Vitest files pass and TypeScript exits with code 0.

### Task 1: Pure Viewport Geometry

**Files:**
- Create: `web/frontend/src/modules/chroma-keyer/imageViewportMath.ts`
- Create: `web/frontend/src/modules/chroma-keyer/imageViewportMath.test.ts`

- [ ] **Step 1: Write failing geometry tests**

Create `imageViewportMath.test.ts` with concrete cases for fit, manual clamps,
discrete levels, wheel normalization, anchor preservation, centering, and
large-content boundaries:

```ts
import { describe, expect, it } from 'vitest'
import {
  MAX_MANUAL_SCALE,
  MIN_MANUAL_SCALE,
  calculateFitScale,
  clampOffset,
  getDiscreteZoomScale,
  getWheelScale,
  zoomAtAnchor,
} from './imageViewportMath'

describe('imageViewportMath', () => {
  it('fits large media without enlarging small media', () => {
    expect(calculateFitScale({ width: 400, height: 300 }, { width: 800, height: 400 }))
      .toBe(0.5)
    expect(calculateFitScale({ width: 800, height: 600 }, { width: 200, height: 100 }))
      .toBe(1)
  })

  it('returns zero for invalid measurements', () => {
    expect(calculateFitScale({ width: 0, height: 300 }, { width: 800, height: 400 }))
      .toBe(0)
    expect(calculateFitScale({ width: 400, height: 300 }, { width: Number.NaN, height: 400 }))
      .toBe(0)
  })

  it('selects discrete manual zoom levels and clamps both ends', () => {
    expect(getDiscreteZoomScale(0.2, 1)).toBe(MIN_MANUAL_SCALE)
    expect(getDiscreteZoomScale(1, 1)).toBe(1.25)
    expect(getDiscreteZoomScale(1, -1)).toBe(0.75)
    expect(getDiscreteZoomScale(8, 1)).toBe(MAX_MANUAL_SCALE)
    expect(getDiscreteZoomScale(0.25, -1)).toBe(MIN_MANUAL_SCALE)
  })

  it('normalizes pixel, line, and page wheel deltas before clamping', () => {
    expect(getWheelScale(1, -500, 0, 600)).toBe(2)
    expect(getWheelScale(1, -31.25, 1, 600)).toBe(2)
    expect(getWheelScale(1, -1, 2, 500)).toBe(2)
    expect(getWheelScale(8, -500, 0, 600)).toBe(MAX_MANUAL_SCALE)
    expect(getWheelScale(0.25, 500, 0, 600)).toBe(MIN_MANUAL_SCALE)
  })

  it('preserves the content point under the zoom anchor', () => {
    expect(zoomAtAnchor(
      { x: 150, y: 100 },
      1,
      2,
      { x: 50, y: 25 },
    )).toEqual({ x: -50, y: -50 })
  })

  it('centers contained content and clamps oversized content to both edges', () => {
    const viewport = { width: 400, height: 300 }
    const image = { width: 200, height: 100 }

    expect(clampOffset({ x: 900, y: -900 }, 1, image, viewport))
      .toEqual({ x: 100, y: 100 })
    expect(clampOffset({ x: 40, y: -500 }, 3, image, viewport))
      .toEqual({ x: 0, y: 0 })
    expect(clampOffset({ x: -500, y: -500 }, 3, image, viewport))
      .toEqual({ x: -200, y: 0 })
  })
})
```

- [ ] **Step 2: Run the tests and verify the red state**

Run:

```powershell
cd web/frontend
npm test -- src/modules/chroma-keyer/imageViewportMath.test.ts
```

Expected: FAIL because `imageViewportMath.ts` does not exist.

- [ ] **Step 3: Implement the pure geometry module**

Create `imageViewportMath.ts`:

```ts
export type ViewportPoint = { x: number; y: number }
export type ViewportSize = { height: number; width: number }

export const MIN_MANUAL_SCALE = 0.25
export const MAX_MANUAL_SCALE = 8
export const ZOOM_LEVELS = [
  0.25, 0.33, 0.5, 0.67, 0.75, 1, 1.25, 1.5, 2, 3, 4, 6, 8,
] as const

const isPositiveSize = ({ height, width }: ViewportSize) =>
  Number.isFinite(height) && Number.isFinite(width) && height > 0 && width > 0

export function clampManualScale(scale: number) {
  if (!Number.isFinite(scale)) {
    return MIN_MANUAL_SCALE
  }
  return Math.min(MAX_MANUAL_SCALE, Math.max(MIN_MANUAL_SCALE, scale))
}

export function calculateFitScale(viewport: ViewportSize, image: ViewportSize) {
  if (!isPositiveSize(viewport) || !isPositiveSize(image)) {
    return 0
  }
  return Math.min(viewport.width / image.width, viewport.height / image.height, 1)
}

export function getDiscreteZoomScale(scale: number, direction: -1 | 1) {
  const current = Number.isFinite(scale) ? scale : MIN_MANUAL_SCALE
  const ordered = direction === 1 ? ZOOM_LEVELS : [...ZOOM_LEVELS].reverse()
  const next = ordered.find((level) => direction === 1 ? level > current : level < current)
  return clampManualScale(next ?? (
    direction === 1 ? MAX_MANUAL_SCALE : MIN_MANUAL_SCALE
  ))
}

export function getWheelScale(
  scale: number,
  deltaY: number,
  deltaMode: number,
  viewportHeight: number,
) {
  const normalizedDelta = deltaY * (
    deltaMode === 1 ? 16 : deltaMode === 2 ? viewportHeight : 1
  )
  return clampManualScale(scale * 2 ** (-normalizedDelta / 500))
}

export function zoomAtAnchor(
  anchor: ViewportPoint,
  oldScale: number,
  nextScale: number,
  offset: ViewportPoint,
): ViewportPoint {
  if (![anchor.x, anchor.y, oldScale, nextScale, offset.x, offset.y].every(Number.isFinite)
      || oldScale <= 0 || nextScale <= 0) {
    return { x: 0, y: 0 }
  }
  return {
    x: anchor.x - ((anchor.x - offset.x) / oldScale) * nextScale,
    y: anchor.y - ((anchor.y - offset.y) / oldScale) * nextScale,
  }
}

export function clampOffset(
  offset: ViewportPoint,
  scale: number,
  image: ViewportSize,
  viewport: ViewportSize,
): ViewportPoint {
  if (!isPositiveSize(image) || !isPositiveSize(viewport)
      || ![offset.x, offset.y, scale].every(Number.isFinite) || scale <= 0) {
    return { x: 0, y: 0 }
  }

  const clampAxis = (value: number, content: number, available: number) =>
    content <= available
      ? (available - content) / 2
      : Math.min(0, Math.max(available - content, value))

  return {
    x: clampAxis(offset.x, image.width * scale, viewport.width),
    y: clampAxis(offset.y, image.height * scale, viewport.height),
  }
}
```

- [ ] **Step 4: Run the focused tests**

Run:

```powershell
npm test -- src/modules/chroma-keyer/imageViewportMath.test.ts
```

Expected: all geometry tests PASS.

- [ ] **Step 5: Commit the geometry unit**

```powershell
git add web/frontend/src/modules/chroma-keyer/imageViewportMath.ts web/frontend/src/modules/chroma-keyer/imageViewportMath.test.ts
git commit -m "feat: add image viewport geometry"
```

### Task 2: Viewport Hook Measurement And Zoom

**Files:**
- Create: `web/frontend/src/modules/chroma-keyer/useImageViewport.ts`
- Create: `web/frontend/src/modules/chroma-keyer/useImageViewport.test.tsx`

- [ ] **Step 1: Write a failing hook harness test**

Create a `ViewportHarness` in `useImageViewport.test.tsx` that assigns a
`400 x 300` rectangle to the hook's `viewportRef`, exposes the scale and
offset, and provides buttons for zoom, fit, and 100%. Add these assertions:

```tsx
function makeRect(width: number, height: number): DOMRect {
  return {
    bottom: height,
    height,
    left: 0,
    right: width,
    toJSON: () => ({}),
    top: 0,
    width,
    x: 0,
    y: 0,
  }
}

function ViewportHarness({
  previewUrl,
  viewportKey,
}: {
  previewUrl: string
  viewportKey: number
}) {
  const viewport = useImageViewport({
    imageHeight: 400,
    imageWidth: 800,
    viewportKey,
  })
  return (
    <div
      data-preview={previewUrl}
      data-testid="viewport"
      onKeyDown={viewport.handleKeyDown}
      onWheel={viewport.handleWheel}
      ref={viewport.viewportRef}
      tabIndex={0}
    >
      <output data-testid="scale">{viewport.scale}</output>
      <output data-testid="offset">
        {Math.round(viewport.offset.x)},{Math.round(viewport.offset.y)}
      </output>
      <button onClick={viewport.zoomIn} type="button">Zoom in</button>
      <button onClick={viewport.fit} type="button">Fit</button>
      <button onClick={viewport.setActualSize} type="button">Actual size</button>
    </div>
  )
}

function measureViewport() {
  const element = screen.getByTestId('viewport')
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(makeRect(400, 300))
  fireEvent(window, new Event('resize'))
}

it('starts fitted, zooms around the center, and restores fit', () => {
  const { rerender } = render(<ViewportHarness viewportKey={1} previewUrl="/one.png" />)
  measureViewport()

  expect(screen.getByTestId('scale')).toHaveTextContent('0.5')
  fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
  expect(screen.getByTestId('scale')).toHaveTextContent('0.67')
  expect(screen.getByTestId('offset')).toHaveTextContent('-68,16')

  fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
  expect(screen.getByTestId('scale')).toHaveTextContent('1')

  rerender(<ViewportHarness viewportKey={1} previewUrl="/two.png" />)
  expect(screen.getByTestId('scale')).toHaveTextContent('1')

  rerender(<ViewportHarness viewportKey={2} previewUrl="/three.png" />)
  expect(screen.getByTestId('scale')).toHaveTextContent('0.5')
})

it('keeps the pointer anchor stable during wheel zoom and prevents scrolling', () => {
  render(<ViewportHarness viewportKey={1} previewUrl="/one.png" />)
  measureViewport()
  const viewport = screen.getByTestId('viewport')
  const wheel = createEvent.wheel(viewport, {
    clientX: 110,
    clientY: 120,
    deltaMode: 0,
    deltaY: -500,
  })
  fireEvent(viewport, wheel)

  expect(wheel.defaultPrevented).toBe(true)
  expect(screen.getByTestId('scale')).toHaveTextContent('1')
  expect(screen.getByTestId('offset')).toHaveTextContent('-110,-20')
})
```

The harness must use image dimensions `{ width: 800, height: 400 }`, install a
controllable `ResizeObserver` stub for observer-specific cases. The common
cases above exercise the required `window.resize` fallback.

- [ ] **Step 2: Run the hook test and verify the red state**

Run:

```powershell
npm test -- src/modules/chroma-keyer/useImageViewport.test.tsx
```

Expected: FAIL because `useImageViewport` is not defined.

- [ ] **Step 3: Implement zoom state and measurement**

Implement this public contract in `useImageViewport.ts`:

```ts
export type ImageViewportMode = 'fit' | 'manual'

type UseImageViewportOptions = {
  imageHeight: number
  imageWidth: number
  viewportKey: number
}

export function useImageViewport(options: UseImageViewportOptions): {
  canZoomIn: boolean
  canZoomOut: boolean
  fit: () => void
  handleKeyDown: (event: React.KeyboardEvent<HTMLElement>) => void
  handleWheel: (event: React.WheelEvent<HTMLElement>) => void
  mode: ImageViewportMode
  offset: ViewportPoint
  scale: number
  setActualSize: () => void
  viewportRef: React.RefObject<HTMLDivElement | null>
  zoomIn: () => void
  zoomOut: () => void
}
```

Use `ResizeObserver` when available and always install a `window.resize`
fallback. `measure()` reads `viewportRef.current.getBoundingClientRect()`.
When dimensions become valid, fit mode sets `scale` to `calculateFitScale`
and centers with `clampOffset`. Manual mode retains scale and only clamps
offset. Reset mode and offset in an effect keyed only by `viewportKey`; do not
key it by `previewUrl`.

Implement `applyScale(nextScale, anchor)` with functional state updates and
`zoomAtAnchor` followed by `clampOffset`. `zoomIn`, `zoomOut`, and
`setActualSize` anchor at the viewport center. `fit()` changes mode to `fit`.
`handleWheel` uses a viewport-relative pointer anchor and `getWheelScale`.
In fit mode, zoom-in selects the first discrete level above the exact fit
scale, including 25% when fit is below 25%; zoom-out is disabled when fit is
at or below 25%. Manual mode disables controls only at 25% and 800%.

Add cases with `ResizeObserver` removed from `globalThis` to prove the initial
measurement and `window.resize` fallback work. Feed a zero and a non-finite
rectangle and assert the hook returns to fit mode with finite scale and zero
offset instead of emitting `NaN` transforms.

- [ ] **Step 4: Run focused hook and geometry tests**

Run:

```powershell
npm test -- src/modules/chroma-keyer/imageViewportMath.test.ts src/modules/chroma-keyer/useImageViewport.test.tsx
```

Expected: both files PASS.

- [ ] **Step 5: Commit measurement and zoom behavior**

```powershell
git add web/frontend/src/modules/chroma-keyer/useImageViewport.ts web/frontend/src/modules/chroma-keyer/useImageViewport.test.tsx
git commit -m "feat: add image viewport zoom state"
```

### Task 3: Space-Drag Pan Lifecycle

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/useImageViewport.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/useImageViewport.test.tsx`

- [ ] **Step 1: Add failing pan lifecycle tests**

Extend the hook contract with:

```ts
handleKeyUp: (event: React.KeyboardEvent<HTMLElement>) => void
handlePointerDown: (event: React.PointerEvent<HTMLElement>) => void
isPanReady: boolean
isPanning: boolean
shouldSuppressClick: () => boolean
```

Extend `ViewportHarness` with `onKeyUp`, `onPointerDown`, `isPanning`, and a
button that consumes the one-shot suppression flag without reading it during
render:

```tsx
const [suppressed, setSuppressed] = useState(false)

<div
  onKeyDown={viewport.handleKeyDown}
  onKeyUp={viewport.handleKeyUp}
  onPointerDown={viewport.handlePointerDown}
  onWheel={viewport.handleWheel}
>
<output data-testid="panning">{String(viewport.isPanning)}</output>
<output data-testid="suppress">{String(suppressed)}</output>
<button
  onClick={() => setSuppressed(viewport.shouldSuppressClick())}
  type="button"
>
  Consume suppression
</button>
</div>
```

Add tests which first measure the viewport, set 100% scale, hold Space, and
start a primary-pointer drag:

```tsx
it('pans only with Space plus the primary pointer and clamps the result', () => {
  render(<ViewportHarness viewportKey={1} previewUrl="/one.png" />)
  measureViewport()
  fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
  const viewport = screen.getByTestId('viewport')

  fireEvent.keyDown(viewport, { key: ' ' })
  fireEvent.pointerDown(viewport, { button: 0, clientX: 200, clientY: 150, pointerId: 7 })
  fireEvent.pointerMove(window, { clientX: 100, clientY: 100, pointerId: 7 })
  fireEvent.pointerUp(window, { pointerId: 7 })

  expect(screen.getByTestId('offset')).toHaveTextContent('-100,-50')
  expect(screen.getByTestId('panning')).toHaveTextContent('false')
  fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
  expect(screen.getByTestId('suppress')).toHaveTextContent('true')
})

it('does not classify movement below four CSS pixels as a drag', () => {
  render(<ViewportHarness viewportKey={1} previewUrl="/one.png" />)
  measureViewport()
  const viewport = screen.getByTestId('viewport')
  fireEvent.keyDown(viewport, { key: ' ' })
  fireEvent.pointerDown(viewport, { button: 0, clientX: 20, clientY: 20, pointerId: 8 })
  fireEvent.pointerMove(window, { clientX: 23, clientY: 20, pointerId: 8 })
  fireEvent.pointerUp(window, { pointerId: 8 })
  fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
  expect(screen.getByTestId('suppress')).toHaveTextContent('false')
})
```

Also spy on `window.addEventListener` and `window.removeEventListener` to verify
identical `pointermove`, `pointerup`, and `pointercancel` listeners are removed
after pointer-up, pointer-cancel, Space release, blur, `viewportKey` change,
and unmount.

- [ ] **Step 2: Run the hook test and verify pan tests fail**

Run:

```powershell
npm test -- src/modules/chroma-keyer/useImageViewport.test.tsx
```

Expected: FAIL because pointer handlers and suppression are absent.

- [ ] **Step 3: Implement pan state and cleanup**

Use refs for `spacePressed`, active pointer data, and one-shot click
suppression:

```ts
type PanSession = {
  dragged: boolean
  originOffset: ViewportPoint
  pointerId: number
  start: ViewportPoint
}

const DRAG_THRESHOLD = 4
const panRef = useRef<PanSession | null>(null)
const suppressClickRef = useRef(false)
const spacePressedRef = useRef(false)
```

`handlePointerDown` starts only when `button === 0` and Space is pressed.
Ignore pointer-down events whose target is inside `.image-viewport-toolbar`.
Window `pointermove` computes total displacement from `start`, marks
`dragged` when `Math.hypot(dx, dy) >= 4`, and clamps
`originOffset + displacement`. The single `finishPan()` function removes all
three window pointer listeners, records one-shot suppression only for a
completed drag, clears the session, and sets `isPanning` false.

`handleKeyDown` sets the Space ref and prevents default; it also handles
`+`, `=`, `-`, `0`, and case-insensitive `f`. `handleKeyUp` clears Space and
ends an active pan. Add a window `blur` listener and cleanup effects for
unmount and `viewportKey` replacement. `shouldSuppressClick()` returns and
clears the one-shot flag. Expose `isPanReady` while Space is held and
`isPanning` only while a pointer session is active.

- [ ] **Step 4: Run all viewport unit tests**

Run:

```powershell
npm test -- src/modules/chroma-keyer/imageViewportMath.test.ts src/modules/chroma-keyer/useImageViewport.test.tsx
```

Expected: all tests PASS, including every listener termination path.

- [ ] **Step 5: Commit pan behavior**

```powershell
git add web/frontend/src/modules/chroma-keyer/useImageViewport.ts web/frontend/src/modules/chroma-keyer/useImageViewport.test.tsx
git commit -m "feat: add space drag viewport panning"
```

### Task 4: Accessible Zoom Toolbar

**Files:**
- Create: `web/frontend/src/modules/chroma-keyer/ImageViewportToolbar.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ImageViewportToolbar.test.tsx`

- [ ] **Step 1: Write failing toolbar tests**

Test all accessible names, native disabled states, status text, and callbacks:

```tsx
it('renders zoom status and dispatches every command', async () => {
  const user = userEvent.setup()
  const callbacks = {
    onActualSize: vi.fn(),
    onFit: vi.fn(),
    onZoomIn: vi.fn(),
    onZoomOut: vi.fn(),
  }
  render(<ImageViewportToolbar canZoomIn canZoomOut scale={1.25} {...callbacks} />)

  expect(screen.getByRole('status')).toHaveTextContent('125%')
  await user.click(screen.getByRole('button', { name: '缩小预览' }))
  await user.click(screen.getByRole('button', { name: '放大预览' }))
  await user.click(screen.getByRole('button', { name: '适应窗口' }))
  await user.click(screen.getByRole('button', { name: '实际大小' }))
  expect(callbacks.onZoomOut).toHaveBeenCalledOnce()
  expect(callbacks.onZoomIn).toHaveBeenCalledOnce()
  expect(callbacks.onFit).toHaveBeenCalledOnce()
  expect(callbacks.onActualSize).toHaveBeenCalledOnce()
})

it('uses native disabled states at zoom limits', () => {
  render(
    <ImageViewportToolbar
      canZoomIn={false}
      canZoomOut={false}
      onActualSize={vi.fn()}
      onFit={vi.fn()}
      onZoomIn={vi.fn()}
      onZoomOut={vi.fn()}
      scale={8}
    />,
  )
  expect(screen.getByRole('button', { name: '缩小预览' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '放大预览' })).toBeDisabled()
})
```

- [ ] **Step 2: Run the toolbar test and verify the red state**

Run:

```powershell
npm test -- src/modules/chroma-keyer/ImageViewportToolbar.test.tsx
```

Expected: FAIL because the component does not exist.

- [ ] **Step 3: Implement the stateless toolbar**

Create the component with `Minus`, `Plus`, `Scan`, and `Maximize` from
`lucide-react`. Use this prop contract:

```ts
type ImageViewportToolbarProps = {
  canZoomIn: boolean
  canZoomOut: boolean
  onActualSize: () => void
  onFit: () => void
  onZoomIn: () => void
  onZoomOut: () => void
  scale: number
}
```

Render a `role="toolbar"` named `预览缩放控制`, four `.icon-button` native
buttons with matching `aria-label` and `title`, and:

```tsx
<output aria-live="polite" className="image-viewport-zoom-status" role="status">
  {Math.round(scale * 100)}%
</output>
```

- [ ] **Step 4: Run the toolbar test**

Run:

```powershell
npm test -- src/modules/chroma-keyer/ImageViewportToolbar.test.tsx
```

Expected: all toolbar tests PASS.

- [ ] **Step 5: Commit the toolbar**

```powershell
git add web/frontend/src/modules/chroma-keyer/ImageViewportToolbar.tsx web/frontend/src/modules/chroma-keyer/ImageViewportToolbar.test.tsx
git commit -m "feat: add image viewport toolbar"
```

### Task 5: Integrate The Viewport With Chroma Sampling

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx`

- [ ] **Step 1: Update Canvas tests to the new contract**

Add required `viewportKey={1}` to existing renders. Replace the Space center
sample table case with an Enter-only assertion, then add:

```tsx
function makeRect({
  height,
  left,
  top,
  width,
}: {
  height: number
  left: number
  top: number
  width: number
}): DOMRect {
  return {
    bottom: top + height,
    height,
    left,
    right: left + width,
    toJSON: () => ({}),
    top,
    width,
    x: left,
    y: top,
  }
}

it('uses Space for pan without sampling after a completed drag', () => {
  const onSample = vi.fn()
  render(
    <ChromaKeyerCanvas
      onSample={onSample}
      originalHeight={400}
      originalWidth={800}
      previewUrl="/preview.png"
      samples={[]}
      viewMode="final"
      viewportKey={1}
    />,
  )
  const image = screen.getByRole('button', { name: '抠像预览' })
  const viewport = image.closest('.chroma-preview-viewport') as HTMLElement
  fireEvent.keyDown(image, { key: ' ' })
  fireEvent.pointerDown(viewport, { button: 0, clientX: 200, clientY: 100, pointerId: 1 })
  fireEvent.pointerMove(window, { clientX: 150, clientY: 100, pointerId: 1 })
  fireEvent.pointerUp(window, { pointerId: 1 })
  fireEvent.click(image, { clientX: 150, clientY: 100 })
  expect(onSample).not.toHaveBeenCalled()
})

it('samples from the transformed image rectangle after zoom', () => {
  const onSample = vi.fn()
  render(
    <ChromaKeyerCanvas
      onSample={onSample}
      originalHeight={400}
      originalWidth={800}
      previewUrl="/preview.png"
      samples={[]}
      viewMode="final"
      viewportKey={1}
    />,
  )
  const image = screen.getByRole('button', { name: '抠像预览' })
  vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
    makeRect({ left: -200, top: -50, width: 1600, height: 800 }),
  )
  fireEvent.click(image, { clientX: 600, clientY: 350 })
  expect(onSample).toHaveBeenCalledWith({
    x: 800,
    y: 400,
    previewHeight: 800,
    previewWidth: 1600,
  })
})
```

Add tests that the transform layer contains both image and marker, toolbar
keyboard commands update its transform, a preview URL rerender keeps the
zoom, a new `viewportKey` resets it, Space alone never samples, and ordinary
click below the drag threshold still samples.

- [ ] **Step 2: Run Canvas tests and verify the red state**

Run:

```powershell
npm test -- src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx
```

Expected: FAIL for the missing `viewportKey`, toolbar, and transform layer.

- [ ] **Step 3: Compose the hook and toolbar in Canvas**

Add `viewportKey: number` to `ChromaKeyerCanvasProps`. Instantiate the hook
with original dimensions and render this hierarchy:

```tsx
<div
  className={[
    'chroma-preview-viewport',
    viewport.isPanReady ? 'is-pan-ready' : '',
    viewport.isPanning ? 'is-panning' : '',
  ].filter(Boolean).join(' ')}
  onKeyDown={viewport.handleKeyDown}
  onKeyUp={viewport.handleKeyUp}
  onPointerDown={viewport.handlePointerDown}
  onWheel={viewport.handleWheel}
  ref={viewport.viewportRef}
>
  <div
    className={`chroma-preview-canvas${viewMode === 'final' ? ' chroma-preview-canvas-final' : ''}`}
  >
    <div
      className="chroma-preview-content"
      style={{
        height: originalHeight,
        transform: `translate3d(${viewport.offset.x}px, ${viewport.offset.y}px, 0) scale(${viewport.scale})`,
        width: originalWidth,
      }}
    >
      <img
        alt="抠像预览"
        aria-describedby="chroma-keyer-preview-sampling-instructions"
        aria-disabled={disabled || undefined}
        className={`chroma-preview-image${disabled ? ' chroma-preview-image-disabled' : ''}`}
        onClick={(event) => {
          if (!viewport.shouldSuppressClick()) {
            sampleAt(event.currentTarget, event.clientX, event.clientY)
          }
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            const rect = event.currentTarget.getBoundingClientRect()
            sampleAt(event.currentTarget, rect.left + rect.width / 2, rect.top + rect.height / 2)
          }
        }}
        role="button"
        src={previewUrl}
        tabIndex={disabled ? -1 : 0}
      />
      {originalWidth && originalHeight
        ? samples.map((sample, index) => (
            <span
              aria-label={`采样点 ${index + 1}`}
              className="chroma-sample-marker"
              key={`${sample.sourceX}-${sample.sourceY}-${index}`}
              style={{
                left: `${(sample.sourceX / originalWidth) * 100}%`,
                top: `${(sample.sourceY / originalHeight) * 100}%`,
              }}
            >
              {index + 1}
            </span>
          ))
        : null}
    </div>
  </div>
  <ImageViewportToolbar
    canZoomIn={viewport.canZoomIn}
    canZoomOut={viewport.canZoomOut}
    onActualSize={viewport.setActualSize}
    onFit={viewport.fit}
    onZoomIn={viewport.zoomIn}
    onZoomOut={viewport.zoomOut}
    scale={viewport.scale}
  />
</div>
```

Keep `buildChromaSamplePayload` unchanged. Update instruction text to:

```tsx
点击预览采样；按 Enter 从画面中心采样；按住空格并拖拽可平移画面。
```

Ensure keyboard zoom events from toolbar buttons bubble to the viewport but
Enter on a toolbar button remains the native button activation path.

- [ ] **Step 4: Run Canvas, toolbar, hook, and geometry tests**

Run:

```powershell
npm test -- src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx src/modules/chroma-keyer/ImageViewportToolbar.test.tsx src/modules/chroma-keyer/useImageViewport.test.tsx src/modules/chroma-keyer/imageViewportMath.test.ts
```

Expected: all focused tests PASS.

- [ ] **Step 5: Commit Canvas integration**

```powershell
git add web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx
git commit -m "feat: integrate zoom and pan with chroma preview"
```

### Task 6: Stable Media Revision

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`

- [ ] **Step 1: Add failing page-level state tests**

After loading an image, zoom to 100%, trigger a Final/Alpha preview refresh,
and assert the `role="status"` remains `100%`. Then change to a different
input path, complete its inspection, and assert status returns to the expected
fit percentage. Add the same-path reload case through the existing file-picker
flow so a successful reinspection also resets.

Use a `ResizeObserver` stub and deterministic canvas rectangle in these tests;
do not assert internal React state or the numeric key. For the same-path
requirement, inspect path A, inspect path B, then inspect path A again and
verify the second successful inspection of A resets the viewport.

- [ ] **Step 2: Run the page test and verify the red state**

Run:

```powershell
npm test -- src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
```

Expected: FAIL because Canvas has no supplied media revision and successful
inspections cannot reset the viewport independently of preview URL changes.

- [ ] **Step 3: Add and pass the page-local revision**

Near the media state add:

```ts
const [viewportKey, setViewportKey] = useState(0)
```

In both successful inspection branches, after stale-request guards pass and
immediately before setting inspected media, increment it:

```ts
setViewportKey((current) => current + 1)
```

Do not increment in preview generation, view-mode changes, video seek/frame
refreshes, playback callbacks, failed inspections, or stale inspection
responses. Add this prop to the existing Canvas call:

```tsx
viewportKey={viewportKey}
```

- [ ] **Step 4: Run page and Canvas regressions**

Run:

```powershell
npm test -- src/modules/chroma-keyer/ChromaKeyerPage.test.tsx src/modules/chroma-keyer/ChromaKeyerCanvas.test.tsx
```

Expected: both files PASS, including existing stale-response and video-frame
ordering tests.

- [ ] **Step 5: Commit media reset semantics**

```powershell
git add web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
git commit -m "feat: reset viewport on media inspection"
```

### Task 7: Responsive Styling And Browser Verification

**Files:**
- Modify: `web/frontend/src/styles/modules.css`
- Modify: `web/frontend/e2e/app.spec.ts`

- [ ] **Step 1: Add failing Playwright coverage**

Add a Playwright test named
`Chroma preview zoom supports inspection without sampling during pan`, using
the existing mocked chroma image and video route helpers. For image preview:

```ts
const preview = page.getByRole('button', { name: '抠像预览' })
const viewport = page.locator('.chroma-preview-viewport')
await expect(page.getByRole('toolbar', { name: '预览缩放控制' })).toBeVisible()
await page.getByRole('button', { name: '实际大小' }).click()
await expect(page.getByRole('status')).toHaveText('100%')

const beforeWheel = await preview.boundingBox()
await viewport.hover({ position: { x: 80, y: 60 } })
await page.mouse.wheel(0, -500)
await expect(page.getByRole('status')).not.toHaveText('100%')
const afterWheel = await preview.boundingBox()
expect(beforeWheel).not.toBeNull()
expect(afterWheel).not.toBeNull()

await page.keyboard.down('Space')
await viewport.hover({ position: { x: 180, y: 100 } })
await page.mouse.down()
await page.mouse.move(120, 100)
await page.mouse.up()
await page.keyboard.up('Space')
expect(sampleRequestCount).toBe(0)

await preview.click({ position: { x: 100, y: 50 } })
await expect.poll(() => sampleRequestCount).toBe(1)
```

After sampling, compare the marker center to the source point transformed
through the image bounding rectangle with a tolerance of 2 CSS pixels.
In the video flow assert the toolbar remains visible after frame URL changes
and its zoom status is preserved.

For widths `1440`, `900`, and `390`, assert:

```ts
expect(await page.evaluate(() => document.documentElement.scrollWidth))
  .toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth))
```

Run the image assertions in light and dark themes and collect page errors:

```ts
const pageErrors: Error[] = []
page.on('pageerror', (error) => pageErrors.push(error))
```

After the complete image and video workflow, assert:

```ts
expect(pageErrors).toEqual([])
```

- [ ] **Step 2: Run the focused browser test and verify the red state**

Run:

```powershell
npm run test:e2e -- app.spec.ts --grep "Chroma preview zoom"
```

Expected: FAIL because fixed viewport dimensions, toolbar placement, and
transform styling are not complete.

- [ ] **Step 3: Add stable viewport and control styles**

Replace image-constrained canvas sizing with a fixed clipping viewport and
unscaled intrinsic content:

```css
.chroma-preview-viewport {
  align-self: center;
  inline-size: min(100%, 960px);
  max-inline-size: 100%;
  position: relative;
}

.chroma-preview-canvas {
  aspect-ratio: 16 / 9;
  background: oklch(14% 0.012 230);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  cursor: crosshair;
  inline-size: 100%;
  max-block-size: min(58vh, 680px);
  overflow: hidden;
  position: relative;
}

.chroma-preview-content {
  left: 0;
  position: absolute;
  top: 0;
  transform-origin: 0 0;
  transition: transform 120ms ease-out;
}

.chroma-preview-image {
  display: block;
  height: 100%;
  max-height: none;
  max-width: none;
  user-select: none;
  width: 100%;
}

.chroma-preview-viewport.is-panning,
.chroma-preview-viewport.is-panning .chroma-preview-image {
  cursor: grabbing;
}

.chroma-preview-viewport.is-pan-ready:not(.is-panning),
.chroma-preview-viewport.is-pan-ready:not(.is-panning) .chroma-preview-image {
  cursor: grab;
}

.chroma-preview-viewport.is-panning .chroma-preview-content {
  transition: none;
}

.image-viewport-toolbar {
  align-items: center;
  background: var(--surface-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-md);
  display: flex;
  gap: var(--space-1);
  padding: var(--space-1);
  position: absolute;
  right: var(--space-2);
  top: var(--space-2);
  z-index: 2;
}

.image-viewport-zoom-status {
  color: var(--text-secondary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  inline-size: 48px;
  line-height: 30px;
  text-align: center;
}

@media (max-width: 759px) {
  .chroma-preview-canvas {
    max-block-size: 42vh;
  }

  .image-viewport-toolbar {
    gap: 2px;
    right: 6px;
    top: 6px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .chroma-preview-content {
    transition: none;
  }
}
```

Retain the existing checkerboard background and disabled visual treatment.
Ensure toolbar icon buttons stay at the existing stable square dimensions and
the toolbar does not wrap.

- [ ] **Step 4: Run Playwright and frontend static checks**

Run:

```powershell
npm run test:e2e -- app.spec.ts --grep "Chroma preview zoom"
npm run lint
npm run build
```

Expected: focused Playwright cases PASS, TypeScript exits 0, and Vite produces
the production bundle.

- [ ] **Step 5: Commit styles and browser coverage**

```powershell
git add web/frontend/src/styles/modules.css web/frontend/e2e/app.spec.ts
git commit -m "test: verify chroma preview zoom and pan"
```

### Task 8: Full Regression And Deployment Check

**Files:**
- Verify only; modify the smallest owning file if a regression is discovered.

- [ ] **Step 1: Run the full frontend unit suite**

Run:

```powershell
cd web/frontend
npm test
```

Expected: all Vitest tests PASS with zero failures.

- [ ] **Step 2: Run TypeScript and production build**

Run:

```powershell
npm run lint
npm run build
```

Expected: both commands exit 0 with no TypeScript errors.

- [ ] **Step 3: Run the full Playwright suite**

Run:

```powershell
npm run test:e2e
```

Expected: all local Playwright tests PASS; hosted-only tests may remain
explicitly skipped.

- [ ] **Step 4: Rebuild the local Docker deployment**

From the repository root run:

```powershell
docker compose up -d --build
docker compose ps
Invoke-WebRequest https://localhost/ | Select-Object StatusCode
```

Expected: all six services are running, health checks are healthy where
defined, and HTTPS returns status 200.

- [ ] **Step 5: Perform real-browser inspection**

Invoke `TRAE-browseruse`. At `https://localhost/`, load both an image and a
video and verify toolbar commands, pointer-anchored wheel zoom, Space-drag,
post-zoom sampling, marker alignment, light/dark themes, and widths 1440,
900, and 390. Capture screenshots under `.temp/` and verify there is no
horizontal overflow, overlap, blank preview, or console error.

- [ ] **Step 6: Request code review**

Invoke `requesting-code-review` against the implementation branch. Address
all confirmed correctness, lifecycle, accessibility, and regression findings
with focused tests before proceeding.

- [ ] **Step 7: Verify the final diff and status**

Run:

```powershell
git diff main...HEAD --check
git status --short
git log --oneline main..HEAD
```

Expected: no whitespace errors, no untracked implementation artifacts, and a
small sequence of focused commits matching Tasks 1-7.

- [ ] **Step 8: Finish the development branch**

Invoke `finishing-a-development-branch` and present merge, PR, or retention
options. Do not merge or push without the user's selected option.
