import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createChromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { ChromaKeyerForm } from './ChromaKeyerForm'
import type { ChromaKeyerErrors, ChromaKeyerValues } from './chromaKeyerValidation'

const apiMocks = vi.hoisted(() => ({
  importDroppedFiles: vi.fn(),
  pickLocalDirectory: vi.fn(),
  pickLocalFiles: vi.fn(),
  pickLocalSaveFile: vi.fn(),
  resolveDroppedFilePaths: vi.fn(),
}))

vi.mock('../../services/apiClient', () => ({
  importDroppedFiles: apiMocks.importDroppedFiles,
  pickLocalDirectory: apiMocks.pickLocalDirectory,
  pickLocalFiles: apiMocks.pickLocalFiles,
  pickLocalSaveFile: apiMocks.pickLocalSaveFile,
  resolveDroppedFilePaths: apiMocks.resolveDroppedFilePaths,
}))

beforeEach(() => {
  apiMocks.importDroppedFiles.mockReset()
  apiMocks.pickLocalDirectory.mockReset()
  apiMocks.pickLocalFiles.mockReset()
  apiMocks.pickLocalSaveFile.mockReset()
  apiMocks.resolveDroppedFilePaths.mockReset()
})

function ControlledForm({
  errors = {},
  initialValues = createChromaKeyerDefaultValues(),
}: {
  errors?: ChromaKeyerErrors
  initialValues?: ChromaKeyerValues
}) {
  const [values, setValues] = useState(initialValues)

  return (
    <ChromaKeyerForm
      disabled={false}
      errors={errors}
      onChange={setValues}
      onInputPathCommit={vi.fn()}
      onSubmit={vi.fn()}
      values={values}
    />
  )
}

describe('ChromaKeyerForm', () => {
  it('preserves Screen Gain edits when a pending local import completes', async () => {
    let resolveImport!: (paths: readonly string[]) => void
    const importResult = new Promise<readonly string[]>((resolve) => {
      resolveImport = resolve
    })
    apiMocks.importDroppedFiles.mockReturnValue(importResult)
    render(<ControlledForm />)
    const input = screen.getByLabelText('输入图片或视频路径')
    const gain = screen.getByRole('spinbutton', { name: 'Screen Gain 数值' })

    expect(gain).toHaveValue(1)
    fireEvent.drop(input, {
      dataTransfer: { files: [new File(['actual'], 'outside.png')], getData: () => '' },
    })
    expect(apiMocks.importDroppedFiles).toHaveBeenCalledOnce()
    fireEvent.change(gain, { target: { value: '1.7' } })
    expect(gain).toHaveValue(1.7)

    await act(async () => {
      resolveImport(['E:\\imports\\outside.png'])
      await importResult
    })

    expect(input).toHaveValue('E:\\imports\\outside.png')
    expect(gain).toHaveValue(1.7)
    expect(screen.getByRole('slider', { name: 'Screen Gain' })).toHaveValue('1.7')
  })

  it('imports actual local File bytes and blocks empty inspect and export until committing the imported path', async () => {
    apiMocks.resolveDroppedFilePaths.mockResolvedValue({ paths: [] })
    let resolveImport!: (paths: readonly string[]) => void
    const importResult = new Promise<readonly string[]>((resolve) => { resolveImport = resolve })
    apiMocks.importDroppedFiles.mockReturnValue(importResult)
    const onInputPathCommit = vi.fn()
    const onSubmit = vi.fn()
    function LocalForm() {
      const [values, setValues] = useState(createChromaKeyerDefaultValues())
      const [pending, setPending] = useState(false)
      return <ChromaKeyerForm disabled={false} errors={{}} isUploadPending={pending} onChange={setValues}
        onInputPathCommit={onInputPathCommit} onSubmit={onSubmit} onUploadPendingChange={setPending} values={values} />
    }
    const { container } = render(<LocalForm />)
    const input = screen.getByLabelText('输入图片或视频路径')
    const file = new File(['actual'], 'outside.png', { type: 'image/png' })
    fireEvent.drop(input, { dataTransfer: { files: [file], getData: () => '' } })
    expect(apiMocks.importDroppedFiles).toHaveBeenCalledWith([file], expect.any(Function), expect.any(AbortSignal))
    fireEvent.blur(input)
    fireEvent.submit(container.querySelector('form')!)
    expect(onInputPathCommit).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(apiMocks.resolveDroppedFilePaths).not.toHaveBeenCalled()
    await act(async () => resolveImport(['E:\\imports\\outside.png']))
    expect(input).toHaveValue('E:\\imports\\outside.png')
    expect(onInputPathCommit).toHaveBeenCalledOnce()
    expect(onInputPathCommit).toHaveBeenCalledWith('E:\\imports\\outside.png')
  })
  it('uses the native picker for one local media path without adding a manual load button', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onInputPathCommit = vi.fn()
    apiMocks.pickLocalFiles.mockResolvedValue({ cancelled: false, paths: ['E:\\media\\screen.png'] })

    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={onChange}
        onInputPathCommit={onInputPathCommit}
        onSubmit={vi.fn()}
        values={createChromaKeyerDefaultValues()}
      />,
    )

    expect(screen.queryByRole('button', { name: '加载图片' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('输入图片或视频路径')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '指定输入图片或视频路径' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ inputPath: 'E:\\media\\screen.png' })))
    expect(onInputPathCommit).toHaveBeenCalledOnce()
    expect(onInputPathCommit).toHaveBeenCalledWith('E:\\media\\screen.png')
    expect(apiMocks.pickLocalFiles).toHaveBeenCalledWith({
      allowedSuffixes: ['.mp4', '.webm', '.mov', '.mkv', '.avi', '.m4v', '.png', '.jpg', '.jpeg'],
      fileKind: 'media',
      multiple: false,
      title: '选择图片或视频文件',
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
        onInputPathCommit={vi.fn()}
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
    expect(screen.queryByLabelText('输出根目录（可选）')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeInTheDocument()
  })

  it('renders a directory output picker and PNG sequence action for video', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const values = {
      ...createChromaKeyerDefaultValues(),
      inputPath: 'E:\\media\\screen.mp4',
      mediaKind: 'video' as const,
      outputFormat: 'png_sequence' as const,
      samples: [[42, 180, 72] as [number, number, number]],
    }
    apiMocks.pickLocalDirectory.mockResolvedValue({ cancelled: false, paths: ['E:\\exports'] })

    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={onChange}
        onInputPathCommit={vi.fn()}
        onSubmit={vi.fn()}
        values={values}
      />,
    )

    expect(screen.queryByLabelText('输出路径（可选）')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '指定输出根目录（可选）' }))

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ outputRoot: 'E:\\exports' })),
    )
    expect(apiMocks.pickLocalDirectory).toHaveBeenCalledWith({ title: '选择 PNG 序列输出根目录' })
    expect(screen.getByRole('button', { name: '导出 PNG 序列' })).toBeEnabled()
  })

  it('defaults video to MP4 with an accessible format selector and save-file picker', async () => {
    const user = userEvent.setup()
    apiMocks.pickLocalSaveFile.mockResolvedValue({ cancelled: false, paths: ['E:\\exports\\packed.mp4'] })
    render(<ControlledForm initialValues={{
      ...createChromaKeyerDefaultValues(),
      mediaKind: 'video',
      samples: [[42, 180, 72]],
    }} />)

    expect(screen.getByRole('combobox', { name: '输出格式' })).toHaveValue('packed_alpha_mp4')
    expect(screen.getByRole('combobox', { name: '输出格式' })).toHaveAccessibleDescription(
      '需要配套播放器恢复透明效果。普通播放器会显示颜色与透明遮罩的左右拼接画面。',
    )
    expect(screen.getByRole('option', { name: 'MP4（颜色 + 透明遮罩）' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导出 MP4' })).toBeEnabled()
    expect(screen.queryByLabelText('输出根目录（可选）')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '指定MP4 输出路径（可选）' }))
    await waitFor(() => expect(screen.getByLabelText('MP4 输出路径（可选）')).toHaveValue('E:\\exports\\packed.mp4'))
    expect(apiMocks.pickLocalSaveFile).toHaveBeenCalledWith({
      fileKind: 'video',
      title: '选择 MP4 输出路径',
    })

    await user.selectOptions(screen.getByRole('combobox', { name: '输出格式' }), 'png_sequence')
    expect(screen.getByLabelText('输出根目录（可选）')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导出 PNG 序列' })).toBeEnabled()
    expect(screen.queryByText(/需要配套播放器/)).not.toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: '输出格式' }), 'packed_alpha_mp4')
    expect(screen.getByLabelText('MP4 输出路径（可选）')).toHaveValue('E:\\exports\\packed.mp4')
  })

  it('does not expose video formats for images', () => {
    render(<ControlledForm initialValues={{ ...createChromaKeyerDefaultValues(), mediaKind: 'image' }} />)
    expect(screen.queryByRole('combobox', { name: '输出格式' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeInTheDocument()
  })

  it('renders a bounded slider and compact number input for every chroma parameter', () => {
    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={vi.fn()}
        onInputPathCommit={vi.fn()}
        onSubmit={vi.fn()}
        values={createChromaKeyerDefaultValues()}
      />,
    )

    const parameters = [
      ['Screen Gain', 1, '0', '2', '0.01'],
      ['Screen Balance', 0.5, '0', '1', '0.01'],
      ['Clip Black', 0, '0', '1', '0.01'],
      ['Clip White', 1, '0', '1', '0.01'],
      ['Shrink/Grow', 0, '-10', '10', '1'],
      ['Feather', 0, '0', '10', '1'],
      ['Despill Amount', 1, '0', '1', '0.01'],
    ] as const

    for (const [label, value, min, max, step] of parameters) {
      const slider = screen.getByRole('slider', { name: label })
      const number = screen.getByRole('spinbutton', { name: `${label} 数值` })

      expect(screen.getByLabelText(label)).toBe(slider)
      expect(screen.getByLabelText(label)).toHaveAttribute('type', 'range')
      expect(slider).toHaveValue(String(value))
      expect(number).toHaveValue(value)
      for (const control of [slider, number]) {
        expect(control).toHaveAttribute('min', min)
        expect(control).toHaveAttribute('max', max)
        expect(control).toHaveAttribute('step', step)
      }
    }

    expect(screen.queryByLabelText('Tolerance')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Softness')).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Final' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Alpha' })).not.toBeChecked()
  })

  it('keeps slider and number values synchronized through controlled rerenders', () => {
    render(<ControlledForm />)

    const slider = screen.getByRole('slider', { name: 'Screen Gain' })
    const number = screen.getByRole('spinbutton', { name: 'Screen Gain 数值' })

    fireEvent.change(slider, { target: { value: '1.37' } })
    expect(slider).toHaveValue('1.37')
    expect(number).toHaveValue(1.37)

    fireEvent.change(number, { target: { value: '0.83' } })
    expect(number).toHaveValue(0.83)
    expect(slider).toHaveValue('0.83')
  })

  it('preserves a cleared numeric parameter as invalid for page validation', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()

    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={onChange}
        onInputPathCommit={vi.fn()}
        onSubmit={vi.fn()}
        values={createChromaKeyerDefaultValues()}
      />,
    )

    await user.clear(screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }))

    expect(onChange.mock.lastCall?.[0].screenGain).toBeNaN()
  })

  it('uses bounded defaults for sliders while cleared model values remain invalid', () => {
    const onChange = vi.fn()
    const values = createChromaKeyerDefaultValues()
    const invalidValues = {
      ...values,
      clipBlack: Number.NaN,
      clipWhite: Number.NaN,
      despillAmount: Number.NaN,
      feather: Number.NaN,
      screenBalance: Number.NaN,
      screenGain: Number.NaN,
      shrinkGrow: Number.NaN,
    }

    render(
      <ChromaKeyerForm
        disabled={false}
        errors={{}}
        onChange={onChange}
        onInputPathCommit={vi.fn()}
        onSubmit={vi.fn()}
        values={invalidValues}
      />,
    )

    const fallbacks = [
      ['Screen Gain', 1],
      ['Screen Balance', 0.5],
      ['Clip Black', 0],
      ['Clip White', 1],
      ['Shrink/Grow', 0],
      ['Feather', 0],
      ['Despill Amount', 1],
    ] as const

    for (const [label, fallback] of fallbacks) {
      expect(screen.getByRole('slider', { name: label })).toHaveValue(String(fallback))
      expect(screen.getByRole('spinbutton', { name: `${label} 数值` })).toHaveValue(null)
    }
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.change(screen.getByRole('slider', { name: 'Screen Gain' }), { target: { value: '1.25' } })
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ screenGain: 1.25 }))
  })

  it('connects parameter errors to both synchronized controls', () => {
    const message = 'Screen Gain 必须是 0 到 2 之间的有限数值'

    render(<ControlledForm errors={{ screenGain: message }} />)

    for (const control of [
      screen.getByRole('slider', { name: 'Screen Gain' }),
      screen.getByRole('spinbutton', { name: 'Screen Gain 数值' }),
    ]) {
      expect(control).toHaveAttribute('aria-invalid', 'true')
      expect(control).toHaveAccessibleDescription(message)
    }
  })
})
