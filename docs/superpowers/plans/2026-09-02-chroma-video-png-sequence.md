# Chroma Video PNG Sequence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add timestamp-based video sampling and previews plus streaming full-video export to an atomic transparent PNG sequence.

**Architecture:** New chroma video domain modules own metadata, FFmpeg frame access, and one-frame-at-a-time export through the existing `key_rgb` function. Video-specific FastAPI routes preserve image compatibility, while the existing React Chroma Keyer becomes an image/video media-session UI with a timestamp timeline.

**Tech Stack:** Python 3.12, NumPy, Pillow, imageio-ffmpeg, FastAPI/Pydantic, React, TypeScript, Vitest, pytest, Playwright.

---

### Task 1: Define Video Domain Contracts

**Files:**
- Create: `src/designtoolkit/chroma_keyer/video_models.py`
- Modify: `src/designtoolkit/chroma_keyer/__init__.py`
- Create: `tests/test_chroma_keyer_video_models.py`

- [ ] **Step 1: Write failing model tests**

Define the desired API in tests:

```python
metadata = VideoMetadata(
    width=1920,
    height=1080,
    fps=30.0,
    duration_seconds=12.5,
    estimated_frame_count=375,
    codec="h264",
)
assert metadata.frame_step_seconds == approx(1 / 30)

options = VideoKeyOptions(
    input_path=Path("screen.mp4"),
    output_root=Path("out"),
)
assert options.input_path == Path("screen.mp4")
```

Parametrize invalid bool, zero, negative, NaN, and infinity values. Test
`validate_timestamp` accepts zero, clamps a requested final position to the
last frame time, and rejects values outside `[0, duration)`.

- [ ] **Step 2: Run RED tests**

```bash
uv run pytest tests/test_chroma_keyer_video_models.py -q
```

Expected: import failure for the new video contracts.

- [ ] **Step 3: Implement immutable contracts**

Create:

```python
@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    fps: float
    duration_seconds: float
    estimated_frame_count: int
    codec: str

@dataclass(frozen=True)
class VideoKeyOptions:
    input_path: Path
    output_root: Path | None = None

@dataclass(frozen=True)
class VideoKeyResult:
    output_dir: Path
    manifest_path: Path
    frame_count: int
    metadata: VideoMetadata

class VideoDecoder(Protocol):
    def probe(self, path: Path) -> VideoMetadata: ...
    def read_frame(self, path: Path, timestamp_seconds: float) -> np.ndarray: ...
    def read_preview_frame(
        self, path: Path, timestamp_seconds: float
    ) -> np.ndarray: ...
    def iter_frames(
        self, path: Path
    ) -> tuple[VideoMetadata, Iterator[np.ndarray]]: ...
```

Use `KeyerError` for contract failures. Add `ProgressCallback` taking processed
frames, estimated total, and current file path.

- [ ] **Step 4: Run GREEN tests and commit**

```bash
uv run pytest tests/test_chroma_keyer_video_models.py -q
uv run ruff check src/designtoolkit/chroma_keyer tests/test_chroma_keyer_video_models.py
git add src/designtoolkit/chroma_keyer/video_models.py src/designtoolkit/chroma_keyer/__init__.py tests/test_chroma_keyer_video_models.py
git commit -m "feat: define chroma video contracts"
```

### Task 2: Implement Bundled FFmpeg Frame Access

**Files:**
- Create: `src/designtoolkit/chroma_keyer/video_io.py`
- Create: `tests/test_chroma_keyer_video_io.py`

- [ ] **Step 1: Write failing adapter tests**

Inject a fake `read_frames` function whose first item is:

```python
{
    "codec": "h264",
    "source_size": (4, 2),
    "size": (4, 2),
    "fps": 25.0,
    "duration": 2.0,
}
```

Assert probe normalization, timestamp seek parameters, RGB shape/dtype, preview
bounding, ordered lazy iteration, generator close, missing metadata rejection,
short frame rejection, zero-frame behavior, and supported suffix validation.

- [ ] **Step 2: Run RED tests**

```bash
uv run pytest tests/test_chroma_keyer_video_io.py -q
```

Expected: import failure for `ImageioVideoDecoder`.

- [ ] **Step 3: Implement the adapter**

Use `imageio_ffmpeg.read_frames(path, pix_fmt="rgb24", ...)`. Convert frame
bytes with:

```python
expected = width * height * 3
if len(frame_bytes) != expected:
    raise KeyerError("Decoded video frame has an invalid byte length")
frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(height, width, 3)
```

For a timestamp read, use the same normalized `-ss` input parameters for source
and preview reads and stop after one frame. Use `resize_preview_rgb` for the
bounded return. Always close generators in `finally`.

- [ ] **Step 4: Add bundled FFmpeg integration tests**

Generate tiny deterministic clips in `tmp_path` with the bundled executable:

```bash
ffmpeg -f lavfi -i color=c=0x00ff00:s=16x12:r=5:d=0.6 ...
```

Create MP4/MOV with a supported built-in encoder and WebM with `libvpx-vp9`.
Assert all three probe and decode at least one frame. Skip only when FFmpeg
explicitly reports an unavailable encoder, not for generic failures.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
uv run pytest tests/test_chroma_keyer_video_io.py -q
uv run ruff check src/designtoolkit/chroma_keyer/video_io.py tests/test_chroma_keyer_video_io.py
git add src/designtoolkit/chroma_keyer/video_io.py tests/test_chroma_keyer_video_io.py
git commit -m "feat: decode chroma video frames"
```

### Task 3: Stream Keyed Frames Into An Atomic Sequence

**Files:**
- Create: `src/designtoolkit/chroma_keyer/video_export.py`
- Modify: `src/designtoolkit/chroma_keyer/__init__.py`
- Create: `tests/test_chroma_keyer_video_export.py`

- [ ] **Step 1: Write failing successful-export tests**

Use a fake decoder that records iteration and yields three frames. Spy on
`key_rgb`. Assert:

```python
assert result.frame_count == 3
assert sorted(path.name for path in result.output_dir.glob("*.png")) == [
    "clip_keyed_000001.png",
    "clip_keyed_000002.png",
    "clip_keyed_000003.png",
]
```

Open each PNG and assert RGBA/source dimensions. Parse `manifest.json` and
assert actual frame count, metadata, sample, params, and pattern. Assert each
frame is keyed before the decoder produces the next frame to guard against
materializing the stream.

- [ ] **Step 2: Write failing atomicity tests**

Cover:

- final directory already exists;
- output root missing or not a directory;
- decoder failure after frame one;
- `key_rgb` failure;
- PNG writer failure;
- manifest writer failure;
- final rename failure;
- zero decoded frames.

Every failure must leave the final directory absent and remove all matching
hidden temporary directories.

- [ ] **Step 3: Run RED tests**

```bash
uv run pytest tests/test_chroma_keyer_video_export.py -q
```

Expected: import failure for `key_video_to_png_sequence`.

- [ ] **Step 4: Implement streaming export**

Compute:

```python
final_dir = output_root / f"{input_path.stem}_keyed_frames"
temporary_dir = output_root / (
    f".{input_path.stem}_keyed_frames.{uuid4().hex}.tmp"
)
```

Create only the temporary directory. For each frame call `key_rgb`, then
`write_rgba_png` to the next six-digit path. Write UTF-8 JSON with
`ensure_ascii=False`, flush it by closing the file, then call
`temporary_dir.replace(final_dir)`. Wrap the whole operation in cleanup logic
using `shutil.rmtree`.

- [ ] **Step 5: Verify progress and commit**

Assert callback records are monotonic, include the current temporary PNG, and
end at the actual count.

```bash
uv run pytest tests/test_chroma_keyer_video_export.py -q
uv run ruff check src/designtoolkit/chroma_keyer/video_export.py tests/test_chroma_keyer_video_export.py
git add src/designtoolkit/chroma_keyer/video_export.py src/designtoolkit/chroma_keyer/__init__.py tests/test_chroma_keyer_video_export.py
git commit -m "feat: export keyed video PNG sequences"
```

### Task 4: Add Video Inspect, Frame, Sample, And Preview APIs

**Files:**
- Modify: `src/designtoolkit/web/schemas.py`
- Modify: `src/designtoolkit/web/routes.py`
- Modify: `src/designtoolkit/web/server.py`
- Modify: `tests/test_web_chroma_keyer.py`

- [ ] **Step 1: Write failing schema tests**

Define strict request/response schemas for:

- video inspect;
- timestamp frame;
- timestamp sample;
- timestamp Final/Alpha preview.

Assert finite timestamps, exact one sample for keyed preview, and extra-field
rejection.

- [ ] **Step 2: Write failing route tests**

Inject a fake `VideoDecoder` into `create_router`/server construction. Assert:

1. inspect returns metadata and first-frame preview;
2. frame returns an unkeyed preview at timestamp;
3. sample maps preview coordinates against source dimensions and samples the
   full-resolution timestamp frame;
4. Final and Alpha preview use bounded timestamp frames plus public `key_rgb`;
5. cache keys change with timestamp, params, view, and source fingerprint;
6. changed files never reuse old entries;
7. decoder and timestamp errors use structured API errors.

- [ ] **Step 3: Run RED tests**

```bash
uv run pytest tests/test_web_chroma_keyer.py -q
```

Expected: missing schemas, routes, and decoder injection.

- [ ] **Step 4: Implement video routes**

Add:

```text
POST /api/chroma-keyer/videos/inspect
POST /api/chroma-keyer/videos/frame
POST /api/chroma-keyer/videos/sample
POST /api/chroma-keyer/videos/preview
```

Reuse `ChromaPreviewStore`, `map_preview_point`, `screen_model_from_samples`,
`KeyerParams`, `key_rgb`, and `make_preview_png`. Include timestamp in every
generated cache key.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
uv run pytest tests/test_web_chroma_keyer.py -q
uv run ruff check src/designtoolkit/web tests/test_web_chroma_keyer.py
git add src/designtoolkit/web/schemas.py src/designtoolkit/web/routes.py src/designtoolkit/web/server.py tests/test_web_chroma_keyer.py
git commit -m "feat: expose chroma video preview APIs"
```

### Task 5: Integrate Video Export Tasks And Progress

**Files:**
- Modify: `src/designtoolkit/web/schemas.py`
- Modify: `src/designtoolkit/web/adapters.py`
- Modify: `src/designtoolkit/web/routes.py`
- Modify: `src/designtoolkit/web/tasks.py`
- Modify: `tests/test_web_chroma_keyer.py`

- [ ] **Step 1: Write failing task request tests**

Add `ChromaVideoKeyerTaskRequest` with input path, exact one sample, params, and
optional output root. Assert old image request remains unchanged.

- [ ] **Step 2: Write failing task manager tests**

Inject a video exporter and decoder. Assert:

- `/api/tasks/chroma-keyer-video` reserves a `chroma_keyer` task;
- conflict behavior matches image export;
- output path is the computed final sequence directory;
- progress is throttled to at most about 100 events, includes frame one and
  final frame, and is monotonic;
- success logs actual frame count and elapsed time;
- exporter failure marks task failed and releases the active-task slot.

- [ ] **Step 3: Run RED tests**

```bash
uv run pytest tests/test_web_chroma_keyer.py -q
```

Expected: missing task schema, adapter, route, and manager path.

- [ ] **Step 4: Implement task integration**

Add a separate video-keyer callable to `TaskManager`. Build options/model/params
in the adapter. Use one decoder instance per export. Throttle progress using:

```python
interval = max(1, estimated_total // 100) if estimated_total else 30
```

Always log processed frame one and the terminal actual frame count.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
uv run pytest tests/test_web_chroma_keyer.py -q
uv run ruff check src/designtoolkit/web tests/test_web_chroma_keyer.py
git add src/designtoolkit/web/schemas.py src/designtoolkit/web/adapters.py src/designtoolkit/web/routes.py src/designtoolkit/web/tasks.py tests/test_web_chroma_keyer.py
git commit -m "feat: run chroma video export tasks"
```

### Task 6: Add Typed Frontend Video Services And State

**Files:**
- Modify: `web/frontend/src/services/apiClient.ts`
- Modify: `web/frontend/src/services/apiClient.test.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts`

- [ ] **Step 1: Write failing API client tests**

Add typed wrappers for video inspect, frame, sample, preview, and task creation.
Assert snake-case wire payloads include timestamp and that export uses
`output_root`, not image `output_path`.

- [ ] **Step 2: Write failing media-state tests**

Extend values with:

```typescript
mediaKind: 'image' | 'video' | null
timestampSeconds: number
outputRoot: string
```

Validate image output paths and video output roots separately. Add
`isVideoPath` suffix tests for MOV/MP4/M4V/WebM/MKV/AVI and case variants.

- [ ] **Step 3: Run RED tests**

```bash
npm --prefix web/frontend test -- apiClient.test.ts chromaKeyerValidation.test.ts
```

Expected: missing types, wrappers, and media state.

- [ ] **Step 4: Implement typed services and validation**

Keep image wrappers backward compatible. Add discriminated response types and
video payload serialization. Do not infer video from MIME; use normalized
suffix only to select the inspect endpoint, while backend remains authoritative.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test -- apiClient.test.ts chromaKeyerValidation.test.ts
npm --prefix web/frontend run lint
git add web/frontend/src/services/apiClient.ts web/frontend/src/services/apiClient.test.ts web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.ts web/frontend/src/modules/chroma-keyer/chromaKeyerDefaults.ts web/frontend/src/modules/chroma-keyer/chromaKeyerValidation.test.ts
git commit -m "feat: add chroma video client contracts"
```

### Task 7: Build Timeline And Unified Media Workflow

**Files:**
- Create: `web/frontend/src/modules/chroma-keyer/ChromaVideoTimeline.tsx`
- Create: `web/frontend/src/modules/chroma-keyer/ChromaVideoTimeline.test.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerForm.test.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.test.tsx`
- Modify: `web/frontend/src/styles/app.css`

- [ ] **Step 1: Write failing timeline tests**

Assert slider min zero, max last valid frame time, step `1/fps`, readable
current/total timecode, fps, dimensions, estimated frames, keyboard control,
and mobile containment.

- [ ] **Step 2: Write failing unified form tests**

Assert picker uses `fileKind: media`. Image keeps PNG output path. Video renders
output-root directory picker and `导出 PNG 序列`.

- [ ] **Step 3: Write failing page workflow tests**

Cover:

- committed video path calls inspect and renders first frame plus metadata;
- seeking waits 100 ms and latest timestamp wins;
- seek before sampling requests unkeyed frame;
- seek after sampling requests keyed Final/Alpha frame;
- Screen Color and seven parameters persist across seek;
- sampling sends selected timestamp;
- video task payload uses output root;
- changing from video to image clears video state;
- image workflow regression remains unchanged.

- [ ] **Step 4: Run RED tests**

```bash
npm --prefix web/frontend test -- ChromaVideoTimeline.test.tsx ChromaKeyerForm.test.tsx ChromaKeyerPage.test.tsx
```

Expected: missing timeline and video media behavior.

- [ ] **Step 5: Implement timeline and media-session union**

Keep one preview canvas. Add separate sequence tokens for inspect, seek, sample,
and keyed previews, or a single well-tested latest-media request generation.
Do not let a stale timestamp response replace a newer frame. Place the timeline
directly below the canvas and keep all dimensions stable.

- [ ] **Step 6: Run GREEN tests and commit**

```bash
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
git add web/frontend/src/modules/chroma-keyer web/frontend/src/styles/app.css
git commit -m "feat: add chroma video timeline workflow"
```

### Task 8: End-To-End Export, Documentation, And Release Verification

**Files:**
- Modify: `docs/studio_chroma_keyer_prd.md`
- Modify: `README.md`
- Modify: `web/frontend/e2e/app.spec.ts`
- Create: `tests/test_chroma_keyer_video_integration.py`

- [ ] **Step 1: Add deterministic end-to-end fixture test**

Generate a tiny green-screen video with a moving red foreground and blended
edge using bundled FFmpeg. Run the public video exporter. Assert actual frame
count, RGBA mode, dimensions, Alpha background/foreground/edge properties,
manifest contents, and no temporary directories.

- [ ] **Step 2: Update product documentation**

Move video/sequence support out of non-goals. Document supported inputs,
timeline sampling, static whole-video parameters, transparent PNG sequence
naming, manifest, progress, atomic completion, and no audio/cancel/range/batch.

- [ ] **Step 3: Add browser workflow**

Use a generated short clip or deterministic API stubs to verify video
auto-load, seek, sample, all seven sliders, Final/Alpha, output-root selection,
task creation, progress, and desktop/mobile layout.

- [ ] **Step 4: Run full verification**

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

Expected: all project tests pass. If the TRAE wrapper reports only its known
post-Playwright sandbox cleanup restriction after Playwright reports all tests
passed, record that separately from the Playwright result.

- [ ] **Step 5: Commit release coverage**

```bash
git add tests/test_chroma_keyer_video_integration.py docs/studio_chroma_keyer_prd.md README.md web/frontend/e2e/app.spec.ts
git commit -m "test: validate chroma video PNG export"
```

- [ ] **Step 6: Run real GUI validation**

Start the video worktree server on an unused port. Load the generated clip,
seek between visibly different frames, sample green, adjust all seven controls,
switch Final/Alpha, export, and inspect PNG/manifest output. Capture desktop and
mobile screenshots and verify no overlap or horizontal overflow.
