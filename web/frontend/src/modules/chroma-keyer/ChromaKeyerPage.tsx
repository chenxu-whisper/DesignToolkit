import { useEffect, useRef, useState } from 'react'
import { Eraser, Pipette, Trash2 } from 'lucide-react'
import {
  createChromaKeyerTask,
  createChromaPreview,
  inspectChromaImage,
  sampleChromaImage,
  type ChromaKeyerPayload,
} from '../../services/apiClient'
import { FieldError } from '../../components/FieldError'
import { createClientTaskEvent } from '../../services/taskEventHelpers'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { ChromaKeyerCanvas, type ChromaCanvasSample } from './ChromaKeyerCanvas'
import { createChromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { ChromaKeyerForm } from './ChromaKeyerForm'
import {
  validateChromaKeyerValues,
  type ChromaKeyerErrors,
  type ChromaKeyerValues,
} from './chromaKeyerValidation'

type InspectedImage = {
  height: number
  inputPath: string
  previewUrl: string
  width: number
}

const previewParameterFields = [
  'screenGain',
  'screenBalance',
  'clipBlack',
  'clipWhite',
  'shrinkGrow',
  'feather',
  'despillAmount',
] as const

function toPayload(values: ChromaKeyerValues): ChromaKeyerPayload {
  if (values.samples.length !== 1) {
    throw new Error('需要且只能保留一个背景采样颜色')
  }
  const [sample] = values.samples

  return {
    inputPath: values.inputPath.trim(),
    outputPath: values.outputPath.trim() || undefined,
    params: {
      clipBlack: values.clipBlack,
      clipWhite: values.clipWhite,
      despillAmount: values.despillAmount,
      feather: values.feather,
      shrinkGrow: values.shrinkGrow,
      screenBalance: values.screenBalance,
      screenGain: values.screenGain,
    },
    samples: [sample],
  }
}

export function ChromaKeyerPage() {
  const status = useTaskStore((state) => state.status)
  const kind = useTaskStore((state) => state.kind)
  const resultPath = useTaskStore((state) => state.resultPath)
  const taskError = useTaskStore((state) => state.error)
  const isCreating = useTaskStore((state) => state.isCreating)
  const startTask = useTaskStore((state) => state.startTask)
  const resetTask = useTaskStore((state) => state.resetTask)
  const beginCreate = useTaskStore((state) => state.beginCreate)
  const endCreate = useTaskStore((state) => state.endCreate)
  const appendEvent = useLogStore((state) => state.appendEvent)
  const clearEvents = useLogStore((state) => state.clearEvents)
  const [values, setValues] = useState<ChromaKeyerValues>(() => createChromaKeyerDefaultValues())
  const [errors, setErrors] = useState<ChromaKeyerErrors>({})
  const [image, setImage] = useState<InspectedImage | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [sampleDetails, setSampleDetails] = useState<ChromaCanvasSample[]>([])
  const [exportError, setExportError] = useState<string | null>(null)
  const [isRequestPending, setIsRequestPending] = useState(false)
  const mountedRef = useRef(true)
  const inspectingPathRef = useRef<string | null>(null)
  const requestSequenceRef = useRef(0)
  const previewSequenceRef = useRef(0)
  const lastViewModeRef = useRef(values.viewMode)
  const disabledByTask = status === 'queued' || status === 'running' || isCreating
  const disabled = disabledByTask || isRequestPending
  const showOutcome = kind === 'chroma_keyer' || kind === null

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const invalidateRequests = () => {
    requestSequenceRef.current += 1
    previewSequenceRef.current += 1
  }

  const updateValues = (nextValues: ChromaKeyerValues) => {
    const didChangePath = nextValues.inputPath !== values.inputPath
    if (didChangePath) {
      invalidateRequests()
      setImage(null)
      setPreviewUrl(null)
      setSampleDetails([])
      setValues({ ...nextValues, samples: [] })
      setErrors({})
      return
    }
    setValues(nextValues)
    const validationErrors = validateChromaKeyerValues(nextValues)
    setErrors((current) => {
      const nextErrors = { ...current }
      for (const field of previewParameterFields) {
        nextErrors[field] = validationErrors[field]
      }
      return nextErrors
    })
  }

  const requestPreview = async (previewValues: ChromaKeyerValues, requestSequence: number) => {
    if (!image || !isPreviewValid(previewValues)) {
      return
    }

    try {
      const response = await createChromaPreview({
        ...toPayload(previewValues),
        viewMode: previewValues.viewMode,
      })
      if (mountedRef.current && requestSequence === previewSequenceRef.current) {
        setPreviewUrl(response.preview_url)
        setErrors((current) => ({ ...current, samples: undefined }))
      }
    } catch (error) {
      if (mountedRef.current && requestSequence === previewSequenceRef.current) {
        setErrors((current) => ({
          ...current,
          samples: error instanceof Error && error.message ? error.message : '无法生成预览',
        }))
      }
    }
  }

  useEffect(() => {
    const requestSequence = ++previewSequenceRef.current
    const isViewModeChange = lastViewModeRef.current !== values.viewMode
    lastViewModeRef.current = values.viewMode
    if (!image || !isPreviewValid(values)) {
      return
    }

    if (isViewModeChange) {
      void requestPreview(values, requestSequence)
      return
    }

    const timeout = window.setTimeout(() => {
      void requestPreview(values, requestSequence)
    }, 150)
    return () => window.clearTimeout(timeout)
  }, [
    image,
    values.inputPath,
    values.samples,
    values.screenGain,
    values.screenBalance,
    values.clipBlack,
    values.clipWhite,
    values.shrinkGrow,
    values.feather,
    values.despillAmount,
    values.viewMode,
  ])

  const handleInspect = async (committedPath: string) => {
    const inputPath = committedPath.trim()
    if (!inputPath) {
      setErrors((current) => ({ ...current, inputPath: '请输入图片路径' }))
      return
    }
    if (image?.inputPath === inputPath || inspectingPathRef.current === inputPath) {
      return
    }

    const requestSequence = ++requestSequenceRef.current
    previewSequenceRef.current += 1
    inspectingPathRef.current = inputPath
    setIsRequestPending(true)
    setErrors({})
    try {
      const response = await inspectChromaImage({ inputPath })
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        setImage({
          height: response.height,
          inputPath,
          previewUrl: response.preview_url,
          width: response.width,
        })
        setPreviewUrl(response.preview_url)
        setValues((current) => ({ ...current, samples: [] }))
        setSampleDetails([])
      }
    } catch (error) {
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        setErrors((current) => ({
          ...current,
          inputPath: error instanceof Error && error.message ? error.message : '无法加载图片',
        }))
      }
    } finally {
      if (inspectingPathRef.current === inputPath) {
        inspectingPathRef.current = null
      }
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        setIsRequestPending(false)
      }
    }
  }

  const handleSample = async (point: { x: number; y: number; previewWidth: number; previewHeight: number }) => {
    if (!image || disabled) {
      return
    }

    const requestSequence = ++requestSequenceRef.current
    previewSequenceRef.current += 1
    setIsRequestPending(true)
    try {
      const response = await sampleChromaImage({ inputPath: image.inputPath, ...point })
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        setValues((current) => ({ ...current, samples: [[...response.rgb]] }))
        setSampleDetails([
          {
            hex: response.hex,
            rgb: response.rgb,
            sourceX: response.source_x,
            sourceY: response.source_y,
          },
        ])
        setErrors((current) => ({ ...current, samples: undefined }))
      }
    } catch (error) {
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        setErrors((current) => ({
          ...current,
          samples: error instanceof Error && error.message ? error.message : '无法吸取颜色',
        }))
      }
    } finally {
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        setIsRequestPending(false)
      }
    }
  }

  const removeSample = () => {
    invalidateRequests()
    setValues((current) => ({ ...current, samples: [] }))
    setSampleDetails([])
    setPreviewUrl(image?.previewUrl ?? null)
  }

  const clearSamples = () => {
    invalidateRequests()
    setValues((current) => ({ ...current, samples: [] }))
    setSampleDetails([])
    setPreviewUrl(image?.previewUrl ?? null)
  }

  const handleSubmit = async () => {
    if (disabled) {
      return
    }

    const nextErrors = validateChromaKeyerValues(values)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) {
      return
    }

    setExportError(null)
    beginCreate()
    try {
      const response = await createChromaKeyerTask(toPayload(values))
      if (!mountedRef.current) {
        return
      }

      const state = useTaskStore.getState()
      if (state.currentTaskId !== null && (state.status === 'queued' || state.status === 'running')) {
        return
      }

      clearEvents()
      resetTask()
      startTask({
        kind: 'chroma_keyer',
        status: response.status === 'queued' ? 'queued' : 'running',
        taskId: response.task_id,
      })
      appendEvent(createClientTaskEvent(response.task_id, 'info', 'created', '任务已提交'))
    } catch (createError) {
      if (!mountedRef.current) {
        return
      }

      const state = useTaskStore.getState()
      if (state.currentTaskId !== null && (state.status === 'queued' || state.status === 'running')) {
        return
      }

      const message = createError instanceof Error && createError.message ? createError.message : '任务创建失败'
      setExportError(message)
    } finally {
      endCreate()
    }
  }

  return (
    <section className="module-surface chroma-keyer-surface" aria-labelledby="chroma-keyer-title">
      <div className="module-header">
        <h1 id="chroma-keyer-title">
          <Pipette aria-hidden="true" size={22} />
          Studio Chroma Keyer
        </h1>
        {showOutcome && status === 'succeeded' && resultPath ? <p className="task-result">输出：{resultPath}</p> : null}
        {showOutcome && status === 'failed' && taskError ? (
          <p className="task-error" role="alert">
            失败：{taskError}
          </p>
        ) : null}
        {exportError ? (
          <p className="task-error" role="alert">
            导出失败：{exportError}
          </p>
        ) : null}
      </div>

      <div className="chroma-editor">
        <div className="chroma-editor-main">
          {image && previewUrl ? (
            <ChromaKeyerCanvas
              onSample={handleSample}
              originalHeight={image.height}
              originalWidth={image.width}
              previewUrl={previewUrl}
              samples={sampleDetails}
              viewMode={values.viewMode}
            />
          ) : (
            <div className="chroma-empty-preview">加载一张本地图片后，可在画面中吸取背景颜色。</div>
          )}
          <div className="chroma-sample-toolbar">
            <h2 className="chroma-section-title">采样颜色</h2>
            <button
              aria-label="清除全部采样点"
              className="icon-button"
              disabled={disabled || sampleDetails.length === 0}
              onClick={clearSamples}
              title="清除全部采样点"
              type="button"
            >
              <Eraser aria-hidden="true" size={18} />
            </button>
          </div>
          <FieldError message={errors.samples} />
          <ol className="chroma-sample-list">
            {sampleDetails.map((sample) => (
              <li className="chroma-sample-item" key={`${sample.sourceX}-${sample.sourceY}`}>
                <span aria-hidden="true" className="chroma-swatch" style={{ backgroundColor: sample.hex }} />
                <span>Screen Color</span>
                <span>{`RGB(${sample.rgb.join(', ')})`}</span>
                <span>{sample.hex}</span>
                <span>{sample.sourceX}, {sample.sourceY}</span>
                <button
                  aria-label="删除 Screen Color"
                  className="icon-button"
                  disabled={disabled}
                  onClick={removeSample}
                  title="删除 Screen Color"
                  type="button"
                >
                  <Trash2 aria-hidden="true" size={17} />
                </button>
              </li>
            ))}
          </ol>
        </div>
        <ChromaKeyerForm
          disabled={disabled}
          errors={errors}
          onChange={updateValues}
          onInputPathCommit={(path) => void handleInspect(path)}
          onSubmit={() => void handleSubmit()}
          values={values}
        />
      </div>
    </section>
  )
}

function isPreviewValid(values: ChromaKeyerValues): boolean {
  return Object.keys(validateChromaKeyerValues(values)).length === 0
}
