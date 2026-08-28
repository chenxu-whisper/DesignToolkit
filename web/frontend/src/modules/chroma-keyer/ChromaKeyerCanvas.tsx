import type { MouseEvent } from 'react'

export type ChromaCanvasSample = {
  hex: string
  sourceX: number
  sourceY: number
}

type ChromaKeyerCanvasProps = {
  onSample: (point: { x: number; y: number; previewWidth: number; previewHeight: number }) => void
  originalHeight?: number
  originalWidth?: number
  previewUrl: string
  samples: readonly ChromaCanvasSample[]
}

const normalizeDimension = (dimension: number) => Math.max(1, Number.isFinite(dimension) ? Math.round(dimension) : 1)

const normalizeCoordinate = (
  coordinate: number,
  offset: number,
  renderedDimension: number,
  normalizedDimension: number,
) => {
  const relativeCoordinate = renderedDimension > 0 ? (coordinate - offset) / renderedDimension : 0
  const normalizedCoordinate = Number.isFinite(relativeCoordinate)
    ? Math.floor(relativeCoordinate * normalizedDimension)
    : 0

  return Math.min(normalizedDimension - 1, Math.max(0, normalizedCoordinate))
}

export function ChromaKeyerCanvas({
  onSample,
  originalHeight,
  originalWidth,
  previewUrl,
  samples,
}: ChromaKeyerCanvasProps) {
  const handleClick = (event: MouseEvent<HTMLImageElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const previewWidth = normalizeDimension(rect.width)
    const previewHeight = normalizeDimension(rect.height)

    onSample({
      x: normalizeCoordinate(event.clientX, rect.left, rect.width, previewWidth),
      y: normalizeCoordinate(event.clientY, rect.top, rect.height, previewHeight),
      previewHeight,
      previewWidth,
    })
  }

  return (
    <div className="chroma-preview-canvas">
      <img alt="抠像预览" className="chroma-preview-image" onClick={handleClick} src={previewUrl} />
      {originalWidth && originalHeight
        ? samples.map((sample, index) => (
            <span
              aria-label={`采样点 ${index + 1}`}
              className="chroma-sample-marker"
              key={`${sample.sourceX}-${sample.sourceY}-${index}`}
              style={{
                left: `${(sample.sourceX / originalWidth) * 100}%`,
                top: `${(sample.sourceY / originalHeight) * 100}%`,
              }}
            >
              {index + 1}
            </span>
          ))
        : null}
    </div>
  )
}
