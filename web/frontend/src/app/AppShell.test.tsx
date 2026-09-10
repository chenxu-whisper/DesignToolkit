import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TaskEvent } from '../services/taskTypes'
import { useLogStore } from '../state/useLogStore'
import { useTaskStore } from '../state/useTaskStore'
import { AppShell } from './AppShell'
import { ThemeProvider } from './theme/ThemeProvider'

type TaskCreateResponse = {
  task_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
}

type TaskResponse = {
  task_id: string
  kind: 'video_frames' | 'media_resize'
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  created_at: string
  started_at: string | null
  finished_at: string | null
  input_path: string
  output_path: string | null
  error: string | null
}

type Subscription = {
  taskId: string
  onEvent: (event: TaskEvent) => void
  onError: (error: Event | Error) => void
  closed: boolean
}

const serviceMocks = vi.hoisted(() => {
  const subscriptions: Subscription[] = []
  const createVideoFramesTask = vi.fn<(payload: unknown) => Promise<TaskCreateResponse>>()
  const createMediaResizeTask = vi.fn<(payload: unknown) => Promise<TaskCreateResponse>>()
  const getTask = vi.fn<(taskId: string) => Promise<TaskResponse>>()
  const subscribeTaskEvents = vi.fn(
    (taskId: string, onEvent: (event: TaskEvent) => void, onError: (error: Event | Error) => void): (() => void) => {
      let subscription: Subscription
      const unsubscribe = () => {
        subscription.closed = true
      }
      subscription = { taskId, onEvent, onError, closed: false }
      subscriptions.push(subscription)
      return unsubscribe
    },
  )

  return {
    createMediaResizeTask,
    createVideoFramesTask,
    getTask,
    subscribeTaskEvents,
    subscriptions,
  }
})

const originalInnerHeight = window.innerHeight

vi.mock('../services/apiClient', () => ({
  createMediaResizeTask: serviceMocks.createMediaResizeTask,
  createVideoFramesTask: serviceMocks.createVideoFramesTask,
  getTask: serviceMocks.getTask,
}))

vi.mock('../services/taskEvents', () => ({
  subscribeTaskEvents: serviceMocks.subscribeTaskEvents,
}))

function makeTaskEvent(overrides: Partial<TaskEvent> = {}): TaskEvent {
  return {
    task_id: 'task-1',
    timestamp: '2026-08-19T12:00:00Z',
    level: 'success',
    phase: 'completed',
    message: 'Task completed',
    details: { output_path: 'frames' },
    ...overrides,
  }
}

function emitIfOpen(subscription: Subscription, event: TaskEvent): void {
  if (!subscription.closed) {
    subscription.onEvent(event)
  }
}

beforeEach(() => {
  localStorage.clear()
  serviceMocks.createMediaResizeTask.mockReset()
  serviceMocks.createVideoFramesTask.mockReset()
  serviceMocks.getTask.mockReset()
  serviceMocks.subscribeTaskEvents.mockClear()
  serviceMocks.subscriptions.length = 0
  useTaskStore.getState().resetTask()
  useLogStore.getState().clearEvents()
})

afterEach(() => {
  Object.defineProperty(window, 'innerHeight', {
    configurable: true,
    value: originalInnerHeight,
  })
})

function renderShell() {
  return render(
    <ThemeProvider>
      <AppShell />
    </ThemeProvider>,
  )
}

describe('AppShell', () => {
  it('opens directly into the tool console shell', () => {
    renderShell()

    expect(screen.getByText('DesignToolkit')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '视频转序列' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '媒体尺寸修改' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '任务日志' })).toBeInTheDocument()
  })

  it('opens 色键抠图 from the navigation', async () => {
    const user = userEvent.setup()

    renderShell()

    await user.click(screen.getByRole('button', { name: '色键抠图' }))

    expect(screen.getByRole('heading', { name: '色键抠图' })).toBeInTheDocument()
  })

  it('marks the selected tool as current in the navigation', async () => {
    const user = userEvent.setup()

    renderShell()

    const videoFramesButton = screen.getByRole('button', { name: '视频转序列' })
    const resizeMediaButton = screen.getByRole('button', { name: '媒体尺寸修改' })

    expect(videoFramesButton).toHaveAttribute('aria-current', 'page')
    expect(resizeMediaButton).not.toHaveAttribute('aria-current')

    await user.click(resizeMediaButton)

    expect(resizeMediaButton).toHaveAttribute('aria-current', 'page')
    expect(videoFramesButton).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('navigation', { name: '工具模块' })).toContainElement(resizeMediaButton)
  })

  it('shows the active module in the command bar', async () => {
    const user = userEvent.setup()

    renderShell()

    expect(screen.getByRole('banner')).toHaveTextContent('视频转序列')
    await user.click(screen.getByRole('button', { name: '媒体尺寸修改' }))
    expect(screen.getByRole('banner')).toHaveTextContent('媒体尺寸修改')
  })

  it('changes theme from the command bar', async () => {
    const user = userEvent.setup()

    renderShell()

    await user.click(screen.getByRole('button', { name: '主题设置' }))
    await user.click(screen.getByRole('menuitemradio', { name: '深色' }))

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  })

  it('supports keyboard navigation and dismissal in the theme menu', async () => {
    const user = userEvent.setup()

    renderShell()

    const trigger = screen.getByRole('button', { name: '主题设置' })
    await user.click(trigger)
    expect(screen.getByRole('menuitemradio', { name: '跟随系统' })).toHaveFocus()

    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitemradio', { name: '浅色' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu', { name: '主题设置' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('clamps a persisted log height and reclamps after viewport resize', async () => {
    localStorage.setItem('designtoolkit-log-collapsed', 'false')
    localStorage.setItem('designtoolkit-log-height', '5000')
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 600 })

    renderShell()

    const shell = document.querySelector<HTMLElement>('.app-shell')
    expect(shell?.style.getPropertyValue('--log-panel-height')).toBe('270px')

    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 400 })
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })

    await waitFor(() => {
      expect(shell?.style.getPropertyValue('--log-panel-height')).toBe('180px')
    })
  })

  it('renders an icon and tooltip for every tool', () => {
    renderShell()

    for (const name of ['视频转序列', '媒体尺寸修改', '色键抠图']) {
      const button = screen.getByRole('button', { name })
      expect(button).toHaveAttribute('title')
      expect(button.querySelector('svg')).toBeInTheDocument()
    }
  })

  it('continues handling terminal task events after switching modules', async () => {
    const user = userEvent.setup()
    serviceMocks.createVideoFramesTask.mockResolvedValue({ task_id: 'task-1', status: 'queued' })

    renderShell()

    await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
    await user.click(screen.getByRole('button', { name: '开始导出' }))

    await waitFor(() => expect(serviceMocks.subscriptions).toHaveLength(1))

    await user.click(screen.getByRole('button', { name: '媒体尺寸修改' }))

    const terminalEvent = makeTaskEvent({ message: 'Export completed', details: { output_path: 'frames' } })
    act(() => {
      emitIfOpen(serviceMocks.subscriptions[0], terminalEvent)
    })

    expect(useTaskStore.getState().status).toBe('succeeded')
    expect(useTaskStore.getState().resultPath).toBe('frames')
    expect(useLogStore.getState().events).toEqual(expect.arrayContaining([terminalEvent]))
  })

  it('disables all module forms while a create request is pending to prevent cross-module submit race', async () => {
    const user = userEvent.setup()
    const videoFramesRequest = createDeferred<TaskCreateResponse>()
    serviceMocks.createVideoFramesTask.mockReturnValue(videoFramesRequest.promise)

    renderShell()

    await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
    const videoSubmitButton = screen.getByRole('button', { name: '开始导出' })
    await user.click(videoSubmitButton)

    expect(serviceMocks.createVideoFramesTask).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: '媒体尺寸修改' }))

    const resizeSubmitButton = screen.getByRole('button', { name: '开始调整' })
    expect(resizeSubmitButton).toBeDisabled()

    await user.click(resizeSubmitButton)
    expect(serviceMocks.createMediaResizeTask).not.toHaveBeenCalled()

    await act(async () => {
      videoFramesRequest.resolve({ task_id: 'task-vf-1', status: 'queued' })
      await videoFramesRequest.promise
    })

    expect(serviceMocks.createMediaResizeTask).not.toHaveBeenCalled()
  })

  it('keeps a running task active and reconnects when only the log stream disconnects', async () => {
    const user = userEvent.setup()
    serviceMocks.createVideoFramesTask.mockResolvedValue({ task_id: 'task-1', status: 'queued' })
    serviceMocks.getTask.mockResolvedValue(makeTaskResponse({ status: 'running' }))

    renderShell()

    await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
    await user.click(screen.getByRole('button', { name: '开始导出' }))
    await waitFor(() => expect(serviceMocks.subscriptions).toHaveLength(1))

    act(() => {
      serviceMocks.subscriptions[0].onError(new Event('error'))
    })

    await waitFor(() => expect(serviceMocks.getTask).toHaveBeenCalledWith('task-1'))
    await waitFor(() => expect(serviceMocks.subscriptions).toHaveLength(2))
    expect(useTaskStore.getState().status).toBe('running')
    expect(useTaskStore.getState().error).toBeNull()
    expect(useLogStore.getState().events.at(-1)?.message).toContain('日志连接中断')
  })

  it('uses the task API status when the log stream disconnects after completion', async () => {
    const user = userEvent.setup()
    serviceMocks.createVideoFramesTask.mockResolvedValue({ task_id: 'task-1', status: 'queued' })
    serviceMocks.getTask.mockResolvedValue(
      makeTaskResponse({
        status: 'succeeded',
        output_path: 'E:\\frames',
      }),
    )

    renderShell()

    await user.type(screen.getByLabelText('输入视频路径'), 'clip.webm')
    await user.click(screen.getByRole('button', { name: '开始导出' }))
    await waitFor(() => expect(serviceMocks.subscriptions).toHaveLength(1))

    act(() => {
      serviceMocks.subscriptions[0].onError(new Event('error'))
    })

    await waitFor(() => expect(useTaskStore.getState().status).toBe('succeeded'))
    expect(useTaskStore.getState().resultPath).toBe('E:\\frames')
    expect(useLogStore.getState().events.at(-1)?.message).toBe('任务已完成')
  })
})

function makeTaskResponse(overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    task_id: 'task-1',
    kind: 'video_frames',
    status: 'running',
    created_at: '2026-08-20T00:00:00Z',
    started_at: '2026-08-20T00:00:01Z',
    finished_at: null,
    input_path: 'clip.webm',
    output_path: null,
    error: null,
    ...overrides,
  }
}

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
