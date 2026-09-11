import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'
import { ImageViewportToolbar } from './ImageViewportToolbar'
import { useImageViewport } from './useImageViewport'

export type ChromaCanvasSample = {
  hex: string
  rgb: readonly [red: number, green: number, blue: number]
  sourceX: number
  sourceY: number
}

type ChromaKeyerCanvasProps = {
  children?: ReactNode
  disabled?: boolean
  onSample: (point: { x: number; y: number; previewWidth: number; previewHeight: number }) => void
  originalHeight?: number
  originalWidth?: number
  previewUrl: string
  samples: readonly ChromaCanvasSample[]
  viewMode: 'final' | 'alpha'
  viewportKey: number
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
  children,
  disabled = false,
  onSample,
  originalHeight,
  originalWidth,
  previewUrl,
  samples,
  viewMode,
  viewportKey,
}: ChromaKeyerCanvasProps) {
  const viewport = useImageViewport({
    imageHeight: originalHeight ?? 0,
    imageWidth: originalWidth ?? 0,
    viewportKey,
  })

  const sampleAt = (element: HTMLImageElement, clientX: number, clientY: number) => {
    if (disabled) {
      return
    }

    const payload = buildChromaSamplePayload(element.getBoundingClientRect(), clientX, clientY)
    if (payload) {
      onSample(payload)
    }
  }

  const handleClick = (event: MouseEvent<HTMLImageElement>) => {
    if (viewport.shouldSuppressClick()) {
      event.preventDefault()
      return
    }
    sampleAt(event.currentTarget, event.clientX, event.clientY)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLImageElement>) => {
    if (event.key !== 'Enter') {
      return
    }

    event.preventDefault()
    const rect = event.currentTarget.getBoundingClientRect()
    sampleAt(event.currentTarget, rect.left + rect.width / 2, rect.top + rect.height / 2)
  }

  return (
    <>
      <div
        className={[
          'chroma-preview-viewport',
          viewport.isPanReady ? 'is-pan-ready' : '',
          viewport.isPanning ? 'is-panning' : '',
        ].filter(Boolean).join(' ')}
        onKeyDown={viewport.handleKeyDown}
        onKeyUp={viewport.handleKeyUp}
        onPointerDown={viewport.handlePointerDown}
        onWheel={viewport.handleWheel}
        ref={viewport.viewportRef}
        tabIndex={0}
      >
        <div
          className={`chroma-preview-canvas${viewMode === 'final' ? ' chroma-preview-canvas-final' : ''}`}
        >
          <div
            className="chroma-preview-content"
            style={{
              height: originalHeight,
              position: 'absolute',
              transform: `translate3d(${viewport.offset.x}px, ${viewport.offset.y}px, 0) scale(${viewport.scale})`,
              transformOrigin: '0 0',
              width: originalWidth,
            }}
          >
            <img
              alt="抠像预览"
              aria-describedby="chroma-keyer-preview-sampling-instructions"
              aria-disabled={disabled || undefined}
              className={`chroma-preview-image${disabled ? ' chroma-preview-image-disabled' : ''}`}
              draggable={false}
              onClick={handleClick}
              onKeyDown={handleKeyDown}
              role="button"
              src={previewUrl}
              tabIndex={disabled ? -1 : 0}
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
        </div>
        <ImageViewportToolbar
          canZoomIn={viewport.canZoomIn}
          canZoomOut={viewport.canZoomOut}
          onActualSize={viewport.setActualSize}
          onFit={viewport.fit}
          onZoomIn={viewport.zoomIn}
          onZoomOut={viewport.zoomOut}
          scale={viewport.scale}
        />
      </div>
      {children}
      <p className="chroma-preview-instructions" id="chroma-keyer-preview-sampling-instructions">
        点击预览采样；按 Enter 从画面中心采样；按住空格并拖拽可平移画面。
      </p>
    </>
  )
}
