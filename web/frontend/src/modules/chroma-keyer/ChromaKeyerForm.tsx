import type { FormEvent } from 'react'
import { ImageUp, SlidersHorizontal } from 'lucide-react'
import { FieldError } from '../../components/FieldError'
import { PathInput } from '../../components/PathInput'
import { pickLocalFiles, pickLocalSaveFile, resolveDroppedFilePaths } from '../../services/apiClient'
import type { ChromaKeyerErrors, ChromaKeyerValues } from './chromaKeyerValidation'

type ChromaKeyerFormProps = {
  disabled: boolean
  errors: ChromaKeyerErrors
  onChange: (values: ChromaKeyerValues) => void
  onInputPathCommit: (path: string) => void
  onSubmit: () => void
  values: ChromaKeyerValues
}

const parameterFields = [
  { fallback: 1, id: 'screenGain', label: 'Screen Gain', max: 2, min: 0, step: 0.01 },
  { fallback: 0.5, id: 'screenBalance', label: 'Screen Balance', max: 1, min: 0, step: 0.01 },
  { fallback: 0, id: 'clipBlack', label: 'Clip Black', max: 1, min: 0, step: 0.01 },
  { fallback: 1, id: 'clipWhite', label: 'Clip White', max: 1, min: 0, step: 0.01 },
  { fallback: 0, id: 'shrinkGrow', label: 'Shrink/Grow', max: 10, min: -10, step: 1 },
  { fallback: 0, id: 'feather', label: 'Feather', max: 10, min: 0, step: 1 },
  { fallback: 1, id: 'despillAmount', label: 'Despill Amount', max: 1, min: 0, step: 0.01 },
] as const

function getSliderValue(value: number, min: number, max: number, fallback: number) {
  return Math.min(max, Math.max(min, Number.isFinite(value) ? value : fallback))
}

export function ChromaKeyerForm({
  disabled,
  errors,
  onChange,
  onInputPathCommit,
  onSubmit,
  values,
}: ChromaKeyerFormProps) {
  const updateValue = <Key extends keyof ChromaKeyerValues>(key: Key, value: ChromaKeyerValues[Key]) => {
    onChange({ ...values, [key]: value })
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onSubmit()
  }

  return (
    <form className="chroma-keyer-form" onSubmit={handleSubmit}>
      <div className="chroma-form-section">
        <h2 className="chroma-section-title">
          <ImageUp aria-hidden="true" size={17} />
          图片
        </h2>
        <PathInput
          disabled={disabled}
          error={errors.inputPath}
          id="chroma-keyer-input-path"
          label="输入图片路径"
          onChange={(value) => updateValue('inputPath', value)}
          onCommit={onInputPathCommit}
          pickPaths={async () => {
            const response = await pickLocalFiles({
              fileKind: 'image',
              multiple: false,
              title: '选择图片文件',
            })
            return response.paths
          }}
          placeholder="PNG、JPG 或 JPEG 本地路径"
          required
          resolveDroppedFilePaths={async (names) => {
            const response = await resolveDroppedFilePaths(names)
            return response.paths
          }}
          value={values.inputPath}
        />
      </div>

      <div className="chroma-form-section">
        <h2 className="chroma-section-title">
          <SlidersHorizontal aria-hidden="true" size={17} />
          抠像参数
        </h2>
        <div className="chroma-parameter-grid">
          {parameterFields.map((field) => {
            const errorId = `chroma-keyer-${field.id}-error`
            const hasError = Boolean(errors[field.id])
            const sliderId = `chroma-keyer-${field.id}`

            return (
              <div className="chroma-parameter-field" key={field.id}>
                <div className="chroma-parameter-header">
                  <label className="field-label" htmlFor={sliderId}>
                    {field.label}
                  </label>
                  <input
                    aria-describedby={hasError ? errorId : undefined}
                    aria-invalid={hasError ? 'true' : undefined}
                    aria-label={`${field.label} 数值`}
                    className="text-input chroma-parameter-number"
                    disabled={disabled}
                    id={`${sliderId}-number`}
                    max={field.max}
                    min={field.min}
                    onChange={(event) =>
                      updateValue(field.id, event.target.value === '' ? Number.NaN : Number(event.target.value))
                    }
                    step={field.step}
                    type="number"
                    value={Number.isNaN(values[field.id]) ? '' : values[field.id]}
                  />
                </div>
                <input
                  aria-describedby={hasError ? errorId : undefined}
                  aria-invalid={hasError ? 'true' : undefined}
                  className="chroma-parameter-range"
                  disabled={disabled}
                  id={sliderId}
                  max={field.max}
                  min={field.min}
                  onChange={(event) => updateValue(field.id, Number(event.target.value))}
                  step={field.step}
                  title={field.label}
                  type="range"
                  value={getSliderValue(values[field.id], field.min, field.max, field.fallback)}
                />
                <div className="chroma-parameter-error">
                  <FieldError id={errorId} message={errors[field.id]} />
                </div>
              </div>
            )
          })}
        </div>
        <fieldset className="chroma-view-mode" disabled={disabled}>
          <legend>预览</legend>
          <label className="choice">
            <input
              checked={values.viewMode === 'final'}
              name="chroma-keyer-view-mode"
              onChange={() => updateValue('viewMode', 'final')}
              type="radio"
              value="final"
            />
            Final
          </label>
          <label className="choice">
            <input
              checked={values.viewMode === 'alpha'}
              name="chroma-keyer-view-mode"
              onChange={() => updateValue('viewMode', 'alpha')}
              type="radio"
              value="alpha"
            />
            Alpha
          </label>
        </fieldset>
      </div>

      <PathInput
        disabled={disabled}
        id="chroma-keyer-output-path"
        label="输出路径（可选）"
        onChange={(value) => updateValue('outputPath', value)}
        pickPaths={async () => {
          const response = await pickLocalSaveFile({
            fileKind: 'image',
            title: '选择 PNG 输出路径',
          })
          return response.paths
        }}
        placeholder="留空时自动生成 _keyed.png"
        value={values.outputPath}
      />
      <button className="submit-button" disabled={disabled || values.samples.length === 0} type="submit">
        导出 PNG
      </button>
    </form>
  )
}
