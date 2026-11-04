import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { MediaResizePage } from './MediaResizePage'

type TaskCreateResponse = {
  task_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
}

const pageMocks = vi.hoisted(() => ({
  createMediaResizeTask: vi.fn<(payload: unknown) => Promise<TaskCreateResponse>>(),
  importDroppedFiles: vi.fn(),
  pickLocalFiles: vi.fn(),
  pickLocalSaveFile: vi.fn(),
  resolveDroppedFilePaths: vi.fn(),
}))

vi.mock('../../services/apiClient', () => ({
  createMediaResizeTask: pageMocks.createMediaResizeTask,
  importDroppedFiles: pageMocks.importDroppedFiles,
  pickLocalFiles: pageMocks.pickLocalFiles,
  pickLocalSaveFile: pageMocks.pickLocalSaveFile,
  resolveDroppedFilePaths: pageMocks.resolveDroppedFilePaths,
}))

beforeEach(() => {
  pageMocks.createMediaResizeTask.mockReset()
  pageMocks.importDroppedFiles.mockReset()
  pageMocks.pickLocalFiles.mockReset()
  pageMocks.pickLocalSaveFile.mockReset()
  pageMocks.resolveDroppedFilePaths.mockReset()
  useTaskStore.getState().resetTask()
  useLogStore.getState().clearEvents()
})

describe('MediaResizePage', () => {
  it('submits one input path with an optional output path', async () => {
    const user = userEvent.setup()
    pageMocks.createMediaResizeTask.mockResolvedValue({ task_id: 'task-single', status: 'queued' })

    render(<MediaResizePage />)

    await user.type(screen.getByLabelText('输入媒体路径'), 'C:\\media\\photo-a.png')
    await user.type(screen.getByLabelText('输出路径'), 'C:\\media\\photo-a-small.png')
    await user.type(screen.getByLabelText('目标尺寸'), '250x250')
    await user.click(screen.getByRole('button', { name: '开始调整' }))

    expect(pageMocks.createMediaResizeTask).toHaveBeenCalledWith({
      input_path: 'C:\\media\\photo-a.png',
      output_path: 'C:\\media\\photo-a-small.png',
      size: '250x250',
    })
  })

  it('submits input_paths and omits output_path for newline-separated media input', async () => {
    const user = userEvent.setup()
    pageMocks.createMediaResizeTask.mockResolvedValue({ task_id: 'task-batch', status: 'queued' })

    render(<MediaResizePage />)

    await user.type(screen.getByLabelText('输入媒体路径'), 'C:\\media\\photo-a.png\n\nD:\\media\\clip-b.mp4')
    await user.type(screen.getByLabelText('目标尺寸'), '250x250')
    await user.click(screen.getByRole('button', { name: '开始调整' }))

    expect(pageMocks.createMediaResizeTask).toHaveBeenCalledWith({
      input_paths: ['C:\\media\\photo-a.png', 'D:\\media\\clip-b.mp4'],
      size: '250x250',
    })
    expect(pageMocks.createMediaResizeTask).not.toHaveBeenCalledWith(
      expect.objectContaining({
        input_path: expect.any(String),
        output_path: expect.any(String),
      }),
    )
  })
})
