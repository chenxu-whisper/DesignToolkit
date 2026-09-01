# Chroma Auto-Load And Live Sliders Design

## Goal

Remove the explicit `加载图片` action and make image loading follow committed
path changes. Replace numeric-only chroma controls with sliders that update the
bounded server preview while the user adjusts them.

## Path Commit Contract

`PathInput` continues to emit `onChange` for every text edit and adds an optional
`onCommit(value)` callback for values that are ready to use.

The component commits a path when:

- the native file picker returns a path;
- a dropped file resolves to a local path;
- the path editor applies its draft;
- the user presses Enter in the single-line path input;
- the single-line path input loses focus.

Typing alone does not commit a path. This prevents requests for partial paths.
Multiline inputs do not gain Enter-to-commit behavior.

The Chroma Keyer input path uses `onCommit` to inspect the image immediately.
The `加载图片` button and the `onInspect` form prop are removed.

## Automatic Inspection

The committed value is passed directly to the inspection handler. The handler
must not read a potentially stale React state value after picker or drop
updates.

On a new path:

1. invalidate outstanding inspect, sample, and preview responses;
2. clear the previous image, generated preview, and Screen Color;
3. inspect the committed path;
4. show the returned source preview and dimensions.

Blank commits display `请输入图片路径`. Committing a path already loaded or
already being inspected is a no-op. Failed inspection leaves the new path in
the field, keeps the preview empty, and displays the existing input-path error.
Only the latest committed path may update the page.

## Slider Controls

Every chroma parameter is rendered as a synchronized control pair:

- a range slider for fast adjustment;
- a compact numeric input for exact values.

The controls retain their existing bounds and steps:

| Parameter | Minimum | Maximum | Step |
|---|---:|---:|---:|
| Screen Gain | 0 | 2 | 0.01 |
| Screen Balance | 0 | 1 | 0.01 |
| Clip Black | 0 | 1 | 0.01 |
| Clip White | 0 | 1 | 0.01 |
| Shrink/Grow | -10 | 10 | 1 |
| Feather | 0 | 10 | 1 |
| Despill Amount | 0 | 1 | 0.01 |

The range input is the primary labelled control. The numeric input has an
accessible label derived from the parameter name. Both update the same value
and display the same validation error.

The layout uses fixed grid tracks and compact values so dragging or changing
digit width does not move adjacent controls.

## Live Preview

Live preview begins after an image and one valid Screen Color are available.
Every valid parameter change schedules a preview after 150 ms. Further changes
within that interval replace the scheduled request.

Preview requests do not disable the form or sliders. Users can continue
dragging while a request is running. Existing sequence tokens provide
latest-response-wins behavior:

- stale successful responses cannot replace the current preview;
- stale errors cannot replace the current error state;
- inspect and sample actions still disable conflicting image actions;
- Final/Alpha view changes remain immediate.

Invalid parameter combinations, including `Clip Black >= Clip White`, do not
send preview requests and show the existing field validation.

The implementation continues to use the bounded FastAPI preview endpoint.
There is no duplicate browser-side keying algorithm, so preview and export keep
the same parameter semantics.

## Error And State Behavior

- Changing the path immediately removes the previous image and Screen Color.
- A failed automatic inspection never restores an image from an older path.
- Committing the same loaded path does not clear samples or issue a duplicate
  request.
- Parameter preview failures leave the last successful preview visible and show
  the error.
- Export remains disabled until inspection and Screen Color sampling succeed.

## Testing

Frontend tests must prove:

1. the `加载图片` button is absent;
2. picker selection updates the path and inspects exactly once;
3. drop and path-editor apply commit and inspect;
4. typing does not inspect until Enter or blur;
5. Enter followed by blur does not inspect the same path twice;
6. a stale inspection cannot replace a newer path;
7. all seven parameters render synchronized range and numeric inputs;
8. dragging a slider updates its numeric value;
9. a valid slider change requests a preview after 150 ms;
10. sliders remain enabled while a preview request is pending;
11. only the latest preview response is rendered;
12. invalid parameter combinations do not request a preview;
13. Final/Alpha switching still requests immediately.

Browser verification uses `assets/frame/test_frame_2.png` and confirms:

- native selection displays the image without another click;
- one Screen Color can be sampled;
- Screen Gain, Screen Balance, Clip, Shrink/Grow, Feather, and Despill sliders
  visibly update Final or Alpha preview;
- rapid dragging does not freeze controls or display an older response;
- desktop and narrow viewports have no overlapping labels, values, or sliders.

