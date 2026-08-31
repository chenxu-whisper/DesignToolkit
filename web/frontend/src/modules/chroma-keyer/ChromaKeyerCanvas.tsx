import type { KeyboardEvent, MouseEvent } from 'react'

export type ChromaCanvasSample = {
  hex: string
  rgb: readonly [red: number, green: number, blue: number]
  sourceX: number
  sourceY: number
}

type ChromaKeyerCanvasProps = {
  onSample: (point: { x: number; y: number; previewWidth: number; previewHeight: number }) => void
  originalHeight?: number
  originalWidth?: number
  previewUrl: string
  samples: readonly ChromaCanvasSample[]
  viewMode: 'final' | 'alpha'
}

type ChromaSamplePoint = {
  x: number
  y: number
  previewWidth: number
  previewHeight: number
}

export function buildChromaSamplePayload(
  rect: Pick<DOMRect, 'height' | 'left' | 'top' | 'width'>,
  clientX: number,
  clientY: number,
): ChromaSamplePoint | null {
  if (![clientX, clientY, rect.height, rect.left, rect.top, rect.width].every(Number.isFinite)) {
    return null
  }

  return {
    x: Math.floor(clientX - rect.left),
    y: Math.floor(clientY - rect.top),
    previewHeight: rect.height,
    previewWidth: rect.width,
  }
}

export function ChromaKeyerCanvas({
  onSample,
  originalHeight,
  originalWidth,
  previewUrl,
  samples,
  viewMode,
}: ChromaKeyerCanvasProps) {
  const sampleAt = (element: HTMLImageElement, clientX: number, clientY: number) => {
    const payload = buildChromaSamplePayload(element.getBoundingClientRect(), clientX, clientY)
    if (payload) {
      onSample(payload)
    }
  }

  const handleClick = (event: MouseEvent<HTMLImageElement>) => {
    sampleAt(event.currentTarget, event.clientX, event.clientY)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLImageElement>) => {
    if (event.key !== 'Enter' && event.key !== ' ') {
      return
    }

    event.preventDefault()
    const rect = event.currentTarget.getBoundingClientRect()
    sampleAt(event.currentTarget, rect.left + rect.width / 2, rect.top + rect.height / 2)
  }

  return (
    <>
      <div
        className={`chroma-preview-canvas${viewMode === 'final' ? ' chroma-preview-canvas-final' : ''}`}
      >
        <img
          alt="抠像预览"
          aria-describedby="chroma-keyer-preview-sampling-instructions"
          className="chroma-preview-image"
          onClick={handleClick}
          onKeyDown={handleKeyDown}
          role="button"
          src={previewUrl}
          tabIndex={0}
        />
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
      <p className="chroma-preview-instructions" id="chroma-keyer-preview-sampling-instructions">
        点击预览采样，或聚焦后按 Enter 或空格从画面中心采样。
      </p>
    </>
  )
}
