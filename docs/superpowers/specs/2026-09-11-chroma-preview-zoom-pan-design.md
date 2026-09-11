# Chroma Preview Zoom And Pan

Date: 2026-09-11

## Objective

Add precise image inspection controls to the chroma-key preview while
preserving the existing click-to-sample workflow and source-coordinate
accuracy. Users can zoom from 25% to 800%, zoom around the pointer, and pan a
zoomed image by holding Space while dragging.

The feature changes only frontend preview interaction. It does not change
preview generation, sampling APIs, task payloads, video playback, or export
behavior.

## Interaction Model

### Default mode

The preview remains a sampling surface:

- A pointer click samples the clicked image location.
- Enter samples the center of the transformed image rectangle when the preview
  has focus, preserving the existing keyboard-sampling behavior.
- A click without meaningful movement always retains the existing sampling
  behavior.
- Space is reassigned from center sampling to the pan modifier. It no longer
  triggers a sample.

### Zoom controls

A compact toolbar is anchored to the preview's top-right corner. It contains:

1. Zoom out.
2. Current zoom percentage.
3. Zoom in.
4. Fit to viewport.
5. Actual size at 100%.

Controls use Lucide icons, accessible labels, and tooltips. The percentage is a
read-only status, not an editable text field.

The supported zoom range is 25% through 800% relative to the preview image's
intrinsic pixels. Button zoom follows these levels:

`25, 33, 50, 67, 75, 100, 125, 150, 200, 300, 400, 600, 800`

Zoom-in selects the next higher level and zoom-out selects the next lower
level. Toolbar and keyboard zoom use the viewport center as their anchor. The
controls disable at their respective limits.

The mouse wheel zooms while the pointer is over the preview. Wheel zoom uses a
continuous factor of `2 ** (-normalizedDeltaY / 500)` and clamps manual scale
to the same 25%-800% range. Pixel deltas are used directly, line deltas are
multiplied by 16, and page deltas are multiplied by the viewport height. The
image point under the pointer stays under the pointer after scaling. The wheel
event does not scroll the surrounding workspace when it changes the zoom.

Keyboard controls work while the preview viewport or one of its controls has
focus:

- `+` or `=` zooms in.
- `-` zooms out.
- `0` selects 100%.
- `F` fits the image to the viewport.
- Enter samples the center of the visible image.

### Pan controls

Holding Space while pressing the primary pointer button starts panning.
Movement updates the viewport translation and changes the cursor from grab to
grabbing. Releasing Space, releasing the pointer, receiving `pointercancel`,
or losing component ownership ends the pan operation and removes global
listeners.

A movement of fewer than 4 CSS pixels is not a drag. A completed drag at or
above that threshold suppresses the synthetic click that follows pointer-up so
it cannot create an accidental color sample.

Pan offsets are constrained independently on each axis:

- If transformed content is smaller than the viewport on an axis, it remains
  centered on that axis.
- If transformed content is larger, its near and far edges can reach the
  viewport edges, but the image cannot be moved completely out of view.

At fit scale, panning is normally inactive because the content is contained.

## Scale And Coordinate Model

The viewer contains three layers:

1. A fixed clipping viewport.
2. A translated and scaled content layer.
3. The preview image and sample markers inside the content layer.

The image and marker layer share one transform origin at the top-left. Sample
markers continue to use percentages of original media width and height, so
they remain attached to the source location through every scale and offset.

Sampling uses the transformed image's `getBoundingClientRect()`. The existing
payload remains:

```ts
{
  x: number
  y: number
  previewWidth: number
  previewHeight: number
}
```

Because all four values come from the same transformed rectangle, the backend's
existing ratio-based source-coordinate mapping remains correct at every zoom
level.

Zoom around an anchor uses content coordinates:

```text
contentX = (anchorX - offsetX) / oldScale
nextOffsetX = anchorX - contentX * nextScale
```

The same calculation applies to Y. The resulting offset is clamped after every
scale or viewport-size change.

## Fit And Reset Behavior

Fit scale is:

```text
min(viewportWidth / imageWidth, viewportHeight / imageHeight, 1)
```

Fit never enlarges a small image beyond 100%. The initial state is fit to
viewport. Fit is a distinct mode and may render below 25% when a large image
cannot otherwise fit. Manual zoom remains constrained to 25%-800%. From fit
mode, zoom-in selects the first discrete level above the fit scale, with 25%
as the minimum manual level. Zoom-out is disabled when fit is already at or
below 25%.

`viewportKey` is a page-local numeric revision that identifies the inspected
media independently from the changing preview URL:

- Every successful source-media inspection increments `viewportKey`, including
  reloading a changed file from the same path.
- A new `viewportKey` resets to fit and centers the media.
- Final/Alpha changes, parameter previews, and video frame refreshes retain
  scale and offset.
- A viewport resize recomputes fit scale. If the user is in fit mode, the image
  stays fitted. If the user selected another scale, that scale is retained and
  only the pan offset is clamped.

## Component Boundaries

### `useImageViewport`

A focused hook owns:

- current scale and mode (`fit` or `manual`);
- current X/Y offset;
- intrinsic image dimensions;
- viewport dimensions;
- zoom-level selection;
- pointer-anchor zoom calculations;
- pan start, movement, cancellation, and cleanup;
- offset clamping;
- reset on `viewportKey` changes.

It exposes state and event handlers without depending on chroma-key business
types or API clients.

### `ImageViewportToolbar`

A presentational component renders zoom commands and status. It receives
current scale, limit state, and callbacks. It owns no transform state.

### `ChromaKeyerCanvas`

The canvas:

- provides the viewport element and image dimensions to `useImageViewport`;
- renders the transformed content layer;
- keeps sampling semantics and sample markers;
- prevents sampling only after a completed pan gesture;
- passes the inspected media identity as `viewportKey`;
- updates its accessible description to distinguish Enter sampling from
  Space-drag panning.

### `ChromaKeyerPage`

The page owns and passes the numeric `viewportKey`. It increments the key only
after a source-media inspection succeeds. Changing preview parameters,
switching between Final and Alpha output, or refreshing a video frame changes
the preview URL without changing this key.

No state is added to Zustand stores.

## Responsive Behavior

Desktop and precise-pointer environments support toolbar, wheel, keyboard, and
Space-drag interactions.

Touch environments retain the toolbar and keyboard semantics when a keyboard is
present. This iteration does not add pinch zoom or one-finger panning because a
single touch must remain available for sampling and introducing a mode switch
would expand the workflow scope.

The toolbar remains inside the viewport rather than the transformed layer, so
it never moves or scales with the media. At narrow widths it keeps fixed icon
button dimensions and may reduce spacing, but it does not wrap over multiple
rows or cover the center of the image.

## Accessibility

- The preview viewport remains keyboard focusable.
- Zoom commands are native buttons with accessible names.
- The zoom percentage uses `role="status"` with polite updates.
- Disabled zoom-limit controls use native `disabled`.
- Keyboard focus uses the existing visible focus system.
- Transform animation is disabled under `prefers-reduced-motion`.
- Pointer gestures have equivalent toolbar and keyboard commands.
- Enter remains the keyboard path for center sampling.

## Error And Edge Handling

Zoom and pan commands are ignored until the image has non-zero intrinsic and
rendered dimensions.

All numeric scale and offset values must remain finite. Invalid measurements
fall back to fit mode and zero offset.

Pointer listeners and temporary keyboard state are removed on pointer-up,
pointer-cancel, blur, media replacement, and component unmount.

If `ResizeObserver` is unavailable, initial image load and window resize still
provide functional measurement updates.

## Testing

### Unit and component tests

Tests cover:

- fit scale calculation with and without upscaling;
- 25% and 800% clamps;
- discrete zoom-level selection;
- pointer-anchor preservation during wheel zoom;
- centering when content is smaller than the viewport;
- pan boundaries when content is larger;
- pointer-up, pointer-cancel, and unmount cleanup;
- the 4px drag threshold;
- click sampling without a drag;
- drag completion suppressing the following click;
- Enter center sampling;
- Space no longer sampling;
- media-key reset;
- preview-URL refresh preserving viewport state;
- toolbar labels, status, and limit states.

### Browser tests

Playwright verifies:

- toolbar visibility in loaded image and video previews;
- zoom-in, zoom-out, fit, and 100% controls;
- wheel zoom around the pointer;
- Space-drag panning without a sample request;
- ordinary click sampling after zoom;
- sample markers remaining aligned;
- no horizontal overflow at 1440px, 900px, and 390px;
- light and dark theme rendering;
- no browser console errors.

## Acceptance Criteria

1. Users can manually inspect an image between 25% and 800%, with a
   fit-to-viewport mode allowed below 25% for large media.
2. Toolbar and wheel zoom keep the requested anchor stable.
3. Holding Space and dragging pans zoomed content within safe boundaries.
4. A completed pan never creates a sample.
5. A normal click and Enter center action still sample correctly.
6. Sample coordinates and markers remain correct at every scale.
7. Replacing media resets to fit; preview updates preserve the view.
8. All temporary listeners are cleaned up on every termination path.
9. The controls remain usable without overlap in desktop, tablet, and phone
   layouts.
10. Vitest, TypeScript, production build, and Playwright suites pass.
