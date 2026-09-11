import {
  act,
  createEvent,
  fireEvent,
  render,
  screen,
} from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useImageViewport } from './useImageViewport'

type HarnessProps = {
  imageHeight?: number
  imageWidth?: number
  isolateReactKeyUp?: boolean
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

type PointerEventInitWithId = MouseEventInit & {
  pointerId?: number
  pointerType?: string
}

function firePointerEvent(
  target: Window | Node,
  type: 'pointercancel' | 'pointerdown' | 'pointermove' | 'pointerup',
  init: PointerEventInitWithId = {},
) {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    ...init,
  })
  if (init.pointerId !== undefined) {
    Object.defineProperty(event, 'pointerId', { value: init.pointerId })
  }
  if (init.pointerType !== undefined) {
    Object.defineProperty(event, 'pointerType', { value: init.pointerType })
  }
  fireEvent(target, event)
  return event
}

function ViewportHarness({
  imageHeight = 400,
  imageWidth = 800,
  isolateReactKeyUp = false,
  onWheelHandled,
  viewportKey,
}: HarnessProps) {
  const viewport = useImageViewport({
    imageHeight,
    imageWidth,
    viewportKey,
  })
  const [suppressed, setSuppressed] = useState(false)

  return (
    <div
      data-testid="viewport"
      onKeyDown={viewport.handleKeyDown}
      onKeyUp={(event) => {
        viewport.handleKeyUp(event)
        if (isolateReactKeyUp) {
          event.stopPropagation()
        }
      }}
      onPointerDown={viewport.handlePointerDown}
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
      <output data-testid="pan-ready">{String(viewport.isPanReady)}</output>
      <output data-testid="panning">{String(viewport.isPanning)}</output>
      <output data-testid="suppress">{String(suppressed)}</output>
      <button onClick={viewport.zoomIn} type="button">Zoom in</button>
      <button onClick={viewport.zoomOut} type="button">Zoom out</button>
      <button onClick={viewport.fit} type="button">Fit</button>
      <button onClick={viewport.setActualSize} type="button">Actual size</button>
      <button
        onClick={() => setSuppressed(viewport.shouldSuppressClick())}
        type="button"
      >
        Consume suppression
      </button>
      <div className="image-viewport-toolbar">
        <button type="button">Toolbar target</button>
      </div>
    </div>
  )
}

function getAddedPanListeners(
  addEventListener: ReturnType<typeof vi.spyOn>,
) {
  const find = (eventName: string) => addEventListener.mock.calls.find(
    ([name]) => name === eventName,
  )?.[1]
  return {
    cancel: find('pointercancel'),
    move: find('pointermove'),
    up: find('pointerup'),
  }
}

function expectPanListenersRemoved(
  removeEventListener: ReturnType<typeof vi.spyOn>,
  listeners: ReturnType<typeof getAddedPanListeners>,
) {
  expect(listeners.move).toBeDefined()
  expect(listeners.up).toBeDefined()
  expect(listeners.cancel).toBeDefined()
  expect(removeEventListener).toHaveBeenCalledWith('pointermove', listeners.move)
  expect(removeEventListener).toHaveBeenCalledWith('pointerup', listeners.up)
  expect(removeEventListener).toHaveBeenCalledWith(
    'pointercancel',
    listeners.cancel,
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

  it('pans from the origin offset and suppresses exactly the next click', () => {
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    const viewport = screen.getByTestId('viewport')

    const spaceDown = createEvent.keyDown(viewport, { key: ' ' })
    fireEvent(viewport, spaceDown)
    expect(spaceDown.defaultPrevented).toBe(true)
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('true')

    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 200,
      clientY: 150,
      pointerId: 7,
    })
    expect(screen.getByTestId('panning')).toHaveTextContent('true')
    firePointerEvent(window, 'pointermove', {
      clientX: 100,
      clientY: 100,
      pointerId: 7,
    })
    firePointerEvent(window, 'pointerup', { pointerId: 7 })

    expect(screen.getByTestId('offset')).toHaveTextContent('-300,-100')
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('true')
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('false')
  })

  it('does not classify movement below four CSS pixels as a drag', () => {
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 20,
      clientY: 20,
      pointerId: 8,
    })
    firePointerEvent(window, 'pointermove', {
      clientX: 23,
      clientY: 20,
      pointerId: 8,
    })
    firePointerEvent(window, 'pointerup', { pointerId: 8 })

    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('false')
  })

  it('prevents native drag only for an accepted pan pointer-down', () => {
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')

    const withoutSpace = firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      pointerId: 20,
    })
    fireEvent.keyDown(viewport, { key: ' ' })
    const nonPrimary = firePointerEvent(viewport, 'pointerdown', {
      button: 1,
      pointerId: 21,
    })
    const toolbar = firePointerEvent(
      screen.getByRole('button', { name: 'Toolbar target' }),
      'pointerdown',
      {
        button: 0,
        pointerId: 22,
      },
    )
    const accepted = firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      pointerId: 23,
    })

    expect(withoutSpace.defaultPrevented).toBe(false)
    expect(nonPrimary.defaultPrevented).toBe(false)
    expect(toolbar.defaultPrevented).toBe(false)
    expect(accepted.defaultPrevented).toBe(true)
    expect(screen.getByTestId('panning')).toHaveTextContent('true')
  })

  it('requires Space, a primary pointer, valid dimensions, and a non-toolbar target', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    const viewport = screen.getByTestId('viewport')
    addEventListener.mockClear()

    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 200,
      clientY: 150,
      pointerId: 1,
      pointerType: 'touch',
    })
    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 1,
      clientX: 200,
      clientY: 150,
      pointerId: 2,
    })
    firePointerEvent(screen.getByRole('button', { name: 'Toolbar target' }), 'pointerdown', {
      button: 0,
      clientX: 200,
      clientY: 150,
      pointerId: 3,
    })
    firePointerEvent(window, 'pointermove', {
      clientX: 0,
      clientY: 0,
      pointerId: 3,
    })

    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    expect(screen.getByTestId('offset')).toHaveTextContent('-200,-50')
    expect(addEventListener.mock.calls.filter(
      ([name]) => String(name).startsWith('pointer'),
    )).toHaveLength(0)
  })

  it('does not start panning with invalid image dimensions', () => {
    render(<ViewportHarness imageWidth={0} viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      pointerId: 4,
    })

    expect(screen.getByTestId('panning')).toHaveTextContent('false')
  })

  it('does not intercept Space from a toolbar button', () => {
    render(<ViewportHarness viewportKey={1} />)
    const toolbarButton = screen.getByRole('button', {
      name: 'Toolbar target',
    })

    const spaceDown = createEvent.keyDown(toolbarButton, { key: ' ' })
    fireEvent(toolbarButton, spaceDown)
    const spaceUp = createEvent.keyUp(toolbarButton, { key: ' ' })
    fireEvent(toolbarButton, spaceUp)

    expect(spaceDown.defaultPrevented).toBe(false)
    expect(spaceUp.defaultPrevented).toBe(false)
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
  })

  it('releases the pan latch on React Space keyup from a toolbar button', () => {
    render(<ViewportHarness isolateReactKeyUp viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    const toolbarButton = screen.getByRole('button', {
      name: 'Toolbar target',
    })
    fireEvent.keyDown(viewport, { key: ' ' })
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('true')

    const spaceUp = createEvent.keyUp(toolbarButton, {
      cancelable: true,
      key: ' ',
    })
    fireEvent(toolbarButton, spaceUp)

    expect(spaceUp.defaultPrevented).toBe(false)
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
  })

  it('globally releases a completed pan from an interactive Space keyup', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    const viewport = screen.getByTestId('viewport')
    const toolbarButton = screen.getByRole('button', {
      name: 'Toolbar target',
    })
    const windowKeyUp = addEventListener.mock.calls.find(
      ([eventName]) => eventName === 'keyup',
    )?.[1] as EventListener | undefined
    expect(windowKeyUp).toBeDefined()

    fireEvent.keyDown(viewport, { key: ' ' })
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('true')
    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 20,
      clientY: 20,
      pointerId: 25,
    })
    const listeners = getAddedPanListeners(addEventListener)
    firePointerEvent(window, 'pointermove', {
      clientX: 24,
      clientY: 20,
      pointerId: 25,
    })

    const spaceUp = new KeyboardEvent('keyup', {
      bubbles: true,
      cancelable: true,
      key: ' ',
    })
    Object.defineProperty(spaceUp, 'target', { value: toolbarButton })
    act(() => {
      windowKeyUp?.(spaceUp)
    })

    expect(spaceUp.defaultPrevented).toBe(false)
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    expectPanListenersRemoved(removeEventListener, listeners)
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('true')
  })

  it('clamps movement to the near and far boundary on both axes', () => {
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 200,
      clientY: 150,
      pointerId: 9,
    })

    firePointerEvent(window, 'pointermove', {
      clientX: 1200,
      clientY: 1150,
      pointerId: 9,
    })
    expect(screen.getByTestId('offset')).toHaveTextContent('0,0')

    firePointerEvent(window, 'pointermove', {
      clientX: -800,
      clientY: -850,
      pointerId: 9,
    })
    expect(screen.getByTestId('offset')).toHaveTextContent('-400,-100')
  })

  it('tracks Space readiness and ends panning on Space release', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    const spaceDown = createEvent.keyDown(viewport, { key: 'Spacebar' })
    fireEvent(viewport, spaceDown)
    fireEvent.keyDown(viewport, { key: ' ', repeat: true })
    expect(spaceDown.defaultPrevented).toBe(true)
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('true')

    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', { button: 0, pointerId: 10 })
    const listeners = getAddedPanListeners(addEventListener)
    const spaceUp = createEvent.keyUp(viewport, { key: ' ' })
    fireEvent(viewport, spaceUp)

    expect(spaceUp.defaultPrevented).toBe(true)
    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    expectPanListenersRemoved(removeEventListener, listeners)
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('false')
  })

  it('suppresses the next click when Space ends a completed drag', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 20,
      clientY: 20,
      pointerId: 19,
    })
    const listeners = getAddedPanListeners(addEventListener)
    firePointerEvent(window, 'pointermove', {
      clientX: 24,
      clientY: 20,
      pointerId: 19,
    })

    fireEvent.keyUp(viewport, { key: ' ' })

    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    expectPanListenersRemoved(removeEventListener, listeners)
    firePointerEvent(window, 'pointerup', { pointerId: 19 })
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('true')
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('false')
  })

  it('ends a completed drag on window Space release and preserves suppression', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    const windowKeyUp = addEventListener.mock.calls.find(
      ([eventName]) => eventName === 'keyup',
    )?.[1]
    expect(windowKeyUp).toBeDefined()

    fireEvent.keyDown(viewport, { key: ' ' })
    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 20,
      clientY: 20,
      pointerId: 24,
    })
    const listeners = getAddedPanListeners(addEventListener)
    firePointerEvent(window, 'pointermove', {
      clientX: 24,
      clientY: 20,
      pointerId: 24,
    })

    fireEvent.keyUp(window, { key: ' ' })

    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    expectPanListenersRemoved(removeEventListener, listeners)
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('true')
  })

  it('ignores movement and pointer-up from a different pointer', () => {
    render(<ViewportHarness viewportKey={1} />)
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }))
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 200,
      clientY: 150,
      pointerId: 11,
    })

    firePointerEvent(window, 'pointermove', {
      clientX: 0,
      clientY: 0,
      pointerId: 12,
    })
    firePointerEvent(window, 'pointerup', { pointerId: 12 })

    expect(screen.getByTestId('offset')).toHaveTextContent('-200,-50')
    expect(screen.getByTestId('panning')).toHaveTextContent('true')
    firePointerEvent(window, 'pointerup', { pointerId: 11 })
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
  })

  it.each([
    ['pointer-up', 'pointerup'],
    ['pointer-cancel', 'pointercancel'],
  ] as const)('removes identical pan listeners on %s', (_, endEvent) => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', { button: 0, pointerId: 13 })
    const listeners = getAddedPanListeners(addEventListener)

    firePointerEvent(window, endEvent, { pointerId: 13 })

    expectPanListenersRemoved(removeEventListener, listeners)
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
  })

  it('clears Space and active pan listeners on window blur', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', { button: 0, pointerId: 14 })
    const listeners = getAddedPanListeners(addEventListener)

    fireEvent.blur(window)

    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    expectPanListenersRemoved(removeEventListener, listeners)
  })

  it('cleans active pan and readiness when the viewport key changes', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    const { rerender } = render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', { button: 0, pointerId: 15 })
    const listeners = getAddedPanListeners(addEventListener)

    rerender(<ViewportHarness viewportKey={2} />)

    expect(screen.getByTestId('pan-ready')).toHaveTextContent('false')
    expect(screen.getByTestId('panning')).toHaveTextContent('false')
    expectPanListenersRemoved(removeEventListener, listeners)
  })

  it('clears prior click suppression when the viewport key changes', () => {
    const { rerender } = render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 0,
      clientY: 0,
      pointerId: 16,
    })
    firePointerEvent(window, 'pointermove', {
      clientX: 4,
      clientY: 0,
      pointerId: 16,
    })
    firePointerEvent(window, 'pointerup', { pointerId: 16 })

    rerender(<ViewportHarness viewportKey={2} />)
    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('false')
  })

  it('does not suppress clicks after pointer cancellation', () => {
    render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 0,
      clientY: 0,
      pointerId: 17,
    })
    firePointerEvent(window, 'pointermove', {
      clientX: 4,
      clientY: 0,
      pointerId: 17,
    })
    firePointerEvent(window, 'pointercancel', { pointerId: 17 })

    fireEvent.click(screen.getByRole('button', { name: 'Consume suppression' }))
    expect(screen.getByTestId('suppress')).toHaveTextContent('false')
  })

  it('removes active pan listeners on unmount', () => {
    const addEventListener = vi.spyOn(window, 'addEventListener')
    const removeEventListener = vi.spyOn(window, 'removeEventListener')
    const { unmount } = render(<ViewportHarness viewportKey={1} />)
    const viewport = screen.getByTestId('viewport')
    const windowKeyUp = addEventListener.mock.calls.find(
      ([eventName]) => eventName === 'keyup',
    )?.[1]
    expect(windowKeyUp).toBeDefined()
    fireEvent.keyDown(viewport, { key: ' ' })
    addEventListener.mockClear()
    firePointerEvent(viewport, 'pointerdown', { button: 0, pointerId: 18 })
    const listeners = getAddedPanListeners(addEventListener)

    unmount()

    expectPanListenersRemoved(removeEventListener, listeners)
    expect(removeEventListener).toHaveBeenCalledWith('keyup', windowKeyUp)
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
