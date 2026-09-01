import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MediaResizeForm } from './MediaResizeForm'

const apiMocks = vi.hoisted(() => ({
  pickLocalFiles: vi.fn(),
  pickLocalSaveFile: vi.fn(),
  resolveDroppedFilePaths: vi.fn(),
}))

vi.mock('../../services/apiClient', () => ({
  pickLocalFiles: apiMocks.pickLocalFiles,
  pickLocalSaveFile: apiMocks.pickLocalSaveFile,
  resolveDroppedFilePaths: apiMocks.resolveDroppedFilePaths,
}))

beforeEach(() => {
  apiMocks.pickLocalFiles.mockReset()
  apiMocks.pickLocalSaveFile.mockReset()
  apiMocks.resolveDroppedFilePaths.mockReset()
})

describe('MediaResizeForm', () => {
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
