import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { ChromaKeyerPage } from './ChromaKeyerPage'

const pageMocks = vi.hoisted(() => ({
  createChromaKeyerTask: vi.fn(),
  createChromaPreview: vi.fn(),
  inspectChromaImage: vi.fn(),
  pickLocalFiles: vi.fn(),
  resolveDroppedFilePaths: vi.fn(),
  sampleChromaImage: vi.fn(),
}))

vi.mock('../../services/apiClient', () => ({
  createChromaKeyerTask: pageMocks.createChromaKeyerTask,
  createChromaPreview: pageMocks.createChromaPreview,
  inspectChromaImage: pageMocks.inspectChromaImage,
  pickLocalFiles: pageMocks.pickLocalFiles,
  resolveDroppedFilePaths: pageMocks.resolveDroppedFilePaths,
  sampleChromaImage: pageMocks.sampleChromaImage,
}))

const inspectResponse = {
  height: 400,
  input_path: 'E:\\media\\screen.png',
  preview_height: 200,
  preview_url: '/api/previews/input.png',
  preview_width: 400,
  width: 800,
}

async function inspectImage() {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('输入图片路径'), 'E:\\media\\screen.png')
  await user.keyboard('{Enter}')
  return user
}

async function sampleImage() {
  const image = await screen.findByRole('button', { name: '抠像预览' })
  vi.spyOn(image, 'getBoundingClientRect').mockReturnValue({
    bottom: 220,
    height: 200,
    left: 10,
    right: 410,
    toJSON: () => ({}),
    top: 20,
    width: 400,
    x: 10,
    y: 20,
  })
  await act(async () => {
    fireEvent.click(image, { clientX: 210, clientY: 120 })
  })
}

beforeEach(() => {
  pageMocks.createChromaKeyerTask.mockReset()
  pageMocks.createChromaPreview.mockReset()
  pageMocks.inspectChromaImage.mockReset()
  pageMocks.pickLocalFiles.mockReset()
  pageMocks.resolveDroppedFilePaths.mockReset()
  pageMocks.sampleChromaImage.mockReset()
  pageMocks.pickLocalFiles.mockResolvedValue({ cancelled: false, paths: ['E:\\media\\screen.png'] })
  pageMocks.inspectChromaImage.mockResolvedValue(inspectResponse)
  pageMocks.sampleChromaImage.mockResolvedValue({
    hex: '#2AB448',
    rgb: [42, 180, 72],
    source_x: 400,
    source_y: 200,
  })
  pageMocks.createChromaPreview.mockResolvedValue({ preview_url: '/api/previews/final.png' })
  pageMocks.createChromaKeyerTask.mockResolvedValue({ status: 'queued', task_id: 'chroma-task-1' })
  useTaskStore.getState().resetTask()
  useLogStore.getState().clearEvents()
})

describe('ChromaKeyerPage', () => {
  it('automatically inspects the exact path selected by the native picker', async () => {
    const user = renderWithUser()

    expect(screen.queryByRole('button', { name: '加载图片' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '指定输入图片路径' }))

    await waitFor(() =>
      expect(pageMocks.inspectChromaImage).toHaveBeenCalledWith({
        inputPath: 'E:\\media\\screen.png',
      }),
    )
    expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce()
    expect(screen.getByLabelText('输入图片路径')).toHaveValue('E:\\media\\screen.png')
    expect(await screen.findByRole('button', { name: '抠像预览' })).toBeInTheDocument()
  })

  it('does not inspect while typing and commits once on Enter followed by blur', async () => {
    const user = renderWithUser()
    const input = screen.getByLabelText('输入图片路径')
    const inspectRequest = createDeferred<typeof inspectResponse>()
    pageMocks.inspectChromaImage.mockReturnValueOnce(inspectRequest.promise)

    await user.type(input, 'E:\\media\\screen.png')

    expect(pageMocks.inspectChromaImage).not.toHaveBeenCalled()

    await user.keyboard('{Enter}')
    fireEvent.blur(input)
    fireEvent.blur(input)

    await waitFor(() => expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce())
    expect(pageMocks.inspectChromaImage).toHaveBeenCalledWith({ inputPath: 'E:\\media\\screen.png' })
    await act(async () => {
      inspectRequest.resolve(inspectResponse)
    })
  })

  it('commits a manually typed path on blur', async () => {
    const user = renderWithUser()
    const input = screen.getByLabelText('输入图片路径')

    await user.type(input, 'E:\\media\\screen.png')

    expect(pageMocks.inspectChromaImage).not.toHaveBeenCalled()
    fireEvent.blur(input)

    await waitFor(() => expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce())
  })

  it('inspects once when keyboard focus tabs from the path to its picker', async () => {
    const user = renderWithUser()
    const input = screen.getByLabelText('输入图片路径')
    const picker = screen.getByRole('button', { name: '指定输入图片路径' })

    await user.type(input, 'E:\\media\\screen.png')
    await user.tab()

    expect(picker).toHaveFocus()
    await waitFor(() => expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce())
    expect(pageMocks.inspectChromaImage).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.png',
    })
  })

  it('shows an input error for a blank committed path', () => {
    render(<ChromaKeyerPage />)

    fireEvent.keyDown(screen.getByLabelText('输入图片路径'), { key: 'Enter' })

    expect(screen.getByRole('alert')).toHaveTextContent('请输入图片路径')
    expect(pageMocks.inspectChromaImage).not.toHaveBeenCalled()
  })

  it('inspects an explicit local image then stores returned Screen Color metadata', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()

    expect(pageMocks.inspectChromaImage).toHaveBeenCalledWith({ inputPath: 'E:\\media\\screen.png' })
    await sampleImage()

    await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
    expect(pageMocks.sampleChromaImage).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.png',
      previewHeight: 200,
      previewWidth: 400,
      x: 200,
      y: 100,
    })
    expect(screen.getByText('RGB(42, 180, 72)')).toBeInTheDocument()
    expect(screen.getByText('400, 200')).toBeInTheDocument()
    expect(screen.getByText('Screen Color')).toBeInTheDocument()
    expect(screen.queryByText('#1')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '删除 Screen Color' })).toHaveLength(1)
  })

  it('replaces the previous Screen Color after a second successful sample', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
    pageMocks.sampleChromaImage.mockResolvedValueOnce({
      hex: '#1E32C8',
      rgb: [30, 50, 200],
      source_x: 410,
      source_y: 210,
    })

    await sampleImage()

    await waitFor(() => expect(screen.getByText('#1E32C8')).toBeInTheDocument())
    expect(screen.queryByText('#2AB448')).not.toBeInTheDocument()
    expect(screen.getByText('RGB(30, 50, 200)')).toBeInTheDocument()
    expect(screen.getByText('410, 210')).toBeInTheDocument()
    expect(screen.queryByText('#1')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '删除 Screen Color' })).toHaveLength(1)
    await waitFor(() =>
      expect(pageMocks.createChromaPreview).toHaveBeenLastCalledWith(
        expect.objectContaining({ samples: [[30, 50, 200]] }),
      ),
    )
  })

  it('preserves the previous Screen Color and payloads when a replacement sample is rejected', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() =>
      expect(pageMocks.createChromaPreview).toHaveBeenCalledWith(
        expect.objectContaining({ samples: [[42, 180, 72]] }),
      ),
    )
    pageMocks.sampleChromaImage.mockRejectedValueOnce(new Error('请选择具有明显主色的纯色幕背景'))

    await sampleImage()

    expect(await screen.findByRole('alert')).toHaveTextContent('请选择具有明显主色的纯色幕背景')
    expect(screen.getByText('#2AB448')).toBeInTheDocument()
    expect(screen.getByText('RGB(42, 180, 72)')).toBeInTheDocument()
    expect(screen.getByText('400, 200')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '删除 Screen Color' })).toHaveLength(1)
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeEnabled()

    fireEvent.change(screen.getByLabelText('Screen Balance'), { target: { value: '0.35' } })
    await waitFor(() =>
      expect(pageMocks.createChromaPreview).toHaveBeenLastCalledWith(
        expect.objectContaining({
          params: expect.objectContaining({ screenBalance: 0.35 }),
          samples: [[42, 180, 72]],
        }),
      ),
    )

    await user.click(screen.getByRole('button', { name: '导出 PNG' }))
    expect(pageMocks.createChromaKeyerTask).toHaveBeenCalledWith(
      expect.objectContaining({
        params: expect.objectContaining({ screenBalance: 0.35 }),
        samples: [[42, 180, 72]],
      }),
    )
  })

  it('clears the inspected session when the input path changes', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
    await user.clear(screen.getByLabelText('输入图片路径'))
    await user.type(screen.getByLabelText('输入图片路径'), 'E:\\media\\replacement.png')

    expect(screen.queryByRole('img', { name: '抠像预览' })).not.toBeInTheDocument()
    expect(screen.queryByText('#2AB448')).not.toBeInTheDocument()
  })

  it('keeps the loaded image and Screen Color when the same path is committed again', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
    const input = screen.getByLabelText('输入图片路径')

    await user.click(input)
    await user.keyboard('{Enter}')
    fireEvent.blur(input)

    expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce()
    expect(screen.getByText('#2AB448')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '抠像预览' })).toBeInTheDocument()
  })

  it('keeps the new path empty and reports an inspection failure', async () => {
    const user = renderWithUser()
    pageMocks.inspectChromaImage.mockRejectedValueOnce(new Error('图片无法读取'))

    await user.type(screen.getByLabelText('输入图片路径'), 'E:\\media\\broken.png')
    await user.keyboard('{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent('图片无法读取')
    expect(screen.getByLabelText('输入图片路径')).toHaveValue('E:\\media\\broken.png')
    expect(screen.queryByRole('button', { name: '抠像预览' })).not.toBeInTheDocument()
    expect(screen.getByText('加载一张本地图片后，可在画面中吸取背景颜色。')).toBeInTheDocument()
  })

  it('does not let a slower older inspection overwrite a newer committed path', async () => {
    const oldRequest = createDeferred<typeof inspectResponse>()
    const newRequest = createDeferred<typeof inspectResponse>()
    pageMocks.inspectChromaImage
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise)
    render(<ChromaKeyerPage />)
    const input = screen.getByLabelText('输入图片路径')

    fireEvent.change(input, { target: { value: 'E:\\media\\old.png' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.change(input, { target: { value: 'E:\\media\\new.png' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(pageMocks.inspectChromaImage).toHaveBeenNthCalledWith(1, { inputPath: 'E:\\media\\old.png' })
    expect(pageMocks.inspectChromaImage).toHaveBeenNthCalledWith(2, { inputPath: 'E:\\media\\new.png' })

    await act(async () => {
      newRequest.resolve({
        ...inspectResponse,
        input_path: 'E:\\media\\new.png',
        preview_url: '/api/previews/new.png',
      })
    })
    expect(await screen.findByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/new.png',
    )

    await act(async () => {
      oldRequest.resolve({
        ...inspectResponse,
        input_path: 'E:\\media\\old.png',
        preview_url: '/api/previews/old.png',
      })
    })

    expect(screen.getByLabelText('输入图片路径')).toHaveValue('E:\\media\\new.png')
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/new.png',
    )
  })

  it('requests Final and immediately re-requests Alpha previews with complete samples', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()

    await waitFor(() =>
      expect(pageMocks.createChromaPreview).toHaveBeenCalledWith(
        expect.objectContaining({
          inputPath: 'E:\\media\\screen.png',
          params: expect.objectContaining({
            screenBalance: 0.5,
            screenGain: 1,
          }),
          samples: [[42, 180, 72]],
          viewMode: 'final',
        }),
      ),
    )
    await user.click(screen.getByRole('radio', { name: 'Alpha' }))

    await waitFor(() =>
      expect(pageMocks.createChromaPreview).toHaveBeenLastCalledWith(
        expect.objectContaining({
          samples: [[42, 180, 72]],
          viewMode: 'alpha',
        }),
      ),
    )
  })

  it('requests a new preview with the updated Screen Balance', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() =>
      expect(pageMocks.createChromaPreview).toHaveBeenCalledWith(
        expect.objectContaining({
          params: expect.objectContaining({ screenBalance: 0.5 }),
        }),
      ),
    )

    fireEvent.change(screen.getByLabelText('Screen Balance'), { target: { value: '0.35' } })

    await waitFor(() =>
      expect(pageMocks.createChromaPreview).toHaveBeenLastCalledWith(
        expect.objectContaining({
          params: expect.objectContaining({ screenBalance: 0.35 }),
          samples: [[42, 180, 72]],
        }),
      ),
    )
  })

  it('keeps the current preview without re-requesting when only the output path changes', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledTimes(1))
    const preview = screen.getByRole('button', { name: '抠像预览' })
    await waitFor(() => expect(preview).toHaveAttribute('src', '/api/previews/final.png'))

    await user.type(screen.getByLabelText('输出路径（可选）'), 'E:\\media\\screen_keyed.png')
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 300))
    })

    expect(pageMocks.createChromaPreview).toHaveBeenCalledTimes(1)
    expect(preview).toHaveAttribute('src', '/api/previews/final.png')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('clears a preview error after a later preview request succeeds', async () => {
    pageMocks.createChromaPreview
      .mockRejectedValueOnce(new Error('预览请求失败'))
      .mockResolvedValueOnce({ preview_url: '/api/previews/recovered.png' })
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    expect(await screen.findByRole('alert')).toHaveTextContent('预览请求失败')

    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.4' } })

    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
        'src',
        '/api/previews/recovered.png',
      ),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('disables export without a sample and while another task is running', async () => {
    render(<ChromaKeyerPage />)

    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeDisabled()

    act(() => {
      useTaskStore.getState().startTask({ kind: 'video_frames', taskId: 'running-task' })
    })

    expect(screen.getByLabelText('输入图片路径')).toBeDisabled()
    expect(screen.getByRole('button', { name: '指定输入图片路径' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeDisabled()
  })

  it('disables image actions while a local inspect request is pending', async () => {
    const user = userEvent.setup()
    const inspectRequest = createDeferred<typeof inspectResponse>()
    pageMocks.inspectChromaImage.mockReturnValue(inspectRequest.promise)

    render(<ChromaKeyerPage />)

    await user.type(screen.getByLabelText('输入图片路径'), 'E:\\media\\screen.png')
    await user.keyboard('{Enter}')

    expect(screen.getByLabelText('输入图片路径')).toBeDisabled()
    expect(screen.getByRole('button', { name: '指定输入图片路径' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeDisabled()
  })

  it('processes deferred inspect and task creation responses after StrictMode effect replay', async () => {
    const user = userEvent.setup()
    const inspectRequest = createDeferred<typeof inspectResponse>()
    const createRequest = createDeferred<{ status: 'queued'; task_id: string }>()
    pageMocks.inspectChromaImage.mockReturnValue(inspectRequest.promise)
    pageMocks.createChromaKeyerTask.mockReturnValue(createRequest.promise)

    render(
      <StrictMode>
        <ChromaKeyerPage />
      </StrictMode>,
    )

    await user.type(screen.getByLabelText('输入图片路径'), 'E:\\media\\screen.png')
    await user.keyboard('{Enter}')
    await act(async () => {
      inspectRequest.resolve(inspectResponse)
    })

    expect(await screen.findByRole('button', { name: '抠像预览' })).toBeInTheDocument()

    await sampleImage()
    await waitFor(() => expect(screen.getByRole('button', { name: '导出 PNG' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: '导出 PNG' }))
    await act(async () => {
      createRequest.resolve({ status: 'queued', task_id: 'strict-mode-task' })
    })

    await waitFor(() =>
      expect(useTaskStore.getState()).toMatchObject({
        currentTaskId: 'strict-mode-task',
        kind: 'chroma_keyer',
        status: 'queued',
      }),
    )
  })

  it('deletes the Screen Color and restores the inspected source preview', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    const preview = screen.getByRole('button', { name: '抠像预览' })
    await waitFor(() => expect(preview).toHaveAttribute('src', '/api/previews/final.png'))

    await user.click(screen.getByRole('button', { name: '删除 Screen Color' }))

    expect(screen.queryByText('#2AB448')).not.toBeInTheDocument()
    expect(preview).toHaveAttribute('src', inspectResponse.preview_url)
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '清除全部采样点' })).toBeDisabled()
  })

  it('clears the Screen Color and restores the inspected source preview', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    const preview = screen.getByRole('button', { name: '抠像预览' })
    await waitFor(() => expect(preview).toHaveAttribute('src', '/api/previews/final.png'))

    await user.click(screen.getByRole('button', { name: '清除全部采样点' }))

    expect(screen.queryByText('#2AB448')).not.toBeInTheDocument()
    expect(preview).toHaveAttribute('src', inspectResponse.preview_url)
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '清除全部采样点' })).toBeDisabled()
  })

  it('exports the complete nested chroma payload after validation', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(screen.getByRole('button', { name: '导出 PNG' })).toBeEnabled())
    await user.type(screen.getByLabelText('输出路径（可选）'), 'E:\\media\\screen_keyed.png')
    await user.clear(screen.getByLabelText('Screen Gain'))
    await user.type(screen.getByLabelText('Screen Gain'), '1.4')
    await user.clear(screen.getByLabelText('Screen Balance'))
    await user.type(screen.getByLabelText('Screen Balance'), '0.35')
    await user.click(screen.getByRole('button', { name: '导出 PNG' }))

    expect(pageMocks.createChromaKeyerTask).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.png',
      outputPath: 'E:\\media\\screen_keyed.png',
      params: {
        clipBlack: 0,
        clipWhite: 1,
        despillAmount: 1,
        feather: 0,
        shrinkGrow: 0,
        screenBalance: 0.35,
        screenGain: 1.4,
      },
      samples: [[42, 180, 72]],
    })
    expect(useTaskStore.getState()).toMatchObject({
      currentTaskId: 'chroma-task-1',
      kind: 'chroma_keyer',
      status: 'queued',
    })
  })

  it('shows create rejection locally without changing the active task or logs', async () => {
    const user = renderWithUser()
    pageMocks.createChromaKeyerTask.mockRejectedValue(new Error('输出路径已存在'))

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(screen.getByRole('button', { name: '导出 PNG' })).toBeEnabled())

    act(() => {
      useTaskStore.getState().startTask({ kind: 'video_frames', taskId: 'completed-task' })
      useTaskStore.getState().completeTask({ resultPath: 'E:\\media\\frames', taskId: 'completed-task' })
      useLogStore.getState().appendEvent({
        details: {},
        level: 'success',
        message: '已有日志',
        phase: 'completed',
        task_id: 'completed-task',
        timestamp: '2026-08-28T00:00:00.000Z',
      })
    })

    await user.click(screen.getByRole('button', { name: '导出 PNG' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('输出路径已存在')
    expect(useTaskStore.getState()).toMatchObject({
      currentTaskId: 'completed-task',
      kind: 'video_frames',
      resultPath: 'E:\\media\\frames',
      status: 'succeeded',
    })
    expect(useLogStore.getState().events).toHaveLength(1)
    expect(useLogStore.getState().events[0]?.message).toBe('已有日志')
  })
})

function renderWithUser() {
  render(<ChromaKeyerPage />)
  return userEvent.setup()
}

function createDeferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve
  })
  return { promise, resolve }
}
