import { describe, expect, it } from 'vitest'
import { useLogStore } from './useLogStore'

describe('useLogStore', () => {
  it('formats appended events as log text', () => {
    useLogStore.getState().clearEvents()
    useLogStore.getState().appendEvent({
      task_id: 'task-1',
      timestamp: '2026-08-19T00:00:00Z',
      level: 'info',
      phase: 'running',
      message: 'Processing',
      details: {},
    })

    expect(useLogStore.getState().text()).toBe('[2026-08-19T00:00:00Z] info running: Processing')
  })

  it('includes elapsed time in copied log text', () => {
    useLogStore.getState().clearEvents()
    useLogStore.getState().appendEvent({
      task_id: 'task-1',
      timestamp: '2026-08-19T00:00:00Z',
      level: 'success',
      phase: 'completed',
      message: 'Completed video 1/3',
      details: { elapsed_seconds: 1.234 },
    })

    expect(useLogStore.getState().text()).toBe(
      '[2026-08-19T00:00:00Z] success completed: Completed video 1/3 耗时 1.23s',
    )
  })
})
