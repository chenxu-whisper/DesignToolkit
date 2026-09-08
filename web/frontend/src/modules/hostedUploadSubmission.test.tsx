import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLogStore } from '../state/useLogStore'
import { useTaskStore } from '../state/useTaskStore'
import { ChromaKeyerPage } from './chroma-keyer/ChromaKeyerPage'
import { MediaResizePage } from './media-resize/MediaResizePage'
import { VideoFramesPage } from './video-frames/VideoFramesPage'

const apiMocks = vi.hoisted(() => ({
  createChromaKeyerTask: vi.fn(),
  createChromaPreview: vi.fn(),
  createChromaVideoFrame: vi.fn(),
  createChromaVideoKeyerTask: vi.fn(),
  createChromaVideoPreview: vi.fn(),
  createMediaResizeTask: vi.fn(),
  createVideoFramesTask: vi.fn(),
  deleteUploadedAsset: vi.fn(),
  inspectChromaImage: vi.fn(),
  inspectChromaVideo: vi.fn(),
  pickLocalDirectory: vi.fn(),
  pickLocalFiles: vi.fn(),
  pickLocalSaveFile: vi.fn(),
  resolveDroppedFilePaths: vi.fn(),
  sampleChromaImage: vi.fn(),
  sampleChromaVideo: vi.fn(),
  uploadFiles: vi.fn(),
}))

vi.mock('../app/runtimeConfig', () => ({
  hostedMode: true,
}))

vi.mock('../services/apiClient', () => apiMocks)

const oldImageReference = `asset:${'a'.repeat(32)}:${'1'.repeat(32)}:old.png`
const newImageReference = `asset:${'b'.repeat(32)}:${'2'.repeat(32)}:new.png`
const oldVideoReference = `asset:${'c'.repeat(32)}:${'3'.repeat(32)}:old.mp4`
const newVideoReference = `asset:${'d'.repeat(32)}:${'4'.repeat(32)}:new.mp4`

beforeEach(() => {
  for (const mock of Object.values(apiMocks)) {
    mock.mockReset()
  }
  apiMocks.createChromaKeyerTask.mockResolvedValue({
    status: 'queued',
    task_id: 'chroma-task',
  })
  apiMocks.createChromaPreview.mockResolvedValue({
    preview_url: '/api/previews/keyed.png',
  })
  apiMocks.createMediaResizeTask.mockResolvedValue({
    status: 'queued',
    task_id: 'resize-task',
  })
  apiMocks.createVideoFramesTask.mockResolvedValue({
    status: 'queued',
    task_id: 'frames-task',
  })
  apiMocks.deleteUploadedAsset.mockResolvedValue(undefined)
  apiMocks.inspectChromaImage.mockResolvedValue({
    height: 400,
    input_path: oldImageReference,
    preview_height: 200,
    preview_url: '/api/previews/input.png',
    preview_width: 400,
    width: 800,
  })
  apiMocks.sampleChromaImage.mockResolvedValue({
    hex: '#2AB448',
    rgb: [42, 180, 72],
    source_x: 400,
    source_y: 200,
  })
  useTaskStore.getState().resetTask()
  useLogStore.getState().clearEvents()
})

describe('hosted replacement uploads', () => {
  it('blocks video frame submission until the new asset replaces the old reference', async () => {
    const replacement = deferred<readonly string[]>()
    apiMocks.uploadFiles
      .mockResolvedValueOnce([oldVideoReference])
      .mockReturnValueOnce(replacement.promise)
    const { container } = render(<VideoFramesPage />)

    upload(container, new File(['old'], 'old.mp4', { type: 'video/mp4' }))
    await waitFor(() => expect(screen.getByLabelText('输入视频')).toHaveValue('old.mp4'))

    upload(container, new File(['new'], 'new.mp4', { type: 'video/mp4' }))
    const submit = screen.getByRole('button', { name: '开始导出' })
    expect(submit).toBeDisabled()
    forceSubmit(submit)
    expect(apiMocks.createVideoFramesTask).not.toHaveBeenCalled()

    await act(async () => {
      replacement.resolve([newVideoReference])
      await replacement.promise
    })

    expect(screen.getByLabelText('输入视频')).toHaveValue('new.mp4')
    expect(apiMocks.deleteUploadedAsset).toHaveBeenCalledWith(oldVideoReference)
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    await waitFor(() =>
      expect(apiMocks.createVideoFramesTask).toHaveBeenCalledWith(
        expect.objectContaining({ input_path: newVideoReference }),
      ),
    )
    expect(apiMocks.createVideoFramesTask.mock.calls[0]?.[0]).not.toHaveProperty('input_paths')
  })

  it('keeps video frame upload pending accurate through StrictMode effect replay', async () => {
    const replacement = deferred<readonly string[]>()
    apiMocks.uploadFiles
      .mockResolvedValueOnce([oldVideoReference])
      .mockReturnValueOnce(replacement.promise)
    const { container } = render(
      <StrictMode>
        <VideoFramesPage />
      </StrictMode>,
    )

    upload(container, new File(['old'], 'old.mp4', { type: 'video/mp4' }))
    await waitFor(() => expect(screen.getByLabelText('输入视频')).toHaveValue('old.mp4'))
    upload(container, new File(['new'], 'new.mp4', { type: 'video/mp4' }))

    expect(screen.getByRole('button', { name: '开始导出' })).toBeDisabled()

    await act(async () => {
      replacement.resolve([newVideoReference])
      await replacement.promise
    })
    expect(screen.getByRole('button', { name: '开始导出' })).toBeEnabled()
  })

  it('blocks media resize submission until the new asset replaces the old reference', async () => {
    const replacement = deferred<readonly string[]>()
    apiMocks.uploadFiles
      .mockResolvedValueOnce([oldImageReference])
      .mockReturnValueOnce(replacement.promise)
    const { container } = render(<MediaResizePage />)
    fireEvent.change(screen.getByLabelText('目标尺寸'), { target: { value: '250x250' } })

    upload(container, new File(['old'], 'old.png', { type: 'image/png' }))
    await waitFor(() => expect(screen.getByLabelText('输入媒体')).toHaveValue('old.png'))

    upload(container, new File(['new'], 'new.png', { type: 'image/png' }))
    const submit = screen.getByRole('button', { name: '开始调整' })
    expect(submit).toBeDisabled()
    forceSubmit(submit)
    expect(apiMocks.createMediaResizeTask).not.toHaveBeenCalled()

    await act(async () => {
      replacement.resolve([newImageReference])
      await replacement.promise
    })

    expect(screen.getByLabelText('输入媒体')).toHaveValue('new.png')
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    await waitFor(() =>
      expect(apiMocks.createMediaResizeTask).toHaveBeenCalledWith({
        input_path: newImageReference,
        output_path: undefined,
        size: '250x250',
      }),
    )
  })

  it('restores media resize submission with the old asset after replacement upload failure', async () => {
    const replacement = deferred<readonly string[]>()
    apiMocks.uploadFiles
      .mockResolvedValueOnce([oldImageReference])
      .mockReturnValueOnce(replacement.promise)
    const { container } = render(<MediaResizePage />)
    fireEvent.change(screen.getByLabelText('目标尺寸'), { target: { value: '250x250' } })

    upload(container, new File(['old'], 'old.png', { type: 'image/png' }))
    await waitFor(() => expect(screen.getByLabelText('输入媒体')).toHaveValue('old.png'))
    upload(container, new File(['broken'], 'broken.png', { type: 'image/png' }))

    await act(async () => {
      replacement.reject(new Error('上传失败'))
      await replacement.promise.catch(() => undefined)
    })

    const submit = screen.getByRole('button', { name: '开始调整' })
    expect(screen.getByLabelText('输入媒体')).toHaveValue('old.png')
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    await waitFor(() =>
      expect(apiMocks.createMediaResizeTask).toHaveBeenCalledWith(
        expect.objectContaining({ input_path: oldImageReference }),
      ),
    )
  })

  it('blocks chroma export until the new asset is inspected and sampled', async () => {
    const replacement = deferred<readonly string[]>()
    apiMocks.uploadFiles
      .mockResolvedValueOnce([oldImageReference])
      .mockReturnValueOnce(replacement.promise)
    const { container } = render(<ChromaKeyerPage />)

    upload(container, new File(['old'], 'old.png', { type: 'image/png' }))
    await waitFor(() =>
      expect(apiMocks.inspectChromaImage).toHaveBeenCalledWith({
        inputPath: oldImageReference,
      }),
    )
    await sampleCurrentImage()
    const submit = await screen.findByRole('button', { name: '导出 PNG' })
    await waitFor(() => expect(submit).toBeEnabled())

    upload(container, new File(['new'], 'new.png', { type: 'image/png' }))
    expect(submit).toBeDisabled()
    expect(screen.getByRole('button', { name: '抠像预览' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    forceSubmit(submit)
    expect(apiMocks.createChromaKeyerTask).not.toHaveBeenCalled()

    await act(async () => {
      replacement.resolve([newImageReference])
      await replacement.promise
    })
    await waitFor(() =>
      expect(apiMocks.inspectChromaImage).toHaveBeenLastCalledWith({
        inputPath: newImageReference,
      }),
    )
    expect(screen.getByLabelText('输入图片或视频')).toHaveValue('new.png')
    expect(submit).toBeDisabled()

    await sampleCurrentImage()
    await waitFor(() => expect(submit).toBeEnabled())
    fireEvent.click(submit)

    await waitFor(() =>
      expect(apiMocks.createChromaKeyerTask).toHaveBeenCalledWith(
        expect.objectContaining({ inputPath: newImageReference }),
      ),
    )
  })
})

function deferred<T>() {
  let reject!: (reason?: unknown) => void
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve
    reject = nextReject
  })
  return { promise, reject, resolve }
}

function forceSubmit(button: HTMLElement) {
  expect(button.closest('form')).not.toBeNull()
  fireEvent.submit(button.closest('form') as HTMLFormElement)
}

async function sampleCurrentImage() {
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
  fireEvent.click(image, { clientX: 210, clientY: 120 })
  await waitFor(() => expect(screen.getByText('#2AB448')).toBeInTheDocument())
}

function upload(container: HTMLElement, file: File) {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')
  if (!input) {
    throw new Error('upload input missing')
  }
  fireEvent.change(input, { target: { files: [file] } })
}
