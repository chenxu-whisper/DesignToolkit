import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
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
  handleKeyUp: (event: KeyboardEvent<HTMLElement>) => void
  handlePointerDown: (event: PointerEvent<HTMLElement>) => void
  handleWheel: (event: WheelEvent<HTMLElement>) => void
  isPanReady: boolean
  isPanning: boolean
  setActualSize: () => void
  shouldSuppressClick: () => boolean
  viewportRef: RefObject<HTMLDivElement | null>
  zoomIn: () => void
  zoomOut: () => void
}

type PanSession = {
  dragged: boolean
  listeners: {
    cancel: (event: globalThis.PointerEvent) => void
    move: (event: globalThis.PointerEvent) => void
    up: (event: globalThis.PointerEvent) => void
  }
  originOffset: ViewportPoint
  pointerId: number | undefined
  start: ViewportPoint
}

const DRAG_THRESHOLD = 4
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
  const panRef = useRef<PanSession | null>(null)
  const spacePressedRef = useRef(false)
  const stateRef = useRef<ImageViewportState>(FIT_FALLBACK)
  const suppressClickRef = useRef(false)
  const [isPanReady, setIsPanReady] = useState(false)
  const [isPanning, setIsPanning] = useState(false)
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

  const finishPan = useCallback((completed: boolean) => {
    const session = panRef.current
    if (!session) {
      if (!completed) {
        suppressClickRef.current = false
      }
      return
    }

    window.removeEventListener('pointermove', session.listeners.move)
    window.removeEventListener('pointerup', session.listeners.up)
    window.removeEventListener('pointercancel', session.listeners.cancel)
    suppressClickRef.current = completed && session.dragged
    panRef.current = null
    setIsPanning(false)
  }, [])

  const clearPanInteraction = useCallback(() => {
    spacePressedRef.current = false
    suppressClickRef.current = false
    setIsPanReady(false)
    finishPan(false)
  }, [finishPan])

  const handleWheel = useCallback((event: WheelEvent<HTMLElement>) => {
    if (!Number.isFinite(event.deltaY) || event.deltaY === 0) {
      return
    }

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
    if (event.key === ' ' || event.key === 'Spacebar') {
      event.preventDefault()
      if (!spacePressedRef.current) {
        spacePressedRef.current = true
        setIsPanReady(true)
      }
      return
    }

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

  const handleKeyUp = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== ' ' && event.key !== 'Spacebar') {
      return
    }
    event.preventDefault()
    spacePressedRef.current = false
    setIsPanReady(false)
    finishPan(true)
  }, [finishPan])

  const handlePointerDown = useCallback((event: PointerEvent<HTMLElement>) => {
    const target = event.target
    if (
      event.button !== 0
      || !spacePressedRef.current
      || panRef.current
      || !isPositiveSize(imageSizeRef.current)
      || !isPositiveSize(viewportSizeRef.current)
      || !Number.isFinite(stateRef.current.scale)
      || stateRef.current.scale <= 0
      || !Number.isFinite(event.clientX)
      || !Number.isFinite(event.clientY)
      || (
        target instanceof Element
        && target.closest('.image-viewport-toolbar')
      )
    ) {
      return
    }

    const matchesPointer = (pointerEvent: globalThis.PointerEvent) => {
      const activePointerId = panRef.current?.pointerId
      return (
        activePointerId === undefined
        || pointerEvent.pointerId === undefined
        || pointerEvent.pointerId === activePointerId
      )
    }
    const move = (pointerEvent: globalThis.PointerEvent) => {
      const session = panRef.current
      if (!session || !matchesPointer(pointerEvent)) {
        return
      }
      const dx = pointerEvent.clientX - session.start.x
      const dy = pointerEvent.clientY - session.start.y
      if (!Number.isFinite(dx) || !Number.isFinite(dy)) {
        return
      }
      if (Math.hypot(dx, dy) >= DRAG_THRESHOLD) {
        session.dragged = true
      }
      updateState((current) => ({
        ...current,
        offset: clampOffset(
          {
            x: session.originOffset.x + dx,
            y: session.originOffset.y + dy,
          },
          current.scale,
          imageSizeRef.current,
          viewportSizeRef.current,
        ),
      }))
    }
    const up = (pointerEvent: globalThis.PointerEvent) => {
      if (matchesPointer(pointerEvent)) {
        finishPan(true)
      }
    }
    const cancel = (pointerEvent: globalThis.PointerEvent) => {
      if (matchesPointer(pointerEvent)) {
        finishPan(false)
      }
    }

    panRef.current = {
      dragged: false,
      listeners: { cancel, move, up },
      originOffset: stateRef.current.offset,
      pointerId: Number.isFinite(event.pointerId)
        ? event.pointerId
        : undefined,
      start: { x: event.clientX, y: event.clientY },
    }
    suppressClickRef.current = false
    setIsPanning(true)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', cancel)
  }, [finishPan, updateState])

  const shouldSuppressClick = useCallback(() => {
    const shouldSuppress = suppressClickRef.current
    suppressClickRef.current = false
    return shouldSuppress
  }, [])

  useEffect(() => {
    clearPanInteraction()
    updateState(() => createFitState())
  }, [viewportKey, clearPanInteraction, createFitState, updateState])

  useEffect(() => {
    const handleBlur = () => {
      clearPanInteraction()
    }
    window.addEventListener('blur', handleBlur)
    return () => {
      window.removeEventListener('blur', handleBlur)
    }
  }, [clearPanInteraction])

  useEffect(() => () => {
    clearPanInteraction()
  }, [clearPanInteraction])

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
    handleKeyUp,
    handlePointerDown,
    handleWheel,
    isPanReady,
    isPanning,
    mode: state.mode,
    offset: state.offset,
    scale: state.scale,
    setActualSize,
    shouldSuppressClick,
    viewportRef,
    zoomIn,
    zoomOut,
  }
}
