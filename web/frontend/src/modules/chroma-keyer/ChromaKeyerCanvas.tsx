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

export function ChromaKeyerCanvas({
  onSample,
  originalHeight,
  originalWidth,
  previewUrl,
  samples,
}: ChromaKeyerCanvasProps) {
  const handleClick = (event: MouseEvent<HTMLImageElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    onSample({
      x: Math.floor(event.clientX - rect.left),
      y: Math.floor(event.clientY - rect.top),
      previewHeight: rect.height,
      previewWidth: rect.width,
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
