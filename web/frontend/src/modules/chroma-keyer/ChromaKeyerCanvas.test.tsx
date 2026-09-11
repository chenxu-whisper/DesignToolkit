import { createEvent, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ChromaKeyerCanvas,
  type ChromaCanvasSample,
} from './ChromaKeyerCanvas'

function makeRect({
  height,
  left = 0,
  top = 0,
  width,
}: {
  height: number
  left?: number
  top?: number
  width: number
}): DOMRect {
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
  disconnect = vi.fn()
  observe = vi.fn()
  unobserve = vi.fn()

  constructor(_callback: ResizeObserverCallback) {}
}

type CanvasProps = React.ComponentProps<typeof ChromaKeyerCanvas>

function renderCanvas(overrides: Partial<CanvasProps> = {}) {
  const props: CanvasProps = {
    onSample: vi.fn(),
    originalHeight: 400,
    originalWidth: 800,
    previewUrl: '/preview.png',
    samples: [],
    viewMode: 'final',
    viewportKey: 1,
    ...overrides,
  }
  return {
    ...render(<ChromaKeyerCanvas {...props} />),
    props,
  }
}

type PointerEventInitWithId = MouseEventInit & {
  pointerId?: number
}

function firePointerEvent(
  target: Window | Node,
  type: 'pointerdown' | 'pointermove' | 'pointerup',
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
  fireEvent(target, event)
}

describe('ChromaKeyerCanvas', () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockReturnValue(makeRect({ height: 300, width: 400 }))
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('gives the keyboard-focusable viewport an accessible name and instructions', () => {
    renderCanvas()

    const viewport = screen.getByRole('group', { name: '抠像预览视口' })
    const instructions = screen.getByText(
      '点击预览采样；按 Enter 从画面中心采样；按住空格并拖拽可平移画面。',
    )

    expect(viewport).toHaveAttribute('tabindex', '0')
    expect(viewport).toHaveAttribute(
      'aria-describedby',
      'chroma-keyer-preview-sampling-instructions',
    )
    expect(instructions).toHaveAttribute(
      'id',
      'chroma-keyer-preview-sampling-instructions',
    )
  })

  it('submits click coordinates relative to the rendered preview', () => {
    const onSample = vi.fn()
    renderCanvas({ onSample })

    const image = screen.getByRole('button', { name: '抠像预览' })
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 200, left: 10, top: 20, width: 400 }),
    )

    fireEvent.click(image, { clientX: 210, clientY: 120 })

    expect(onSample).toHaveBeenCalledWith({
      x: 200,
      y: 100,
      previewHeight: 200,
      previewWidth: 400,
    })
  })

  it('preserves fractional preview dimensions and floors near-edge click coordinates', () => {
    const onSample = vi.fn()
    renderCanvas({ onSample })

    const image = screen.getByRole('button', { name: '抠像预览' })
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 200.5, left: 10, top: 20, width: 400.5 }),
    )

    fireEvent.click(image, { clientX: 410.4, clientY: 220.4 })

    expect(onSample).toHaveBeenCalledWith({
      x: 400,
      y: 200,
      previewHeight: 200.5,
      previewWidth: 400.5,
    })
  })

  it('samples the transformed image center once when Enter is pressed on the viewport', () => {
    const onSample = vi.fn()
    renderCanvas({ onSample })

    const viewport = screen.getByRole('group', { name: '抠像预览视口' })
    const image = screen.getByRole('button', { name: '抠像预览' })
    const getImageRect = vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 200.5, left: 10, top: 20, width: 400.5 }),
    )
    viewport.focus()
    const enter = createEvent.keyDown(viewport, { key: 'Enter' })

    fireEvent(viewport, enter)

    expect(viewport).toHaveFocus()
    expect(enter.defaultPrevented).toBe(true)
    expect(getImageRect).toHaveBeenCalledOnce()
    expect(onSample).toHaveBeenCalledOnce()
    expect(onSample).toHaveBeenCalledWith({
      x: 200,
      y: 100,
      previewHeight: 200.5,
      previewWidth: 400.5,
    })
  })

  it('samples the transformed image center once when image Enter bubbles', () => {
    const onSample = vi.fn()
    renderCanvas({ onSample })

    const image = screen.getByRole('button', { name: '抠像预览' })
    const getImageRect = vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 200.5, left: 10, top: 20, width: 400.5 }),
    )
    const enter = createEvent.keyDown(image, { key: 'Enter' })

    fireEvent(image, enter)

    expect(enter.defaultPrevented).toBe(true)
    expect(image).toHaveAttribute('aria-describedby')
    expect(getImageRect).toHaveBeenCalledOnce()
    expect(onSample).toHaveBeenCalledOnce()
    expect(onSample).toHaveBeenCalledWith({
      x: 200,
      y: 100,
      previewHeight: 200.5,
      previewWidth: 400.5,
    })

    const space = createEvent.keyDown(image, { key: ' ' })
    fireEvent(image, space)
    expect(space.defaultPrevented).toBe(true)
    expect(onSample).toHaveBeenCalledOnce()
  })

  it('renders the transformed image and markers together below the checkerboard and toolbar', () => {
    renderCanvas({
      samples: [{
        hex: '#2AB448',
        rgb: [42, 180, 72],
        sourceX: 200,
        sourceY: 100,
      }],
    })

    const image = screen.getByRole('button', { name: '抠像预览' })
    const marker = screen.getByLabelText('采样点 1')
    const content = image.closest('.chroma-preview-content')
    const canvas = image.closest('.chroma-preview-canvas')
    const viewport = image.closest('.chroma-preview-viewport')
    const toolbar = screen.getByRole('toolbar', { name: '预览缩放控制' })

    expect(content).not.toBeNull()
    expect(marker.closest('.chroma-preview-content')).toBe(content)
    expect(content?.parentElement).toBe(canvas)
    expect(canvas).toHaveClass('chroma-preview-canvas-final')
    expect(canvas?.parentElement).toBe(viewport)
    expect(toolbar.parentElement).toBe(viewport)
    expect(toolbar.closest('.chroma-preview-content')).toBeNull()
    expect(image).toHaveAttribute('draggable', 'false')
    expect(content).toHaveStyle({
      height: '400px',
      transform: 'translate3d(0px, 50px, 0) scale(0.5)',
      width: '800px',
    })
  })

  it('samples from the transformed image rectangle after zoom', () => {
    const onSample = vi.fn()
    renderCanvas({ onSample })

    const image = screen.getByRole('button', { name: '抠像预览' })
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 800, left: -200, top: -50, width: 1600 }),
    )

    fireEvent.click(image, { clientX: 600, clientY: 350 })

    expect(onSample).toHaveBeenCalledWith({
      x: 800,
      y: 400,
      previewHeight: 800,
      previewWidth: 1600,
    })
  })

  it('suppresses exactly the immediate click after a completed Space drag', () => {
    const onSample = vi.fn()
    renderCanvas({ onSample })
    const image = screen.getByRole('button', { name: '抠像预览' })
    const viewport = image.closest('.chroma-preview-viewport') as HTMLElement
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 200, width: 400 }),
    )

    fireEvent.keyDown(image, { key: ' ' })
    expect(viewport).toHaveClass('is-pan-ready')
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 200,
      clientY: 100,
      pointerId: 1,
    })
    expect(viewport).toHaveClass('is-panning')
    firePointerEvent(window, 'pointermove', {
      clientX: 196,
      clientY: 100,
      pointerId: 1,
    })
    firePointerEvent(window, 'pointerup', { pointerId: 1 })

    fireEvent.click(image, { clientX: 150, clientY: 100 })
    expect(onSample).not.toHaveBeenCalled()
    fireEvent.click(image, { clientX: 150, clientY: 100 })
    expect(onSample).toHaveBeenCalledOnce()
  })

  it('samples a click after movement below the drag threshold', () => {
    const onSample = vi.fn()
    renderCanvas({ onSample })
    const image = screen.getByRole('button', { name: '抠像预览' })
    const viewport = image.closest('.chroma-preview-viewport') as HTMLElement
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 200, width: 400 }),
    )

    fireEvent.keyDown(viewport, { key: ' ' })
    firePointerEvent(viewport, 'pointerdown', {
      button: 0,
      clientX: 20,
      clientY: 20,
      pointerId: 2,
    })
    firePointerEvent(window, 'pointermove', {
      clientX: 23,
      clientY: 20,
      pointerId: 2,
    })
    firePointerEvent(window, 'pointerup', { pointerId: 2 })
    fireEvent.click(image, { clientX: 100, clientY: 50 })

    expect(onSample).toHaveBeenCalledOnce()
  })

  it('wires toolbar and keyboard zoom, actual-size, and fit commands', async () => {
    const user = userEvent.setup()
    renderCanvas()
    const image = screen.getByRole('button', { name: '抠像预览' })
    const content = image.closest('.chroma-preview-content') as HTMLElement
    const status = screen.getByRole('status')

    expect(status).toHaveTextContent('50%')
    fireEvent.click(screen.getByRole('button', { name: '实际大小' }))
    expect(status).toHaveTextContent('100%')
    expect(content.style.transform).toContain('scale(1)')
    fireEvent.click(screen.getByRole('button', { name: '适应窗口' }))
    expect(status).toHaveTextContent('50%')

    for (const [key, expected] of [
      ['+', '67%'],
      ['-', '50%'],
      ['0', '100%'],
      ['F', '50%'],
    ]) {
      const command = createEvent.keyDown(image, { key })
      fireEvent(image, command)
      expect(command.defaultPrevented).toBe(true)
      expect(status).toHaveTextContent(expected)
    }

    const toolbarZoom = screen.getByRole('button', { name: '放大预览' })
    toolbarZoom.focus()
    await user.keyboard('{Enter}')
    expect(toolbarZoom).toHaveFocus()
    expect(status).toHaveTextContent('67%')
  })

  it('zooms around the wheel pointer and prevents workspace scrolling', () => {
    renderCanvas()
    const image = screen.getByRole('button', { name: '抠像预览' })
    const viewport = image.closest('.chroma-preview-viewport') as HTMLElement
    const content = image.closest('.chroma-preview-content')
    const wheel = createEvent.wheel(viewport, {
      cancelable: true,
      clientX: 110,
      clientY: 120,
      deltaMode: 0,
      deltaY: -500,
    })
    const preventDefault = vi.spyOn(wheel, 'preventDefault')

    fireEvent(viewport, wheel)

    expect(preventDefault).toHaveBeenCalledOnce()
    expect(screen.getByRole('status')).toHaveTextContent('100%')
    expect(content).toHaveStyle({
      transform: 'translate3d(-110px, -20px, 0) scale(1)',
    })
  })

  it('preserves manual zoom across preview URLs and resets fit for a new viewport key', () => {
    const { rerender } = renderCanvas()
    fireEvent.click(screen.getByRole('button', { name: '实际大小' }))
    expect(screen.getByRole('status')).toHaveTextContent('100%')

    rerender(
      <ChromaKeyerCanvas
        onSample={vi.fn()}
        originalHeight={400}
        originalWidth={800}
        previewUrl="/updated.png"
        samples={[]}
        viewMode="alpha"
        viewportKey={1}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('100%')

    rerender(
      <ChromaKeyerCanvas
        onSample={vi.fn()}
        originalHeight={400}
        originalWidth={800}
        previewUrl="/replacement.png"
        samples={[]}
        viewMode="alpha"
        viewportKey={2}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('50%')
    expect(screen.getByRole('button', { name: '抠像预览' })
      .closest('.chroma-preview-content')).toHaveStyle({
        transform: 'translate3d(0px, 50px, 0) scale(0.5)',
      })
  })

  it('keeps marker percentages stable through viewport transforms', () => {
    const sample: ChromaCanvasSample = {
      hex: '#2AB448',
      rgb: [42, 180, 72],
      sourceX: 200,
      sourceY: 100,
    }
    renderCanvas({ samples: [sample] })
    const marker = screen.getByLabelText('采样点 1')

    expect(marker).toHaveStyle({ left: '25%', top: '25%' })
    fireEvent.click(screen.getByRole('button', { name: '实际大小' }))
    expect(marker).toHaveStyle({ left: '25%', top: '25%' })
    expect(marker.closest('.chroma-preview-content')).toHaveStyle({
      transform: 'translate3d(-200px, -50px, 0) scale(1)',
    })
  })

  it('blocks disabled sampling while leaving inspection controls usable', () => {
    const onSample = vi.fn()
    renderCanvas({ disabled: true, onSample })
    const viewport = screen.getByRole('group', { name: '抠像预览视口' })
    const image = screen.getByRole('button', { name: '抠像预览' })
    const getImageRect = vi.spyOn(image, 'getBoundingClientRect').mockReturnValue(
      makeRect({ height: 200, width: 400 }),
    )

    fireEvent.click(image, { clientX: 10, clientY: 10 })
    fireEvent.keyDown(image, { key: 'Enter' })
    viewport.focus()
    const viewportEnter = createEvent.keyDown(viewport, { key: 'Enter' })
    fireEvent(viewport, viewportEnter)

    expect(image).toHaveAttribute('aria-disabled', 'true')
    expect(image).toHaveAttribute('tabindex', '-1')
    expect(image).toHaveClass('chroma-preview-image-disabled')
    expect(viewport).toHaveFocus()
    expect(viewportEnter.defaultPrevented).toBe(true)
    expect(getImageRect).not.toHaveBeenCalled()
    expect(onSample).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '实际大小' }))
    expect(screen.getByRole('status')).toHaveTextContent('100%')
  })

  it.each([
    ['final', true],
    ['alpha', false],
  ] as const)('applies the checkerboard class only for %s previews', (viewMode, hasCheckerboard) => {
    renderCanvas({ viewMode })

    const canvas = screen.getByRole('button', { name: '抠像预览' })
      .closest('.chroma-preview-canvas')
    expect(canvas).toHaveClass('chroma-preview-canvas')
    if (hasCheckerboard) {
      expect(canvas).toHaveClass('chroma-preview-canvas-final')
    } else {
      expect(canvas).not.toHaveClass('chroma-preview-canvas-final')
    }
  })

  it('renders compositional content after the viewport and before exact instructions', () => {
    renderCanvas({
      children: <div data-testid="canvas-slot">Timeline</div>,
    })

    const viewport = screen.getByRole('button', { name: '抠像预览' })
      .closest('.chroma-preview-viewport')
    const slot = screen.getByTestId('canvas-slot')
    const instructions = screen.getByText(
      '点击预览采样；按 Enter 从画面中心采样；按住空格并拖拽可平移画面。',
    )

    expect(viewport?.nextElementSibling).toBe(slot)
    expect(slot.nextElementSibling).toBe(instructions)
  })
})
