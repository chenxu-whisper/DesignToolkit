# Chroma Video Playback Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add compact, frame-aware playback controls that preview unkeyed or current Final/Alpha video frames at real-time source progress with bounded server requests.

**Architecture:** `ChromaVideoTimeline` renders the four transport buttons and remains presentational. A focused `useChromaVideoPlayback` hook owns wall-clock scheduling, one-request-in-flight backpressure, frame skipping, and playback lifetime; `ChromaKeyerPage` supplies the existing frame/preview API callback and owns media state, request invalidation, and action-specific stop behavior.

**Tech Stack:** React 19, TypeScript, Lucide React, Vitest, Testing Library, Playwright, existing FastAPI chroma video preview APIs.

---

### Task 1: Add Compact Timeline Transport Controls

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaVideoTimeline.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaVideoTimeline.test.tsx`
- Modify: `web/frontend/src/styles/app.css`

- [ ] **Step 1: Write failing transport rendering tests**

Add default callback props to the test fixture:

```typescript
const defaultProps = {
  durationSeconds: 12.5,
  estimatedFrameCount: 375,
  fps: 30,
  height: 1080,
  isPlaying: false,
  onChange: vi.fn(),
  onPlayPause: vi.fn(),
  onRestart: vi.fn(),
  onStepBackward: vi.fn(),
  onStepForward: vi.fn(),
  value: 1.25,
  width: 1920,
}
```

Add tests that assert:

```typescript
expect(screen.getByRole('button', { name: '回到开头' })).toBeEnabled()
expect(screen.getByRole('button', { name: '上一帧' })).toBeEnabled()
expect(screen.getByRole('button', { name: '播放' })).toBeEnabled()
expect(screen.getByRole('button', { name: '下一帧' })).toBeEnabled()
expect(screen.queryByRole('button', { name: '暂停' })).not.toBeInTheDocument()
```

Rerender with `isPlaying` and assert the center button is named `暂停`.
Click each control and assert only its matching callback fires.

- [ ] **Step 2: Write failing disabled and containment tests**

Render with `disabled` and assert all four buttons and the slider are disabled.
Extend the stylesheet test:

```typescript
const transport = getComputedStyle(
  screen.getByTestId('chroma-video-transport'),
)
expect(transport.display).toBe('flex')
expect(transport.flexShrink).toBe('0')
expect(controls.maxInlineSize).toBe('100%')
```

Assert every transport button has a non-empty `title`, stable inline/block size,
and does not resize the timeline track.

- [ ] **Step 3: Run the timeline tests to verify RED**

```bash
npm --prefix web/frontend test -- ChromaVideoTimeline.test.tsx
```

Expected: failures because transport props and buttons do not exist.

- [ ] **Step 4: Implement the presentational controls**

Import Lucide icons:

```typescript
import { Pause, Play, RotateCcw, StepBack, StepForward } from 'lucide-react'
```

Extend props:

```typescript
isPlaying: boolean
onPlayPause: () => void
onRestart: () => void
onStepBackward: () => void
onStepForward: () => void
```

Render the transport group before the range input:

```tsx
<div
  aria-label="视频播放控制"
  className="chroma-video-transport"
  data-testid="chroma-video-transport"
  role="group"
>
  <TransportButton
    disabled={disabled}
    icon={RotateCcw}
    label="回到开头"
    onClick={onRestart}
  />
  <TransportButton
    disabled={disabled}
    icon={StepBack}
    label="上一帧"
    onClick={onStepBackward}
  />
  <TransportButton
    disabled={disabled}
    icon={isPlaying ? Pause : Play}
    label={isPlaying ? '暂停' : '播放'}
    onClick={onPlayPause}
  />
  <TransportButton
    disabled={disabled}
    icon={StepForward}
    label="下一帧"
    onClick={onStepForward}
  />
</div>
```

Use a private `TransportButton` component with `aria-label`, `title`,
`type="button"`, the existing `icon-button` class, and an 18 px icon.

- [ ] **Step 5: Implement responsive layout styles**

Change the desktop timeline controls to:

```css
.chroma-video-timeline-controls {
  align-items: center;
  display: grid;
  gap: 10px;
  grid-template-columns: auto minmax(0, 1fr) minmax(0, 176px);
  max-inline-size: 100%;
  min-height: 32px;
  min-inline-size: 0;
}

.chroma-video-transport {
  display: flex;
  flex-shrink: 0;
  gap: 4px;
}

.chroma-video-transport .icon-button {
  block-size: 32px;
  inline-size: 32px;
}
```

At `max-width: 760px`, place transport and timecode on the first row and make
the range span the full second row:

```css
.chroma-video-timeline-controls {
  grid-template-areas:
    "transport timecode"
    "range range";
  grid-template-columns: auto minmax(0, 1fr);
}
```

Assign each child its grid area and keep timecode right-aligned on mobile.

- [ ] **Step 6: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test -- ChromaVideoTimeline.test.tsx
npm --prefix web/frontend run lint
git add web/frontend/src/modules/chroma-keyer/ChromaVideoTimeline.tsx web/frontend/src/modules/chroma-keyer/ChromaVideoTimeline.test.tsx web/frontend/src/styles/app.css
git commit -m "feat: add chroma video transport controls"
```

### Task 2: Build The Real-Time Playback Scheduler

**Files:**
- Create: `web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.ts`
- Create: `web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.test.tsx`

- [ ] **Step 1: Write failing scheduler tests**

Define the hook contract in tests:

```typescript
type PlaybackFrame = {
  previewUrl: string
  timestampSeconds: number
}

const playback = renderHook(() =>
  useChromaVideoPlayback({
    canPlay: true,
    durationSeconds: 3,
    fps: 4,
    renderedTimestampSeconds: 0,
    requestFrame,
    onFrame,
    onTargetChange,
    onError,
    onPause,
  }),
)
```

Use `vi.useFakeTimers()` and stub
`requestAnimationFrame`/`cancelAnimationFrame` with timers. Cover:

- Play starts from the rendered timestamp.
- Elapsed wall time is quantized with
  `Math.floor(elapsedSeconds * fps) / fps`.
- Only one `requestFrame` promise is active.
- While active, repeated ticks replace the queued target.
- Resolving the active request dispatches only the latest queued target.
- Pause cancels future ticks and calls `onPause` with the last rendered time.
- A stale response after pause does not call `onFrame`.
- A request failure pauses once and calls `onError`.
- The last frame is rendered before automatic pause.
- Play at the last frame starts from zero.
- Changing `canPlay` to false stops playback.
- Unmount cancels animation work and ignores pending responses.

- [ ] **Step 2: Run scheduler tests to verify RED**

```bash
npm --prefix web/frontend test -- useChromaVideoPlayback.test.tsx
```

Expected: import failure for `useChromaVideoPlayback`.

- [ ] **Step 3: Implement frame-grid helpers**

Export:

```typescript
export function quantizePlaybackTimestamp(
  timestampSeconds: number,
  fps: number,
  durationSeconds: number,
): number {
  const max = getLastVideoFrameTime(durationSeconds, fps)
  const frameIndex = Math.floor(Math.max(0, timestampSeconds) * fps + 1e-9)
  return Math.min(max, frameIndex / fps)
}
```

Reject invalid playback inputs by returning zero and never starting playback.
Reuse `getLastVideoFrameTime` rather than duplicating final-frame logic.

- [ ] **Step 4: Implement the bounded scheduler hook**

The hook options are:

```typescript
type UseChromaVideoPlaybackOptions = {
  canPlay: boolean
  durationSeconds: number
  fps: number
  renderedTimestampSeconds: number | null
  requestFrame: (timestampSeconds: number) => Promise<PlaybackFrame>
  onError: (error: unknown) => void
  onFrame: (frame: PlaybackFrame) => void
  onPause: (renderedTimestampSeconds: number | null) => void
  onTargetChange: (timestampSeconds: number) => void
}

type PausePlaybackOptions = {
  synchronize?: boolean
}
```

Return:

```typescript
{
  isPlaying,
  pause: (options?: PausePlaybackOptions) => void,
  play,
  togglePlayback,
}
```

Store animation handle, playback generation, current target, in-flight flag,
wall-clock start, source start, and latest callbacks in refs. The request loop
must follow:

```typescript
targetRef.current = quantizePlaybackTimestamp(
  startTimestampRef.current + (now - startWallTimeRef.current) / 1000,
  fps,
  durationSeconds,
)
onTargetChangeRef.current(targetRef.current)

if (!requestInFlightRef.current) {
  void dispatchLatestTarget(generation)
}
```

`pause` defaults `synchronize` to `true` and passes the last rendered timestamp
to `onPause`; internal invalidation paths may opt out when another action will
replace the media session immediately. `dispatchLatestTarget` sets the in-flight flag before awaiting. After success,
it calls `onFrame` only for the current generation, clears the flag, then
dispatches the newest target if it advanced. Stop only after the last target
has successfully rendered.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test -- useChromaVideoPlayback.test.tsx
npm --prefix web/frontend run lint
git add web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.ts web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.test.tsx
git commit -m "feat: schedule real-time chroma video playback"
```

### Task 3: Integrate Playback With Chroma Preview Requests

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`

- [ ] **Step 1: Write failing unkeyed playback tests**

With a loaded video and fake clock, assert:

```typescript
await user.click(screen.getByRole('button', { name: '播放' }))
expect(screen.getByRole('button', { name: '暂停' })).toBeInTheDocument()
```

Advance 500 ms for a 4 FPS clip. Hold the first frame request unresolved,
advance another 750 ms, and assert only one request exists. Resolve it, then
assert the second request uses the latest quantized target rather than every
intermediate frame:

```typescript
expect(pageMocks.createChromaVideoFrame).toHaveBeenNthCalledWith(2, {
  inputPath: 'E:\\media\\screen.mp4',
  timestampSeconds: 1.25,
})
```

Assert the keyed API is not called before sampling.

- [ ] **Step 2: Write failing keyed Final/Alpha playback tests**

After sampling, start playback and assert `createChromaVideoPreview` receives
the exact Screen Color, all seven parameters, current view mode, input path,
and quantized timestamp. Switch to Alpha, restart playback, and assert
`viewMode: 'alpha'`.

- [ ] **Step 3: Run page playback tests to verify RED**

```bash
npm --prefix web/frontend test -- ChromaKeyerPage.test.tsx -t "playback"
```

Expected: failures because the page does not pass transport callbacks and has
no playback scheduler.

- [ ] **Step 4: Add playback request integration**

Create a callback that reads current refs at dispatch time:

```typescript
const requestPlaybackFrame = async (timestampSeconds: number) => {
  const playbackMedia = mediaRef.current
  const playbackValues = valuesRef.current
  if (!playbackMedia || playbackMedia.kind !== 'video') {
    throw new Error('视频未加载')
  }

  const response = playbackValues.samples.length === 1
    ? await createChromaVideoPreview({
        ...toBasePayload(playbackValues),
        timestampSeconds,
        viewMode: playbackValues.viewMode,
      })
    : await createChromaVideoFrame({
        inputPath: playbackMedia.inputPath,
        timestampSeconds,
      })

  return {
    previewUrl: response.preview_url,
    timestampSeconds: response.timestamp_seconds,
  }
}
```

Use `useChromaVideoPlayback` with:

- `onTargetChange`: update only `values.timestampSeconds`;
- `onFrame`: update preview URL and rendered timestamp without moving the
  requested target backward;
- `onPause`: synchronize requested time to the last rendered timestamp;
- `onError`: preserve the preview and set `errors.timestampSeconds`.

Split the current `synchronizeRenderedVideoTimestamp` behavior into:

```typescript
setRenderedVideoTimestamp(video, timestampSeconds)
synchronizeRenderedVideoTimestamp(video, timestampSeconds)
```

The first updates only rendered refs/state. The second also updates form values
and remains used by manual seeks.

- [ ] **Step 5: Wire controls and immediate frame commands**

Refactor manual seeking into:

```typescript
requestVideoTimestamp(timestampSeconds, delayMilliseconds)
```

Keep slider calls at 100 ms. Restart and previous/next controls call
`pause()` and dispatch at zero delay:

```typescript
const frameStep = 1 / media.fps
requestVideoTimestamp(currentTimestamp - frameStep, 0)
requestVideoTimestamp(currentTimestamp + frameStep, 0)
requestVideoTimestamp(0, 0)
```

Clamp every command through `getLastVideoFrameTime`. Pass all transport props
to `ChromaVideoTimeline`.

- [ ] **Step 6: Verify GREEN and commit**

```bash
npm --prefix web/frontend test -- ChromaKeyerPage.test.tsx
npm --prefix web/frontend run lint
git add web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
git commit -m "feat: play chroma video previews"
```

### Task 4: Complete Playback Interruption And Error Semantics

**Files:**
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.test.tsx`

- [ ] **Step 1: Write failing interruption tests**

Start playback, then independently verify each action pauses it before
continuing:

- slider seek;
- restart and frame-step;
- Screen Color sample/remove;
- any of seven parameter changes;
- Final/Alpha switch;
- media path change or same-path video refresh;
- export submission;
- task state becoming queued/running.

For each case assert the Pause button becomes Play, an older playback response
cannot replace the newer preview, and requested time is synchronized to the
rendered frame before the next action computes its payload.

- [ ] **Step 2: Write failing endpoint and failure tests**

Use a 3-second, 4 FPS video. Assert:

- the last request targets `2.75`;
- successful rendering of `2.75` pauses and leaves the timeline at `2.75`;
- clicking Play again first requests timestamp zero;
- a rejected playback request pauses, preserves the last successful `src`,
  rolls the timeline back, and displays the backend message;
- Play clears the old timestamp error before retrying;
- sampling controls are disabled during playback.

- [ ] **Step 3: Run interruption tests to verify RED**

```bash
npm --prefix web/frontend test -- ChromaKeyerPage.test.tsx useChromaVideoPlayback.test.tsx
```

Expected: new stop-condition and error-state assertions fail.

- [ ] **Step 4: Implement one playback stop boundary**

Add a page helper:

```typescript
const stopVideoPlayback = (options?: { synchronize?: boolean }) => {
  playback.pause({ synchronize: options?.synchronize ?? true })
}
```

Invoke it before every action listed in Step 1. In `updateValues`, compare
playback-affecting fields against `valuesRef.current` before assigning:

```typescript
const playbackInputChanged = (
  nextValues.inputPath !== current.inputPath
  || nextValues.samples !== current.samples
  || nextValues.viewMode !== current.viewMode
  || previewParameterFields.some((field) => nextValues[field] !== current[field])
)
```

Do not stop playback for output path/root edits alone.

Set the canvas disabled state to include `isPlaying`, preventing sampling until
the current rendered frame is stable. Ensure task transitions call pause once,
not on every render.

- [ ] **Step 5: Implement end and failure handling**

On scheduler failure:

```typescript
setErrors((current) => ({
  ...current,
  timestampSeconds:
    error instanceof Error && error.message
      ? error.message
      : '无法播放视频',
}))
```

Keep the last preview URL. On retry, clear `timestampSeconds` before starting.
On end, preserve the final rendered frame and set `isPlaying` false. Starting
from the final frame resets the scheduler start source to zero and dispatches
zero immediately.

- [ ] **Step 6: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test -- ChromaKeyerPage.test.tsx useChromaVideoPlayback.test.tsx
npm --prefix web/frontend test
npm --prefix web/frontend run lint
git add web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.ts web/frontend/src/modules/chroma-keyer/useChromaVideoPlayback.test.tsx
git commit -m "fix: harden chroma video playback state"
```

### Task 5: Add Browser Coverage And Product Documentation

**Files:**
- Modify: `web/frontend/e2e/app.spec.ts`
- Modify: `docs/studio_chroma_keyer_prd.md`
- Modify: `README.md`

- [ ] **Step 1: Extend deterministic browser route stubs**

In the complete chroma video workflow, collect playback preview bodies and
return each requested timestamp. Install Playwright's clock before loading the
page:

```typescript
await page.clock.install()
```

Keep one playback response pending while advancing the clock to prove that no
second request is sent until the first completes.

- [ ] **Step 2: Add the playback browser workflow**

Assert:

1. Four transport buttons appear after video inspection.
2. Play changes to Pause.
3. Unkeyed playback uses `/videos/frame`.
4. A slow response causes the next request to skip to the latest frame-grid
   timestamp.
5. Sampling then playback uses `/videos/preview` with complete Final/Alpha
   payloads.
6. Pause preserves the rendered preview and timestamp.
7. Previous/next frame and restart clamp correctly.
8. The last frame stops playback and a second Play starts at zero.

- [ ] **Step 3: Extend layout assertions**

At desktop and 390 px widths, measure:

```typescript
const transport = editor.querySelector<HTMLElement>('.chroma-video-transport')
const controls = editor.querySelector<HTMLElement>('.chroma-video-timeline-controls')
```

Assert both fit inside `.chroma-editor-main`, no button overlaps the range or
timecode, button boxes remain stable, and document/body scroll width does not
exceed client width.

- [ ] **Step 4: Update product documentation**

Document:

- playback/pause, restart, and frame-step controls;
- current Final/Alpha playback;
- real-time catch-up by dropping preview frames when decoding is slow;
- no audio, looping, speed control, or range playback.

Keep README and PRD wording synchronized.

- [ ] **Step 5: Run browser and documentation verification**

```bash
npm --prefix web/frontend run build
npm --prefix web/frontend run test:e2e
git diff --check
```

Expected: all Playwright tests pass with no desktop/mobile containment failure.

- [ ] **Step 6: Commit**

```bash
git add web/frontend/e2e/app.spec.ts docs/studio_chroma_keyer_prd.md README.md
git commit -m "test: validate chroma video playback controls"
```

### Task 6: Final Review And Release Verification

**Files:**
- Review all files changed by Tasks 1-5.

- [ ] **Step 1: Run specification review**

Compare the implementation line by line with:

```text
docs/superpowers/specs/2026-09-03-chroma-video-playback-controls-design.md
```

Resolve every missing or extra behavior before proceeding.

- [ ] **Step 2: Run code-quality review**

Review request generations, animation cleanup, callback freshness, one-request
backpressure, timestamp quantization, error recovery, and image regressions.
Resolve all P0-P2 findings and rerun the affected focused tests.

- [ ] **Step 3: Run the full release matrix**

```bash
uv run pytest tests -q
uv run ruff check .
uv run python -m compileall src tests
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
npm --prefix web/frontend run test:e2e
git diff --check
```

Expected:

- all backend tests pass except documented Windows symlink privilege skips;
- all frontend unit and browser tests pass;
- Ruff, compileall, TypeScript, production build, and diff checks exit zero.

- [ ] **Step 4: Run real GUI validation**

Start the merged worktree server on an unused port. Load a generated video,
sample Screen Color, play Final and Alpha, pause during a slow response, step
both directions, restart, reach the final frame, and replay from zero. Capture
desktop and 390 px screenshots and verify no overlap or horizontal overflow.

- [ ] **Step 5: Commit final review fixes if required**

If review or real GUI validation required changes:

```bash
git add <only-the-reviewed-files>
git commit -m "fix: finalize chroma video playback controls"
```

If no changes were required, do not create an empty commit.
