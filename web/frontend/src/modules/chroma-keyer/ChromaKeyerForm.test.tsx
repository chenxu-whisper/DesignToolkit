import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createChromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { ChromaKeyerForm } from './ChromaKeyerForm'

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

describe('ChromaKeyerForm', () => {
  it('uses the native picker for one local image path', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    apiMocks.pickLocalFiles.mockResolvedValue({ cancelled: false, paths: ['E:\\media\\screen.png'] })

    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={onChange}
        onInspect={vi.fn()}
        onSubmit={vi.fn()}
        values={createChromaKeyerDefaultValues()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入图片路径' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ inputPath: 'E:\\media\\screen.png' })))
    expect(apiMocks.pickLocalFiles).toHaveBeenCalledWith({
      fileKind: 'image',
      multiple: false,
      title: '选择图片文件',
    })
  })

  it('uses the native picker for a PNG output path', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    apiMocks.pickLocalSaveFile.mockResolvedValue({ cancelled: false, paths: ['E:\\media\\screen_keyed.png'] })

    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={onChange}
        onInspect={vi.fn()}
        onSubmit={vi.fn()}
        values={createChromaKeyerDefaultValues()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输出路径（可选）' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ outputPath: 'E:\\media\\screen_keyed.png' })))
    expect(apiMocks.pickLocalSaveFile).toHaveBeenCalledWith({
      fileKind: 'image',
      title: '选择 PNG 输出路径',
    })
  })

  it('renders compact controls for every chroma validation parameter', () => {
    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={vi.fn()}
        onInspect={vi.fn()}
        onSubmit={vi.fn()}
        values={createChromaKeyerDefaultValues()}
      />,
    )

    expect(screen.getByLabelText('Screen Gain')).toHaveValue(1)
    expect(screen.getByLabelText('Screen Gain')).toHaveAttribute('max', '2')
    expect(screen.getByLabelText('Screen Gain')).toHaveAttribute('step', '0.01')
    expect(screen.getByLabelText('Screen Balance')).toHaveValue(0.5)
    expect(screen.getByLabelText('Screen Balance')).toHaveAttribute('max', '1')
    expect(screen.getByLabelText('Screen Balance')).toHaveAttribute('step', '0.01')
    expect(screen.queryByLabelText('Tolerance')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Softness')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Clip Black')).toHaveValue(0)
    expect(screen.getByLabelText('Clip White')).toHaveValue(1)
    expect(screen.getByLabelText('Shrink/Grow')).toHaveValue(0)
    expect(screen.getByLabelText('Feather')).toHaveValue(0)
    expect(screen.getByLabelText('Despill Amount')).toHaveValue(1)
    expect(screen.getByRole('radio', { name: 'Final' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Alpha' })).not.toBeChecked()
  })

  it('preserves a cleared numeric parameter as invalid for page validation', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()

    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={onChange}
        onInspect={vi.fn()}
        onSubmit={vi.fn()}
        values={createChromaKeyerDefaultValues()}
      />,
    )

    await user.clear(screen.getByLabelText('Screen Gain'))

    expect(onChange.mock.lastCall?.[0].screenGain).toBeNaN()
  })
})
