# Chroma Projection Matte Design

## Goal

Improve Studio Chroma Keyer for sampled, near-solid backgrounds. The revised
pipeline must preserve semi-transparent edges while reducing sampled-background
spill, instead of treating the matte as a mostly binary RGB-distance threshold.

This change applies to the existing single-image keyer, preview endpoint, and
RGBA export. It does not add AI matting, trimaps, new user controls, video
processing, or new dependencies.

## Current Failure

The current alpha generator measures Euclidean RGB distance from the sampled
background. With a broad tolerance, lightly contaminated semi-transparent
foreground pixels can be classified as fully foreground or fully background.
`shrink_grow` then thresholds the continuous matte at 0.5 before morphology,
which further removes intermediate alpha. The current despill mask is largest
only at alpha 0.5, so it can leave visible background-channel spill near
retained edges.

## Algorithm

### Continuous Projection Matte

For each input pixel, calculate its difference from the sampled background
mean. Use the sampled screen-color direction to measure background-aligned
change and use the perpendicular residual to retain foreground color
information. Combine:

- the signed background-axis projection;
- the residual chroma distance from the sampled background; and
- sampled color variance.

The background-axis and residual confidences each use the same smoothstep
remap as RGB-distance confidence, controlled by `tolerance` and `softness`.
Their combined foreground confidence produces a continuous alpha value in
`[0, 255]`:

- sampled background approaches `0`;
- clearly distinct foreground approaches `255`;
- mixed pixels remain strictly between `0` and `255`.

The implementation will use float32 intermediates, guard zero-length color
directions, and retain the existing `KeyerParams` validation contract.

### Continuous Matte Refinement

`clip_black` and `clip_white` retain their existing endpoint remap. When
`shrink_grow` is nonzero, morphology operates on the normalized continuous
matte rather than a thresholded binary mask:

- positive values dilate alpha to grow foreground;
- negative values erode alpha to shrink foreground;
- the transformed alpha is blended with the source matte to avoid hard
  transitions.

`feather` remains a Gaussian blur over the continuous alpha.

### Spill Suppression

Despill uses the sampled-background contribution and inverse alpha as the
effect strength. It remains restricted to non-opaque edge pixels, but does not
depend on the symmetric `alpha * (1 - alpha)` peak alone.

For a clear dominant sampled channel, reduce that channel toward the larger of
the other two channels. For an ambiguous sampled color, move the pixel toward
the sampled model's neutral luminance. The operation is clamped to uint8 and
mixed by `despill_amount`. Opaque foreground remains unchanged.

## Compatibility

- Existing API payloads, parameter names, preview modes, and output files stay
  unchanged.
- Preview and export continue to call the same public keying pipeline.
- Parameter names and validation ranges stay unchanged. The defaults become
  `tolerance = 0.00` and `softness = 1.00`, intentionally spanning the full
  continuous opacity range to preserve screen-contaminated edges. Users raise
  tolerance and lower softness when they need a tighter key.
- RGB-distance confidence remains the neutral-color fallback and blends into
  the projected matte for low-chroma sampled colors; it is not exposed as a
  separate mode.

## Tests

Add synthetic pixel and small-image tests that prove:

1. sampled background becomes transparent;
2. distant foreground remains opaque;
3. a pixel mixed between foreground and sampled background has intermediate
   alpha;
4. a green-contaminated semi-transparent edge retains intermediate alpha and
   has a reduced green channel after despill;
5. continuous shrink/grow preserves intermediate alpha rather than replacing
   it with a binary value;
6. preview Alpha and Final RGBA output match the export pipeline.

Run the existing core, route, frontend, and E2E suites after the focused
regressions pass.
