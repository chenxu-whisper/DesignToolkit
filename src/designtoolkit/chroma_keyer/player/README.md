# Packed Alpha Player

Open `index.html` directly in a modern WebGL-capable browser, then select the
exported MP4 and its matching JSON. All four player files must remain together.
The classic script works offline from `file://`; no CDN, module loader, server,
upload, or installation is required. File selectors provide browser-managed
object URLs, not arbitrary filesystem access. Playback starts only after Play.

The exporter can place this directory as `<outputstem>_player` beside the MP4
and JSON. Hosted downloads include these resources together. The example does
not guess paths or automatically read neighboring files.

## Format

This is **not native MP4 transparency**. An ordinary video player displays both
halves. One H.264/yuv420p video stores straight (non-premultiplied) RGB on the
left and grayscale alpha on the right. Alpha 0 is transparent, 255 opaque.
H.264 is lossy, including the mask; small alpha and color errors are expected.

Required JSON fields:

```json
{
  "version": 1,
  "layout": "color_left_alpha_right",
  "alpha_mode": "straight",
  "original_width": 65,
  "original_height": 33,
  "stored_width": 66,
  "stored_height": 34
}
```

`stored_width` and `stored_height` are the size **of each half**, padded upward
to even. Decoded video dimensions must be `2 * stored_width` by `stored_height`.
Original pixels occupy each half's top-left corner. Padding is not displayed;
the output canvas has the original dimensions. Additional encoder fields are
accepted but duration/seeking use decoded video timing.

The shader clamps sampling to the original crop's pixel centers independently
in each half. It uses the browser-decoded grayscale directly, without a second
limited-range expansion, sharpening, or alpha threshold. WebGL uses
`alpha: true, premultipliedAlpha: false`, blending disabled, and straight shader
output. The browser composites onto the checkerboard, light, or dark background.

## Reuse

Include `player.js` as a classic script on a page with a canvas. The exported
`window.PackedAlphaPlayer` is independent of the demo and needs no CSS.

```html
<canvas id="output"></canvas>
<script src="player.js"></script>
<script>
  const player = new PackedAlphaPlayer(document.getElementById('output'));
  player.addEventListener('statechange', event => console.log(event.detail));
  player.addEventListener('error', event => console.error(event.detail));
  // videoFile and metadataFile come from file input elements.
  async function preview(videoFile, metadataFile) {
    await player.load(videoFile, metadataFile);
    await player.play(); // call from a user action; autoplay can be rejected
  }
</script>
```

- `load(videoFileOrURL, metadataFileOrObject): Promise<player>` validates and
  decodes, draws the first frame, and leaves playback stopped.
- `play(): Promise<void>` starts drawing; rejected playback is visible and
  retryable. `pause()` stops frame callbacks.
- `seek(seconds)` clamps a finite time to decoded duration and redraws the
  paused frame after seek completion.
- `setMuted(boolean)` changes audio; `video` exposes standard media timing.
- `render(): boolean` draws the current decoded frame synchronously, returning
  false if unavailable or if texture upload/drawing failed.
- `destroy()` is idempotent: cancels pending loads/frame callbacks, detaches
  listeners, clears video source/canvas, revokes owned URLs and deletes GL resources.
- `canvas`, `video`, `metadata`, `status`, `message` are readable. Events:
  `statechange` (detail `{status, message}`), `error` (detail `Error`),
  `timeupdate`, `volumechange`.

States: `empty`, `loading`, `ready`, `playing`, `paused`, `error`,
`context-lost`, `destroyed`. Load failures reject their promise; replacements
cancel previous loads with `AbortError`. Context loss pauses playback and stops
drawing. Restoration rebuilds GPU resources and redraws a paused frame when
available; otherwise select the files again. No automatic playback resumes.

Remote video URLs require normal browser access and correct CORS response
headers (`Access-Control-Allow-Origin`). The player sets `crossOrigin=anonymous`
before assigning `src`. It does not bypass browser origin restrictions. Metadata
URLs are not fetched by this API; fetch JSON in the host application and pass
the parsed object. Decode has a 15-second readiness timeout. The supported
dimensions are limited by the browser's texture and viewport capabilities.

## Isolated Browser Regression

From `web/frontend`:

```powershell
node e2e/scripts/packed-alpha-fixture.mjs --test
```

The runner uses installed Chrome (override `PACKED_ALPHA_BROWSER` with a
Playwright channel), bundled FFmpeg via the root `.venv/Scripts/python.exe`
(override `PACKED_ALPHA_PYTHON`), and an OS-assigned loopback port. It never starts
the React/API/hosted suite. Fixtures, reports, and configuration stay under
`.temp/packed-alpha-player`. Tests encode actual H.264/CRF18/yuv420p, load files
through visible selectors, and read decoded RGBA plus light/dark composited
pixels, with 8/255 interior and 12/255 edge tolerances.

The suite also cross-validates `key_video_to_packed_mp4` and its actual JSON.
Only the keyed RGB/alpha result is made deterministic; production packing,
encoding, process handling, and metadata generation are unchanged.

Verified on 2026-11-03: red baseline failed because the player heading/entry
point was missing; the same command passed all 10 browser tests after
implementation. Actual production-encoded browser samples:

| Sample | RGBA (0-255) |
| --- | --- |
| Transparent | 192, 192, 192, 0 |
| Half alpha | 192, 192, 192, 128 |
| Opaque | 192, 192, 192, 255 |
| Rightmost original pixel (seam) | 191, 191, 191, 253 |
| Bottommost original pixel | 65, 65, 65, 254 |
| Half alpha over dark (24) | 108, 108, 108, 255 |
| Half alpha over light (240) | 216, 216, 216, 255 |

The `file://` regression verifies zero HTTP requests. Desktop light/dark and
390px-wide screenshots plus decoded pixel JSON are retained in the artifact
directory. These are Chrome browser results, not a claim of cross-browser
certification; decoding and WebGL capability remain device-dependent.
