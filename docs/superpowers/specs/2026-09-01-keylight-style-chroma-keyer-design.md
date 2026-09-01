# Keylight-Style Chroma Keyer Design

## Goal

Replace the current projection matte controls with a predictable color-difference
keyer inspired by Keylight's public behavior. The implementation must use one
screen color, expose Screen Gain and Screen Balance, preserve fractional alpha,
and continue using sampled-background RGB recovery for despill.

This is an independent implementation based on public behavior. It does not copy
or claim compatibility with Foundry's proprietary Keylight implementation.

## Screen Color Sampling

The editor stores exactly one Screen Color.

- Clicking the preview with sampling enabled replaces the current color and
  source coordinate.
- A successful replacement removes the previous sample from UI and request
  state.
- Clearing the color disables Final/Alpha preview generation and export.
- A rejected replacement leaves the previous valid Screen Color intact and
  displays `请选择具有明显主色的纯色幕背景`.
- The existing `samples` request field remains for compatibility, but it must
  contain exactly one RGB triplet.

The internal `ScreenColorModel` remains the transport type. Its mean is the
single sampled color and its standard deviation is zero.

## Matte Algorithm

Normalize the source pixel `x` and sampled screen color `s` to `[0, 1]`.
Let `k` be the index of the largest component in `s`, and let `j` and `l` be
the other two channel indices.

For Screen Balance `b`:

```text
balanced_other(v) =
    (1 - b) * max(v[j], v[l]) + b * min(v[j], v[l])

screen_saturation = s[k] - balanced_other(s)
pixel_saturation = x[k] - balanced_other(x)
```

The sampled color is valid only when:

```text
screen_saturation >= 0.05
```

Otherwise processing fails with the user-facing message:
`请选择具有明显主色的纯色幕背景`.

For Screen Gain `g`:

```text
screen_coverage = clamp(
    g * pixel_saturation / screen_saturation,
    0,
    1,
)
alpha = 1 - screen_coverage
```

This produces zero alpha for the sampled screen color, full alpha for pixels
whose primary component does not match the screen, and continuous alpha for
screen-contaminated edges.

## Controls

`Tolerance` and `Softness` are removed from the domain model, API schema,
frontend state, form, and request payloads.

They are replaced by:

| Control | Range | Default | Meaning |
|---|---:|---:|---|
| Screen Gain | 0.00-2.00 | 1.00 | Multiplies sampled-screen coverage; higher values key more |
| Screen Balance | 0.00-1.00 | 0.50 | Chooses the comparison point between the stronger and weaker non-primary channels |

Existing Clip Black, Clip White, Shrink/Grow, Feather, and Despill Amount
controls remain. Matte processing order remains:

1. color-difference screen matte;
2. Clip Black/White;
3. continuous Shrink/Grow;
4. Feather.

## Despill And Output

Final preview and PNG export continue through the shared `key_rgb` pipeline.
Fractional pixels use sampled-background RGB recovery:

```text
C = aF + (1 - a)B
F = clip((C - (1 - a)B) / max(a, 1/255), 0, 255)
result = lerp(C, F, despill_amount)
```

The default Despill Amount is `1.0`. Alpha `0` and `255` pixels remain
byte-identical in RGB. A true foreground color close to the sampled screen color
cannot be distinguished without a trimap and may shift; lowering Despill Amount
is the supported mitigation.

## API And Validation

- `samples` remains an array for request compatibility but has exact length one.
- `screen_gain` accepts finite values in `[0, 2]`.
- `screen_balance` accepts finite values in `[0, 1]`.
- `tolerance` and `softness` become forbidden extra fields.
- Preview and export reject missing, multiple, or low-saturation screen colors
  before image processing.
- Validation failures use the existing structured API error format.

## UI

- The sampling list becomes one Screen Color row without `#1` numbering.
- A new valid click replaces the row instead of appending.
- Screen Gain and Screen Balance numeric controls replace Tolerance and
  Softness.
- Final and Alpha view behavior is unchanged.
- Removing or invalidating Screen Color restores the inspected source preview
  and disables export.

## Testing

Automated coverage must include:

1. green, blue, and red sampled screens;
2. exact sampled color becoming transparent;
3. opposite/foreground colors remaining opaque;
4. 25%, 50%, and 75% screen-contaminated edges retaining fractional alpha;
5. Screen Gain increasing screen coverage;
6. Screen Balance endpoints using the stronger and weaker alternate channels;
7. low-saturation screen colors being rejected;
8. a second valid click replacing, not appending, Screen Color;
9. invalid replacement preserving the previous valid Screen Color;
10. request schemas enforcing exactly one sample and rejecting removed fields;
11. Final preview and export sharing the same alpha and recovered RGB;
12. default values matching backend, API, and frontend.

Real-asset validation uses `assets/frame/test_frame_2.png` with one clean green
background sample. On black and white composites:

- title and wing-edge green contamination must be substantially reduced;
- fractional edge alpha must remain present;
- the output must remain source-sized RGBA;
- the known green foreground bar may shift color and is accepted as the
  documented no-trimap limitation.

