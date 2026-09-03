# Chroma Video Playback Controls Design

## Goal

Add compact playback controls to the Studio Chroma Keyer video timeline so a
loaded clip can be reviewed in motion without leaving the existing preview
canvas. Playback must continue to show the current preview mode: unkeyed frames
before Screen Color sampling, and keyed Final or Alpha frames after sampling.

The feature remains a preview workflow. It does not add audio playback, variable
speed, looping, range playback, or a new streaming backend.

## User Experience

After a video loads, the timeline control band contains these icon buttons:

1. Return to start.
2. Step backward one source frame.
3. Play or pause.
4. Step forward one source frame.

The controls and timeline share one compact row. Existing timecode and metadata
remain visible below that row. Buttons use Lucide icons, stable square
dimensions, accessible names, and tooltips. On narrow viewports the transport
controls remain grouped while the timeline takes a full row; the page must not
overflow horizontally.

The controls are disabled while video inspection or export is active. Keyed
playback is also disabled when the current Screen Color or keyer parameters are
invalid.

## Playback Semantics

- Before Screen Color sampling, playback requests unkeyed video frames.
- After sampling, playback requests the current Final or Alpha preview with the
  same Screen Color and seven parameters used by static seeking and export.
- Playback runs at normal speed according to source time, not according to
  response count.
- At most one frame or keyed-preview request is in flight.
- If decoding is slower than the source frame rate, intermediate frames are
  skipped and the next request targets the latest source time.
- Pause preserves the last successfully rendered frame and synchronizes the
  timeline to that frame.
- Reaching the end pauses on the last valid frame.
- Pressing Play while stopped on the last frame restarts from timestamp zero.
- Return-to-start and single-frame buttons pause playback before seeking.
- Dragging the timeline pauses playback and retains the existing 100 ms seek
  debounce.
- Single-frame and return-to-start commands dispatch immediately.
- Sampling, removing Screen Color, changing a keyer parameter, switching
  Final/Alpha, loading or refreshing media, and starting export all pause
  playback before their existing behavior runs.
- Sampling remains unavailable while playback is running. The user pauses on a
  rendered frame before sampling.

## Architecture

`ChromaVideoTimeline` remains a presentational component. It receives:

- `isPlaying`;
- play/pause, restart, previous-frame, and next-frame callbacks;
- the existing media metadata, timestamp, disabled state, and seek callback.

`ChromaKeyerPage` owns playback state and scheduling because it already owns
media identity, requested and rendered timestamps, API selection, request
generations, validation, and preview state.

No backend route or response schema changes are required. Playback reuses:

- `POST /api/chroma-keyer/videos/frame` before sampling;
- `POST /api/chroma-keyer/videos/preview` after sampling.

## Scheduler

Playback uses a requestAnimationFrame-driven wall-clock scheduler:

1. Starting playback records `performance.now()` and the rendered source
   timestamp.
2. Each animation callback computes the desired source timestamp from elapsed
   wall time and clamps it to the last valid frame time.
3. The desired timestamp is quantized to the source frame grid.
4. If no preview request is active, the scheduler requests the latest desired
   timestamp.
5. While a request is active, later animation callbacks only replace the stored
   target timestamp. They do not create more requests.
6. When a response succeeds, the preview and rendered timestamp are updated.
   If playback is still active and the target has advanced, the next latest
   target is dispatched.
7. At the last valid frame, playback stops only after that frame is rendered.

This keeps memory and server load bounded while maintaining real-time progress.
The displayed timeline may lead the rendered canvas during a slow request, but
sampling remains disabled. Pausing rolls the requested timestamp back to the
last rendered frame.

## Request Ownership And Races

Playback uses the existing latest-intent generation guards. Starting, pausing,
manual seeking, parameter changes, view changes, sampling, media inspection,
and task creation invalidate incompatible playback work.

An in-flight request is not required to support transport-level cancellation.
Its response is ignored when its generation no longer matches the current
media intent.

Playback target, animation-frame handle, in-flight status, start wall time, and
start source timestamp are stored in refs so animation callbacks do not close
over stale React state. `isPlaying` remains React state for rendering controls.

Unmounting the page cancels the animation callback and invalidates in-flight
playback responses.

## Errors

If a playback request fails:

- playback stops;
- the timeline returns to the last successfully rendered timestamp;
- the last successful preview remains visible;
- the existing timeline error region displays the API error;
- Play remains available and clears the stale timestamp error before
  dispatching the retry request.

Invalid media metadata or keyer parameters never start playback. Existing image
behavior is unchanged.

## Testing

### Timeline Component

- Renders the four icon controls only for video.
- Uses Play and Pause accessible names according to state.
- Disables all transport controls with the timeline.
- Dispatches restart, previous-frame, play/pause, and next-frame callbacks.
- Keeps controls and range contained at desktop and 390 px widths.

### Page Workflow

- Plays unkeyed frames before sampling.
- Plays keyed Final and Alpha frames after sampling with complete parameters.
- Advances according to elapsed wall time.
- Keeps at most one request in flight and skips stale intermediate targets.
- Pauses and synchronizes to the last rendered timestamp.
- Stops on the last frame and restarts from zero.
- Steps exactly `1 / fps` and clamps at both ends.
- Pauses on seek, parameter changes, view changes, sampling, media changes, and
  export.
- Ignores responses from playback invalidated by a newer action.
- Stops and preserves the last preview when a playback request fails.

### Browser Verification

Use deterministic route stubs and a controlled clock to verify the full
play/pause flow, dropped-frame catch-up, Final/Alpha payloads, end-of-video
restart, icon accessibility, and desktop/mobile containment.
