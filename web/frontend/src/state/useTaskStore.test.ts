import { beforeEach, describe, expect, it } from 'vitest'
import { useTaskStore } from './useTaskStore'

describe('useTaskStore', () => {
  beforeEach(() => {
    useTaskStore.getState().resetTask()
  })

  it('tracks running and completed tasks', () => {
    useTaskStore.getState().startTask({ taskId: 'task-1', kind: 'video_frames' })
    expect(useTaskStore.getState().status).toBe('running')

    useTaskStore.getState().completeTask({ taskId: 'task-1', resultPath: 'frames' })
    expect(useTaskStore.getState().status).toBe('succeeded')
    expect(useTaskStore.getState().resultPath).toBe('frames')
  })

  it('clears stale result path when a task fails after a previous success', () => {
    useTaskStore.getState().startTask({ taskId: 'task-1', kind: 'video_frames' })
    useTaskStore.getState().completeTask({ taskId: 'task-1', resultPath: 'frames' })

    useTaskStore.getState().failTask({ taskId: 'task-1', error: 'failed' })

    expect(useTaskStore.getState().status).toBe('failed')
    expect(useTaskStore.getState().error).toBe('failed')
    expect(useTaskStore.getState().resultPath).toBeNull()
  })

  it('ignores terminal updates for stale task ids', () => {
    useTaskStore.getState().startTask({ taskId: 'task-1', kind: 'video_frames' })

    useTaskStore.getState().completeTask({ taskId: 'task-2', resultPath: 'wrong-output' })
    useTaskStore.getState().failTask({ taskId: 'task-2', error: 'wrong-error' })

    expect(useTaskStore.getState().currentTaskId).toBe('task-1')
    expect(useTaskStore.getState().status).toBe('running')
    expect(useTaskStore.getState().resultPath).toBeNull()
    expect(useTaskStore.getState().error).toBeNull()
  })
})
