import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createChromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { ChromaKeyerForm } from './ChromaKeyerForm'
import type { ChromaKeyerErrors, ChromaKeyerValues } from './chromaKeyerValidation'

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
  it('uses the native picker for one local image path', async () => {
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
    await user.click(screen.getByRole('button', { name: '指定输入图片路径' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ inputPath: 'E:\\media\\screen.png' })))
    expect(onInputPathCommit).toHaveBeenCalledOnce()
    expect(onInputPathCommit).toHaveBeenCalledWith('E:\\media\\screen.png')
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

      expect(screen.getByLabelText(label)).toBe(number)
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
