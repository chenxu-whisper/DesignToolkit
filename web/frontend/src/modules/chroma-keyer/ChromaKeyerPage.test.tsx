import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { ChromaKeyerPage } from './ChromaKeyerPage'

const pageMocks = vi.hoisted(() => ({
  createChromaKeyerTask: vi.fn(),
  createChromaPreview: vi.fn(),
  createChromaVideoFrame: vi.fn(),
  createChromaVideoKeyerTask: vi.fn(),
  createChromaVideoPreview: vi.fn(),
  inspectChromaImage: vi.fn(),
  inspectChromaVideo: vi.fn(),
  pickLocalDirectory: vi.fn(),
  pickLocalFiles: vi.fn(),
  pickLocalSaveFile: vi.fn(),
  resolveDroppedFilePaths: vi.fn(),
  sampleChromaImage: vi.fn(),
  sampleChromaVideo: vi.fn(),
}))

vi.mock('../../services/apiClient', () => ({
  createChromaKeyerTask: pageMocks.createChromaKeyerTask,
  createChromaPreview: pageMocks.createChromaPreview,
  createChromaVideoFrame: pageMocks.createChromaVideoFrame,
  createChromaVideoKeyerTask: pageMocks.createChromaVideoKeyerTask,
  createChromaVideoPreview: pageMocks.createChromaVideoPreview,
  inspectChromaImage: pageMocks.inspectChromaImage,
  inspectChromaVideo: pageMocks.inspectChromaVideo,
  pickLocalDirectory: pageMocks.pickLocalDirectory,
  pickLocalFiles: pageMocks.pickLocalFiles,
  pickLocalSaveFile: pageMocks.pickLocalSaveFile,
  resolveDroppedFilePaths: pageMocks.resolveDroppedFilePaths,
  sampleChromaImage: pageMocks.sampleChromaImage,
  sampleChromaVideo: pageMocks.sampleChromaVideo,
}))

const inspectResponse = {
  height: 400,
  input_path: 'E:\\media\\screen.png',
  preview_height: 200,
  preview_url: '/api/previews/input.png',
  preview_width: 400,
  width: 800,
}

const videoInspectResponse = {
  duration_seconds: 12.5,
  estimated_frame_count: 375,
  fps: 30,
  height: 1080,
  input_path: 'E:\\media\\screen.mp4',
  preview_height: 360,
  preview_url: '/api/previews/video-first.png',
  preview_width: 640,
  timestamp_seconds: 0,
  width: 1920,
}

async function inspectImage() {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('输入图片或视频路径'), 'E:\\media\\screen.png')
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

async function inspectVideo() {
  const input = screen.getByLabelText('输入图片或视频路径')
  fireEvent.change(input, { target: { value: 'E:\\media\\screen.mp4' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  await screen.findByRole('slider', { name: '视频时间轴' })
}

beforeEach(() => {
  pageMocks.createChromaKeyerTask.mockReset()
  pageMocks.createChromaPreview.mockReset()
  pageMocks.createChromaVideoFrame.mockReset()
  pageMocks.createChromaVideoKeyerTask.mockReset()
  pageMocks.createChromaVideoPreview.mockReset()
  pageMocks.inspectChromaImage.mockReset()
  pageMocks.inspectChromaVideo.mockReset()
  pageMocks.pickLocalDirectory.mockReset()
  pageMocks.pickLocalFiles.mockReset()
  pageMocks.pickLocalSaveFile.mockReset()
  pageMocks.resolveDroppedFilePaths.mockReset()
  pageMocks.sampleChromaImage.mockReset()
  pageMocks.sampleChromaVideo.mockReset()
  pageMocks.pickLocalFiles.mockResolvedValue({ cancelled: false, paths: ['E:\\media\\screen.png'] })
  pageMocks.inspectChromaImage.mockResolvedValue(inspectResponse)
  pageMocks.inspectChromaVideo.mockResolvedValue(videoInspectResponse)
  pageMocks.sampleChromaImage.mockResolvedValue({
    hex: '#2AB448',
    rgb: [42, 180, 72],
    source_x: 400,
    source_y: 200,
  })
  pageMocks.sampleChromaVideo.mockResolvedValue({
    hex: '#2AB448',
    rgb: [42, 180, 72],
    source_x: 960,
    source_y: 540,
    timestamp_seconds: 0,
  })
  pageMocks.createChromaPreview.mockResolvedValue({ preview_url: '/api/previews/final.png' })
  pageMocks.createChromaVideoFrame.mockResolvedValue({
    preview_url: '/api/previews/video-frame.png',
    timestamp_seconds: 1.25,
  })
  pageMocks.createChromaVideoPreview.mockResolvedValue({
    preview_url: '/api/previews/video-keyed.png',
    timestamp_seconds: 1.25,
  })
  pageMocks.createChromaKeyerTask.mockResolvedValue({ status: 'queued', task_id: 'chroma-task-1' })
  pageMocks.createChromaVideoKeyerTask.mockResolvedValue({
    status: 'queued',
    task_id: 'chroma-video-task-1',
  })
  useTaskStore.getState().resetTask()
  useLogStore.getState().clearEvents()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChromaKeyerPage', () => {
  it('automatically inspects the exact path selected by the native picker', async () => {
    const user = renderWithUser()

    expect(screen.queryByRole('button', { name: '加载图片' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '指定输入图片或视频路径' }))

    await waitFor(() =>
      expect(pageMocks.inspectChromaImage).toHaveBeenCalledWith({
        inputPath: 'E:\\media\\screen.png',
      }),
    )
    expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce()
    expect(screen.getByLabelText('输入图片或视频路径')).toHaveValue('E:\\media\\screen.png')
    expect(await screen.findByRole('button', { name: '抠像预览' })).toBeInTheDocument()
  })

  it('does not inspect while typing and commits once on Enter followed by blur', async () => {
    const user = renderWithUser()
    const input = screen.getByLabelText('输入图片或视频路径')
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
    const input = screen.getByLabelText('输入图片或视频路径')

    await user.type(input, 'E:\\media\\screen.png')

    expect(pageMocks.inspectChromaImage).not.toHaveBeenCalled()
    fireEvent.blur(input)

    await waitFor(() => expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce())
  })

  it('keeps the picker focused and guarded while a Tab-committed inspection is pending', async () => {
    const user = renderWithUser()
    const input = screen.getByLabelText('输入图片或视频路径')
    const picker = screen.getByRole('button', { name: '指定输入图片或视频路径' })
    const inspectRequest = createDeferred<typeof inspectResponse>()
    pageMocks.inspectChromaImage.mockReturnValueOnce(inspectRequest.promise)

    await user.type(input, 'E:\\media\\screen.png')
    await user.tab()

    expect(picker).toHaveFocus()
    await waitFor(() => expect(pageMocks.inspectChromaImage).toHaveBeenCalledOnce())
    expect(pageMocks.inspectChromaImage).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.png',
    })
    expect(picker).toHaveFocus()
    expect(picker).not.toBeDisabled()
    expect(picker).toHaveAttribute('aria-disabled', 'true')

    await user.keyboard('{Enter}')
    await user.click(picker)

    expect(pageMocks.pickLocalFiles).not.toHaveBeenCalled()

    await act(async () => {
      inspectRequest.resolve(inspectResponse)
    })

    await waitFor(() => expect(picker).toHaveAttribute('aria-disabled', 'false'))
    expect(input).toBeEnabled()
    expect(picker).toHaveFocus()
  })

  it('shows an input error for a blank committed path', () => {
    render(<ChromaKeyerPage />)

    fireEvent.keyDown(screen.getByLabelText('输入图片或视频路径'), { key: 'Enter' })

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

  it('applies a pending valid preview after a replacement sample is rejected', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    const preview = screen.getByRole('button', { name: '抠像预览' })
    await waitFor(() => expect(preview).toHaveAttribute('src', '/api/previews/final.png'))
    const previewRequest = createDeferred<{ preview_url: string }>()
    pageMocks.createChromaPreview.mockReturnValueOnce(previewRequest.promise)
    pageMocks.sampleChromaImage.mockRejectedValueOnce(new Error('请选择具有明显主色的纯色幕背景'))

    fireEvent.click(screen.getByRole('radio', { name: 'Alpha' }))
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledTimes(2))
    await sampleImage()

    expect(await screen.findByRole('alert')).toHaveTextContent('请选择具有明显主色的纯色幕背景')
    expect(screen.getByText('#2AB448')).toBeInTheDocument()

    await act(async () => {
      previewRequest.resolve({ preview_url: '/api/previews/pending-valid.png' })
    })

    expect(preview).toHaveAttribute('src', '/api/previews/pending-valid.png')
    expect(screen.getByText('#2AB448')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('请选择具有明显主色的纯色幕背景')
  })

  it('clears the inspected session when the input path changes', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
    await user.clear(screen.getByLabelText('输入图片或视频路径'))
    await user.type(screen.getByLabelText('输入图片或视频路径'), 'E:\\media\\replacement.png')

    expect(screen.queryByRole('img', { name: '抠像预览' })).not.toBeInTheDocument()
    expect(screen.queryByText('#2AB448')).not.toBeInTheDocument()
  })

  it('keeps the loaded image and Screen Color when the same path is committed again', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
    const input = screen.getByLabelText('输入图片或视频路径')

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

    await user.type(screen.getByLabelText('输入图片或视频路径'), 'E:\\media\\broken.png')
    await user.keyboard('{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent('图片无法读取')
    expect(screen.getByLabelText('输入图片或视频路径')).toHaveValue('E:\\media\\broken.png')
    expect(screen.queryByRole('button', { name: '抠像预览' })).not.toBeInTheDocument()
    expect(screen.getByText('加载本地图片或视频后，可在画面中吸取背景颜色。')).toBeInTheDocument()
  })

  it('does not let a slower older inspection overwrite a newer committed path', async () => {
    const oldRequest = createDeferred<typeof inspectResponse>()
    const newRequest = createDeferred<typeof inspectResponse>()
    pageMocks.inspectChromaImage
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise)
    render(<ChromaKeyerPage />)
    const input = screen.getByLabelText('输入图片或视频路径')

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

    expect(screen.getByLabelText('输入图片或视频路径')).toHaveValue('E:\\media\\new.png')
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/new.png',
    )
  })

  it('requests Final and immediately re-requests Alpha previews with complete samples', async () => {
    render(<ChromaKeyerPage />)

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
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    await act(async () => {
      fireEvent.click(screen.getByRole('radio', { name: 'Alpha' }))
    })

    expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce()
    expect(pageMocks.createChromaPreview).toHaveBeenLastCalledWith(
      expect.objectContaining({
        samples: [[42, 180, 72]],
        viewMode: 'alpha',
      }),
    )
  })

  it('debounces valid parameter changes for 150 ms and replaces the pending timer', async () => {
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
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByLabelText('Screen Balance'), { target: { value: '0.35' } })
    act(() => {
      vi.advanceTimersByTime(149)
    })
    expect(pageMocks.createChromaPreview).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Screen Balance'), { target: { value: '0.4' } })
    act(() => {
      vi.advanceTimersByTime(149)
    })
    expect(pageMocks.createChromaPreview).not.toHaveBeenCalled()

    await act(async () => {
      vi.advanceTimersByTime(1)
    })
    expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce()
    expect(pageMocks.createChromaPreview).toHaveBeenLastCalledWith(
      expect.objectContaining({
        params: expect.objectContaining({ screenBalance: 0.4 }),
        samples: [[42, 180, 72]],
      }),
    )
  })

  it('keeps the form and sampling controls enabled while a preview request is pending', async () => {
    const user = renderWithUser()

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce())
    const previewRequest = createDeferred<{ preview_url: string }>()
    pageMocks.createChromaPreview.mockReturnValueOnce(previewRequest.promise)

    await user.click(screen.getByRole('radio', { name: 'Alpha' }))

    expect(pageMocks.createChromaPreview).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('slider', { name: 'Screen Gain' })).toBeEnabled()
    expect(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' })).toBeEnabled()
    expect(screen.getByLabelText('输出路径（可选）')).toBeEnabled()
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '删除 Screen Color' })).toBeEnabled()
  })

  it('allows overlapping parameter previews and renders only the latest success', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce())
    const olderRequest = createDeferred<{ preview_url: string }>()
    const latestRequest = createDeferred<{ preview_url: string }>()
    pageMocks.createChromaPreview
      .mockReturnValueOnce(olderRequest.promise)
      .mockReturnValueOnce(latestRequest.promise)
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.2' } })
    act(() => {
      vi.advanceTimersByTime(150)
    })
    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.4' } })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    expect(pageMocks.createChromaPreview).toHaveBeenCalledTimes(2)
    expect(pageMocks.createChromaPreview).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ params: expect.objectContaining({ screenGain: 1.2 }) }),
    )
    expect(pageMocks.createChromaPreview).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ params: expect.objectContaining({ screenGain: 1.4 }) }),
    )

    await act(async () => {
      latestRequest.resolve({ preview_url: '/api/previews/latest.png' })
    })
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/latest.png',
    )

    await act(async () => {
      olderRequest.resolve({ preview_url: '/api/previews/stale.png' })
    })
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/latest.png',
    )
  })

  it('ignores a stale preview error after the latest preview succeeds', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce())
    const olderRequest = createDeferred<{ preview_url: string }>()
    const latestRequest = createDeferred<{ preview_url: string }>()
    pageMocks.createChromaPreview
      .mockReturnValueOnce(olderRequest.promise)
      .mockReturnValueOnce(latestRequest.promise)
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.2' } })
    act(() => {
      vi.advanceTimersByTime(150)
    })
    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.4' } })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    expect(pageMocks.createChromaPreview).toHaveBeenCalledTimes(2)
    await act(async () => {
      latestRequest.resolve({ preview_url: '/api/previews/latest.png' })
    })
    await act(async () => {
      olderRequest.reject(new Error('过期预览失败'))
    })

    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/latest.png',
    )
    expect(screen.queryByText('过期预览失败')).not.toBeInTheDocument()
  })

  it('keeps action pending state when a stale preview finishes', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce())
    const previewRequest = createDeferred<{ preview_url: string }>()
    const sampleRequest = createDeferred<{
      hex: string
      rgb: [number, number, number]
      source_x: number
      source_y: number
    }>()
    pageMocks.createChromaPreview.mockReturnValueOnce(previewRequest.promise)
    pageMocks.sampleChromaImage.mockReturnValueOnce(sampleRequest.promise)

    fireEvent.click(screen.getByRole('radio', { name: 'Alpha' }))
    await sampleImage()

    expect(pageMocks.sampleChromaImage).toHaveBeenCalledTimes(2)
    expect(screen.getByLabelText('输入图片或视频路径')).toBeDisabled()

    await act(async () => {
      previewRequest.resolve({ preview_url: '/api/previews/stale.png' })
    })

    expect(screen.getByLabelText('输入图片或视频路径')).toBeDisabled()

    await act(async () => {
      sampleRequest.resolve({
        hex: '#32C850',
        rgb: [50, 200, 80],
        source_x: 400,
        source_y: 200,
      })
    })
    expect(screen.getByLabelText('输入图片或视频路径')).toBeEnabled()
  })

  it('keeps the last successful preview visible when the latest preview fails', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    const preview = screen.getByRole('button', { name: '抠像预览' })
    await waitFor(() => expect(preview).toHaveAttribute('src', '/api/previews/final.png'))
    pageMocks.createChromaPreview.mockRejectedValueOnce(new Error('最新预览失败'))
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.4' } })
    await act(async () => {
      vi.advanceTimersByTime(150)
    })

    expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce()
    expect(preview).toHaveAttribute('src', '/api/previews/final.png')
    expect(screen.getByRole('alert')).toHaveTextContent('最新预览失败')
  })

  it('shows numeric validation and sends no preview for an invalid parameter', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce())
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), {
      target: { value: '' },
    })
    act(() => {
      vi.advanceTimersByTime(500)
    })

    expect(screen.getByRole('alert')).toHaveTextContent('Screen Gain 必须是 0 到 2 之间的有限数值')
    expect(pageMocks.createChromaPreview).not.toHaveBeenCalled()
  })

  it('shows cross-field validation and sends no preview when Clip Black is not below Clip White', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce())
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByRole('spinbutton', { name: 'Clip Black 数值' }), {
      target: { value: '1' },
    })
    act(() => {
      vi.advanceTimersByTime(500)
    })

    expect(screen.getByRole('alert')).toHaveTextContent('Clip Black 必须小于 Clip White')
    expect(pageMocks.createChromaPreview).not.toHaveBeenCalled()
  })

  it('keeps a corrected parameter debounced after a view change made while invalid', async () => {
    render(<ChromaKeyerPage />)

    await inspectImage()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaPreview).toHaveBeenCalledOnce())
    pageMocks.createChromaPreview.mockImplementation(() => new Promise(() => {}))
    pageMocks.createChromaPreview.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), {
      target: { value: '' },
    })
    fireEvent.click(screen.getByRole('radio', { name: 'Alpha' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), {
      target: { value: '1.2' },
    })

    expect(pageMocks.createChromaPreview).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(149)
    })
    expect(pageMocks.createChromaPreview).not.toHaveBeenCalled()

    await act(async () => {
      vi.advanceTimersByTime(1)
    })
    expect(pageMocks.createChromaPreview).toHaveBeenCalledWith(
      expect.objectContaining({
        params: expect.objectContaining({ screenGain: 1.2 }),
        viewMode: 'alpha',
      }),
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

    expect(screen.getByLabelText('输入图片或视频路径')).toBeDisabled()
    expect(screen.getByRole('button', { name: '指定输入图片或视频路径' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeDisabled()
  })

  it('disables image actions while a local inspect request is pending', async () => {
    const user = userEvent.setup()
    const inspectRequest = createDeferred<typeof inspectResponse>()
    pageMocks.inspectChromaImage.mockReturnValue(inspectRequest.promise)

    render(<ChromaKeyerPage />)

    await user.type(screen.getByLabelText('输入图片或视频路径'), 'E:\\media\\screen.png')
    await user.keyboard('{Enter}')

    expect(screen.getByLabelText('输入图片或视频路径')).toBeDisabled()
    expect(screen.getByRole('button', { name: '指定输入图片或视频路径' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
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

    await user.type(screen.getByLabelText('输入图片或视频路径'), 'E:\\media\\screen.png')
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
    await user.clear(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }))
    await user.type(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), '1.4')
    await user.clear(screen.getByRole('spinbutton', { name: 'Screen Balance 数值' }))
    await user.type(screen.getByRole('spinbutton', { name: 'Screen Balance 数值' }), '0.35')
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

  it('detects a committed video path and renders its first frame with metadata', async () => {
    render(<ChromaKeyerPage />)

    await inspectVideo()

    expect(pageMocks.inspectChromaVideo).toHaveBeenCalledOnce()
    expect(pageMocks.inspectChromaVideo).toHaveBeenCalledWith({ inputPath: 'E:\\media\\screen.mp4' })
    expect(pageMocks.inspectChromaImage).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/video-first.png',
    )
    expect(screen.getByText('30 FPS')).toBeInTheDocument()
    expect(screen.getByText('1920 × 1080')).toBeInTheDocument()
    expect(screen.getByText('约 375 帧')).toBeInTheDocument()
  })

  it('renders the video timeline and its error directly below the canvas', async () => {
    pageMocks.createChromaVideoFrame.mockRejectedValueOnce(new Error('视频帧无法读取'))
    render(<ChromaKeyerPage />)
    await inspectVideo()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1' },
    })

    const error = await screen.findByRole('alert')
    const canvas = screen.getByRole('button', { name: '抠像预览' }).parentElement
    const timeline = screen.getByTestId('chroma-video-timeline')
    const timelineRange = screen.getByRole('slider', { name: '视频时间轴' })
    const instructions = screen.getByText(
      '点击预览采样，或聚焦后按 Enter 或空格从画面中心采样。',
    )

    expect(error).toHaveAttribute('id', 'chroma-video-timestamp-error')
    expect(timelineRange).toHaveAttribute('aria-describedby', 'chroma-video-timestamp-error')
    expect(timelineRange).toHaveAttribute('aria-invalid', 'true')
    expect(canvas?.nextElementSibling).toBe(timeline)
    expect(timeline.nextElementSibling).toBe(error)
    expect(error.nextElementSibling).toBe(instructions)
  })

  it('debounces video seeks for 100 ms and never renders a stale frame response', async () => {
    const olderRequest = createDeferred<{ preview_url: string; timestamp_seconds: number }>()
    const latestRequest = createDeferred<{ preview_url: string; timestamp_seconds: number }>()
    pageMocks.createChromaVideoFrame
      .mockReturnValueOnce(olderRequest.promise)
      .mockReturnValueOnce(latestRequest.promise)
    render(<ChromaKeyerPage />)
    await inspectVideo()
    pageMocks.createChromaVideoFrame.mockClear()
    pageMocks.createChromaVideoFrame
      .mockReturnValueOnce(olderRequest.promise)
      .mockReturnValueOnce(latestRequest.promise)
    vi.useFakeTimers()
    const timeline = screen.getByRole('slider', { name: '视频时间轴' })

    fireEvent.change(timeline, { target: { value: '1' } })
    act(() => {
      vi.advanceTimersByTime(99)
    })
    expect(pageMocks.createChromaVideoFrame).not.toHaveBeenCalled()
    await act(async () => {
      vi.advanceTimersByTime(1)
    })

    fireEvent.change(timeline, { target: { value: '2' } })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })

    expect(pageMocks.createChromaVideoFrame).toHaveBeenNthCalledWith(1, {
      inputPath: 'E:\\media\\screen.mp4',
      timestampSeconds: 1,
    })
    expect(pageMocks.createChromaVideoFrame).toHaveBeenNthCalledWith(2, {
      inputPath: 'E:\\media\\screen.mp4',
      timestampSeconds: 2,
    })

    await act(async () => {
      latestRequest.resolve({ preview_url: '/api/previews/video-latest.png', timestamp_seconds: 2 })
    })
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/video-latest.png',
    )

    await act(async () => {
      olderRequest.resolve({ preview_url: '/api/previews/video-stale.png', timestamp_seconds: 1 })
    })
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/video-latest.png',
    )
    expect(timeline).toHaveValue('2')
  })

  it('ignores sampling while the latest requested video timestamp is still rendering', async () => {
    const seekRequest = createDeferred<{ preview_url: string; timestamp_seconds: number }>()
    pageMocks.createChromaVideoFrame.mockReturnValueOnce(seekRequest.promise)
    render(<ChromaKeyerPage />)
    await inspectVideo()
    vi.useFakeTimers()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1' },
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    const image = screen.getByRole('button', { name: '抠像预览' })
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
    fireEvent.click(image, { clientX: 210, clientY: 120 })

    expect(pageMocks.sampleChromaVideo).not.toHaveBeenCalled()
  })

  it('samples the backend-resolved timestamp after the latest frame renders', async () => {
    pageMocks.createChromaVideoFrame.mockResolvedValueOnce({
      preview_url: '/api/previews/video-resolved.png',
      timestamp_seconds: 1.2,
    })
    render(<ChromaKeyerPage />)
    await inspectVideo()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1.25' },
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
        'src',
        '/api/previews/video-resolved.png',
      ),
    )
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toHaveValue('1.2')

    await sampleImage()

    expect(pageMocks.sampleChromaVideo).toHaveBeenCalledWith(
      expect.objectContaining({ timestampSeconds: 1.2 }),
    )
  })

  it('rolls a failed seek back to the rendered timestamp and keeps that frame sampleable', async () => {
    pageMocks.createChromaVideoFrame.mockRejectedValueOnce(new Error('视频帧无法读取'))
    render(<ChromaKeyerPage />)
    await inspectVideo()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1' },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('视频帧无法读取')
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toHaveValue('0')

    await sampleImage()

    expect(pageMocks.sampleChromaVideo).toHaveBeenCalledWith(
      expect.objectContaining({ timestampSeconds: 0 }),
    )
  })

  it('keeps an unsampled seek valid when a parameter change cannot dispatch a keyed preview', async () => {
    const seekRequest = createDeferred<{ preview_url: string; timestamp_seconds: number }>()
    pageMocks.createChromaVideoFrame.mockReturnValueOnce(seekRequest.promise)
    render(<ChromaKeyerPage />)
    await inspectVideo()
    vi.useFakeTimers()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1' },
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.4' } })

    expect(pageMocks.createChromaVideoPreview).not.toHaveBeenCalled()
    await act(async () => {
      seekRequest.resolve({
        preview_url: '/api/previews/video-unkeyed-latest.png',
        timestamp_seconds: 1,
      })
    })

    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/video-unkeyed-latest.png',
    )
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toHaveValue('1')
  })

  it('rolls back a sampled seek invalidated before its debounce fires and permits the next valid seek', async () => {
    pageMocks.createChromaVideoPreview.mockResolvedValueOnce({
      preview_url: '/api/previews/video-keyed-zero.png',
      timestamp_seconds: 0,
    })
    render(<ChromaKeyerPage />)
    await inspectVideo()
    await sampleImage()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
        'src',
        '/api/previews/video-keyed-zero.png',
      ),
    )
    pageMocks.createChromaVideoFrame.mockClear()
    pageMocks.createChromaVideoPreview.mockClear()
    vi.useFakeTimers()
    const timeline = screen.getByRole('slider', { name: '视频时间轴' })
    const preview = screen.getByRole('button', { name: '抠像预览' })

    fireEvent.change(timeline, { target: { value: '1' } })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), {
      target: { value: '' },
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })

    expect(pageMocks.createChromaVideoFrame).not.toHaveBeenCalled()
    expect(pageMocks.createChromaVideoPreview).not.toHaveBeenCalled()
    expect(timeline).toHaveValue('0')
    expect(preview).toBeEnabled()
    expect(preview).toHaveAttribute('src', '/api/previews/video-keyed-zero.png')
    expect(screen.getByText('#2AB448')).toBeInTheDocument()

    pageMocks.createChromaVideoPreview.mockResolvedValueOnce({
      preview_url: '/api/previews/video-keyed-two.png',
      timestamp_seconds: 2,
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), {
      target: { value: '1.2' },
    })
    fireEvent.change(timeline, { target: { value: '2' } })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })

    expect(pageMocks.createChromaVideoFrame).not.toHaveBeenCalled()
    expect(pageMocks.createChromaVideoPreview).toHaveBeenCalledOnce()
    expect(pageMocks.createChromaVideoPreview).toHaveBeenCalledWith(
      expect.objectContaining({
        params: expect.objectContaining({ screenGain: 1.2 }),
        timestampSeconds: 2,
      }),
    )
    expect(timeline).toHaveValue('2')
  })

  it('blocks sampled video seeks until keyed preview parameters are valid', async () => {
    render(<ChromaKeyerPage />)
    await inspectVideo()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaVideoPreview).toHaveBeenCalledOnce())
    pageMocks.createChromaVideoFrame.mockClear()
    pageMocks.createChromaVideoPreview.mockClear()
    vi.useFakeTimers()
    const timeline = screen.getByRole('slider', { name: '视频时间轴' })
    const initialTimestamp = timeline.getAttribute('value')

    fireEvent.change(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), {
      target: { value: '' },
    })
    fireEvent.change(screen.getByLabelText('输出根目录（可选）'), {
      target: { value: 'E:\\media\\screen.mp4' },
    })

    expect(timeline).toBeDisabled()
    fireEvent.keyDown(timeline, { key: 'ArrowRight' })
    await act(async () => {
      vi.advanceTimersByTime(500)
    })
    expect(timeline).toHaveValue(initialTimestamp)
    expect(pageMocks.createChromaVideoFrame).not.toHaveBeenCalled()
    expect(pageMocks.createChromaVideoPreview).not.toHaveBeenCalled()

    pageMocks.createChromaVideoPreview.mockResolvedValueOnce({
      preview_url: '/api/previews/video-keyed-restored.png',
      timestamp_seconds: 1,
    })
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }), {
      target: { value: '1.2' },
    })

    expect(timeline).toBeEnabled()
    fireEvent.change(timeline, { target: { value: '1' } })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })

    expect(pageMocks.createChromaVideoFrame).not.toHaveBeenCalled()
    expect(pageMocks.createChromaVideoPreview).toHaveBeenCalledWith(
      expect.objectContaining({
        params: expect.objectContaining({ screenGain: 1.2 }),
        timestampSeconds: 1,
      }),
    )
    expect(timeline).toHaveValue('1')
  })

  it('keeps a seek valid until a debounced replacement preview actually dispatches', async () => {
    pageMocks.createChromaVideoPreview.mockResolvedValueOnce({
      preview_url: '/api/previews/video-keyed-zero.png',
      timestamp_seconds: 0,
    })
    render(<ChromaKeyerPage />)
    await inspectVideo()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaVideoPreview).toHaveBeenCalledOnce())
    pageMocks.createChromaVideoPreview.mockClear()
    const seekRequest = createDeferred<{ preview_url: string; timestamp_seconds: number }>()
    pageMocks.createChromaVideoPreview.mockReturnValueOnce(seekRequest.promise)
    vi.useFakeTimers()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1' },
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.4' } })

    expect(pageMocks.createChromaVideoPreview).toHaveBeenCalledOnce()
    await act(async () => {
      seekRequest.resolve({
        preview_url: '/api/previews/video-keyed-seek.png',
        timestamp_seconds: 1,
      })
    })

    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/video-keyed-seek.png',
    )
  })

  it('releases sample pending state when a newer video seek supersedes it', async () => {
    const sampleRequest = createDeferred<{
      hex: string
      rgb: [number, number, number]
      source_x: number
      source_y: number
      timestamp_seconds: number
    }>()
    pageMocks.sampleChromaVideo.mockReturnValueOnce(sampleRequest.promise)
    render(<ChromaKeyerPage />)
    await inspectVideo()

    await sampleImage()
    expect(screen.getByLabelText('输入图片或视频路径')).toBeDisabled()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1.25' },
    })
    await waitFor(() => expect(pageMocks.createChromaVideoFrame).toHaveBeenCalledOnce())

    expect(screen.getByLabelText('输入图片或视频路径')).toBeEnabled()
    await act(async () => {
      sampleRequest.resolve({
        hex: '#2AB448',
        rgb: [42, 180, 72],
        source_x: 960,
        source_y: 540,
        timestamp_seconds: 0,
      })
    })
    expect(screen.queryByText('#2AB448')).not.toBeInTheDocument()
  })

  it('samples the selected video timestamp through the current preview dimensions', async () => {
    render(<ChromaKeyerPage />)
    await inspectVideo()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1.25' },
    })
    await waitFor(() => expect(pageMocks.createChromaVideoFrame).toHaveBeenCalledOnce())
    await sampleImage()

    expect(pageMocks.sampleChromaVideo).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.mp4',
      previewHeight: 200,
      previewWidth: 400,
      timestampSeconds: 1.25,
      x: 200,
      y: 100,
    })
    expect(pageMocks.sampleChromaImage).not.toHaveBeenCalled()
    expect(screen.getByText('#2AB448')).toBeInTheDocument()
  })

  it('uses keyed video previews after sampling and preserves the sample and all parameters across seeks', async () => {
    render(<ChromaKeyerPage />)
    await inspectVideo()
    await sampleImage()
    await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Screen Gain'), { target: { value: '1.4' } })
    fireEvent.change(screen.getByLabelText('Screen Balance'), { target: { value: '0.35' } })
    fireEvent.change(screen.getByLabelText('Clip Black'), { target: { value: '0.1' } })
    fireEvent.change(screen.getByLabelText('Clip White'), { target: { value: '0.9' } })
    fireEvent.change(screen.getByLabelText('Shrink/Grow'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Feather'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('Despill Amount'), { target: { value: '0.8' } })
    fireEvent.click(screen.getByRole('radio', { name: 'Alpha' }))
    await waitFor(() => expect(pageMocks.createChromaVideoPreview).toHaveBeenCalled())
    pageMocks.createChromaVideoPreview.mockClear()
    pageMocks.createChromaVideoFrame.mockClear()
    vi.useFakeTimers()

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '2.5' },
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })

    expect(pageMocks.createChromaVideoFrame).not.toHaveBeenCalled()
    expect(pageMocks.createChromaVideoPreview).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.mp4',
      params: {
        clipBlack: 0.1,
        clipWhite: 0.9,
        despillAmount: 0.8,
        feather: 3,
        screenBalance: 0.35,
        screenGain: 1.4,
        shrinkGrow: 2,
      },
      samples: [[42, 180, 72]],
      timestampSeconds: 2.5,
      viewMode: 'alpha',
    })
    expect(screen.getAllByText('Screen Color')).toHaveLength(1)
  })

  it('loads the unkeyed frame at the current timestamp when the video sample is removed', async () => {
    const user = renderWithUser()
    await inspectVideo()
    await sampleImage()
    await waitFor(() => expect(pageMocks.createChromaVideoPreview).toHaveBeenCalled())
    pageMocks.createChromaVideoPreview.mockResolvedValueOnce({
      preview_url: '/api/previews/video-keyed-2.png',
      timestamp_seconds: 2,
    })

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '2' },
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
        'src',
        '/api/previews/video-keyed-2.png',
      ),
    )
    pageMocks.createChromaVideoFrame.mockClear()
    pageMocks.createChromaVideoFrame.mockResolvedValueOnce({
      preview_url: '/api/previews/video-unkeyed-2.png',
      timestamp_seconds: 2,
    })

    await user.click(screen.getByRole('button', { name: '删除 Screen Color' }))

    await waitFor(() =>
      expect(pageMocks.createChromaVideoFrame).toHaveBeenCalledWith({
        inputPath: 'E:\\media\\screen.mp4',
        timestampSeconds: 2,
      }),
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
        'src',
        '/api/previews/video-unkeyed-2.png',
      ),
    )
  })

  it('exports video to outputRoot without sending an image outputPath', async () => {
    const user = renderWithUser()
    await inspectVideo()
    await sampleImage()
    await waitFor(() => expect(screen.getByRole('button', { name: '导出 PNG 序列' })).toBeEnabled())

    await user.type(screen.getByLabelText('输出根目录（可选）'), 'E:\\exports')
    await user.click(screen.getByRole('button', { name: '导出 PNG 序列' }))

    expect(pageMocks.createChromaVideoKeyerTask).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.mp4',
      outputRoot: 'E:\\exports',
      params: {
        clipBlack: 0,
        clipWhite: 1,
        despillAmount: 1,
        feather: 0,
        screenBalance: 0.5,
        screenGain: 1,
        shrinkGrow: 0,
      },
      samples: [[42, 180, 72]],
    })
    expect(pageMocks.createChromaVideoKeyerTask.mock.calls[0]?.[0]).not.toHaveProperty('outputPath')
    expect(pageMocks.createChromaKeyerTask).not.toHaveBeenCalled()
  })

  it('clears video-only state when switching to an image and keeps image export semantics', async () => {
    const user = renderWithUser()
    await inspectVideo()
    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1.25' },
    })
    await user.type(screen.getByLabelText('输出根目录（可选）'), 'E:\\exports')
    const input = screen.getByLabelText('输入图片或视频路径')

    fireEvent.change(input, { target: { value: 'E:\\media\\screen.png' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(await screen.findByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'src',
      '/api/previews/input.png',
    )
    expect(screen.queryByRole('slider', { name: '视频时间轴' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('输出路径（可选）')).toHaveValue('')
    expect(screen.queryByLabelText('输出根目录（可选）')).not.toBeInTheDocument()

    await sampleImage()
    await waitFor(() => expect(screen.getByRole('button', { name: '导出 PNG' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: '导出 PNG' }))

    expect(pageMocks.createChromaKeyerTask).toHaveBeenCalledWith({
      inputPath: 'E:\\media\\screen.png',
      outputPath: undefined,
      params: {
        clipBlack: 0,
        clipWhite: 1,
        despillAmount: 1,
        feather: 0,
        screenBalance: 0.5,
        screenGain: 1,
        shrinkGrow: 0,
      },
      samples: [[42, 180, 72]],
    })
  })
})

function renderWithUser() {
  render(<ChromaKeyerPage />)
  return userEvent.setup()
}

function createDeferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve
    reject = nextReject
  })
  return { promise, reject, resolve }
}
