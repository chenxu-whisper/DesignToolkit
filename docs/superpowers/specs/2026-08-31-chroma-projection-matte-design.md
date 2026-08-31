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

Despill treats a fractional-alpha source pixel as a composite over the sampled
background instead of suppressing one dominant channel. For normalized alpha
`a` in `(0, 1)`, observed RGB `C`, sampled-background mean `B`, and unknown
foreground RGB `F`:

```text
C = aF + (1 - a)B
F = clip((C - (1 - a)B) / max(a, 1/255), 0, 255)
result = lerp(C, F, despill_amount)
```

The implementation uses float32 intermediates and rounds and clamps only when
converting the result to uint8. Pixels whose alpha byte is `0` or `255` bypass
the calculation and retain their input RGB bytes exactly. An amount of `0`
also returns RGB byte-identical to the input. For fractional alpha, amounts
between `0` and `1` interpolate linearly between the observed composite and
the recovered foreground; amount `1` selects the recovered foreground.

This recovery is driven by the complete sampled RGB mean and therefore works
for green, blue, red, and non-primary screen colors without dominant-channel
or neutral-color branches.

The model cannot distinguish a true foreground color close to the sampled
screen color from screen contribution without a trimap or another foreground
prior. Such colors may shift after unmixing. Users can reduce
`despill_amount` when preserving those colors is more important than removing
edge contamination.

## Compatibility

- Existing API payloads, parameter names, preview modes, and output files stay
  unchanged.
- Preview and export continue to call the same public keying pipeline.
- Parameter names and validation ranges stay unchanged. The defaults become
  `tolerance = 0.00` and `softness = 1.00`, intentionally spanning the full
  continuous opacity range to preserve screen-contaminated edges. Users raise
  tolerance and lower softness when they need a tighter key.
- The `despill_amount` default becomes `1.00` in the backend model, API schema,
  and frontend form so the sampled-background recovery is fully applied by
  default.
- RGB-distance confidence remains the neutral-color fallback and blends into
  the projected matte for low-chroma sampled colors; it is not exposed as a
  separate mode.

## Tests

Add synthetic pixel and small-image tests that prove:

1. sampled background becomes transparent;
2. distant foreground remains opaque;
3. a pixel mixed between foreground and sampled background has intermediate
   alpha;
4. synthetic white composited over sampled green at 25%, 50%, and 75% alpha
   recovers white within uint8 rounding tolerance at amount `1`;
5. amount `0`, `0.5`, and `1` prove unchanged input, linear interpolation, and
   the recovered endpoint;
6. alpha bytes `0` and `255` preserve all RGB bytes exactly;
7. a committed real chroma asset has a quantitative regression over its
   fractional-alpha edge population;
8. continuous shrink/grow preserves intermediate alpha rather than replacing
   it with a binary value;
9. preview Alpha and Final RGBA output match the export pipeline; and
10. backend, API-schema, and frontend defaults all use
    `despill_amount = 1.00`.

Run the existing core, route, frontend, and E2E suites after the focused
regressions pass.
