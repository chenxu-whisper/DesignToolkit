import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
  type WheelEvent,
} from 'react'
import {
  MAX_MANUAL_SCALE,
  MIN_MANUAL_SCALE,
  calculateFitScale,
  clampOffset,
  getDiscreteZoomScale,
  getWheelScale,
  zoomAtAnchor,
  type ViewportPoint,
  type ViewportSize,
} from './imageViewportMath'

export type ImageViewportMode = 'fit' | 'manual'

type UseImageViewportOptions = {
  imageHeight: number
  imageWidth: number
  viewportKey: number
}

type ImageViewportState = {
  mode: ImageViewportMode
  offset: ViewportPoint
  scale: number
}

type ImageViewport = ImageViewportState & {
  canZoomIn: boolean
  canZoomOut: boolean
  fit: () => void
  handleKeyDown: (event: KeyboardEvent<HTMLElement>) => void
  handleWheel: (event: WheelEvent<HTMLElement>) => void
  setActualSize: () => void
  viewportRef: RefObject<HTMLDivElement | null>
  zoomIn: () => void
  zoomOut: () => void
}

const EMPTY_SIZE: ViewportSize = { height: 0, width: 0 }
const FIT_FALLBACK: ImageViewportState = {
  mode: 'fit',
  offset: { x: 0, y: 0 },
  scale: 0,
}

function isPositiveSize({ height, width }: ViewportSize) {
  return (
    Number.isFinite(height)
    && Number.isFinite(width)
    && height > 0
    && width > 0
  )
}

export function useImageViewport({
  imageHeight,
  imageWidth,
  viewportKey,
}: UseImageViewportOptions): ImageViewport {
  const viewportRef = useRef<HTMLDivElement>(null)
  const viewportSizeRef = useRef<ViewportSize>(EMPTY_SIZE)
  const imageSizeRef = useRef<ViewportSize>({
    height: imageHeight,
    width: imageWidth,
  })
  const stateRef = useRef<ImageViewportState>(FIT_FALLBACK)
  const [state, setState] = useState<ImageViewportState>(FIT_FALLBACK)

  imageSizeRef.current = { height: imageHeight, width: imageWidth }
  stateRef.current = state

  const updateState = useCallback((
    update: (current: ImageViewportState) => ImageViewportState,
  ) => {
    setState((current) => {
      const next = update(current)
      stateRef.current = next
      return next
    })
  }, [])

  const createFitState = useCallback((): ImageViewportState => {
    const image = imageSizeRef.current
    const viewport = viewportSizeRef.current
    const scale = calculateFitScale(viewport, image)
    if (scale <= 0) {
      return FIT_FALLBACK
    }
    return {
      mode: 'fit',
      offset: clampOffset({ x: 0, y: 0 }, scale, image, viewport),
      scale,
    }
  }, [])

  const measure = useCallback(() => {
    const element = viewportRef.current
    if (!element) {
      return
    }

    const { height, width } = element.getBoundingClientRect()
    const viewport = { height, width }
    viewportSizeRef.current = viewport

    if (!isPositiveSize(viewport) || !isPositiveSize(imageSizeRef.current)) {
      updateState(() => FIT_FALLBACK)
      return
    }

    updateState((current) => {
      if (current.mode === 'fit') {
        return createFitState()
      }
      return {
        ...current,
        offset: clampOffset(
          current.offset,
          current.scale,
          imageSizeRef.current,
          viewport,
        ),
      }
    })
  }, [createFitState, updateState])

  const applyScale = useCallback((
    nextScale: number,
    anchor: ViewportPoint,
    forceManual = false,
  ) => {
    const image = imageSizeRef.current
    const viewport = viewportSizeRef.current
    const current = stateRef.current
    if (
      !isPositiveSize(image)
      || !isPositiveSize(viewport)
      || !Number.isFinite(current.scale)
      || current.scale <= 0
      || !Number.isFinite(nextScale)
      || nextScale <= 0
      || (nextScale === current.scale && !forceManual)
    ) {
      return false
    }

    updateState((latest) => ({
      mode: 'manual',
      offset: clampOffset(
        zoomAtAnchor(anchor, latest.scale, nextScale, latest.offset),
        nextScale,
        image,
        viewport,
      ),
      scale: nextScale,
    }))
    return true
  }, [updateState])

  const getCenterAnchor = useCallback((): ViewportPoint => ({
    x: viewportSizeRef.current.width / 2,
    y: viewportSizeRef.current.height / 2,
  }), [])

  const zoomIn = useCallback(() => {
    applyScale(
      getDiscreteZoomScale(stateRef.current.scale, 1),
      getCenterAnchor(),
    )
  }, [applyScale, getCenterAnchor])

  const zoomOut = useCallback(() => {
    const current = stateRef.current
    if (current.mode === 'fit' && current.scale <= MIN_MANUAL_SCALE) {
      return
    }
    applyScale(
      getDiscreteZoomScale(current.scale, -1),
      getCenterAnchor(),
    )
  }, [applyScale, getCenterAnchor])

  const setActualSize = useCallback(() => {
    applyScale(1, getCenterAnchor(), true)
  }, [applyScale, getCenterAnchor])

  const fit = useCallback(() => {
    updateState(() => createFitState())
  }, [createFitState, updateState])

  const handleWheel = useCallback((event: WheelEvent<HTMLElement>) => {
    const current = stateRef.current
    if (
      current.mode === 'fit'
      && current.scale <= MIN_MANUAL_SCALE
      && event.deltaY > 0
    ) {
      return
    }

    const element = viewportRef.current
    if (!element) {
      return
    }
    const rectangle = element.getBoundingClientRect()
    if (!isPositiveSize(rectangle)) {
      return
    }
    const nextScale = getWheelScale(
      current.scale,
      event.deltaY,
      event.deltaMode,
      rectangle.height,
    )
    const applied = applyScale(nextScale, {
      x: event.clientX - rectangle.left,
      y: event.clientY - rectangle.top,
    })
    if (applied) {
      event.preventDefault()
    }
  }, [applyScale])

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    switch (event.key.toLowerCase()) {
      case '+':
      case '=':
        event.preventDefault()
        zoomIn()
        break
      case '-':
        event.preventDefault()
        zoomOut()
        break
      case '0':
        event.preventDefault()
        setActualSize()
        break
      case 'f':
        event.preventDefault()
        fit()
        break
    }
  }, [fit, setActualSize, zoomIn, zoomOut])

  useEffect(() => {
    updateState(() => createFitState())
  }, [viewportKey, createFitState, updateState])

  useEffect(() => {
    measure()
    const element = viewportRef.current
    const observer = (
      element && typeof globalThis.ResizeObserver === 'function'
    )
      ? new ResizeObserver(measure)
      : null
    if (element) {
      observer?.observe(element)
    }
    window.addEventListener('resize', measure)

    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [measure])

  const hasValidDimensions = (
    isPositiveSize(imageSizeRef.current)
    && isPositiveSize(viewportSizeRef.current)
    && state.scale > 0
  )

  return {
    canZoomIn: hasValidDimensions && state.scale < MAX_MANUAL_SCALE,
    canZoomOut: hasValidDimensions && state.scale > MIN_MANUAL_SCALE,
    fit,
    handleKeyDown,
    handleWheel,
    mode: state.mode,
    offset: state.offset,
    scale: state.scale,
    setActualSize,
    viewportRef,
    zoomIn,
    zoomOut,
  }
}
