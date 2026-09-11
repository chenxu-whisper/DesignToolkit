import { describe, expect, it } from 'vitest'
import {
  MAX_MANUAL_SCALE,
  MIN_MANUAL_SCALE,
  ZOOM_LEVELS,
  calculateFitScale,
  clampManualScale,
  clampOffset,
  getDiscreteZoomScale,
  getWheelScale,
  zoomAtAnchor,
} from './imageViewportMath'

describe('imageViewportMath', () => {
  it('exports the supported manual zoom range and levels', () => {
    expect(MIN_MANUAL_SCALE).toBe(0.25)
    expect(MAX_MANUAL_SCALE).toBe(8)
    expect(ZOOM_LEVELS).toEqual([
      0.25, 0.33, 0.5, 0.67, 0.75, 1, 1.25, 1.5, 2, 3, 4, 6, 8,
    ])
  })

  it('clamps manual scales to the supported range', () => {
    expect(clampManualScale(0.2)).toBe(MIN_MANUAL_SCALE)
    expect(clampManualScale(2)).toBe(2)
    expect(clampManualScale(9)).toBe(MAX_MANUAL_SCALE)
  })

  it('fits large media without enlarging small media', () => {
    expect(calculateFitScale({ width: 400, height: 300 }, { width: 800, height: 400 })).toBe(0.5)
    expect(calculateFitScale({ width: 800, height: 600 }, { width: 200, height: 100 })).toBe(1)
  })

  it('returns zero for invalid fit measurements', () => {
    expect(calculateFitScale({ width: 0, height: 300 }, { width: 800, height: 400 })).toBe(0)
    expect(
      calculateFitScale({ width: 400, height: 300 }, { width: Number.NaN, height: 400 }),
    ).toBe(0)
  })

  it('selects discrete manual zoom levels and clamps both ends', () => {
    expect(getDiscreteZoomScale(0.2, 1)).toBe(MIN_MANUAL_SCALE)
    expect(getDiscreteZoomScale(1, 1)).toBe(1.25)
    expect(getDiscreteZoomScale(1, -1)).toBe(0.75)
    expect(getDiscreteZoomScale(8, 1)).toBe(MAX_MANUAL_SCALE)
    expect(getDiscreteZoomScale(0.25, -1)).toBe(MIN_MANUAL_SCALE)
  })

  it('normalizes pixel, line, and page wheel deltas before clamping', () => {
    expect(getWheelScale(1, -500, 0, 600)).toBe(2)
    expect(getWheelScale(1, -31.25, 1, 600)).toBe(2)
    expect(getWheelScale(1, -1, 2, 500)).toBe(2)
    expect(getWheelScale(8, -500, 0, 600)).toBe(MAX_MANUAL_SCALE)
    expect(getWheelScale(0.25, 500, 0, 600)).toBe(MIN_MANUAL_SCALE)
  })

  it('preserves the content point under the zoom anchor', () => {
    expect(zoomAtAnchor({ x: 150, y: 100 }, 1, 2, { x: 50, y: 25 })).toEqual({
      x: -50,
      y: -50,
    })
  })

  it('returns the origin when anchor zoom inputs are invalid', () => {
    expect(zoomAtAnchor({ x: Number.NaN, y: 100 }, 1, 2, { x: 50, y: 25 })).toEqual({
      x: 0,
      y: 0,
    })
    expect(zoomAtAnchor({ x: 150, y: 100 }, 0, 2, { x: 50, y: 25 })).toEqual({
      x: 0,
      y: 0,
    })
  })

  it('centers contained content and clamps oversized content to both edges', () => {
    const viewport = { width: 400, height: 300 }
    const image = { width: 200, height: 100 }

    expect(clampOffset({ x: 900, y: -900 }, 1, image, viewport)).toEqual({ x: 100, y: 100 })
    expect(clampOffset({ x: 40, y: -500 }, 3, image, viewport)).toEqual({ x: 0, y: 0 })
    expect(clampOffset({ x: -500, y: -500 }, 3, image, viewport)).toEqual({ x: -200, y: 0 })
  })

  it('returns the origin when offset clamp inputs are invalid', () => {
    expect(
      clampOffset(
        { x: Number.POSITIVE_INFINITY, y: 0 },
        1,
        { width: 200, height: 100 },
        { width: 400, height: 300 },
      ),
    ).toEqual({ x: 0, y: 0 })
  })
})
