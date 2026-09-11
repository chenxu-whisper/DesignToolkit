import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ImageViewportToolbar } from './ImageViewportToolbar'

function createProps(overrides: Partial<React.ComponentProps<typeof ImageViewportToolbar>> = {}) {
  return {
    canZoomIn: true,
    canZoomOut: true,
    onActualSize: vi.fn(),
    onFit: vi.fn(),
    onZoomIn: vi.fn(),
    onZoomOut: vi.fn(),
    scale: 1.25,
    ...overrides,
  }
}

describe('ImageViewportToolbar', () => {
  it('renders accessible controls and dispatches each toolbar action exactly once', async () => {
    const user = userEvent.setup()
    const props = createProps()
    render(<ImageViewportToolbar {...props} />)

    const toolbar = screen.getByRole('toolbar', { name: '预览缩放控制' })
    expect(toolbar).toHaveClass('image-viewport-toolbar')
    expect(Array.from(toolbar.children)).toEqual([
      within(toolbar).getByRole('button', { name: '缩小预览' }),
      within(toolbar).getByRole('status'),
      within(toolbar).getByRole('button', { name: '放大预览' }),
      within(toolbar).getByRole('button', { name: '适应窗口' }),
      within(toolbar).getByRole('button', { name: '实际大小' }),
    ])
    expect(screen.getByRole('status').textContent).toBe('125%')

    for (const [label, callbackName] of [
      ['缩小预览', 'onZoomOut'],
      ['放大预览', 'onZoomIn'],
      ['适应窗口', 'onFit'],
      ['实际大小', 'onActualSize'],
    ] as const) {
      const button = screen.getByRole('button', { name: label })
      expect(button).toHaveAttribute('title', label)
      expect(button).toHaveAttribute('type', 'button')
      expect(button).toHaveClass('icon-button')

      await user.click(button)

      expect(props[callbackName]).toHaveBeenCalledOnce()
      for (const [name, callback] of Object.entries(props)) {
        if (name.startsWith('on') && name !== callbackName && typeof callback === 'function') {
          expect(callback).not.toHaveBeenCalled()
        }
      }
      vi.clearAllMocks()
    }
  })

  it('natively disables only unavailable zoom limit actions', async () => {
    const user = userEvent.setup()
    const props = createProps({ canZoomIn: false, canZoomOut: false })
    render(<ImageViewportToolbar {...props} />)

    const zoomOut = screen.getByRole('button', { name: '缩小预览' })
    const zoomIn = screen.getByRole('button', { name: '放大预览' })
    const fit = screen.getByRole('button', { name: '适应窗口' })
    const actualSize = screen.getByRole('button', { name: '实际大小' })

    expect(zoomOut).toBeDisabled()
    expect(zoomIn).toBeDisabled()
    expect(fit).toBeEnabled()
    expect(actualSize).toBeEnabled()

    await user.click(zoomOut)
    await user.click(zoomIn)

    expect(props.onZoomOut).not.toHaveBeenCalled()
    expect(props.onZoomIn).not.toHaveBeenCalled()
  })

  it.each([
    [0.333, '33%'],
    [8, '800%'],
  ])('rounds scale %s to a stable percentage status', (scale, expected) => {
    render(<ImageViewportToolbar {...createProps({ scale })} />)

    const status = screen.getByRole('status')
    expect(status.tagName).toBe('OUTPUT')
    expect(status).toHaveAttribute('aria-live', 'polite')
    expect(status).toHaveClass('image-viewport-zoom-status')
    expect(status.textContent).toBe(expected)
  })

  it('uses the requested decorative Lucide icons', () => {
    render(<ImageViewportToolbar {...createProps()} />)

    for (const [label, iconClass] of [
      ['缩小预览', 'lucide-minus'],
      ['放大预览', 'lucide-plus'],
      ['适应窗口', 'lucide-scan'],
      ['实际大小', 'lucide-maximize'],
    ]) {
      const icon = screen.getByRole('button', { name: label }).querySelector('svg')
      expect(icon).toHaveClass(iconClass)
      expect(icon).toHaveAttribute('aria-hidden', 'true')
    }
  })
})
