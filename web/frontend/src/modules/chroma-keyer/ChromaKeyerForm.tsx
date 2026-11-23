import type { FormEvent } from 'react'
import { ImageUp, SlidersHorizontal } from 'lucide-react'
import { hostedMode } from '../../app/runtimeConfig'
import { FieldError } from '../../components/FieldError'
import { PathInput } from '../../components/PathInput'
import {
  deleteUploadedAsset,
  importDroppedFiles,
  pickLocalDirectory,
  pickLocalFiles,
  pickLocalSaveFile,
  resolveDroppedFilePaths,
  uploadFiles,
} from '../../services/apiClient'
import type { ChromaVideoOutputFormat } from '../../services/apiClient'
import type { ChromaKeyerErrors, ChromaKeyerValues } from './chromaKeyerValidation'

type ChromaKeyerFormProps = {
  disabled: boolean
  errors: ChromaKeyerErrors
  isUploadPending?: boolean
  onChange: (values: ChromaKeyerValues) => void
  onInputPathCommit: (path: string) => void
  onSubmit: () => void
  onUploadPendingChange?: (pending: boolean) => void
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

const chromaInputAllowedSuffixes = [
  '.mp4',
  '.webm',
  '.mov',
  '.mkv',
  '.avi',
  '.m4v',
  '.png',
  '.jpg',
  '.jpeg',
] as const

function getSliderValue(value: number, min: number, max: number, fallback: number) {
  return Math.min(max, Math.max(min, Number.isFinite(value) ? value : fallback))
}

export function ChromaKeyerForm({
  disabled,
  errors,
  isUploadPending = false,
  onChange,
  onInputPathCommit,
  onSubmit,
  onUploadPendingChange,
  values,
}: ChromaKeyerFormProps) {
  const isSourceAlpha = values.mediaKind === 'video' && values.alphaSource === 'source'
  const updateValue = <Key extends keyof ChromaKeyerValues>(key: Key, value: ChromaKeyerValues[Key]) => {
    onChange({ ...values, [key]: value })
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (disabled || isUploadPending) {
      return
    }
    onSubmit()
  }

  return (
    <form className="chroma-keyer-form" onSubmit={handleSubmit}>
      <div className="chroma-form-section">
        <h2 className="chroma-section-title">
          <ImageUp aria-hidden="true" size={17} />
          媒体
        </h2>
        <PathInput
          deleteUploadedAsset={hostedMode ? deleteUploadedAsset : undefined}
          disabled={disabled}
          error={errors.inputPath}
          id="chroma-keyer-input-path"
          importDroppedFiles={hostedMode ? undefined : importDroppedFiles}
          label={hostedMode ? '输入图片或视频' : '输入图片或视频路径'}
          onChange={(value) => updateValue('inputPath', value)}
          onCommit={onInputPathCommit}
          onUploadPendingChange={onUploadPendingChange}
          pickPaths={async () => {
            const response = await pickLocalFiles({
              allowedSuffixes: chromaInputAllowedSuffixes,
              fileKind: 'media',
              multiple: false,
              title: '选择图片或视频文件',
            })
            return response.paths
          }}
          placeholder="PNG、JPG、JPEG 或视频本地路径"
          required
          resolveDroppedFilePaths={async (names) => {
            const response = await resolveDroppedFilePaths(names)
            return response.paths
          }}
          uploadAccept={chromaInputAllowedSuffixes.join(',')}
          uploadFiles={hostedMode ? uploadFiles : undefined}
          value={values.inputPath}
        />
      </div>

      {values.mediaKind === 'video' ? (
        <div className="chroma-form-section">
          <h2 className="chroma-section-title">透明来源</h2>
          <div className="field">
            <label className="field-label" htmlFor="chroma-keyer-alpha-source">透明来源</label>
            <select
              className="text-input"
              disabled={disabled}
              id="chroma-keyer-alpha-source"
              onChange={(event) => onChange({
                ...values,
                alphaSource: event.target.value as ChromaKeyerValues['alphaSource'],
                ...(event.target.value === 'source' ? { outputFormat: 'packed_alpha_mp4' as const } : {}),
              })}
              value={values.alphaSource ?? 'chroma_key'}
            >
              <option value="chroma_key">色键抠图</option>
              <option value="source">视频原始透明通道</option>
            </select>
          </div>
          {isSourceAlpha ? (
            <>
              <div className="field">
                <label className="field-label" htmlFor="chroma-keyer-source-alpha-mode">Alpha 解释</label>
                <select
                  aria-describedby="chroma-keyer-source-alpha-help"
                  className="text-input"
                  disabled={disabled}
                  id="chroma-keyer-source-alpha-mode"
                  onChange={(event) => updateValue('sourceAlphaMode', event.target.value as ChromaKeyerValues['sourceAlphaMode'])}
                  value={values.sourceAlphaMode ?? 'straight'}
                >
                  <option value="straight">直通 Alpha</option>
                  <option value="premultiplied">预乘 Alpha</option>
                </select>
              </div>
              <p className="chroma-format-help" id="chroma-keyer-source-alpha-help">
                保留视频原始透明通道，无需背景采样或抠像参数。请按素材选择直通或预乘 Alpha；无原生 Alpha 的视频无法转换。
              </p>
              <p className="chroma-format-help">
                MP4 颜色与遮罩有损压缩，Alpha 转为 8 位；使用配套播放器恢复透明效果。
              </p>
            </>
          ) : null}
        </div>
      ) : null}

      {!isSourceAlpha ? (
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
      </div>
      ) : null}

      <div className="chroma-form-section">
        <h2 className="chroma-section-title">预览</h2>
        <fieldset className="chroma-view-mode" disabled={disabled}>
          <legend>通道</legend>
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

      {!hostedMode || values.mediaKind === 'video' ? (
        <div className="chroma-form-section">
          <h2 className="chroma-section-title">输出</h2>
          {values.mediaKind === 'video' ? (
            <div className="field">
              <label className="field-label" htmlFor="chroma-keyer-output-format">输出格式</label>
              <select
                aria-describedby={values.outputFormat === 'packed_alpha_mp4' ? 'chroma-keyer-format-help' : undefined}
                className="text-input"
                disabled={disabled || isSourceAlpha}
                id="chroma-keyer-output-format"
                onChange={(event) => updateValue('outputFormat', event.target.value as ChromaVideoOutputFormat)}
                value={values.outputFormat}
              >
                <option value="packed_alpha_mp4">MP4（颜色 + 透明遮罩）</option>
                <option value="png_sequence">PNG 序列</option>
              </select>
              <FieldError message={errors.outputFormat} />
              {values.outputFormat === 'packed_alpha_mp4' ? (
                <p className="chroma-format-help" id="chroma-keyer-format-help">
                  需要配套播放器恢复透明效果。普通播放器会显示颜色与透明遮罩的左右拼接画面。
                </p>
              ) : null}
            </div>
          ) : null}
          {hostedMode ? null : values.mediaKind === 'video' && values.outputFormat === 'png_sequence' ? (
            <PathInput
              disabled={disabled}
              error={errors.outputRoot}
              id="chroma-keyer-output-root"
              label="输出根目录（可选）"
              onChange={(value) => updateValue('outputRoot', value)}
              pickPaths={async () => {
                const response = await pickLocalDirectory({ title: '选择 PNG 序列输出根目录' })
                return response.paths
              }}
              placeholder="留空时在视频旁创建序列目录"
              value={values.outputRoot}
            />
          ) : values.mediaKind === 'video' ? (
            <PathInput
              disabled={disabled}
              error={errors.outputPath}
              id="chroma-keyer-output-path"
              label="MP4 输出路径（可选）"
              onChange={(value) => updateValue('outputPath', value)}
              pickPaths={async () => {
                const response = await pickLocalSaveFile({
                  fileKind: 'video',
                  title: '选择 MP4 输出路径',
                })
                return response.paths
              }}
              placeholder="留空时自动生成 _keyed_packed.mp4"
              value={values.outputPath}
            />
          ) : (
            <PathInput
              disabled={disabled}
              error={errors.outputPath}
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
          )}
        </div>
      ) : null}
      <div className="chroma-inspector-footer">
        <button
          className="submit-button"
          disabled={disabled || isUploadPending || (!isSourceAlpha && values.samples.length === 0)}
          type="submit"
        >
          {values.mediaKind === 'video'
            ? values.outputFormat === 'packed_alpha_mp4' ? '导出 MP4' : '导出 PNG 序列'
            : '导出 PNG'}
        </button>
      </div>
    </form>
  )
}
