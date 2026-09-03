# Chroma Video PNG Sequence Design

## Goal

Extend Studio Chroma Keyer from still images to local video files while reusing
the existing single-frame Keylight-style pipeline. Users can seek to any
representative video frame, sample one Screen Color, tune the existing seven
parameters, and export the entire video as a transparent PNG sequence.

The first video phase is intentionally limited to one source video, one static
Screen Color, and one static parameter set for the whole clip.

## Supported Media

Image behavior remains unchanged.

Video input uses the bundled FFmpeg from `imageio-ffmpeg`. The supported picker
surface includes MOV, MP4, M4V, WebM, MKV, and AVI. A file is accepted only when
FFmpeg can decode it and report:

- positive width and height;
- positive playback frame rate;
- positive duration.

Video output is always a directory containing RGBA PNG files. This phase does
not create MP4, MOV, or WebM output and does not preserve audio because an image
sequence has no audio container.

## User Workflow

1. The existing Studio Chroma Keyer path field and native picker accept images
   and videos.
2. A committed path is classified by its suffix and inspected by the matching
   image or video endpoint.
3. Video inspection shows the first decoded frame and reports source size,
   duration, playback frame rate, and estimated frame count.
4. A timeline below the canvas selects a timestamp. Its step is `1 / fps`, and
   its displayed value uses a readable timecode.
5. Seeking requests a frame after a short debounce and uses latest-response-wins
   sequencing.
6. The user samples Screen Color on the current frame. The request includes the
   selected timestamp and maps preview coordinates to that decoded frame's
   source dimensions.
7. Screen Color and all seven Keyer parameters persist when the timeline moves.
8. Final and Alpha previews key only the selected frame.
9. The user selects an optional output root directory and starts export.
10. The task processes every decoded video frame with the same Screen Color and
    parameters and publishes a completed PNG sequence directory.

There is no separate video-keyer module. Image and video workflows share the
same form and canvas, with timeline and video metadata rendered only for video.

## Video Metadata And Frame Access

Add a video adapter in the chroma-keyer domain with these responsibilities:

- `probe(path)` returns codec, source dimensions, fps, duration, and estimated
  frame count;
- `read_frame(path, timestamp_seconds)` returns one full-resolution RGB uint8
  frame and the actual source dimensions;
- `read_preview_frame(path, timestamp_seconds)` returns a frame bounded to the
  existing `1280x720` preview limit;
- `iter_frames(path)` yields metadata first and then one RGB uint8 frame at a
  time in decoded order.

The production implementation uses `imageio_ffmpeg.read_frames`. Seeking uses
the same FFmpeg seek parameters for source sampling and preview extraction so
both requests resolve to the same frame around a timestamp.

Timestamps must be finite and in `[0, duration)`. The UI clamps the final
timeline position to the last valid frame time. Video metadata and previews use
the existing source fingerprint strategy so a changed file cannot reuse stale
cache entries.

## Video API

Existing image routes remain compatible. Add video-specific routes:

### Inspect

`POST /api/chroma-keyer/videos/inspect`

Request:

```json
{"input_path": "C:\\clips\\screen.mp4"}
```

Response includes:

```json
{
  "input_path": "C:\\clips\\screen.mp4",
  "width": 1920,
  "height": 1080,
  "preview_width": 1280,
  "preview_height": 720,
  "preview_url": "/api/chroma-keyer/previews/<id>",
  "duration_seconds": 12.5,
  "fps": 30.0,
  "estimated_frame_count": 375,
  "timestamp_seconds": 0.0
}
```

### Unkeyed Frame

`POST /api/chroma-keyer/videos/frame`

Request contains `input_path` and `timestamp_seconds`. The response contains the
bounded frame preview URL and resolved timestamp.

### Sample

`POST /api/chroma-keyer/videos/sample`

Request contains input path, timestamp, preview coordinates, and preview
dimensions. It returns the same RGB, HEX, and source-coordinate shape as image
sampling plus the resolved timestamp.

### Keyed Preview

`POST /api/chroma-keyer/videos/preview`

Request contains input path, timestamp, exactly one sample, KeyerParams, and
Final/Alpha view mode. It keys a bounded frame and returns a preview URL.

### Export Task

`POST /api/tasks/chroma-keyer-video`

Request contains:

- `input_path`;
- exactly one RGB sample;
- KeyerParams;
- optional `output_root`.

The task kind remains `chroma_keyer` so the existing shared task store, SSE
events, mutual exclusion, and result display continue to work.

## Streaming Export

Introduce `VideoKeyOptions`, `VideoKeyMetadata`, and `VideoKeyResult` domain
types. The exporter accepts a video decoder, ScreenColorModel, KeyerParams, and
an optional progress callback.

The default final directory is:

```text
<source parent>/<source stem>_keyed_frames
```

When `output_root` is supplied, the final directory is:

```text
<output root>/<source stem>_keyed_frames
```

The final directory must not already exist. Export uses a unique hidden sibling
temporary directory on the same volume:

```text
.<source stem>_keyed_frames.<uuid>.tmp
```

For each decoded frame:

1. construct a zero-copy NumPy RGB view over the frame bytes when possible;
2. call the existing public `key_rgb`;
3. atomically write one RGBA PNG into the temporary directory;
4. release frame and intermediate arrays before decoding the next frame;
5. report processed-frame progress.

File names are:

```text
<source stem>_keyed_000001.png
<source stem>_keyed_000002.png
...
```

No whole-video frame list is created. Peak application memory is bounded by one
source frame, one keyed frame, one Alpha matte, and temporary arrays used by the
single-frame algorithm.

After all frames succeed, write `manifest.json`:

```json
{
  "version": 1,
  "source_path": "C:\\clips\\screen.mp4",
  "width": 1920,
  "height": 1080,
  "fps": 30.0,
  "duration_seconds": 12.5,
  "frame_count": 375,
  "pattern": "screen_keyed_%06d.png",
  "screen_color": [0, 255, 0],
  "params": {
    "screen_gain": 1.0,
    "screen_balance": 0.5,
    "clip_black": 0.0,
    "clip_white": 1.0,
    "shrink_grow": 0,
    "feather": 0,
    "despill_amount": 1.0
  }
}
```

The temporary directory is then atomically renamed to the final directory.

On decode, keying, PNG, manifest, or rename failure:

- close the decoder generator;
- recursively delete the temporary directory;
- leave the final directory absent;
- fail the task with a user-facing message.

The first phase does not support overwrite, resume, cancellation, batch input,
in/out points, parameter keyframes, FPS conversion, or frame-count limiting.

## Progress

The exporter calls a progress callback after each written frame. The task layer
throttles SSE log entries to at most about 100 progress events while always
reporting the first frame and final frame.

Progress details contain:

- `processed_frames`;
- estimated `total_frames` when available;
- `progress` from `0` to `1` when total is known;
- current output file;
- elapsed seconds.

The existing terminal success event reports the final sequence directory and
actual frame count. No cancel action is added.

## Frontend

The existing Chroma Keyer state becomes a media-session union:

- image session: existing dimensions and preview URL;
- video session: dimensions, preview URL, duration, fps, estimated frames, and
  selected timestamp.

The form changes by media kind:

- input label becomes `输入图片或视频路径`;
- picker uses `fileKind: media`;
- image output remains an optional PNG save path;
- video output becomes an optional output-root directory picker;
- submit text becomes `导出 PNG 序列` for video.

The timeline is an unframed control band directly below the preview. It contains
a range slider, current/total timecode, fps, dimensions, and estimated frame
count. It does not create a decorative card.

Seeking keeps Screen Color and parameters. Before a sample exists, it requests
an unkeyed frame. After a sample exists, it requests the selected Final or Alpha
preview. Timeline requests and parameter-preview requests share latest-wins
guards so an older timestamp cannot replace a newer frame.

## Validation And Errors

- Unsupported suffixes are rejected before FFmpeg starts.
- Missing or unreadable inputs use structured API errors.
- Invalid or unavailable video metadata is rejected.
- Timestamp NaN, infinity, negative values, and values at or beyond duration
  are rejected.
- Short, corrupt, and truncated frame buffers are rejected.
- Export requires exactly one valid Screen Color.
- Output root must exist and be a directory.
- The computed final sequence directory must not exist.
- A zero-frame decode fails and leaves no output.
- Existing image validation and export behavior remain unchanged.

## Testing

### Domain Tests

1. Video metadata normalization and validation.
2. Timestamp validation and final-frame clamping.
3. One-frame reads return correctly shaped RGB uint8 arrays.
4. Frame iteration yields in order without materializing the source.
5. Every frame goes through the public `key_rgb`.
6. Output files are RGBA, source-sized, and sequentially numbered.
7. Manifest fields match actual output and KeyerParams.
8. Existing final directory is rejected.
9. Decode, keying, PNG, manifest, and rename failures remove temporary output.
10. Zero-frame input fails.
11. Progress is monotonic and ends at the actual frame count.

### FFmpeg Integration Tests

Generate tiny deterministic MOV, MP4, and WebM clips with bundled FFmpeg. Verify
that each can be probed and decoded. Export a short green-screen clip with a
moving foreground and verify:

- output frame count equals decoded frame count;
- every PNG has RGBA mode and source dimensions;
- background Alpha is low;
- foreground Alpha remains opaque;
- fractional edge Alpha exists where the fixture contains blended edges;
- manifest fps and actual frame count are correct.

### API And Task Tests

Cover inspect, seek, sample, Final/Alpha preview, cache invalidation, path-change
fingerprints, schema bounds, task conflicts, progress events, success output,
and cleanup on failure.

### Frontend And Browser Tests

Cover automatic video loading, metadata rendering, timeline seek debounce,
latest-frame wins, timestamp-aware sampling, parameter persistence, video output
directory selection, export payload, progress display, and unchanged image
workflow.

Browser validation uses a generated short green-screen video and confirms:

- the first frame appears after path commit;
- seeking changes the displayed frame;
- sampling and seven sliders update the selected-frame preview;
- export creates the expected transparent PNG sequence and manifest;
- desktop and mobile layouts do not overlap or overflow.
