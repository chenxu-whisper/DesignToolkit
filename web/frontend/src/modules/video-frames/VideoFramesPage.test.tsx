import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TaskEvent } from '../../services/taskTypes'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { VideoFramesPage } from './VideoFramesPage'

type TaskCreateResponse = {
  task_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
}

const pageMocks = vi.hoisted(() => {
  const createVideoFramesTask = vi.fn<(payload: unknown) => Promise<TaskCreateResponse>>()
  const subscribeTaskEvents = vi.fn(
    (_taskId: string, _onEvent: (event: TaskEvent) => void, _onError: (error: Event | Error) => void): (() => void) =>
      vi.fn(),
  )

  return {
    createVideoFramesTask,
    subscribeTaskEvents,
  }
})

vi.mock('../../services/apiClient', () => ({
  createVideoFramesTask: pageMocks.createVideoFramesTask,
  importDroppedFiles: vi.fn(),
}))

vi.mock('../../services/taskEvents', () => ({
  subscribeTaskEvents: pageMocks.subscribeTaskEvents,
}))

function createDeferred<T>(): {
  promise: Promise<T>
  reject: (reason?: unknown) => void
  resolve: (value: T | PromiseLike<T>) => void
} {
  let reject!: (reason?: unknown) => void
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve
    reject = nextReject
  })

  return { promise, reject, resolve }
}

beforeEach(() => {
  pageMocks.createVideoFramesTask.mockReset()
  pageMocks.subscribeTaskEvents.mockClear()
  useTaskStore.getState().resetTask()
  useLogStore.getState().clearEvents()
})

describe('VideoFramesPage', () => {
  it('submits one input path for single-line video input', async () => {
    const user = userEvent.setup()
    pageMocks.createVideoFramesTask.mockResolvedValue({ task_id: 'task-single', status: 'queued' })

    render(<VideoFramesPage />)

    await user.type(screen.getByLabelText('输入视频路径'), 'C:\\clips\\clip-a.mp4')
    await user.click(screen.getByRole('button', { name: '开始导出' }))

    expect(pageMocks.createVideoFramesTask).toHaveBeenCalledWith(
      expect.objectContaining({
        input_path: 'C:\\clips\\clip-a.mp4',
      }),
    )
    expect(pageMocks.createVideoFramesTask).not.toHaveBeenCalledWith(
      expect.objectContaining({
        input_paths: expect.any(Array),
      }),
    )
  })

  it('submits input_paths for newline-separated video input', async () => {
    const user = userEvent.setup()
    pageMocks.createVideoFramesTask.mockResolvedValue({ task_id: 'task-batch', status: 'queued' })

    render(<VideoFramesPage />)

    await user.type(screen.getByLabelText('输入视频路径'), 'C:\\clips\\clip-a.mp4\n\nD:\\clips\\clip-b.webm')
    await user.click(screen.getByRole('button', { name: '开始导出' }))

    expect(pageMocks.createVideoFramesTask).toHaveBeenCalledWith(
      expect.objectContaining({
        input_paths: ['C:\\clips\\clip-a.mp4', 'D:\\clips\\clip-b.webm'],
      }),
    )
    expect(pageMocks.createVideoFramesTask).not.toHaveBeenCalledWith(
      expect.objectContaining({
        input_path: expect.any(String),
      }),
    )
  })

  it('submits only once while the create request is pending', async () => {
    const user = userEvent.setup()
    const createRequest = createDeferred<TaskCreateResponse>()
    pageMocks.createVideoFramesTask.mockReturnValue(createRequest.promise)

    render(<VideoFramesPage />)

    await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
    const submitButton = screen.getByRole('button', { name: '开始导出' })
    await user.dblClick(submitButton)

    expect(pageMocks.createVideoFramesTask).toHaveBeenCalledTimes(1)
    expect(submitButton).toBeDisabled()
  })

  it('does not call startTask from an unmounted page when create resolves later', async () => {
    const user = userEvent.setup()
    const createRequest = createDeferred<TaskCreateResponse>()
    pageMocks.createVideoFramesTask.mockReturnValue(createRequest.promise)
    const startTaskSpy = vi.spyOn(useTaskStore.getState(), 'startTask')
    const { unmount } = render(<VideoFramesPage />)

    await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
    await user.click(screen.getByRole('button', { name: '开始导出' }))
    expect(pageMocks.createVideoFramesTask).toHaveBeenCalledTimes(1)

    unmount()
    await act(async () => {
      createRequest.resolve({ task_id: 'task-late', status: 'queued' })
      await createRequest.promise
    })

    expect(startTaskSpy).not.toHaveBeenCalled()
    expect(pageMocks.subscribeTaskEvents).not.toHaveBeenCalled()
    expect(useTaskStore.getState().currentTaskId).toBeNull()
    startTaskSpy.mockRestore()
  })

  it('does not let stale create errors overwrite an already running task', async () => {
    const user = userEvent.setup()
    const createRequest = createDeferred<TaskCreateResponse>()
    pageMocks.createVideoFramesTask.mockReturnValue(createRequest.promise)

    render(<VideoFramesPage />)

    await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
    await user.click(screen.getByRole('button', { name: '开始导出' }))
    act(() => {
      useTaskStore.getState().startTask({ taskId: 'task-running', kind: 'media_resize' })
    })

    await act(async () => {
      createRequest.reject(new Error('Duplicate task'))
      await createRequest.promise.catch(() => undefined)
    })

    expect(useTaskStore.getState().currentTaskId).toBe('task-running')
    expect(useTaskStore.getState().status).toBe('running')
    expect(useTaskStore.getState().error).toBeNull()
  })
})
