import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { VideoFramesForm } from './VideoFramesForm'

const apiMocks = vi.hoisted(() => ({ importDroppedFiles: vi.fn(), resolveDroppedFilePaths: vi.fn() }))
vi.mock('../../services/apiClient', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../services/apiClient')>(),
  ...apiMocks,
}))

describe('VideoFramesForm', () => {
  it('imports local videos before allowing submission and merges existing paths', async () => {
    apiMocks.resolveDroppedFilePaths.mockResolvedValue({ paths: [] })
    let resolveImport!: (paths: readonly string[]) => void
    apiMocks.importDroppedFiles.mockReturnValue(new Promise<readonly string[]>((resolve) => { resolveImport = resolve }))
    const onSubmit = vi.fn()
    const { container } = render(<VideoFramesForm disabled={false} onSubmit={onSubmit} />)
    const input = screen.getByLabelText('输入视频路径')
    fireEvent.change(input, { target: { value: 'D:\\existing\\old.mp4' } })
    const file = new File(['actual'], 'outside.mp4', { type: 'video/mp4' })
    fireEvent.drop(input, { dataTransfer: { files: [file], getData: () => '' } })
    expect(apiMocks.importDroppedFiles).toHaveBeenCalledWith([file], expect.any(Function), expect.any(AbortSignal))
    expect(screen.getByRole('button', { name: '开始导出' })).toBeDisabled()
    fireEvent.submit(container.querySelector('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
    await act(async () => resolveImport(['E:\\imports\\outside.mp4']))
    expect(input).toHaveValue('D:\\existing\\old.mp4\nE:\\imports\\outside.mp4')
    await act(async () => fireEvent.submit(container.querySelector('form')!))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ inputPath: 'D:\\existing\\old.mp4\nE:\\imports\\outside.mp4' }))
    expect(apiMocks.resolveDroppedFilePaths).not.toHaveBeenCalled()
  })
  it('renders video frame fields', () => {
    render(<VideoFramesForm disabled={false} onSubmit={vi.fn()} />)

    expect(screen.getByLabelText('输入视频路径')).toBeInTheDocument()
    expect(screen.getByLabelText('输出目录')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始导出' })).toBeInTheDocument()
  })

  it('uses a multiline input for batch video paths', () => {
    render(<VideoFramesForm disabled={false} onSubmit={vi.fn()} />)

    expect(screen.getByLabelText('输入视频路径').tagName).toBe('TEXTAREA')
    expect(screen.getByRole('button', { name: '指定输入视频路径' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '指定输出目录' })).toBeInTheDocument()
  })
})
