import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MediaResizeForm } from './MediaResizeForm'

const apiMocks = vi.hoisted(() => ({
  importDroppedFiles: vi.fn(),
  pickLocalFiles: vi.fn(),
  pickLocalSaveFile: vi.fn(),
  resolveDroppedFilePaths: vi.fn(),
}))

vi.mock('../../services/apiClient', () => ({
  importDroppedFiles: apiMocks.importDroppedFiles,
  pickLocalFiles: apiMocks.pickLocalFiles,
  pickLocalSaveFile: apiMocks.pickLocalSaveFile,
  resolveDroppedFilePaths: apiMocks.resolveDroppedFilePaths,
}))

beforeEach(() => {
  apiMocks.importDroppedFiles.mockReset()
  apiMocks.pickLocalFiles.mockReset()
  apiMocks.pickLocalSaveFile.mockReset()
  apiMocks.resolveDroppedFilePaths.mockReset()
})

describe('MediaResizeForm', () => {
  it('keeps local import pending ahead of submit and preserves existing input on import error', async () => {
    apiMocks.resolveDroppedFilePaths.mockResolvedValue({ paths: [] })
    let rejectImport!: (error: Error) => void
    apiMocks.importDroppedFiles.mockReturnValue(new Promise<readonly string[]>((_resolve, reject) => { rejectImport = reject }))
    const onSubmit = vi.fn()
    const { container } = render(<MediaResizeForm disabled={false} onSubmit={onSubmit} />)
    const input = screen.getByLabelText('输入媒体路径')
    fireEvent.change(input, { target: { value: 'D:\\old\\original.png' } })
    const file = new File(['actual'], 'outside.png', { type: 'image/png' })
    fireEvent.drop(input, { dataTransfer: { files: [file], getData: () => '' } })
    expect(apiMocks.importDroppedFiles).toHaveBeenCalledWith([file], expect.any(Function), expect.any(AbortSignal))
    expect(screen.getByRole('button', { name: '开始调整' })).toBeDisabled()
    fireEvent.submit(container.querySelector('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
    await act(async () => rejectImport(new Error('Local import failed')))
    expect(screen.getByText('Local import failed')).toBeInTheDocument()
    expect(input).toHaveValue('D:\\old\\original.png')
    expect(screen.getByRole('button', { name: '开始调整' })).not.toBeDisabled()
    expect(apiMocks.resolveDroppedFilePaths).not.toHaveBeenCalled()
  })
  it('renders media resize fields', () => {
    render(<MediaResizeForm disabled={false} onSubmit={vi.fn()} />)

    expect(screen.getByLabelText('输入媒体路径')).toBeInTheDocument()
    expect(screen.getByLabelText('输出路径')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '指定输入媒体路径' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '指定输出路径' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始调整' })).toBeInTheDocument()
  })

  it('uses a multiline media input that appends native picker selections', async () => {
    const user = userEvent.setup()
    apiMocks.pickLocalFiles.mockResolvedValue({
      paths: ['C:\\media\\photo-a.png', 'D:\\media\\clip-b.mp4'],
      cancelled: false,
    })

    render(<MediaResizeForm disabled={false} onSubmit={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() =>
      expect(screen.getByLabelText('输入媒体路径')).toHaveValue('C:\\media\\photo-a.png\nD:\\media\\clip-b.mp4'),
    )
    expect(apiMocks.pickLocalFiles).toHaveBeenCalledWith({
      fileKind: 'media',
      multiple: true,
      title: '选择媒体文件',
    })
  })

  it('disables the single output path when multiple media inputs are entered', async () => {
    const user = userEvent.setup()

    render(<MediaResizeForm disabled={false} onSubmit={vi.fn()} />)

    await user.type(screen.getByLabelText('输出路径'), 'C:\\media\\small.png')
    await user.type(screen.getByLabelText('输入媒体路径'), 'C:\\media\\photo-a.png\nD:\\media\\clip-b.mp4')

    await waitFor(() => expect(screen.getByLabelText('输出路径')).toBeDisabled())
    expect(screen.getByLabelText('输出路径')).toHaveValue('')
    expect(screen.getByRole('button', { name: '指定输出路径' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })
})
