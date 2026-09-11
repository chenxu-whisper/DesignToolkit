export type ViewportPoint = { x: number; y: number }
export type ViewportSize = { height: number; width: number }

export const MIN_MANUAL_SCALE = 0.25
export const MAX_MANUAL_SCALE = 8
export const ZOOM_LEVELS = [
  0.25, 0.33, 0.5, 0.67, 0.75, 1, 1.25, 1.5, 2, 3, 4, 6, 8,
] as const

const isPositiveSize = ({ height, width }: ViewportSize) =>
  Number.isFinite(height) && Number.isFinite(width) && height > 0 && width > 0

export function clampManualScale(scale: number) {
  if (!Number.isFinite(scale)) {
    return MIN_MANUAL_SCALE
  }
  return Math.min(MAX_MANUAL_SCALE, Math.max(MIN_MANUAL_SCALE, scale))
}

export function calculateFitScale(viewport: ViewportSize, image: ViewportSize) {
  if (!isPositiveSize(viewport) || !isPositiveSize(image)) {
    return 0
  }
  return Math.min(viewport.width / image.width, viewport.height / image.height, 1)
}

export function getDiscreteZoomScale(scale: number, direction: -1 | 1) {
  const current = Number.isFinite(scale) ? scale : MIN_MANUAL_SCALE
  const ordered = direction === 1 ? ZOOM_LEVELS : [...ZOOM_LEVELS].reverse()
  const next = ordered.find((level) => (direction === 1 ? level > current : level < current))
  return clampManualScale(next ?? (direction === 1 ? MAX_MANUAL_SCALE : MIN_MANUAL_SCALE))
}

export function getWheelScale(
  scale: number,
  deltaY: number,
  deltaMode: number,
  viewportHeight: number,
) {
  const normalizedDeltaY =
    deltaY * (deltaMode === 1 ? 16 : deltaMode === 2 ? viewportHeight : 1)
  return clampManualScale(scale * 2 ** (-normalizedDeltaY / 500))
}

export function zoomAtAnchor(
  anchor: ViewportPoint,
  oldScale: number,
  nextScale: number,
  offset: ViewportPoint,
): ViewportPoint {
  if (
    ![anchor.x, anchor.y, oldScale, nextScale, offset.x, offset.y].every(Number.isFinite) ||
    oldScale <= 0 ||
    nextScale <= 0
  ) {
    return { x: 0, y: 0 }
  }
  return {
    x: anchor.x - ((anchor.x - offset.x) / oldScale) * nextScale,
    y: anchor.y - ((anchor.y - offset.y) / oldScale) * nextScale,
  }
}

export function clampOffset(
  offset: ViewportPoint,
  scale: number,
  image: ViewportSize,
  viewport: ViewportSize,
): ViewportPoint {
  if (
    !isPositiveSize(image) ||
    !isPositiveSize(viewport) ||
    ![offset.x, offset.y, scale].every(Number.isFinite) ||
    scale <= 0
  ) {
    return { x: 0, y: 0 }
  }

  const clampAxis = (value: number, content: number, available: number) =>
    content <= available
      ? (available - content) / 2
      : Math.min(0, Math.max(available - content, value))

  return {
    x: clampAxis(offset.x, image.width * scale, viewport.width),
    y: clampAxis(offset.y, image.height * scale, viewport.height),
  }
}
