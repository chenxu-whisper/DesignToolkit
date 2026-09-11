import {
  act,
  createEvent,
  fireEvent,
  render,
  screen,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useImageViewport } from './useImageViewport'

type HarnessProps = {
  imageHeight?: number
  imageWidth?: number
  onWheelHandled?: (defaultPrevented: boolean) => void
  viewportKey: number
}

function makeRect(
  width: number,
  height: number,
  left = 0,
  top = 0,
): DOMRect {
  return {
    bottom: top + height,
    height,
    left,
    right: left + width,
    toJSON: () => ({}),
    top,
    width,
    x: left,
    y: top,
  }
}

class ResizeObserverStub {
  static instances: ResizeObserverStub[] = []

  callback: ResizeObserverCallback
  disconnect = vi.fn()
  observe = vi.fn()
  unobserve = vi.fn()

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    ResizeObserverStub.instances.push(this)
  }

  trigger() {
    this.callback([], this as unknown as ResizeObserver)
  }
}

function ViewportHarness({
  imageHeight = 400,
  imageWidth = 800,
  onWheelHandled,
  viewportKey,
}: HarnessProps) {
  const viewport = useImageViewport({
    imageHeight,
    imageWidth,
    viewportKey,
  })

  return (
    <div
      data-testid="viewport"
      onKeyDown={viewport.handleKeyDown}
      onWheel={(event) => {
        viewport.handleWheel(event)
        onWheelHandled?.(event.defaultPrevented)
      }}
      ref={viewport.viewportRef}
      tabIndex={0}
    >
      <output data-testid="mode">{viewport.mode}</output>
      <output data-testid="scale">{viewport.scale}</output>
      <output data-testid="offset">
        {viewport.offset.x},{viewport.offset.y}
      </output>
      <output data-testid="can-zoom-in">{String(viewport.canZoomIn)}</output>
      <output data-testid="can-zoom-out">{String(viewport.canZoomOut)}</output>
      <button onClick={viewport.zoomIn} type="button">Zoom in</button>
      <button onClick={viewport.zoomOut} type="button">Zoom out</button>
      <button onClick={viewport.fit} type="button">Fit</button>
      <button onClick={viewport.setActualSize} type="button">Actual size</button>
    </div>
  )
}

describe('useImageViewport', () => {
  let rectangle: DOMRect

  beforeEach(() => {
    rectangle = makeRect(400, 300)
    ResizeObserverStub.instances = []
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(() => rectangle)
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('starts fitted, zooms around the center, and restores fit', () => {
    render(<ViewportHarness viewportKey={1} />)

    expect(screen.getByTestId('mode')).toHaveTextContent('fit')
    expect(screen.getByTestId('scale')).toHaveTextContent('0.5')
    expect(screen.getByTestId('offset')).toHaveTextContent('0,50')

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(screen.getByTestId('mode')).toHaveTextContent('manual')
    expect(screen.getByTestId('scale')).toHaveTextContent('0.67')
    expect(screen.getByTestId('offset')).toHaveTextContent('-68,16')

    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    expect(screen.getByTestId('scale')).toHaveTextContent('1')

    fireEvent.click(screen.getByRole('button', { name: 'Fit' }))
    expect(screen.getByTestId('mode')).toHaveTextContent('fit')
    expect(screen.getByTestId('scale')).toHaveTextContent('0.5')
    expect(screen.getByTestId('offset')).toHaveTextContent('0,50')
  })

  it('sets manual actual size when fit is already 100%', () => {
    render(
      <ViewportHarness
        imageHeight={100}
        imageWidth={100}
        viewportKey={1}
      />,
    )

    expect(screen.getByTestId('mode')).toHaveTextContent('fit')
    expect(screen.getByTestId('scale')).toHaveTextContent('1')
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    expect(screen.getByTestId('mode')).toHaveTextContent('manual')
    expect(screen.getByTestId('scale')).toHaveTextContent('1')
  })

  it('preserves manual state for the same key and resets for a new viewport key', () => {
    const { rerender } = render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))

    rerender(<ViewportHarness viewportKey={1} />)
    expect(screen.getByTestId('mode')).toHaveTextContent('manual')
    expect(screen.getByTestId('scale')).toHaveTextContent('1')

    rerender(<ViewportHarness viewportKey={2} />)
    expect(screen.getByTestId('mode')).toHaveTextContent('fit')
    expect(screen.getByTestId('scale')).toHaveTextContent('0.5')
    expect(screen.getByTestId('offset')).toHaveTextContent('0,50')
  })

  it('keeps the pointer anchor stable during wheel zoom and prevents scrolling', () => {
    const onWheelHandled = vi.fn()
    render(
      <ViewportHarness
        onWheelHandled={onWheelHandled}
        viewportKey={1}
      />,
    )
    const viewport = screen.getByTestId('viewport')
    const wheel = createEvent.wheel(viewport, {
      cancelable: true,
      clientX: 110,
      clientY: 120,
      deltaMode: 0,
      deltaY: -500,
    })

    fireEvent(viewport, wheel)

    expect(onWheelHandled).toHaveBeenCalledWith(true)
    expect(screen.getByTestId('mode')).toHaveTextContent('manual')
    expect(screen.getByTestId('scale')).toHaveTextContent('1')
    expect(screen.getByTestId('offset')).toHaveTextContent('-110,-20')
  })

  it('ignores a zero-delta wheel event below the minimum manual scale', () => {
    const onWheelHandled = vi.fn()
    rectangle = makeRect(100, 100)
    render(
      <ViewportHarness
        imageHeight={1000}
        imageWidth={1000}
        onWheelHandled={onWheelHandled}
        viewportKey={1}
      />,
    )
    const viewport = screen.getByTestId('viewport')
    const wheel = createEvent.wheel(viewport, {
      cancelable: true,
      deltaMode: 0,
      deltaY: 0,
    })

    fireEvent(viewport, wheel)

    expect(onWheelHandled).toHaveBeenCalledWith(false)
    expect(screen.getByTestId('mode')).toHaveTextContent('fit')
    expect(screen.getByTestId('scale')).toHaveTextContent('0.1')
  })

  it('ignores a non-finite wheel delta without corrupting viewport state', () => {
    const onWheelHandled = vi.fn()
    render(
      <ViewportHarness
        onWheelHandled={onWheelHandled}
        viewportKey={1}
      />,
    )
    const viewport = screen.getByTestId('viewport')
    const wheel = createEvent.wheel(viewport, {
      cancelable: true,
      deltaMode: 0,
      deltaY: 1,
    })
    Object.defineProperty(wheel, 'deltaY', { value: Number.NaN })

    fireEvent(viewport, wheel)

    const scale = Number(screen.getByTestId('scale').textContent)
    expect(onWheelHandled).toHaveBeenCalledWith(false)
    expect(Number.isFinite(scale)).toBe(true)
    expect(scale).toBe(0.5)
    expect(screen.getByTestId('mode')).toHaveTextContent('fit')
  })

  it('handles zoom keyboard commands without hijacking Enter', () => {
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')

    for (const [key, expectedScale, expectedMode] of [
      ['+', '0.67', 'manual'],
      ['=', '0.75', 'manual'],
      ['-', '0.67', 'manual'],
      ['0', '1', 'manual'],
      ['F', '0.5', 'fit'],
    ]) {
      const event = createEvent.keyDown(viewport, { key })
      fireEvent(viewport, event)
      expect(event.defaultPrevented).toBe(true)
      expect(screen.getByTestId('scale')).toHaveTextContent(expectedScale)
      expect(screen.getByTestId('mode')).toHaveTextContent(expectedMode)
    }

    const enter = createEvent.keyDown(viewport, { key: 'Enter' })
    fireEvent(viewport, enter)
    expect(enter.defaultPrevented).toBe(false)
    expect(screen.getByTestId('scale')).toHaveTextContent('0.5')
  })

  it('reports zoom limits for sub-minimum fit and manual endpoints', () => {
    rectangle = makeRect(100, 100)
    render(
      <ViewportHarness
        imageHeight={1000}
        imageWidth={1000}
        viewportKey={1}
      />,
    )

    expect(screen.getByTestId('scale')).toHaveTextContent('0.1')
    expect(screen.getByTestId('can-zoom-in')).toHaveTextContent('true')
    expect(screen.getByTestId('can-zoom-out')).toHaveTextContent('false')

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(screen.getByTestId('scale')).toHaveTextContent('0.25')
    expect(screen.getByTestId('can-zoom-out')).toHaveTextContent('false')

    const viewport = screen.getByTestId('viewport')
    fireEvent.wheel(viewport, { deltaMode: 0, deltaY: -1_000_000 })
    expect(screen.getByTestId('scale')).toHaveTextContent('8')
    expect(screen.getByTestId('can-zoom-in')).toHaveTextContent('false')
    expect(screen.getByTestId('can-zoom-out')).toHaveTextContent('true')

    fireEvent.wheel(viewport, { deltaMode: 0, deltaY: 1_000_000 })
    expect(screen.getByTestId('scale')).toHaveTextContent('0.25')
    expect(screen.getByTestId('can-zoom-in')).toHaveTextContent('true')
    expect(screen.getByTestId('can-zoom-out')).toHaveTextContent('false')
  })

  it('recomputes fit when ResizeObserver reports a viewport change', () => {
    render(<ViewportHarness viewportKey={1} />)
    const observer = ResizeObserverStub.instances[0]

    expect(observer.observe).toHaveBeenCalledWith(screen.getByTestId('viewport'))
    rectangle = makeRect(200, 100)
    act(() => {
      observer.trigger()
    })

    expect(screen.getByTestId('scale')).toHaveTextContent('0.25')
    expect(screen.getByTestId('offset')).toHaveTextContent('0,0')
  })

  it('preserves manual scale and clamps its offset after resize', () => {
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    expect(screen.getByTestId('offset')).toHaveTextContent('-200,-50')

    rectangle = makeRect(1000, 700)
    act(() => {
      ResizeObserverStub.instances[0].trigger()
    })

    expect(screen.getByTestId('mode')).toHaveTextContent('manual')
    expect(screen.getByTestId('scale')).toHaveTextContent('1')
    expect(screen.getByTestId('offset')).toHaveTextContent('100,150')
  })

  it('measures initially and on window resize without ResizeObserver', () => {
    vi.stubGlobal('ResizeObserver', undefined)
    render(<ViewportHarness viewportKey={1} />)

    expect(screen.getByTestId('scale')).toHaveTextContent('0.5')
    rectangle = makeRect(200, 100)
    fireEvent(window, new Event('resize'))

    expect(screen.getByTestId('scale')).toHaveTextContent('0.25')
    expect(screen.getByTestId('offset')).toHaveTextContent('0,0')
  })

  it.each([
    ['zero', makeRect(0, 0)],
    ['non-finite', makeRect(Number.POSITIVE_INFINITY, Number.NaN)],
  ])('falls back safely for a %s viewport rectangle', (_, invalidRectangle) => {
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))

    rectangle = invalidRectangle
    act(() => {
      ResizeObserverStub.instances[0].trigger()
    })

    const scale = Number(screen.getByTestId('scale').textContent)
    expect(screen.getByTestId('mode')).toHaveTextContent('fit')
    expect(Number.isFinite(scale)).toBe(true)
    expect(screen.getByTestId('offset')).toHaveTextContent('0,0')
  })

  it('disconnects the observer and removes the resize listener on unmount', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    const { unmount } = render(<ViewportHarness viewportKey={1} />)
    const observer = ResizeObserverStub.instances[0]
    const resizeListener = addEventListener.mock.calls.find(
      ([eventName]) => eventName === 'resize',
    )?.[1]

    expect(resizeListener).toBeDefined()
    unmount()

    expect(observer.disconnect).toHaveBeenCalledOnce()
    expect(removeEventListener).toHaveBeenCalledWith('resize', resizeListener)
  })
})
