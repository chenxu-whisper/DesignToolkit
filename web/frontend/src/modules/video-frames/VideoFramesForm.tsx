import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { FieldError } from '../../components/FieldError'
import { PathInput } from '../../components/PathInput'
import { SizeInput } from '../../components/SizeInput'
import { SubmitButton } from '../../components/SubmitButton'
import { hostedMode } from '../../app/runtimeConfig'
import {
  pickLocalDirectory,
  pickLocalFiles,
  resolveDroppedFilePaths,
  uploadFiles,
} from '../../services/apiClient'
import { createVideoFramesDefaultValues } from './videoFramesDefaults'
import {
  validateVideoFramesValues,
  type VideoFramesErrors,
  type VideoFramesValues,
} from './videoFramesValidation'

type VideoFramesFormProps = {
  disabled: boolean
  onSubmit: (values: VideoFramesValues) => Promise<void> | void
}

export function VideoFramesForm({ disabled, onSubmit }: VideoFramesFormProps) {
  const [values, setValues] = useState<VideoFramesValues>(() => createVideoFramesDefaultValues())
  const [errors, setErrors] = useState<VideoFramesErrors>({})
  const [isSubmitting, setIsSubmitting] = useState(false)
  const mountedRef = useRef(true)
  const submittingRef = useRef(false)
  const formDisabled = disabled || isSubmitting

  useEffect(() => {
    return () => {
      mountedRef.current = false
    }
  }, [])

  const updateValue = <Key extends keyof VideoFramesValues>(key: Key, value: VideoFramesValues[Key]) => {
    setValues((current) => ({ ...current, [key]: value }))
    setErrors((current) => ({ ...current, [key]: undefined }))
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (disabled || submittingRef.current) {
      return
    }

    const nextErrors = validateVideoFramesValues(values)
    setErrors(nextErrors)

    if (Object.keys(nextErrors).length > 0) {
      return
    }

    submittingRef.current = true
    setIsSubmitting(true)
    try {
      await onSubmit(values)
    } finally {
      submittingRef.current = false
      if (mountedRef.current) {
        setIsSubmitting(false)
      }
    }
  }

  return (
    <form className="module-form" onSubmit={handleSubmit}>
      <PathInput
        disabled={formDisabled}
        error={errors.inputPath}
        id="video-frames-input-path"
        label={hostedMode ? '输入视频' : '输入视频路径'}
        multiline
        onChange={(value) => updateValue('inputPath', value)}
        pickPaths={async () => {
          const response = await pickLocalFiles({
            fileKind: 'video',
            multiple: true,
            title: '选择视频文件',
          })
          return response.paths
        }}
        placeholder="每行一个视频路径"
        required
        resolveDroppedFilePaths={async (names) => {
          const response = await resolveDroppedFilePaths(names)
          return response.paths
        }}
        rows={4}
        uploadAccept="video/*,.mkv,.m4v"
        uploadFiles={hostedMode ? uploadFiles : undefined}
        value={values.inputPath}
      />
      {!hostedMode ? (
        <PathInput
          disabled={formDisabled}
          error={errors.outputDir}
          id="video-frames-output-dir"
          label="输出目录"
          onChange={(value) => updateValue('outputDir', value)}
          pickPaths={async () => {
            const response = await pickLocalDirectory({ title: '选择输出目录' })
            return response.paths
          }}
          value={values.outputDir}
        />
      ) : null}

      <div className="form-row">
        <div className="field">
          <label className="field-label" htmlFor="video-frames-image-format">
            图片格式
          </label>
          <select
            className="text-input"
            disabled={formDisabled}
            id="video-frames-image-format"
            onChange={(event) => updateValue('imageFormat', event.target.value as VideoFramesValues['imageFormat'])}
            value={values.imageFormat}
          >
            <option value="png">png</option>
            <option value="jpg">jpg</option>
            <option value="jpeg">jpeg</option>
          </select>
        </div>
        <SizeInput
          disabled={formDisabled}
          error={errors.size}
          id="video-frames-size"
          label="输出尺寸（可选）"
          onChange={(value) => updateValue('size', value)}
          value={values.size}
        />
      </div>

      <fieldset className="field-group" disabled={formDisabled}>
        <legend>抽帧方式</legend>
        <label className="choice">
          <input
            checked={values.samplingMode === 'original'}
            name="video-frames-sampling"
            onChange={() => updateValue('samplingMode', 'original')}
            type="radio"
            value="original"
          />
          原始帧率
        </label>
        <label className="choice">
          <input
            checked={values.samplingMode === 'fps'}
            name="video-frames-sampling"
            onChange={() => updateValue('samplingMode', 'fps')}
            type="radio"
            value="fps"
          />
          指定 FPS
        </label>
        <label className="choice">
          <input
            checked={values.samplingMode === 'frames'}
            name="video-frames-sampling"
            onChange={() => updateValue('samplingMode', 'frames')}
            type="radio"
            value="frames"
          />
          指定总帧数
        </label>
      </fieldset>

      {values.samplingMode === 'fps' ? (
        <div className="field">
          <label className="field-label" htmlFor="video-frames-fps">
            FPS
          </label>
          <input
            aria-describedby={errors.fps ? 'video-frames-fps-error' : undefined}
            aria-invalid={errors.fps ? 'true' : undefined}
            className="text-input"
            disabled={formDisabled}
            id="video-frames-fps"
            min="0"
            onChange={(event) => updateValue('fps', event.target.value)}
            step="0.01"
            type="number"
            value={values.fps}
          />
          <FieldError id="video-frames-fps-error" message={errors.fps} />
        </div>
      ) : null}

      {values.samplingMode === 'frames' ? (
        <div className="field">
          <label className="field-label" htmlFor="video-frames-frames">
            总帧数
          </label>
          <input
            aria-describedby={errors.frames ? 'video-frames-frames-error' : undefined}
            aria-invalid={errors.frames ? 'true' : undefined}
            className="text-input"
            disabled={formDisabled}
            id="video-frames-frames"
            min="1"
            onChange={(event) => updateValue('frames', event.target.value)}
            step="1"
            type="number"
            value={values.frames}
          />
          <FieldError id="video-frames-frames-error" message={errors.frames} />
        </div>
      ) : null}

      <label className="choice">
        <input
          checked={values.overwrite}
          disabled={formDisabled}
          onChange={(event) => updateValue('overwrite', event.target.checked)}
          type="checkbox"
        />
        覆盖已有文件
      </label>

      <SubmitButton disabled={formDisabled}>开始导出</SubmitButton>
    </form>
  )
}
