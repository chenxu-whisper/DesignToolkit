import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TaskEvent } from '../services/taskTypes'
import { useLogStore } from '../state/useLogStore'
import { LogPanel } from './LogPanel'

const originalClipboardDescriptor =
  Object.getOwnPropertyDescriptor(navigator, 'clipboard') ??
  Object.getOwnPropertyDescriptor(Navigator.prototype, 'clipboard')

afterEach(() => {
  vi.restoreAllMocks()
  if (originalClipboardDescriptor) {
    Object.defineProperty(navigator, 'clipboard', originalClipboardDescriptor)
  } else {
    Reflect.deleteProperty(navigator, 'clipboard')
  }
})

function seedEvent(overrides: Partial<TaskEvent> = {}) {
  useLogStore.getState().clearEvents()
  useLogStore.getState().appendEvent({
    task_id: 'task-1',
    timestamp: '2026-09-10T12:00:00Z',
    level: 'info',
    phase: 'running',
    message: 'Task running',
    details: {},
    ...overrides,
  })
}

function LogHarness({
  initialCollapsed = false,
  initialHeight = 160,
}: {
  initialCollapsed?: boolean
  initialHeight?: number
}) {
  const [collapsed, setCollapsed] = useState(initialCollapsed)
  const [height, setHeight] = useState(initialHeight)

  return (
    <LogPanel
      collapsed={collapsed}
      height={height}
      onCollapsedChange={setCollapsed}
      onHeightChange={setHeight}
    />
  )
}

describe('LogPanel', () => {
  it('renders log messages', () => {
    seedEvent({
      level: 'success',
      message: 'Task completed',
      phase: 'completed',
      timestamp: '2026-08-18T10:00:00Z',
    })

    render(<LogHarness />)

    expect(screen.getByText('Task completed')).toBeInTheDocument()
  })

  it('renders elapsed time details', () => {
    seedEvent({
      message: 'Completed video 1/3',
      details: { elapsed_seconds: 1.234 },
      level: 'success',
      phase: 'completed',
      timestamp: '2026-08-18T10:00:00Z',
    })

    render(<LogHarness />)

    expect(screen.getByText('耗时 1.23s')).toBeInTheDocument()
  })

  it('attaches a rejection handler to clipboard writes', async () => {
    const user = userEvent.setup()
    const catchHandler = vi.fn<(onRejected?: (reason: unknown) => unknown) => Promise<void>>(() => Promise.resolve())
    const writeText = vi.fn<() => Promise<void>>(() => ({ catch: catchHandler }) as unknown as Promise<void>)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })

    render(<LogHarness />)

    await user.click(screen.getByRole('button', { name: '复制日志' }))

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(catchHandler).toHaveBeenCalledTimes(1)
  })

  it('collapses to a latest-event summary', async () => {
    seedEvent({ message: 'Frame 12 exported' })
    const user = userEvent.setup()

    render(<LogHarness />)

    await user.click(screen.getByRole('button', { name: '折叠任务日志' }))

    expect(screen.getByRole('region', { name: '任务日志' })).toHaveAttribute(
      'data-collapsed',
      'true',
    )
    expect(screen.getByText('Frame 12 exported')).toBeInTheDocument()
    expect(screen.queryByRole('log')).not.toBeInTheDocument()
  })

  it('resizes with arrow keys', async () => {
    const user = userEvent.setup()
    render(<LogHarness initialHeight={160} />)
    const separator = screen.getByRole('separator', { name: '调整任务日志高度' })

    separator.focus()
    await user.keyboard('{ArrowUp}{ArrowUp}')

    expect(separator).toHaveAttribute('aria-valuenow', '192')
  })

  it('clamps keyboard resizing to the available bounds', async () => {
    const user = userEvent.setup()
    render(<LogHarness initialHeight={160} />)
    const separator = screen.getByRole('separator', { name: '调整任务日志高度' })

    separator.focus()
    await user.keyboard('{Home}')
    expect(separator).toHaveAttribute('aria-valuenow', '120')

    await user.keyboard('{End}')
    expect(separator).toHaveAttribute(
      'aria-valuenow',
      String(Math.floor(window.innerHeight * 0.45)),
    )
  })

  it('removes active pointer listeners when unmounted during a drag', () => {
    const addListener = vi.spyOn(window, 'addEventListener')
    const removeListener = vi.spyOn(window, 'removeEventListener')
    const { unmount } = render(<LogHarness />)
    const separator = screen.getByRole('separator', { name: '调整任务日志高度' })

    fireEvent.pointerDown(separator, { clientY: 500 })
    const pointerMove = addListener.mock.calls.find(([type]) => type === 'pointermove')?.[1]
    const pointerUp = addListener.mock.calls.find(([type]) => type === 'pointerup')?.[1]
    const pointerCancel = addListener.mock.calls.find(([type]) => type === 'pointercancel')?.[1]
    unmount()

    expect(removeListener).toHaveBeenCalledWith('pointermove', pointerMove)
    expect(removeListener).toHaveBeenCalledWith('pointerup', pointerUp)
    expect(removeListener).toHaveBeenCalledWith('pointercancel', pointerCancel)
  })
})
