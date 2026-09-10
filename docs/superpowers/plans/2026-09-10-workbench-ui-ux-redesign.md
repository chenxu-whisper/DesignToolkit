# DesignToolkit Workbench UI/UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the React interface as a desktop-first, inspector-oriented media workbench with graphite/mint themes, responsive navigation, and a collapsible resizable log panel.

**Architecture:** Keep all processing APIs and Zustand business stores unchanged. Add isolated UI state for theme and panel layout, refactor the shell into explicit command-bar/tool-rail/workspace/log regions, and adapt chroma keying into a wide-screen inspector plus responsive dialog. Split visual rules into semantic tokens and responsibility-focused style sheets.

**Tech Stack:** React 19, TypeScript, Vite, Vitest, Testing Library, Playwright, Lucide React, CSS custom properties, native dialog and media queries.

---

## File Map

- Create `web/frontend/src/app/theme/ThemeProvider.tsx`: theme preference, system resolution, persistence, and context API.
- Create `web/frontend/src/app/theme/ThemeProvider.test.tsx`: theme state and persistence tests.
- Create `web/frontend/src/app/useMediaQuery.ts`: reactive media-query hook for responsive inspector behavior.
- Create `web/frontend/src/app/useMediaQuery.test.tsx`: media-query subscription tests.
- Create `web/frontend/src/test/matchMedia.ts`: reusable controllable media-query test stub.
- Modify `web/frontend/src/app/App.tsx`: install the theme provider.
- Modify `web/frontend/src/app/AppShell.tsx`: own shell-only log layout state and pass active-module context.
- Modify `web/frontend/src/app/AppShell.test.tsx`: command bar, navigation, theme, and log shell integration.
- Modify `web/frontend/src/app/types.ts`: add a Lucide icon type to module metadata.
- Modify `web/frontend/src/app/modules.ts`: assign icons and concise descriptions to modules.
- Modify `web/frontend/src/components/HeaderBar.tsx`: command bar, theme menu, task state, and log toggle.
- Modify `web/frontend/src/components/Sidebar.tsx`: icon-based tool rail with tooltips.
- Modify `web/frontend/src/components/TaskStatusBadge.tsx`: icon plus text task states.
- Modify `web/frontend/src/components/LogPanel.tsx`: controlled collapse and pointer/keyboard resizing.
- Modify `web/frontend/src/components/LogPanel.test.tsx`: collapsed summary, bounds, keyboard resize, copy, and clear tests.
- Create `web/frontend/src/modules/chroma-keyer/ChromaInspector.tsx`: wide-screen inspector and responsive native dialog.
- Create `web/frontend/src/modules/chroma-keyer/ChromaInspector.test.tsx`: dialog state and focus restoration.
- Modify `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`: preview-first workspace and inspector orchestration.
- Modify `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx`: inspector groups and sticky action footer.
- Modify `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`: responsive inspector trigger and preserved workflow assertions.
- Create `web/frontend/src/styles/tokens.css`: OKLCH primitives and semantic light/dark theme tokens.
- Create `web/frontend/src/styles/shell.css`: shell, command bar, tool rail, workspace, and log layout.
- Create `web/frontend/src/styles/controls.css`: shared form, button, status, tooltip, and focus states.
- Create `web/frontend/src/styles/modules.css`: simple-module and chroma workspace layouts.
- Modify `web/frontend/src/styles/app.css`: import focused style sheets and retain only reset rules.
- Modify `web/frontend/src/main.tsx`: keep style entry point and ensure root initialization remains stable.
- Create `web/frontend/e2e/workbench-ui.spec.ts`: viewport, theme, log, inspector, overflow, and console checks.

### Task 1: Theme Preference And Semantic Color Foundation

**Files:**
- Create: `web/frontend/src/app/theme/ThemeProvider.tsx`
- Create: `web/frontend/src/app/theme/ThemeProvider.test.tsx`
- Create: `web/frontend/src/test/matchMedia.ts`
- Modify: `web/frontend/src/app/App.tsx`
- Create: `web/frontend/src/styles/tokens.css`
- Modify: `web/frontend/src/styles/app.css`

- [ ] **Step 1: Write failing theme tests**

Create tests that define the public theme behavior:

```tsx
export function installMatchMedia(initialMatches: boolean) {
  let matches = initialMatches
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const mediaQuery = {
    addEventListener: (_type: 'change', listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    matches,
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    removeEventListener: (_type: 'change', listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
    setMatches(nextMatches: boolean) {
      matches = nextMatches
      Object.assign(mediaQuery, { matches })
      listeners.forEach((listener) => listener({ matches } as MediaQueryListEvent))
    },
  }
  vi.stubGlobal('matchMedia', vi.fn(() => mediaQuery))
  return mediaQuery
}
```

Import the helper into the theme test and define the probe locally:

```tsx
function ThemeProbe() {
  const { preference, resolvedTheme, setPreference } = useTheme()
  return (
    <>
      <span>{preference} / {resolvedTheme}</span>
      <button onClick={() => setPreference('light')} type="button">浅色</button>
    </>
  )
}

it('uses the system theme until the user selects an explicit theme', async () => {
  installMatchMedia(true)
  render(
    <ThemeProvider>
      <ThemeProbe />
    </ThemeProvider>,
  )

  expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  expect(screen.getByText('system / dark')).toBeInTheDocument()
})

it('persists an explicit light theme and ignores later system changes', async () => {
  const media = installMatchMedia(true)
  const user = userEvent.setup()
  render(
    <ThemeProvider>
      <ThemeProbe />
    </ThemeProvider>,
  )

  await user.click(screen.getByRole('button', { name: '浅色' }))
  media.setMatches(false)

  expect(localStorage.getItem('designtoolkit-theme')).toBe('light')
  expect(document.documentElement).toHaveAttribute('data-theme', 'light')
})
```

- [ ] **Step 2: Run the theme tests and verify RED**

Run:

```bash
npm test -- --run src/app/theme/ThemeProvider.test.tsx
```

Expected: FAIL because `ThemeProvider` and `useTheme` do not exist.

- [ ] **Step 3: Implement the theme provider**

Implement the stable public API:

```tsx
export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

type ThemeContextValue = {
  preference: ThemePreference
  resolvedTheme: ResolvedTheme
  setPreference: (preference: ThemePreference) => void
}

const STORAGE_KEY = 'designtoolkit-theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'
const ThemeContext = createContext<ThemeContextValue | null>(null)

function readPreference(): ThemePreference {
  const stored = window.localStorage.getItem(STORAGE_KEY)
  return stored === 'light' || stored === 'dark' ? stored : 'system'
}

export function ThemeProvider({ children }: PropsWithChildren) {
  const [preference, setPreference] = useState<ThemePreference>(readPreference)
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(
    () => window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light',
  )
  const resolvedTheme = preference === 'system' ? systemTheme : preference

  useEffect(() => {
    if (preference !== 'system') return
    const media = window.matchMedia(DARK_QUERY)
    const syncTheme = () => setSystemTheme(media.matches ? 'dark' : 'light')
    syncTheme()
    media.addEventListener('change', syncTheme)
    return () => media.removeEventListener('change', syncTheme)
  }, [preference])

  useEffect(() => {
    if (preference === 'system') {
      window.localStorage.removeItem(STORAGE_KEY)
    } else {
      window.localStorage.setItem(STORAGE_KEY, preference)
    }
    document.documentElement.dataset.theme = resolvedTheme
    document.documentElement.style.colorScheme = resolvedTheme
  }, [preference, resolvedTheme])

  const value = useMemo(
    () => ({ preference, resolvedTheme, setPreference }),
    [preference, resolvedTheme],
  )
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext)
  if (!value) throw new Error('useTheme must be used within ThemeProvider')
  return value
}
```

Wrap `AppShell` in `ThemeProvider` from `App.tsx`.

Define OKLCH primitives and semantic tokens in `tokens.css`, including:

```css
:root {
  color-scheme: light;
  --surface-app: oklch(96.5% 0.008 215);
  --surface-panel: oklch(99% 0.004 215);
  --surface-raised: oklch(100% 0 0);
  --text-primary: oklch(23% 0.018 230);
  --text-secondary: oklch(43% 0.018 230);
  --border-subtle: oklch(88% 0.012 220);
  --accent: oklch(64% 0.135 165);
  --accent-strong: oklch(48% 0.12 165);
  --focus-ring: oklch(64% 0.135 165);
}

:root[data-theme='dark'] {
  color-scheme: dark;
  --surface-app: oklch(16% 0.012 230);
  --surface-panel: oklch(20% 0.014 230);
  --surface-raised: oklch(24% 0.014 230);
  --text-primary: oklch(94% 0.008 210);
  --text-secondary: oklch(74% 0.014 220);
  --border-subtle: oklch(31% 0.016 225);
  --accent: oklch(76% 0.12 165);
  --accent-strong: oklch(68% 0.13 165);
  --focus-ring: oklch(76% 0.12 165);
}
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
npm test -- --run src/app/theme/ThemeProvider.test.tsx
npm run lint
```

Expected: theme tests pass and TypeScript exits zero.

- [ ] **Step 5: Commit**

```bash
git add web/frontend/src/app/theme web/frontend/src/app/App.tsx web/frontend/src/styles
git commit -m "feat: add adaptive workbench themes"
```

### Task 2: Command Bar And Icon Tool Rail

**Files:**
- Modify: `web/frontend/src/app/types.ts`
- Modify: `web/frontend/src/app/modules.ts`
- Modify: `web/frontend/src/app/AppShell.tsx`
- Modify: `web/frontend/src/app/AppShell.test.tsx`
- Modify: `web/frontend/src/components/HeaderBar.tsx`
- Modify: `web/frontend/src/components/Sidebar.tsx`
- Modify: `web/frontend/src/components/TaskStatusBadge.tsx`

- [ ] **Step 1: Write failing shell tests**

Add focused expectations:

```tsx
it('shows the active module in the command bar', async () => {
  const user = userEvent.setup()
  render(<App />)

  expect(screen.getByRole('banner')).toHaveTextContent('视频转序列')
  await user.click(screen.getByRole('button', { name: '媒体尺寸修改' }))
  expect(screen.getByRole('banner')).toHaveTextContent('媒体尺寸修改')
})

it('changes theme from the command bar', async () => {
  const user = userEvent.setup()
  render(<App />)

  await user.click(screen.getByRole('button', { name: '主题设置' }))
  await user.click(screen.getByRole('menuitemradio', { name: '深色' }))

  expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
})
```

Retain all existing module-switching and task-race tests.

- [ ] **Step 2: Run shell tests and verify RED**

Run:

```bash
npm test -- --run src/app/AppShell.test.tsx
```

Expected: FAIL because the command bar has no active module text or theme menu.

- [ ] **Step 3: Implement shell metadata and controls**

Extend module metadata:

```ts
export type ToolModule = {
  component: ComponentType
  description: string
  icon: LucideIcon
  id: ModuleId
  label: string
}
```

Use `Film`, `Scaling`, and `Pipette` in `modules.ts`. Pass the active module to
`HeaderBar`. Render navigation icons with `aria-hidden="true"` and preserve the
button's label as its accessible name.

Implement `HeaderBar` with:

```tsx
<header className="command-bar">
  <div className="brand-lockup">
    <span className="brand-mark" aria-hidden="true"><PanelsTopLeft size={17} /></span>
    <span className="brand-name">DesignToolkit</span>
  </div>
  <div className="command-context">{activeModule.label}</div>
  <div className="command-actions">
    <TaskStatusBadge status={status} />
    <ThemeMenu />
    <button
      aria-controls="task-log-panel"
      aria-expanded={!logsCollapsed}
      aria-label={logsCollapsed ? '展开任务日志' : '折叠任务日志'}
      className="icon-button command-icon-button"
      onClick={onToggleLogs}
      title={logsCollapsed ? '展开任务日志' : '折叠任务日志'}
      type="button"
    >
      {logsCollapsed ? <PanelBottomOpen aria-hidden="true" /> : <PanelBottomClose aria-hidden="true" />}
    </button>
  </div>
</header>
```

Use a native `<details>` menu with `role="menu"` and
`role="menuitemradio"` theme choices. Close the menu after selection.

- [ ] **Step 4: Run shell tests and verify GREEN**

Run:

```bash
npm test -- --run src/app/AppShell.test.tsx src/app/theme/ThemeProvider.test.tsx
npm run lint
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add web/frontend/src/app web/frontend/src/components
git commit -m "feat: rebuild workbench command navigation"
```

### Task 3: Collapsible And Resizable Log Panel

**Files:**
- Modify: `web/frontend/src/app/AppShell.tsx`
- Modify: `web/frontend/src/components/LogPanel.tsx`
- Modify: `web/frontend/src/components/LogPanel.test.tsx`

- [ ] **Step 1: Write failing log interaction tests**

Define controlled behavior and keyboard bounds:

```tsx
function seedEvent(overrides: Partial<TaskEvent> = {}) {
  useLogStore.getState().clearEvents()
  useLogStore.getState().appendEvent({
    task_id: 'task-1',
    timestamp: '2026-09-10T12:00:00Z',
    level: 'info',
    phase: 'processing',
    message: 'Task running',
    details: {},
    ...overrides,
  })
}

function LogHarness({ initialHeight = 160 }: { initialHeight?: number }) {
  const [collapsed, setCollapsed] = useState(false)
  const [height, setHeight] = useState(initialHeight)
  return (
    <LogPanel
      collapsed={collapsed}
      height={height}
      onCollapsedChange={setCollapsed}
      onHeightChange={setHeight}
    />
  )
}

it('collapses to a latest-event summary', async () => {
  seedEvent({ message: 'Frame 12 exported' })
  const user = userEvent.setup()
  render(<LogHarness />)

  await user.click(screen.getByRole('button', { name: '折叠任务日志' }))

  expect(screen.getByRole('region', { name: '任务日志' })).toHaveAttribute(
    'data-collapsed',
    'true',
  )
  expect(screen.getByText('Frame 12 exported')).toBeInTheDocument()
  expect(screen.queryByRole('log')).not.toBeInTheDocument()
})

it('resizes with arrow keys and clamps to bounds', async () => {
  const user = userEvent.setup()
  render(<LogHarness initialHeight={160} />)
  const separator = screen.getByRole('separator', { name: '调整任务日志高度' })

  separator.focus()
  await user.keyboard('{ArrowUp}{ArrowUp}')

  expect(separator).toHaveAttribute('aria-valuenow', '192')
})
```

- [ ] **Step 2: Run log tests and verify RED**

Run:

```bash
npm test -- --run src/components/LogPanel.test.tsx
```

Expected: FAIL because collapse and separator controls do not exist.

- [ ] **Step 3: Implement controlled log layout**

Use these props:

```ts
type LogPanelProps = {
  collapsed: boolean
  height: number
  onCollapsedChange: (collapsed: boolean) => void
  onHeightChange: (height: number) => void
}
```

The separator uses:

```tsx
<div
  aria-label="调整任务日志高度"
  aria-orientation="horizontal"
  aria-valuemax={maxHeight}
  aria-valuemin={120}
  aria-valuenow={height}
  onKeyDown={handleResizeKeyDown}
  onPointerDown={handleResizePointerDown}
  role="separator"
  tabIndex={0}
/>
```

Clamp pointer and keyboard results to `120..Math.floor(innerHeight * 0.45)`.
Use 16px keyboard increments and preserve the last expanded height. Store
collapsed state and height in local storage from `AppShell`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
npm test -- --run src/components/LogPanel.test.tsx src/app/AppShell.test.tsx
npm run lint
```

Expected: log and shell tests pass.

- [ ] **Step 5: Commit**

```bash
git add web/frontend/src/app/AppShell.tsx web/frontend/src/components/LogPanel.tsx web/frontend/src/components/LogPanel.test.tsx
git commit -m "feat: add adjustable task log panel"
```

### Task 4: Shared Workbench Visual System

**Files:**
- Create: `web/frontend/src/styles/shell.css`
- Create: `web/frontend/src/styles/controls.css`
- Create: `web/frontend/src/styles/modules.css`
- Modify: `web/frontend/src/styles/app.css`
- Modify: `web/frontend/src/components/PathInput.tsx`
- Modify: `web/frontend/src/components/SubmitButton.tsx`

- [ ] **Step 1: Write a failing browser contract**

Create `web/frontend/e2e/workbench-ui.spec.ts`:

```ts
test('renders a stable desktop workbench shell', async ({ page }) => {
  await page.goto('/')

  await expect(page.locator('.app-shell')).toHaveCSS(
    'grid-template-columns',
    /72px/,
  )
  await expect(page.locator('.command-bar')).toHaveCSS('height', '52px')
  await expect(page.locator('body')).toHaveCSS('overflow', 'hidden')
})

test('keeps focus visible on keyboard navigation', async ({ page }) => {
  await page.goto('/')
  await page.keyboard.press('Tab')
  const focused = page.locator(':focus-visible')
  await expect(focused).toHaveCount(1)
  await expect(focused).toHaveCSS('outline-style', 'solid')
})
```

- [ ] **Step 2: Run the browser contract and verify RED**

Run:

```bash
npx playwright test e2e/workbench-ui.spec.ts
```

Expected: FAIL because `.command-bar`, the 72px rail, and unified focus
treatment are not implemented.

- [ ] **Step 3: Implement the style architecture**

Make `app.css` the import and reset entry:

```css
@import './tokens.css';
@import './shell.css';
@import './controls.css';
@import './modules.css';

* { box-sizing: border-box; }
html, body, #root { height: 100%; }
body {
  margin: 0;
  overflow: hidden;
  background: var(--surface-app);
  color: var(--text-primary);
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
}
```

Use a spacing scale from 4px to 32px, 4-8px control radii, semantic z-index
tokens, stable 36px desktop controls, and 44px controls under
`(pointer: coarse)`. Apply a shared 2px mint `:focus-visible` outline. Use
explicit surface tokens rather than transparent overlays.

Update `PathInput` and `SubmitButton` only where additional class hooks or
loading semantics are required; preserve their accessible labels and existing
behavior.

- [ ] **Step 4: Run browser and component tests and verify GREEN**

Run:

```bash
npx playwright test e2e/workbench-ui.spec.ts
npm test -- --run
npm run lint
```

Expected: Playwright shell checks pass and all 334 or more Vitest tests pass.

- [ ] **Step 5: Commit**

```bash
git add web/frontend/src/styles web/frontend/src/components web/frontend/e2e/workbench-ui.spec.ts
git commit -m "feat: apply professional workbench visual system"
```

### Task 5: Chroma Inspector And Responsive Dialog

**Files:**
- Create: `web/frontend/src/app/useMediaQuery.ts`
- Create: `web/frontend/src/app/useMediaQuery.test.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaInspector.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaInspector.test.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`
- Modify: `web/frontend/src/styles/modules.css`

- [ ] **Step 1: Write failing responsive inspector tests**

Define the hook and inspector behavior:

```tsx
it('updates when the media query changes', () => {
  const media = installMatchMedia(false)
  const { result } = renderHook(() => useMediaQuery('(max-width: 1179px)'))
  act(() => media.setMatches(true))
  expect(result.current).toBe(true)
})

it('opens the parameter inspector as a modal on compact screens', async () => {
  installMatchMedia(true)
  const user = userEvent.setup()
  render(<ChromaKeyerPage />)

  await user.click(screen.getByRole('button', { name: '打开参数检查器' }))
  expect(screen.getByRole('dialog', { name: '色键参数' })).toHaveAttribute(
    'open',
  )
})
```

Add a test that closes the dialog with its close button and restores focus to
the trigger.

- [ ] **Step 2: Run inspector tests and verify RED**

Run:

```bash
npm test -- --run src/app/useMediaQuery.test.tsx src/modules/chroma-keyer/ChromaInspector.test.tsx src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
```

Expected: FAIL because the hook, inspector component, and trigger do not exist.

- [ ] **Step 3: Implement responsive inspector behavior**

Implement `useMediaQuery` with a concrete external-store subscription:

```ts
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((notify: () => void) => {
    const media = window.matchMedia(query)
    media.addEventListener('change', notify)
    return () => media.removeEventListener('change', notify)
  }, [query])
  const getSnapshot = useCallback(
    () => window.matchMedia(query).matches,
    [query],
  )
  return useSyncExternalStore(subscribe, getSnapshot, () => false)
}
```

`ChromaInspector` accepts:

```ts
type ChromaInspectorProps = PropsWithChildren<{
  compact: boolean
  onOpenChange: (open: boolean) => void
  open: boolean
  triggerRef: RefObject<HTMLButtonElement | null>
}>
```

For wide layouts, render an `<aside aria-label="色键参数">`. For compact
layouts, render a native `<dialog aria-labelledby="chroma-inspector-title">`,
call `showModal()` when opened, close on Escape or backdrop dismissal, and
restore focus to `triggerRef`.

Refactor `ChromaKeyerPage` into:

```tsx
<section className="module-surface chroma-keyer-surface">
  <div className="module-header chroma-module-header">
    <h1 id="chroma-keyer-title">
      <Pipette aria-hidden="true" size={20} />
      色键抠图
    </h1>
    {compact ? (
      <button
        aria-haspopup="dialog"
        className="secondary-action-button chroma-inspector-trigger"
        onClick={() => setInspectorOpen(true)}
        ref={inspectorTriggerRef}
        type="button"
      >
        <SlidersHorizontal aria-hidden="true" size={17} />
        打开参数检查器
      </button>
    ) : null}
  </div>
  <div className="chroma-workbench">
    <div className="chroma-stage">
      {media && previewUrl ? (
        <ChromaKeyerCanvas
          disabled={disabledByTask || isUploadPending || playback.isPlaying || videoPreviewOutOfDate}
          onSample={handleSample}
          originalHeight={media.height}
          originalWidth={media.width}
          previewUrl={previewUrl}
          samples={sampleDetails}
          viewMode={values.viewMode}
        />
      ) : (
        <div className="chroma-empty-preview">选择图片或视频以开始抠像</div>
      )}
      <div className="chroma-sample-toolbar">
        <h2 className="chroma-section-title">采样颜色</h2>
        <button
          aria-label="清除全部采样点"
          className="icon-button"
          disabled={disabled || sampleDetails.length === 0}
          onClick={clearSamples}
          title="清除全部采样点"
          type="button"
        >
          <Eraser aria-hidden="true" size={18} />
        </button>
      </div>
      <FieldError message={errors.samples} />
      <ol className="chroma-sample-list">
        {sampleDetails.map((sample) => (
          <li className="chroma-sample-item" key={`${sample.sourceX}-${sample.sourceY}`}>
            <span aria-hidden="true" className="chroma-swatch" style={{ backgroundColor: sample.hex }} />
            <span>Screen Color</span>
            <span>{`RGB(${sample.rgb.join(', ')})`}</span>
            <span>{sample.hex}</span>
            <span>{sample.sourceX}, {sample.sourceY}</span>
            <button
              aria-label="删除 Screen Color"
              className="icon-button"
              disabled={disabled}
              onClick={removeSample}
              title="删除 Screen Color"
              type="button"
            >
              <Trash2 aria-hidden="true" size={17} />
            </button>
          </li>
        ))}
      </ol>
    </div>
    <ChromaInspector
      compact={compact}
      onOpenChange={setInspectorOpen}
      open={inspectorOpen}
      triggerRef={inspectorTriggerRef}
    >
      <ChromaKeyerForm
        disabled={disabled}
        errors={errors}
        isUploadPending={isUploadPending}
        onChange={updateValues}
        onInputPathCommit={(path) => void handleInspect(path)}
        onSubmit={() => void handleSubmit()}
        onUploadPendingChange={handleUploadPendingChange}
        values={values}
      />
    </ChromaInspector>
  </div>
</section>
```

The existing video timeline remains a child of `ChromaKeyerCanvas`. Split
`ChromaKeyerForm` into media, parameters, preview, output, and sticky action
sections without changing values, callbacks, labels, or payloads.

- [ ] **Step 4: Run focused and full tests**

Run:

```bash
npm test -- --run src/app/useMediaQuery.test.tsx src/modules/chroma-keyer/ChromaInspector.test.tsx src/modules/chroma-keyer/ChromaKeyerPage.test.tsx
npm test -- --run
npm run lint
```

Expected: focused tests pass and all existing tests remain green.

- [ ] **Step 5: Commit**

```bash
git add web/frontend/src/app/useMediaQuery* web/frontend/src/modules/chroma-keyer web/frontend/src/styles/modules.css
git commit -m "feat: add responsive chroma inspector"
```

### Task 6: Responsive, Accessibility, And Visual Verification

**Files:**
- Modify: `web/frontend/e2e/workbench-ui.spec.ts`
- Modify: `web/frontend/src/styles/shell.css`
- Modify: `web/frontend/src/styles/controls.css`
- Modify: `web/frontend/src/styles/modules.css`
- Modify: `web/frontend/src/components/HeaderBar.tsx`
- Modify: `web/frontend/src/components/Sidebar.tsx`

- [ ] **Step 1: Add failing responsive and theme browser tests**

Extend the Playwright contract:

```ts
test.describe('responsive workbench', () => {
  for (const viewport of [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'tablet', width: 900, height: 900 },
    { name: 'phone', width: 390, height: 844 },
  ]) {
    test(`${viewport.name} has no horizontal overflow`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await page.goto('/')
      const dimensions = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }))
      expect(dimensions.scrollWidth).toBe(dimensions.clientWidth)
    })
  }
})

test('persists dark mode across reloads', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '主题设置' }).click()
  await page.getByRole('menuitemradio', { name: '深色' }).click()
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
})
```

Add compact-viewport inspector tests, log min/max resize assertions, reduced
motion checks, and a console error collector.

- [ ] **Step 2: Run browser tests and verify RED**

Run:

```bash
npx playwright test e2e/workbench-ui.spec.ts
```

Expected: at least one compact-layout, overflow, or reduced-motion assertion
fails before final responsive CSS is complete.

- [ ] **Step 3: Complete responsive and accessibility styling**

Implement the approved breakpoints:

```css
@media (max-width: 1179px) {
  .chroma-workbench { grid-template-columns: minmax(0, 1fr); }
  .chroma-inspector-trigger { display: inline-flex; }
}

@media (max-width: 759px) {
  .app-shell { grid-template-areas: "header" "nav" "workspace" "logs"; }
  .tool-rail { flex-direction: row; overflow-x: auto; }
  .module-form { grid-template-columns: minmax(0, 1fr); }
  .chroma-inspector-dialog { height: 100dvh; inset: 0; width: 100vw; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
  }
}
```

Add safe-area padding for full-screen mobile overlays, pointer-capability touch
targets, wrapping for long values, and explicit z-index tokens.

- [ ] **Step 4: Run complete automated verification**

Run:

```bash
npm test -- --run
npm run lint
npm run build
npx playwright test
```

Expected:

- 334 or more Vitest tests pass;
- TypeScript exits zero;
- Vite production build exits zero;
- local Playwright workbench tests pass;
- hosted-only tests skip unless `VITE_HOSTED_MODE=true`.

- [ ] **Step 5: Run browser visual verification**

Start the development server:

```bash
npm run dev -- --host 127.0.0.1 --port 5174
```

Capture and inspect screenshots at:

- 1440x900 light theme;
- 1440x900 dark theme;
- 900x900 compact inspector;
- 390x844 phone navigation and full-screen inspector.

For every viewport, inspect all three modules and verify:

- no blank or clipped primary regions;
- no incoherent overlap;
- no horizontal overflow;
- stable control dimensions;
- visible keyboard focus;
- collapsed and expanded log behavior;
- no browser console errors.

- [ ] **Step 6: Commit**

```bash
git add web/frontend
git commit -m "fix: finish responsive workbench polish"
```

### Task 7: Final Quality Gate

**Files:**
- Verify all files changed by Tasks 1-6.

- [ ] **Step 1: Inspect the final diff**

Run:

```bash
git diff --check
git status --short
git diff --stat main...HEAD
```

Expected: no whitespace errors and only planned frontend, test, and plan files
are changed.

- [ ] **Step 2: Run the final verification suite**

Run:

```bash
npm test -- --run
npm run lint
npm run build
npx playwright test
```

Expected: all available checks pass with no unexpected warnings.

- [ ] **Step 3: Verify local production service behavior**

Rebuild the frontend-serving image or run the Vite preview, then verify:

```bash
curl -I http://127.0.0.1:5174/
```

Expected: HTTP 200.

- [ ] **Step 4: Record remaining environmental limits**

Document any checks that require physical touch hardware or hosted mode. Do not
claim those checks passed unless they were actually run.
