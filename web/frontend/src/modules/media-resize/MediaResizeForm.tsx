import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { hostedMode } from '../../app/runtimeConfig'
import { PathInput } from '../../components/PathInput'
import { SizeInput } from '../../components/SizeInput'
import { SubmitButton } from '../../components/SubmitButton'
import {
  pickLocalFiles,
  pickLocalSaveFile,
  resolveDroppedFilePaths,
  uploadFiles,
} from '../../services/apiClient'
import { createMediaResizeDefaultValues } from './mediaResizeDefaults'
import {
  normalizeMediaResizeInputPaths,
  validateMediaResizeValues,
  type MediaResizeErrors,
  type MediaResizeValues,
} from './mediaResizeValidation'

type MediaResizeFormProps = {
  disabled: boolean
  onSubmit: (values: MediaResizeValues) => Promise<void> | void
}

export function MediaResizeForm({ disabled, onSubmit }: MediaResizeFormProps) {
  const [values, setValues] = useState<MediaResizeValues>(() => createMediaResizeDefaultValues())
  const [errors, setErrors] = useState<MediaResizeErrors>({})
  const [isSubmitting, setIsSubmitting] = useState(false)
  const mountedRef = useRef(true)
  const submittingRef = useRef(false)
  const formDisabled = disabled || isSubmitting
  const isBatchInput = normalizeMediaResizeInputPaths(values.inputPath).length > 1

  useEffect(() => {
    return () => {
      mountedRef.current = false
    }
  }, [])

  const updateValue = <Key extends keyof MediaResizeValues>(key: Key, value: MediaResizeValues[Key]) => {
    setValues((current) => ({ ...current, [key]: value }))
    setErrors((current) => ({ ...current, [key]: undefined }))
  }

  const updateInputPath = (value: string) => {
    const isNextBatchInput = normalizeMediaResizeInputPaths(value).length > 1
    setValues((current) => ({
      ...current,
      inputPath: value,
      outputPath: isNextBatchInput ? '' : current.outputPath,
    }))
    setErrors((current) => ({ ...current, inputPath: undefined, outputPath: undefined }))
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (disabled || submittingRef.current) {
      return
    }

    const nextErrors = validateMediaResizeValues(values)
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
        id="media-resize-input-path"
        label={hostedMode ? '输入媒体' : '输入媒体路径'}
        multiline
        onChange={updateInputPath}
        pickPaths={async () => {
          const response = await pickLocalFiles({
            fileKind: 'media',
            multiple: true,
            title: '选择媒体文件',
          })
          return response.paths
        }}
        placeholder="每行一个媒体路径"
        required
        resolveDroppedFilePaths={async (names) => {
          const response = await resolveDroppedFilePaths(names)
          return response.paths
        }}
        rows={4}
        uploadAccept="image/*,video/*,.mkv,.m4v"
        uploadFiles={hostedMode ? uploadFiles : undefined}
        value={values.inputPath}
      />
      {!hostedMode ? (
        <PathInput
          disabled={formDisabled || isBatchInput}
          error={errors.outputPath}
          id="media-resize-output-path"
          label="输出路径"
          onChange={(value) => updateValue('outputPath', value)}
          pickPaths={async () => {
            const response = await pickLocalSaveFile({
              fileKind: 'media',
              title: '选择输出路径',
            })
            return response.paths
          }}
          placeholder={isBatchInput ? '批量输入时自动生成 _resized 输出路径' : undefined}
          value={values.outputPath}
        />
      ) : null}
      <SizeInput
        disabled={formDisabled}
        error={errors.size}
        id="media-resize-size"
        label="目标尺寸"
        onChange={(value) => updateValue('size', value)}
        required
        value={values.size}
      />
      <SubmitButton disabled={formDisabled}>开始调整</SubmitButton>
    </form>
  )
}
