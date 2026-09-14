# Review Top Five Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the five highest-priority review findings while preserving supported cross-container video conversion and making the standard verification suite fail closed.

**Architecture:** Media resize command construction becomes media-aware: images keep exact bounds, videos keep even dimensions, and cross-container audio uses an explicit compatible codec. The app shell consumes existing global creation state to prevent module unmount during task creation. Playwright uses isolated local and hosted projects with separate Vite servers, and the standard command no longer accepts an empty test suite.

**Tech Stack:** Python 3.12, pytest, Ruff, FFmpeg, React 19, TypeScript, Vitest, Testing Library, Playwright, Docker Compose

---

## File Map

- Create `tests/test_media_resize.py`: media resize command, validation, and cleanup regressions.
- Modify `src/designtoolkit/media_resize/core.py`: media-aware filters, audio strategy, and failed-output cleanup.
- Modify `web/frontend/src/components/Sidebar.tsx`: native disabled navigation state.
- Modify `web/frontend/src/app/AppShell.tsx`: consume global `isCreating` and disable navigation.
- Modify `web/frontend/src/app/AppShell.test.tsx`: deferred create/navigation regression tests.
- Modify `web/frontend/playwright.config.ts`: separate local and hosted projects and servers.
- Modify `web/frontend/e2e/hosted.spec.ts`: make hosted tests unconditional inside the hosted project.
- Modify `web/frontend/package.json`: remove `--pass-with-no-tests`.

### Task 1: Media-Aware Resize Commands

**Files:**
- Create: `tests/test_media_resize.py`
- Modify: `src/designtoolkit/media_resize/core.py`

- [ ] **Step 1: Add failing image-bound tests**

Create a fake backend that records commands and add tests proving image
resizes do not request even rounding:

```python
from pathlib import Path

import pytest

from designtoolkit.media_resize import (
    MediaResizeError,
    MediaResizeOptions,
    ResizeBackendResult,
    resize_media,
)


class RecordingBackend:
    def __init__(self, result: ResizeBackendResult | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.result = result or ResizeBackendResult(returncode=0)

    def get_ffmpeg_exe(self) -> str:
        return "ffmpeg"

    def run(self, command: list[str]) -> ResizeBackendResult:
        self.commands.append(tuple(command))
        return self.result


@pytest.mark.parametrize("size", [(1, 1), (1, 10), (10, 1)])
def test_image_resize_preserves_one_pixel_bounds(tmp_path: Path, size: tuple[int, int]) -> None:
    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"image")
    backend = RecordingBackend()

    result = resize_media(
        MediaResizeOptions(input_path=input_path, size=size),
        backend,
    )

    command = backend.commands[0]
    scale = command[command.index("-vf") + 1]
    assert scale == f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease"
    assert "force_divisible_by" not in scale
    assert result.output_path == tmp_path / "input_resized.png"
```

- [ ] **Step 2: Verify the image tests fail**

Run:

```powershell
uv run pytest tests/test_media_resize.py -k one_pixel -v
```

Expected: FAIL because the command still contains `force_divisible_by=2`.

- [ ] **Step 3: Add failing video dimension and audio tests**

Add tests for video bounds and command construction:

```python
@pytest.mark.parametrize("size", [(1, 10), (10, 1), (1, 1)])
def test_video_resize_rejects_sub_two_pixel_bounds(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"video")
    backend = RecordingBackend()

    with pytest.raises(MediaResizeError, match="at least 2"):
        resize_media(MediaResizeOptions(input_path=input_path, size=size), backend)

    assert backend.commands == []


@pytest.mark.parametrize(
    ("source_suffix", "target_suffix", "audio_codec"),
    [
        (".mkv", ".webm", "libopus"),
        (".mkv", ".mp4", "aac"),
        (".mkv", ".mov", "aac"),
        (".mkv", ".m4v", "aac"),
        (".mkv", ".avi", "libmp3lame"),
        (".webm", ".mkv", "aac"),
    ],
)
def test_cross_container_video_uses_compatible_audio(
    tmp_path: Path,
    source_suffix: str,
    target_suffix: str,
    audio_codec: str,
) -> None:
    input_path = tmp_path / f"input{source_suffix}"
    output_path = tmp_path / f"output{target_suffix}"
    input_path.write_bytes(b"video")
    backend = RecordingBackend()

    resize_media(
        MediaResizeOptions(
            input_path=input_path,
            output_path=output_path,
            size=(640, 360),
        ),
        backend,
    )

    command = backend.commands[0]
    assert ("-map", "0:v:0") == tuple(command[command.index("-map"):command.index("-map") + 2])
    assert "0:a?" in command
    assert command[command.index("-c:a") + 1] == audio_codec


def test_same_container_video_copies_optional_audio(tmp_path: Path) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"video")
    backend = RecordingBackend()

    resize_media(
        MediaResizeOptions(input_path=input_path, output_path=output_path, size=(640, 360)),
        backend,
    )

    command = backend.commands[0]
    assert "0:a?" in command
    assert command[command.index("-c:a") + 1] == "copy"
    assert "force_divisible_by=2" in command[command.index("-vf") + 1]
```

- [ ] **Step 4: Verify the video tests fail**

Run:

```powershell
uv run pytest tests/test_media_resize.py -k "video or container" -v
```

Expected: FAIL because sub-two-pixel video bounds are accepted and all audio
uses copy.

- [ ] **Step 5: Add a failing partial-output cleanup test**

```python
def test_failed_resize_removes_partial_output(tmp_path: Path) -> None:
    input_path = tmp_path / "input.png"
    output_path = tmp_path / "output.png"
    input_path.write_bytes(b"image")

    class PartialOutputBackend(RecordingBackend):
        def run(self, command: list[str]) -> ResizeBackendResult:
            Path(command[-1]).write_bytes(b"partial")
            return ResizeBackendResult(returncode=1, stderr="encode failed")

    with pytest.raises(MediaResizeError, match="encode failed"):
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                output_path=output_path,
                size=(100, 100),
            ),
            PartialOutputBackend(),
        )

    assert not output_path.exists()
```

- [ ] **Step 6: Verify the cleanup test fails**

Run:

```powershell
uv run pytest tests/test_media_resize.py -k partial_output -v
```

Expected: FAIL because the partial file remains.

- [ ] **Step 7: Implement media-aware validation and command construction**

Add:

```python
AUDIO_CODEC_BY_VIDEO_SUFFIX = {
    ".avi": "libmp3lame",
    ".m4v": "aac",
    ".mkv": "aac",
    ".mov": "aac",
    ".mp4": "aac",
    ".webm": "libopus",
}
```

In `resize_media`, determine the validated media type, reject video target
bounds below two, and pass `media_type` to `_build_ffmpeg_command`.

Build image filters without even rounding. Build video filters with
`force_divisible_by=2`, map `0:v:0` and optional `0:a?`, copy audio only when
normalized suffixes match, and otherwise select the target codec from the
mapping.

On a non-zero backend result:

```python
cleanup_error: OSError | None = None
try:
    output_path.unlink(missing_ok=True)
except OSError as exc:
    cleanup_error = exc

detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
if cleanup_error is not None:
    detail += f"; failed to remove partial output: {cleanup_error}"
raise MediaResizeError(
    f"FFmpeg failed with exit code {result.returncode}{detail}",
    exit_code=1,
)
```

- [ ] **Step 8: Run backend tests and static checks**

Run:

```powershell
uv run pytest tests/test_media_resize.py -v
uv run ruff check src tests
uv run python -m compileall -q src tests
```

Expected: all commands exit 0.

- [ ] **Step 9: Commit media resize fixes**

```powershell
git add src/designtoolkit/media_resize/core.py tests/test_media_resize.py
git commit -m "fix: harden media resize commands"
```

### Task 2: Preserve Task Creation Across Navigation

**Files:**
- Modify: `web/frontend/src/components/Sidebar.tsx`
- Modify: `web/frontend/src/app/AppShell.tsx`
- Modify: `web/frontend/src/app/AppShell.test.tsx`

- [ ] **Step 1: Write the failing deferred-create test**

Replace the existing test that navigates during creation with:

```tsx
it('keeps the submitting module mounted until task creation resolves', async () => {
  const user = userEvent.setup()
  const request = createDeferred<TaskCreateResponse>()
  serviceMocks.createVideoFramesTask.mockReturnValue(request.promise)
  renderShell()

  await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
  await user.click(screen.getByRole('button', { name: '开始导出' }))

  const resizeModule = screen.getByRole('button', { name: '媒体尺寸修改' })
  expect(resizeModule).toBeDisabled()
  await user.click(resizeModule)
  expect(screen.getByRole('heading', { name: '视频转序列' })).toBeInTheDocument()

  await act(async () => {
    request.resolve({ task_id: 'task-vf-1', status: 'queued' })
    await request.promise
  })

  expect(useTaskStore.getState()).toMatchObject({
    currentTaskId: 'task-vf-1',
    isCreating: false,
    status: 'queued',
  })
  expect(resizeModule).toBeEnabled()
})
```

Add a rejected-request case proving navigation is restored after failure.

- [ ] **Step 2: Run the test and verify the red state**

Run:

```powershell
cd web/frontend
npm test -- src/app/AppShell.test.tsx
```

Expected: FAIL because navigation buttons remain enabled.

- [ ] **Step 3: Add the disabled Sidebar contract**

Update `SidebarProps` and its buttons:

```tsx
type SidebarProps = {
  activeModuleId: ModuleId
  disabled?: boolean
  modules: ToolModule[]
  onModuleSelect: (moduleId: ModuleId) => void
}

export function Sidebar({
  activeModuleId,
  disabled = false,
  modules,
  onModuleSelect,
}: SidebarProps) {
  // ...
  return <button disabled={disabled} /* existing props */ />
}
```

Read global creation state in `AppShell`:

```tsx
const isCreating = useTaskStore((state) => state.isCreating)

<Sidebar
  activeModuleId={activeModuleId}
  disabled={isCreating}
  modules={toolModules}
  onModuleSelect={setActiveModuleId}
/>
```

- [ ] **Step 4: Run AppShell and task-store tests**

Run:

```powershell
npm test -- src/app/AppShell.test.tsx src/state/useTaskStore.test.ts
npm run lint
```

Expected: all tests pass and TypeScript exits 0.

- [ ] **Step 5: Commit navigation protection**

```powershell
git add web/frontend/src/components/Sidebar.tsx web/frontend/src/app/AppShell.tsx web/frontend/src/app/AppShell.test.tsx
git commit -m "fix: preserve pending task creation"
```

### Task 3: Fail-Closed Local And Hosted E2E

**Files:**
- Modify: `web/frontend/playwright.config.ts`
- Modify: `web/frontend/e2e/hosted.spec.ts`
- Modify: `web/frontend/package.json`

- [ ] **Step 1: Capture the hosted-test red state**

Run:

```powershell
cd web/frontend
npm run test:e2e
```

Expected before the change: 26 tests pass and 2 hosted tests are skipped.

- [ ] **Step 2: Capture the empty-suite red state**

Run:

```powershell
npm run test:e2e -- --grep "__no_such_test__"
```

Expected before the change: exit code 0 because `--pass-with-no-tests` is
present.

- [ ] **Step 3: Configure isolated local and hosted projects**

Use two project/server pairs:

```ts
const localUrl = 'http://127.0.0.1:5173'
const hostedUrl = 'http://127.0.0.1:5175'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  outputDir: '../../.temp/playwright/test-results',
  reporter: [['html', {
    open: 'never',
    outputFolder: '../../.temp/playwright/report',
  }]],
  use: {
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'local',
      testIgnore: /hosted\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: localUrl },
    },
    {
      name: 'hosted',
      testMatch: /hosted\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: hostedUrl },
    },
  ],
  webServer: [
    {
      command: 'npm run dev -- --port 5173',
      reuseExistingServer: true,
      url: localUrl,
    },
    {
      command: 'npm run dev -- --port 5175',
      env: { ...process.env, VITE_HOSTED_MODE: 'true' },
      reuseExistingServer: true,
      url: hostedUrl,
    },
  ],
})
```

Remove the file-level `test.skip` from `hosted.spec.ts`.

Change:

```json
"test:e2e": "playwright test e2e"
```

- [ ] **Step 4: Verify hosted tests execute**

Run:

```powershell
npm run test:e2e
```

Expected: all 28 tests pass with zero skipped.

- [ ] **Step 5: Verify empty discovery fails**

Run:

```powershell
npm run test:e2e -- --grep "__no_such_test__"
```

Expected: non-zero exit with `Error: No tests found`.

- [ ] **Step 6: Commit fail-closed E2E configuration**

```powershell
git add web/frontend/playwright.config.ts web/frontend/e2e/hosted.spec.ts web/frontend/package.json
git commit -m "test: run local and hosted e2e suites"
```

### Task 4: Full Verification And Deployment

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run complete backend verification**

```powershell
uv sync
uv run ruff check src tests
uv run python -m compileall -q src tests
uv run pytest
```

Expected: zero Ruff findings, compileall exits 0, all pytest tests pass.

- [ ] **Step 2: Run complete frontend verification**

```powershell
cd web/frontend
npm ci
npm test
npm run lint
npm run build
npm run test:e2e
```

Expected: all Vitest and all 28 Playwright tests pass with zero skipped;
TypeScript and production build exit 0.

- [ ] **Step 3: Verify the empty-suite guard again**

```powershell
npm run test:e2e -- --grep "__no_such_test__"
```

Expected: non-zero exit specifically because no tests are found.

- [ ] **Step 4: Request final code review**

Invoke `requesting-code-review` for the full branch against its base. Resolve
all confirmed Critical and Important findings with failing regression tests,
then re-run the affected suites.

- [ ] **Step 5: Rebuild Docker from the completed branch**

From the repository root:

```powershell
docker compose --env-file ..\..\.env up -d --build
docker compose --env-file ..\..\.env ps
```

Expected: six services running, with `app` and `redis` healthy.

- [ ] **Step 6: Verify HTTPS and built assets**

```powershell
$response = Invoke-WebRequest https://localhost/ -UseBasicParsing
$response.StatusCode
$response.Content | Select-String 'assets/index-'
```

Expected: status 200 and hashed JS/CSS assets from the rebuilt image.

- [ ] **Step 7: Verify final repository state**

```powershell
git diff main...HEAD --check
git status --short
git log --oneline main..HEAD
```

Expected: no whitespace errors, no untracked implementation artifacts, and
focused commits for the specification and three implementation units.
