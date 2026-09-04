# Hosted Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the anonymous hosted deployment resistant to upload, decode, output, worker-failure, and cleanup resource-exhaustion paths on the approved 8 vCPU / 16 GiB / 100 GB baseline.

**Architecture:** Stream upload bodies directly into governed `/data` storage, coordinate upload/output capacity and session lifecycle with Redis Lua transactions, and enforce media/output limits before and during processing. Keep local mode unchanged, make all release limits configurable only downward from compiled safety ceilings, and add RQ callbacks plus periodic reconciliation for terminal-state recovery.

**Tech Stack:** Python 3.12, FastAPI/Starlette request streaming, Redis 8, RQ 2.12, Pillow, imageio-ffmpeg, React/TypeScript/XHR, Docker Compose, Caddy, pytest/fakeredis, Vitest, Playwright.

---

## File Map

- Create `src/designtoolkit/web/resources.py`: Redis-backed upload/output reservations and session leases.
- Create `src/designtoolkit/web/media_limits.py`: image/video metadata validation for hosted requests.
- Create `src/designtoolkit/web/output_limits.py`: per-task output accounting and FFmpeg/Python write guards.
- Modify `src/designtoolkit/web/hosted.py`: settings ceilings, streamed storage, quarantine cleanup.
- Modify `src/designtoolkit/web/routes.py`: raw upload contract, leases, media validation.
- Modify `src/designtoolkit/web/redis_tasks.py`: RQ callbacks, terminalization, reconciliation.
- Modify `src/designtoolkit/web/worker.py`: output reservation, lease heartbeat, limits, cleanup.
- Modify `src/designtoolkit/web/cleanup.py`: stale task reconciliation and leased cleanup.
- Modify `src/designtoolkit/video_frames/ffmpeg_backend.py`: monitored subprocess execution.
- Modify `src/designtoolkit/video_frames/core.py`: pass output location to monitored backend.
- Modify `src/designtoolkit/web/tasks.py`: invoke aggregate output guard after produced files.
- Modify `web/frontend/src/services/apiClient.ts`: send raw file bodies and encoded filenames.
- Modify `compose.yaml`: resource ceilings and environment wiring.
- Modify `.env.example`: document safe configurable limits.
- Modify `README.md`: deployment and operations documentation.
- Modify `pyproject.toml` and `uv.lock`: remove `python-multipart` after multipart removal.
- Extend `tests/test_web_hosted_storage.py`, `tests/test_web_hosted_api.py`, and `tests/test_web_redis_tasks.py`.
- Create `tests/test_web_resources.py`, `tests/test_web_media_limits.py`, and `tests/test_web_output_limits.py`.
- Extend `web/frontend/src/services/apiClient.test.ts` and `web/frontend/e2e/hosted.spec.ts`.

### Task 1: Redis Resource Coordination

**Files:**
- Create: `src/designtoolkit/web/resources.py`
- Create: `tests/test_web_resources.py`

- [ ] **Step 1: Write failing reservation tests**

Cover atomic capacity, concurrency, idempotent release, expiry, and lease/delete exclusion:

```python
def test_upload_reservations_enforce_count_and_bytes_atomically():
    coordinator = coordinator_with_limits(max_uploads=2, max_bytes=10)
    first = coordinator.reserve_upload("a" * 32, 4, free_bytes=100)
    second = coordinator.reserve_upload("b" * 32, 6, free_bytes=100)
    with pytest.raises(ResourceUnavailableError):
        coordinator.reserve_upload("c" * 32, 1, free_bytes=100)
    first.release()
    second.release()


def test_cleanup_claim_cannot_overlap_active_session_lease():
    lease = coordinator.acquire_session("a" * 32)
    assert coordinator.claim_session_cleanup("a" * 32) is None
    lease.release()
    claim = coordinator.claim_session_cleanup("a" * 32)
    assert claim is not None
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
uv run pytest tests/test_web_resources.py -q
```

Expected: collection fails because `designtoolkit.web.resources` does not exist.

- [ ] **Step 3: Implement coordinator primitives**

Define these public interfaces:

```python
class ResourceUnavailableError(RuntimeError):
    pass


@dataclass
class RedisReservation:
    coordinator: RedisResourceCoordinator
    kind: Literal["upload", "output"]
    token: str
    owner_id: str
    reserved_bytes: int

    def refresh(self) -> None: ...
    def release(self) -> None: ...


@dataclass
class RedisSessionLease:
    coordinator: RedisResourceCoordinator
    session_id: str
    token: str

    def refresh(self) -> None: ...
    def release(self) -> None: ...
    def __enter__(self) -> RedisSessionLease: ...
    def __exit__(self, *_args: object) -> None: ...


class RedisResourceCoordinator:
    def reserve_upload(
        self, session_id: str, requested_bytes: int, *, free_bytes: int
    ) -> RedisReservation: ...

    def reserve_output(
        self, task_id: str, session_id: str, requested_bytes: int, *, free_bytes: int
    ) -> RedisReservation: ...

    def acquire_session(self, session_id: str) -> RedisSessionLease: ...
    def claim_session_cleanup(self, session_id: str) -> str | None: ...
    def release_cleanup_claim(self, session_id: str, token: str) -> None: ...
```

Use Lua scripts so count/byte checks and mutations occur in one Redis operation.
Store reservations in hashes with TTL-indexed token keys. Clamp TTL to at least
twice the heartbeat interval. Make release idempotent.

- [ ] **Step 4: Run focused tests and verify GREEN**

```powershell
uv run pytest tests/test_web_resources.py -q
```

Expected: all resource coordination tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/designtoolkit/web/resources.py tests/test_web_resources.py
git commit -m "feat: add hosted resource coordination"
```

### Task 2: Stream Upload Bodies Into Governed Storage

**Files:**
- Modify: `src/designtoolkit/web/hosted.py`
- Modify: `src/designtoolkit/web/routes.py`
- Modify: `src/designtoolkit/web/app.py`
- Modify: `web/frontend/src/services/apiClient.ts`
- Modify: `tests/test_web_hosted_storage.py`
- Modify: `tests/test_web_hosted_api.py`
- Modify: `web/frontend/src/services/apiClient.test.ts`
- Modify: `web/frontend/e2e/hosted.spec.ts`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Replace UploadFile tests with raw-stream tests**

Add tests for required length and filename headers, chunk overflow, disconnect
cleanup, reservation release, and sequential multi-file uploads:

```python
@pytest.mark.anyio
async def test_stream_upload_releases_reservation_after_chunk_limit(tmp_path):
    stream = async_chunks(b"123", b"45")
    with pytest.raises(UploadRejectedError, match="500 MB"):
        await storage.store_stream(
            "a" * 32,
            filename="../screen.PNG",
            content_length=5,
            chunks=stream,
            reservation=reservation,
        )
    assert reservation.released
    assert list(tmp_path.rglob(".uploading")) == []
```

API tests must assert `411` without `Content-Length`, `400` without
`X-Upload-Filename`, `413` above 500 MiB, and `201` for a valid streamed body.
Frontend tests must assert:

```typescript
expect(xhr.open).toHaveBeenCalledWith('POST', '/api/uploads')
expect(xhr.setRequestHeader).toHaveBeenCalledWith(
  'X-Upload-Filename',
  encodeURIComponent(file.name),
)
expect(xhr.send).toHaveBeenCalledWith(file)
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
uv run pytest tests/test_web_hosted_storage.py tests/test_web_hosted_api.py -q
cd web/frontend
npm test -- --run src/services/apiClient.test.ts
```

Expected: tests fail because the endpoint still expects multipart `UploadFile`.

- [ ] **Step 3: Implement streamed storage**

Replace `HostedStorage.store_upload()` with:

```python
async def store_stream(
    self,
    session_id: str,
    *,
    filename: str,
    content_length: int,
    chunks: AsyncIterable[bytes],
    reservation: RedisReservation,
) -> UploadedAsset:
```

Validate suffix and declared length before creating `.uploading`. For every
non-empty chunk, update `size`, reject when `size > content_length` or
`size > max_upload_bytes`, write the chunk, and refresh the reservation and
session lease. Require `size == content_length` before atomic rename. In
`finally`, release the reservation; on failure remove the asset directory.

- [ ] **Step 4: Implement the raw upload route**

Use `Request.stream()` and decode the filename header:

```python
encoded_name = http_request.headers.get("x-upload-filename")
content_length = parse_content_length(http_request.headers.get("content-length"))
filename = unquote(encoded_name) if encoded_name is not None else ""
reservation = resources.reserve_upload(
    http_request.state.session_id,
    content_length,
    free_bytes=disk_usage(settings.data_dir).free,
)
asset = await storage.store_stream(
    http_request.state.session_id,
    filename=filename,
    content_length=content_length,
    chunks=http_request.stream(),
    reservation=reservation,
)
```

Map missing length to `411`, capacity/size errors to `413` or `503`, and invalid
filename/type to `400`.

- [ ] **Step 5: Update XHR transport**

```typescript
request.open('POST', '/api/uploads')
request.responseType = 'json'
request.setRequestHeader('Content-Type', file.type || 'application/octet-stream')
request.setRequestHeader('X-Upload-Filename', encodeURIComponent(file.name))
request.send(file)
```

Keep one-file-at-a-time behavior and aggregate progress unchanged.

- [ ] **Step 6: Remove multipart dependency**

Remove `python-multipart` from `pyproject.toml`, then run:

```powershell
uv lock
```

- [ ] **Step 7: Run focused tests and verify GREEN**

```powershell
uv run pytest tests/test_web_hosted_storage.py tests/test_web_hosted_api.py -q
cd web/frontend
npm test -- --run src/services/apiClient.test.ts
npx playwright test e2e/hosted.spec.ts --pass-with-no-tests
```

Expected: all focused tests pass.

- [ ] **Step 8: Commit**

```powershell
git add pyproject.toml uv.lock src/designtoolkit/web/hosted.py src/designtoolkit/web/routes.py src/designtoolkit/web/app.py tests/test_web_hosted_storage.py tests/test_web_hosted_api.py web/frontend/src/services/apiClient.ts web/frontend/src/services/apiClient.test.ts web/frontend/e2e/hosted.spec.ts
git commit -m "feat: stream hosted uploads into governed storage"
```

### Task 3: Validate Decoded Media Before Queueing

**Files:**
- Create: `src/designtoolkit/web/media_limits.py`
- Create: `tests/test_web_media_limits.py`
- Modify: `src/designtoolkit/web/hosted.py`
- Modify: `src/designtoolkit/web/routes.py`
- Modify: `tests/test_web_hosted_api.py`

- [ ] **Step 1: Write failing image and video limit tests**

```python
def test_rejects_image_above_pixel_limit(tmp_path):
    validator = HostedMediaValidator(max_image_pixels=100, ...)
    image_path = write_image(tmp_path, size=(11, 10))
    with pytest.raises(MediaLimitError, match="100 pixels"):
        validator.validate(image_path)


def test_rejects_video_above_dimensions_or_duration(fake_decoder, tmp_path):
    fake_decoder.metadata = VideoMetadata(
        width=3841, height=2160, fps=30, duration_seconds=1, ...
    )
    with pytest.raises(MediaLimitError, match="3840x2160"):
        validator.validate(tmp_path / "clip.mp4")
```

Add API coverage proving an invalid member rejects an entire batch before
`queue.enqueue()` is called.

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/test_web_media_limits.py tests/test_web_hosted_api.py -q
```

Expected: module/import failures and no pre-queue rejection.

- [ ] **Step 3: Implement media policy**

Define:

```python
@dataclass(frozen=True)
class HostedMediaLimits:
    max_image_pixels: int = 100_000_000
    max_video_width: int = 3840
    max_video_height: int = 2160
    max_video_duration_seconds: float = 1800.0


class HostedMediaValidator:
    def validate(self, path: Path) -> None: ...
    def validate_many(self, paths: Iterable[Path]) -> None: ...
```

Use `PIL.Image.open(path)` without decoding pixels for image dimensions. Use
`ImageioVideoDecoder.probe(path)` for video metadata. Treat unreadable metadata
as `MediaLimitError`. Check positive finite dimensions and duration.

- [ ] **Step 4: Wire limits into hosted request conversion**

Add clamped settings:

```python
max_image_pixels: int = 100_000_000
max_video_width: int = 3840
max_video_height: int = 2160
max_video_duration_seconds: int = 1800
```

In hosted mode, resolve every asset reference, validate all paths, then create
the task output directory and enqueue. Return stable `media_limit_exceeded`
errors with HTTP `422`.

- [ ] **Step 5: Run tests and verify GREEN**

```powershell
uv run pytest tests/test_web_media_limits.py tests/test_web_hosted_api.py -q
```

Expected: all media policy tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/designtoolkit/web/media_limits.py src/designtoolkit/web/hosted.py src/designtoolkit/web/routes.py tests/test_web_media_limits.py tests/test_web_hosted_api.py
git commit -m "feat: enforce hosted media decode limits"
```

### Task 4: Enforce Output Budgets During Processing

**Files:**
- Create: `src/designtoolkit/web/output_limits.py`
- Create: `tests/test_web_output_limits.py`
- Modify: `src/designtoolkit/video_frames/ffmpeg_backend.py`
- Modify: `src/designtoolkit/video_frames/core.py`
- Modify: `src/designtoolkit/web/tasks.py`
- Modify: `src/designtoolkit/web/worker.py`
- Modify: `tests/test_web_redis_tasks.py`

- [ ] **Step 1: Write failing budget tests**

Cover aggregate accounting without repeated full scans and monitored FFmpeg
termination:

```python
def test_budget_rejects_aggregate_files_before_limit_is_exceeded(tmp_path):
    guard = OutputBudgetGuard(tmp_path, max_bytes=8, safety_bytes=1)
    first = tmp_path / "one.bin"
    first.write_bytes(b"1234")
    guard.account(first)
    second = tmp_path / "two.bin"
    second.write_bytes(b"5678")
    with pytest.raises(OutputLimitExceeded):
        guard.account(second)


def test_ffmpeg_backend_terminates_when_guard_rejects(monkeypatch):
    process = FakePopen(poll_results=[None, None])
    backend = FfmpegBackend(output_guard=rejecting_guard, popen=fake_popen(process))
    result = backend.run(["ffmpeg", "out.mp4"])
    assert process.terminate_called
    assert result.returncode != 0
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/test_web_output_limits.py tests/test_web_redis_tasks.py -q
```

Expected: missing output-limit types and no monitored subprocess support.

- [ ] **Step 3: Implement `OutputBudgetGuard`**

```python
class OutputLimitExceeded(RuntimeError):
    pass


class OutputBudgetGuard:
    def __init__(self, root: Path, max_bytes: int, safety_bytes: int = 1): ...
    def account(self, path: Path) -> None: ...
    def check_tree(self) -> None: ...
    @property
    def used_bytes(self) -> int: ...
```

Track `(device, inode) -> last_size` where available and resolved path otherwise.
Only add positive size deltas. Reject at `used_bytes + safety_bytes >
max_bytes`. Ensure all accounted paths remain below `root`.

- [ ] **Step 4: Monitor FFmpeg outputs**

Add optional constructor dependencies to `FfmpegBackend`:

```python
def __init__(
    self,
    *,
    output_guard: Callable[[], None] | None = None,
    poll_interval: float = 0.25,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> None:
```

Replace `subprocess.run` with `Popen`, `communicate(timeout=poll_interval)`, and
guard checks between timeouts. On `OutputLimitExceeded`, terminate, wait, then
kill if needed. On Linux, set a child-only `RLIMIT_FSIZE` equal to the remaining
single-file budget.

- [ ] **Step 5: Connect aggregate checks**

Construct guarded backends in `RedisTaskExecutor`. For chroma PNG sequences,
call `guard.account(current_path)` before emitting each progress event. After
each resize/export result call `guard.check_tree()`. Retain
`prepare_download_artifact()` as the final validation.

- [ ] **Step 6: Run focused tests and verify GREEN**

```powershell
uv run pytest tests/test_web_output_limits.py tests/test_web_redis_tasks.py -q
```

Expected: budget and worker tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/designtoolkit/web/output_limits.py src/designtoolkit/video_frames/ffmpeg_backend.py src/designtoolkit/video_frames/core.py src/designtoolkit/web/tasks.py src/designtoolkit/web/worker.py tests/test_web_output_limits.py tests/test_web_redis_tasks.py
git commit -m "feat: enforce hosted output budgets during processing"
```

### Task 5: Reserve Output Capacity and Recover Failed RQ Jobs

**Files:**
- Modify: `src/designtoolkit/web/resources.py`
- Modify: `src/designtoolkit/web/redis_tasks.py`
- Modify: `src/designtoolkit/web/worker.py`
- Modify: `tests/test_web_resources.py`
- Modify: `tests/test_web_redis_tasks.py`

- [ ] **Step 1: Write failing output reservation and callback tests**

```python
def test_two_workers_cannot_overreserve_same_free_space():
    first = coordinator.reserve_output(
        "task-1", "a" * 32, 5, free_bytes=15, min_free_bytes=5
    )
    with pytest.raises(ResourceUnavailableError):
        coordinator.reserve_output(
            "task-2", "b" * 32, 6, free_bytes=15, min_free_bytes=5
        )
    first.release()


def test_rq_failure_callback_terminalizes_task_and_releases_resources(...):
    handle_job_failure(job, connection, RuntimeError, RuntimeError("timeout"), None)
    assert manager.get_task(job.id).status is TaskStatus.FAILED
    assert coordinator.output_reserved_bytes() == 0
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/test_web_resources.py tests/test_web_redis_tasks.py -q
```

Expected: output reservation and callback tests fail.

- [ ] **Step 3: Wire reservation lifecycle into `run_task`**

Before `mark_running`, acquire a session lease and reserve
`settings.max_output_bytes` using current `disk_usage().free`. Refresh both
from output progress checks. Release both in `finally`. Delete partial task
output on any failure.

- [ ] **Step 4: Add importable RQ callbacks**

Implement module-level callbacks with the RQ 2.12 signature:

```python
def handle_job_failure(job, connection, exc_type, exc_value, traceback) -> None:
    finalize_external_failure(job.id, connection, str(exc_value))


def handle_job_stopped(job, connection, *_args) -> None:
    finalize_external_failure(job.id, connection, "Task was stopped")
```

Pass them through `Queue.enqueue(..., on_failure=..., on_stopped=...)`.
Terminalization and resource release must be idempotent.

- [ ] **Step 5: Add stale task reconciliation**

Extend `RedisTaskManager.reconcile_tasks()` to inspect each queued/running
record with `queue.fetch_job(task_id)` and `job.get_status(refresh=True)`.
Use `StartedJobRegistry` and `FailedJobRegistry` as supporting evidence.
Only terminalize when the RQ state is terminal/missing beyond the grace period.

- [ ] **Step 6: Run focused tests and verify GREEN**

```powershell
uv run pytest tests/test_web_resources.py tests/test_web_redis_tasks.py -q
```

Expected: all reservation, callback, and reconciliation tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/designtoolkit/web/resources.py src/designtoolkit/web/redis_tasks.py src/designtoolkit/web/worker.py tests/test_web_resources.py tests/test_web_redis_tasks.py
git commit -m "feat: recover failed hosted worker jobs"
```

### Task 6: Make Cleanup Lease-Safe

**Files:**
- Modify: `src/designtoolkit/web/hosted.py`
- Modify: `src/designtoolkit/web/routes.py`
- Modify: `src/designtoolkit/web/worker.py`
- Modify: `src/designtoolkit/web/cleanup.py`
- Modify: `tests/test_web_hosted_storage.py`
- Modify: `tests/test_web_hosted_api.py`
- Modify: `tests/test_web_resources.py`
- Modify: `tests/test_web_redis_tasks.py`

- [ ] **Step 1: Write failing cleanup race tests**

Test that active upload/download/worker leases prevent cleanup, new leases fail
after a cleanup claim, a failed rename releases the claim, and `.trash`
directories are retried:

```python
def test_cleanup_claim_blocks_new_lease_until_quarantine_finishes(...):
    claim = coordinator.claim_session_cleanup(session_id)
    assert claim is not None
    with pytest.raises(SessionDeletingError):
        coordinator.acquire_session(session_id)
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/test_web_resources.py tests/test_web_hosted_storage.py tests/test_web_hosted_api.py tests/test_web_redis_tasks.py -q
```

Expected: cleanup can still race active operations.

- [ ] **Step 3: Add quarantine cleanup**

Replace `cleanup_expired(protected_sessions=...)` with:

```python
def cleanup_expired(
    self,
    *,
    retention_seconds: int,
    coordinator: RedisResourceCoordinator,
) -> int:
```

For each valid session directory, check recursive mtime, claim cleanup, recheck
mtime, atomically rename to `root / ".trash" / f"{session_id}-{token}"`, clear
Redis session state, then delete. Release the claim on any pre-rename failure.

- [ ] **Step 4: Hold leases across file operations**

Wrap upload, image/video inspect, frame extraction, sampling, preview generation,
task request path resolution, Worker processing, and download responses. For
downloads, use a `BackgroundTask` to release the lease only after the response
body finishes.

- [ ] **Step 5: Reconcile before cleanup**

In `cleanup_once()`, call `manager.reconcile_tasks()` and
`coordinator.reconcile_reservations()` before scanning sessions. Remove the
old `active_owner_ids()` snapshot dependency.

- [ ] **Step 6: Run focused tests and verify GREEN**

```powershell
uv run pytest tests/test_web_resources.py tests/test_web_hosted_storage.py tests/test_web_hosted_api.py tests/test_web_redis_tasks.py -q
```

Expected: all lease, cleanup, and route ownership tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/designtoolkit/web/hosted.py src/designtoolkit/web/routes.py src/designtoolkit/web/worker.py src/designtoolkit/web/cleanup.py tests/test_web_resources.py tests/test_web_hosted_storage.py tests/test_web_hosted_api.py tests/test_web_redis_tasks.py
git commit -m "feat: coordinate hosted session cleanup"
```

### Task 7: Add Container Limits and Deployment Documentation

**Files:**
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `tests/test_compose_config.py`

- [ ] **Step 1: Write failing Compose configuration tests**

Use `subprocess.run(["docker", "compose", "config", "--format", "json"])` when
Docker is available and skip otherwise. Assert resolved limits:

```python
assert services["app"]["cpus"] == 1.0
assert services["app"]["mem_limit"] == 1610612736
assert services["worker-1"]["cpus"] == 3.0
assert services["worker-1"]["mem_limit"] == 5368709120
assert services["worker-2"]["pids_limit"] == 256
```

- [ ] **Step 2: Run test and verify RED**

```powershell
uv run pytest tests/test_compose_config.py -q
```

Expected: assertions fail because resource limits are absent.

- [ ] **Step 3: Add environment-configurable service limits**

Add `cpus`, `mem_limit`, and `pids_limit` to each service using these defaults:

```yaml
app:
  cpus: "${APP_CPUS:-1.0}"
  mem_limit: "${APP_MEMORY:-1536m}"
  pids_limit: "${APP_PIDS:-256}"
worker-1:
  cpus: "${WORKER_CPUS:-3.0}"
  mem_limit: "${WORKER_MEMORY:-5g}"
  pids_limit: "${WORKER_PIDS:-256}"
```

Apply the same worker values to `worker-2`; use the approved values from the
design for Redis, Caddy, and cleanup. Preserve `pull_policy: never`.

- [ ] **Step 4: Document all environment settings**

Add safe limits and service resource variables to `.env.example`. Add README
sections for:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f app worker-1 worker-2
docker compose pull
docker compose up -d --build
docker compose down
```

Document DNS setup, HTTPS behavior, persisted volumes, backup implications, and
the requirement to keep at least 15 GiB free before two output jobs start.

- [ ] **Step 5: Run configuration test and verify GREEN**

```powershell
uv run pytest tests/test_compose_config.py -q
docker compose config --quiet
```

Expected: test passes and Compose exits zero.

- [ ] **Step 6: Commit**

```powershell
git add compose.yaml .env.example README.md tests/test_compose_config.py
git commit -m "chore: harden hosted container resources"
```

### Task 8: Full Verification and Release Commit

**Files:**
- Modify only files required by failures discovered in this task.

- [ ] **Step 1: Run Python verification**

```powershell
uv run pytest
uv run ruff check .
uv run python -m compileall -q src tests
```

Expected: all tests pass, Ruff exits zero, compileall exits zero.

- [ ] **Step 2: Run frontend verification**

```powershell
cd web/frontend
npm test -- --run
npm run build
npm run test:e2e
```

Expected: 301 or more Vitest tests pass; local Playwright passes with only the
Hosted-only test skipped.

- [ ] **Step 3: Run Hosted Playwright separately**

```powershell
$env:VITE_HOSTED_MODE = "true"
npx playwright test e2e/hosted.spec.ts --pass-with-no-tests
Remove-Item Env:VITE_HOSTED_MODE
```

Expected: Hosted test passes.

- [ ] **Step 4: Verify a cold Compose build**

Preserve named volumes, remove containers and the local application image, then
run the supported one-command deployment:

```powershell
docker compose down
docker image rm designtoolkit:latest
docker compose up -d --build
docker compose ps --all
```

Expected: all six services are running; `app` and `redis` are healthy.

- [ ] **Step 5: Run real hosted smoke tests**

Through `http://localhost`, verify:

- Browser uploads a real image using the raw-body endpoint.
- A resize task reaches `succeeded`.
- SSE emits created, running, and completed events.
- Browser download produces a valid resized image.
- A second anonymous session receives `404` for the first session's task and
  download.
- Four concurrent upload reservations are accepted and the fifth receives
  `429`.
- A synthetic expired, unleased session is quarantined and deleted.
- An actively leased session survives cleanup.

- [ ] **Step 6: Review the final diff**

```powershell
git diff --check HEAD
git status --short
git diff --stat HEAD
```

Expected: no whitespace errors, no files outside the approved scope, and no
cache/test artifacts outside `.temp/`.

- [ ] **Step 7: Request final code review**

Review the complete diff from design commit `7a696a8` through the working tree.
Fix every Critical and Important finding, then rerun Steps 1-6.

- [ ] **Step 8: Commit implementation**

```powershell
git add .dockerignore .env.example Caddyfile Dockerfile compose.yaml README.md pyproject.toml uv.lock src tests web/frontend
git commit -m "feat: deploy hardened hosted processing"
```

- [ ] **Step 9: Push and verify**

```powershell
git push origin main
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
```

Expected: local `HEAD` equals `origin/main`, with no uncommitted implementation
changes.
