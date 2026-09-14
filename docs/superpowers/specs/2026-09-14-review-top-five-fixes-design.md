# Review Top Five Fixes

Date: 2026-09-14

## Objective

Fix the five P1 findings selected by the full-project review:

1. Preserve requested one-pixel image bounds instead of converting them to
   FFmpeg's zero-dimension sentinel.
2. Keep cross-container video resize working when the source audio codec is
   incompatible with the target container.
3. Prevent module navigation from losing an asynchronously created task.
4. Run hosted upload E2E coverage as part of the standard Playwright command.
5. Make an empty Playwright test discovery fail verification.

The changes remain limited to media resize behavior, task-creation navigation
state, and browser-test configuration. They do not redesign the UI or change
the public request payloads.

## Media Resize Dimensions

### Images

Image outputs keep the requested bounding box exactly:

- The FFmpeg scale filter uses `force_original_aspect_ratio=decrease`.
- It does not use `force_divisible_by=2`.
- A valid `1x1`, `1xN`, or `Nx1` request remains valid.
- The resulting dimensions remain positive and do not exceed either requested
  bound.

### Videos

Video encoders commonly require even dimensions for the existing pixel format.
Video resize therefore keeps `force_divisible_by=2`, but target bounds smaller
than two pixels on either axis are rejected before FFmpeg starts. The user gets
a deterministic `MediaResizeError` instead of a successful output with the
wrong dimensions.

The media type is determined from the validated input and output suffixes
before constructing the FFmpeg command. The command builder receives enough
information to choose the correct filter without probing the media again.

## Cross-Container Audio

The existing API continues to allow conversion between supported video
containers.

### Same container

When normalized input and output suffixes match:

- Map the primary video stream and optional primary audio stream.
- Copy the audio stream without re-encoding.
- Keep FFmpeg responsible for selecting the default compatible video encoder,
  matching existing behavior.

### Different container

When normalized suffixes differ:

- Map the primary video stream.
- Map the primary audio stream only when present.
- Transcode audio to a codec compatible with the target:
  - `.webm`: `libopus`
  - `.mp4`, `.mov`, `.m4v`: `aac`
  - `.avi`: `libmp3lame`
  - `.mkv`: `aac`
- Do not map attachments, data streams, or subtitles into an incompatible
  target container.

If FFmpeg returns a failure, remove a newly created output file before raising
`MediaResizeError`. Never remove a file that existed before the operation;
existing outputs remain rejected by validation.

## Task Creation Navigation

`isCreating` is already global task state. `AppShell` reads it and passes a
disabled state to `Sidebar`.

While a task creation request is pending:

- Every module navigation button is natively disabled.
- The active module remains mounted until the request resolves.
- The successful response always reaches the submitting page and registers its
  `task_id` in the global task store.
- Existing queued or running tasks do not permanently lock navigation; this
  guard applies only to the short create-request interval.

No new global store field is introduced.

## Playwright Projects

The standard `npm run test:e2e` command runs two explicit projects:

1. `local`: all non-hosted E2E files against a normal Vite server.
2. `hosted`: only `hosted.spec.ts` against a second Vite server built with
   `VITE_HOSTED_MODE=true`.

The servers use distinct ports so their compile-time environments cannot
contaminate each other. Project-level `testMatch` and `testIgnore` prevent
running the general suite twice.

`hosted.spec.ts` no longer conditionally skips itself based on the parent
process environment. The hosted project's server configuration is the source
of truth.

The `test:e2e` script removes `--pass-with-no-tests`. A missing test directory,
incorrect filter, or broken project configuration must produce a non-zero
exit status.

## Error Handling

- Invalid sub-two-pixel video bounds fail before invoking FFmpeg.
- Missing audio streams remain valid because audio mapping is optional.
- FFmpeg failures retain stderr context and clean partial output.
- Module navigation is restored in the existing `finally` path that clears
  `isCreating`, for both successful and failed create requests.
- Either Playwright server failing to start fails the standard E2E command.

## TDD And Verification

### Backend regression tests

Add tracked pytest coverage for:

- `1x1`, `1xN`, and `Nx1` image command filters without even rounding.
- Video commands retaining even rounding.
- Sub-two-pixel video targets failing before backend invocation.
- Same-container audio copy.
- Cross-container codec selection for WebM, MP4/MOV/M4V, AVI, and MKV.
- Optional audio mapping.
- Partial output cleanup after FFmpeg failure.

### Frontend component tests

Add tests proving:

- Sidebar buttons are disabled only while `isCreating` is true.
- A deferred create request cannot be interrupted by module navigation.
- Success still registers the returned task ID.
- Failure restores navigation.

### Browser configuration tests

Verify through Playwright listing and execution:

- The local project discovers non-hosted tests.
- The hosted project discovers and runs both hosted tests.
- The standard E2E command executes both projects.
- A deliberately unmatched grep exits non-zero, proving empty discovery is not
  accepted.

### Full verification

Run:

- `uv run ruff check src tests`
- `uv run python -m compileall -q src tests`
- `uv run pytest`
- `npm test`
- `npm run lint`
- `npm run build`
- `npm run test:e2e`
- `docker compose up -d --build`
- HTTPS status and asset checks at `https://localhost/`

## Acceptance Criteria

1. Image resize honors one-pixel bounds.
2. Video resize rejects impossible sub-two-pixel bounds before FFmpeg.
3. Cross-container video resize uses a target-compatible audio codec.
4. Failed media resize leaves no new partial output.
5. A create request cannot be orphaned by module navigation.
6. Hosted upload and rollback E2E tests run in the standard suite.
7. Zero discovered E2E tests cause a non-zero command result.
8. Backend and frontend regression tests pass.
9. Ruff, compileall, TypeScript, production build, and full Playwright pass.
10. The Docker deployment is healthy and HTTPS serves the rebuilt assets.
