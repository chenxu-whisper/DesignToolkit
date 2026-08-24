import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
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

describe('LogPanel', () => {
  it('renders log messages', () => {
    useLogStore.getState().clearEvents()
    useLogStore.getState().appendEvent({
      task_id: 'task-1',
      timestamp: '2026-08-18T10:00:00Z',
      level: 'success',
      phase: 'completed',
      message: 'Task completed',
      details: {},
    })

    render(<LogPanel />)

    expect(screen.getByText('Task completed')).toBeInTheDocument()
  })

  it('renders elapsed time details', () => {
    useLogStore.getState().clearEvents()
    useLogStore.getState().appendEvent({
      task_id: 'task-1',
      timestamp: '2026-08-18T10:00:00Z',
      level: 'success',
      phase: 'completed',
      message: 'Completed video 1/3',
      details: { elapsed_seconds: 1.234 },
    })

    render(<LogPanel />)

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

    render(<LogPanel />)

    await user.click(screen.getByRole('button', { name: '复制日志' }))

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(catchHandler).toHaveBeenCalledTimes(1)
  })
})
