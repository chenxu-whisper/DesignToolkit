import { useEffect, useRef, useState } from 'react'
import { Eraser, Pipette, Trash2 } from 'lucide-react'
import {
  createChromaKeyerTask,
  createChromaPreview,
  createChromaVideoFrame,
  createChromaVideoKeyerTask,
  createChromaVideoPreview,
  inspectChromaImage,
  inspectChromaVideo,
  sampleChromaImage,
  sampleChromaVideo,
  type ChromaKeyerPayload,
  type ChromaVideoKeyerPayload,
} from '../../services/apiClient'
import { FieldError } from '../../components/FieldError'
import { createClientTaskEvent } from '../../services/taskEventHelpers'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { ChromaKeyerCanvas, type ChromaCanvasSample } from './ChromaKeyerCanvas'
import { createChromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { ChromaKeyerForm } from './ChromaKeyerForm'
import { ChromaVideoTimeline, getLastVideoFrameTime } from './ChromaVideoTimeline'
import {
  isVideoPath,
  validateChromaKeyerValues,
  type ChromaKeyerErrors,
  type ChromaKeyerValues,
} from './chromaKeyerValidation'

type InspectedMediaBase = {
  height: number
  inputPath: string
  previewUrl: string
  width: number
}

type InspectedImage = InspectedMediaBase & {
  kind: 'image'
}

type InspectedVideo = InspectedMediaBase & {
  durationSeconds: number
  estimatedFrameCount: number
  fps: number
  kind: 'video'
}

type InspectedMedia = InspectedImage | InspectedVideo

const previewParameterFields = [
  'screenGain',
  'screenBalance',
  'clipBlack',
  'clipWhite',
  'shrinkGrow',
  'feather',
  'despillAmount',
] as const

function toBasePayload(values: ChromaKeyerValues): Omit<ChromaKeyerPayload, 'outputPath'> {
  if (values.samples.length !== 1) {
    throw new Error('需要且只能保留一个背景采样颜色')
  }
  const [sample] = values.samples

  return {
    inputPath: values.inputPath.trim(),
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

function toPayload(values: ChromaKeyerValues): ChromaKeyerPayload {
  return {
    ...toBasePayload(values),
    outputPath: values.outputPath.trim() || undefined,
  }
}

function toVideoPayload(values: ChromaKeyerValues): ChromaVideoKeyerPayload {
  return {
    ...toBasePayload(values),
    outputRoot: values.outputRoot.trim() || undefined,
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
  const [media, setMedia] = useState<InspectedMedia | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [sampleDetails, setSampleDetails] = useState<ChromaCanvasSample[]>([])
  const [exportError, setExportError] = useState<string | null>(null)
  const [isRequestPending, setIsRequestPending] = useState(false)
  const mountedRef = useRef(true)
  const inspectingPathRef = useRef<string | null>(null)
  const requestSequenceRef = useRef(0)
  const previewSequenceRef = useRef(0)
  const seekTimeoutRef = useRef<number | null>(null)
  const lastViewModeRef = useRef(values.viewMode)
  const mediaRef = useRef(media)
  const valuesRef = useRef(values)
  mediaRef.current = media
  valuesRef.current = values
  const disabledByTask = status === 'queued' || status === 'running' || isCreating
  const disabled = disabledByTask || isRequestPending
  const showOutcome = kind === 'chroma_keyer' || kind === null

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (seekTimeoutRef.current !== null) {
        window.clearTimeout(seekTimeoutRef.current)
      }
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
      if (seekTimeoutRef.current !== null) {
        window.clearTimeout(seekTimeoutRef.current)
        seekTimeoutRef.current = null
      }
      setMedia(null)
      setPreviewUrl(null)
      setSampleDetails([])
      const resetValues = {
        ...nextValues,
        mediaKind: null,
        outputRoot: '',
        samples: [],
        timestampSeconds: 0,
      } satisfies ChromaKeyerValues
      valuesRef.current = resetValues
      setValues(resetValues)
      setErrors({})
      return
    }
    valuesRef.current = nextValues
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

  const requestPreview = async (
    previewValues: ChromaKeyerValues,
    previewSequence: number,
    actionSequence: number,
  ) => {
    const previewMedia = mediaRef.current
    if (!previewMedia || !isPreviewValid(previewValues)) {
      return
    }

    try {
      const response = previewMedia.kind === 'video'
        ? await createChromaVideoPreview({
            ...toBasePayload(previewValues),
            timestampSeconds: previewValues.timestampSeconds,
            viewMode: previewValues.viewMode,
          })
        : await createChromaPreview({
            ...toBasePayload(previewValues),
            viewMode: previewValues.viewMode,
          })
      if (mountedRef.current && previewSequence === previewSequenceRef.current) {
        setPreviewUrl(response.preview_url)
        if (actionSequence === requestSequenceRef.current) {
          setErrors((current) => ({ ...current, samples: undefined }))
        }
      }
    } catch (error) {
      if (
        mountedRef.current
        && previewSequence === previewSequenceRef.current
        && actionSequence === requestSequenceRef.current
      ) {
        setErrors((current) => ({
          ...current,
          samples: error instanceof Error && error.message ? error.message : '无法生成预览',
        }))
      }
    }
  }

  useEffect(() => {
    const previewSequence = ++previewSequenceRef.current
    const actionSequence = requestSequenceRef.current
    const isViewModeChange = lastViewModeRef.current !== values.viewMode
    lastViewModeRef.current = values.viewMode
    if (!media || !isPreviewValid(values)) {
      return
    }

    if (isViewModeChange) {
      void requestPreview(values, previewSequence, actionSequence)
      return
    }

    const timeout = window.setTimeout(() => {
      void requestPreview(values, previewSequence, actionSequence)
    }, 150)
    return () => window.clearTimeout(timeout)
  }, [
    media,
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
    if (media?.inputPath === inputPath || inspectingPathRef.current === inputPath) {
      return
    }

    const mediaKind = isVideoPath(inputPath) ? 'video' : 'image'
    const requestSequence = ++requestSequenceRef.current
    previewSequenceRef.current += 1
    inspectingPathRef.current = inputPath
    setIsRequestPending(true)
    setErrors({})
    setValues((current) => {
      const nextValues: ChromaKeyerValues = {
        ...current,
        mediaKind,
        timestampSeconds: 0,
      }
      valuesRef.current = nextValues
      return nextValues
    })
    try {
      if (mediaKind === 'video') {
        const response = await inspectChromaVideo({ inputPath })
        if (!mountedRef.current || requestSequence !== requestSequenceRef.current) {
          return
        }
        const inspectedVideo: InspectedVideo = {
          durationSeconds: response.duration_seconds,
          estimatedFrameCount: response.estimated_frame_count,
          fps: response.fps,
          height: response.height,
          inputPath,
          kind: 'video',
          previewUrl: response.preview_url,
          width: response.width,
        }
        mediaRef.current = inspectedVideo
        setMedia(inspectedVideo)
        setPreviewUrl(response.preview_url)
        setValues((current) => {
          const nextValues = {
            ...current,
            mediaKind: 'video' as const,
            samples: [],
            timestampSeconds: Math.min(
              getLastVideoFrameTime(response.duration_seconds, response.fps),
              Math.max(0, response.timestamp_seconds),
            ),
          }
          valuesRef.current = nextValues
          return nextValues
        })
        setSampleDetails([])
      } else {
        const response = await inspectChromaImage({ inputPath })
        if (!mountedRef.current || requestSequence !== requestSequenceRef.current) {
          return
        }
        const inspectedImage: InspectedImage = {
          height: response.height,
          inputPath,
          kind: 'image',
          previewUrl: response.preview_url,
          width: response.width,
        }
        mediaRef.current = inspectedImage
        setMedia(inspectedImage)
        setPreviewUrl(response.preview_url)
        setValues((current) => {
          const nextValues = {
            ...current,
            mediaKind: 'image' as const,
            outputRoot: '',
            samples: [],
            timestampSeconds: 0,
          }
          valuesRef.current = nextValues
          return nextValues
        })
        setSampleDetails([])
      }
    } catch (error) {
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        setErrors((current) => ({
          ...current,
          inputPath: error instanceof Error && error.message
            ? error.message
            : mediaKind === 'video'
              ? '无法加载视频'
              : '无法加载图片',
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

  const handleSeek = (timestampSeconds: number) => {
    const currentMedia = mediaRef.current
    if (!currentMedia || currentMedia.kind !== 'video') {
      return
    }

    const timestamp = Math.min(
      getLastVideoFrameTime(currentMedia.durationSeconds, currentMedia.fps),
      Math.max(0, timestampSeconds),
    )
    const requestSequence = ++requestSequenceRef.current
    const previewSequence = ++previewSequenceRef.current
    const nextValues = { ...valuesRef.current, timestampSeconds: timestamp }
    valuesRef.current = nextValues
    setValues(nextValues)
    setIsRequestPending(false)
    setErrors((current) => ({ ...current, timestampSeconds: undefined }))

    if (seekTimeoutRef.current !== null) {
      window.clearTimeout(seekTimeoutRef.current)
    }
    seekTimeoutRef.current = window.setTimeout(async () => {
      seekTimeoutRef.current = null
      if (requestSequence !== requestSequenceRef.current) {
        return
      }

      const seekMedia = mediaRef.current
      const seekValues = valuesRef.current
      if (!seekMedia || seekMedia.kind !== 'video' || seekMedia.inputPath !== currentMedia.inputPath) {
        return
      }

      try {
        const hasSample = seekValues.samples.length === 1
        const response = hasSample
          ? await createChromaVideoPreview({
              ...toBasePayload(seekValues),
              timestampSeconds: timestamp,
              viewMode: seekValues.viewMode,
            })
          : await createChromaVideoFrame({
              inputPath: seekMedia.inputPath,
              timestampSeconds: timestamp,
            })
        if (
          !mountedRef.current
          || requestSequence !== requestSequenceRef.current
          || previewSequence !== previewSequenceRef.current
        ) {
          return
        }

        const resolvedTimestamp = Math.min(
          getLastVideoFrameTime(seekMedia.durationSeconds, seekMedia.fps),
          Math.max(0, response.timestamp_seconds),
        )
        setPreviewUrl(response.preview_url)
        setValues((current) => {
          const resolvedValues = { ...current, timestampSeconds: resolvedTimestamp }
          valuesRef.current = resolvedValues
          return resolvedValues
        })
        if (!hasSample) {
          setMedia((current) => {
            if (!current || current.kind !== 'video' || current.inputPath !== seekMedia.inputPath) {
              return current
            }
            const nextMedia = { ...current, previewUrl: response.preview_url }
            mediaRef.current = nextMedia
            return nextMedia
          })
        }
      } catch (error) {
        if (
          mountedRef.current
          && requestSequence === requestSequenceRef.current
          && previewSequence === previewSequenceRef.current
        ) {
          setErrors((current) => ({
            ...current,
            timestampSeconds: error instanceof Error && error.message ? error.message : '无法加载视频帧',
          }))
        }
      }
    }, 100)
  }

  const handleSample = async (point: { x: number; y: number; previewWidth: number; previewHeight: number }) => {
    if (!media || disabled) {
      return
    }

    const requestSequence = ++requestSequenceRef.current
    if (media.kind === 'video') {
      previewSequenceRef.current += 1
    }
    setIsRequestPending(true)
    try {
      const response = media.kind === 'video'
        ? await sampleChromaVideo({
            inputPath: media.inputPath,
            timestampSeconds: valuesRef.current.timestampSeconds,
            ...point,
          })
        : await sampleChromaImage({ inputPath: media.inputPath, ...point })
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        previewSequenceRef.current += 1
        setValues((current) => {
          const nextValues = {
            ...current,
            samples: [[...response.rgb] as [number, number, number]],
            timestampSeconds: 'timestamp_seconds' in response
              && typeof response.timestamp_seconds === 'number'
              ? response.timestamp_seconds
              : current.timestampSeconds,
          }
          valuesRef.current = nextValues
          return nextValues
        })
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

  const clearSamples = () => {
    const currentMedia = mediaRef.current
    const requestSequence = ++requestSequenceRef.current
    previewSequenceRef.current += 1
    const timestampSeconds = valuesRef.current.timestampSeconds
    setValues((current) => {
      const nextValues = { ...current, samples: [] }
      valuesRef.current = nextValues
      return nextValues
    })
    setSampleDetails([])
    if (!currentMedia || currentMedia.kind === 'image') {
      setPreviewUrl(currentMedia?.previewUrl ?? null)
      return
    }

    void createChromaVideoFrame({
      inputPath: currentMedia.inputPath,
      timestampSeconds,
    }).then((response) => {
      if (
        !mountedRef.current
        || requestSequence !== requestSequenceRef.current
      ) {
        return
      }
      setPreviewUrl(response.preview_url)
      setValues((current) => {
        const nextValues = { ...current, timestampSeconds: response.timestamp_seconds }
        valuesRef.current = nextValues
        return nextValues
      })
      setMedia((current) => {
        if (!current || current.kind !== 'video' || current.inputPath !== currentMedia.inputPath) {
          return current
        }
        const nextMedia = { ...current, previewUrl: response.preview_url }
        mediaRef.current = nextMedia
        return nextMedia
      })
    }).catch((error: unknown) => {
      if (
        mountedRef.current
        && requestSequence === requestSequenceRef.current
      ) {
        setErrors((current) => ({
          ...current,
          timestampSeconds: error instanceof Error && error.message ? error.message : '无法加载视频帧',
        }))
      }
    })
  }

  const removeSample = clearSamples

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
      const response = values.mediaKind === 'video'
        ? await createChromaVideoKeyerTask(toVideoPayload(values))
        : await createChromaKeyerTask(toPayload(values))
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
          {media && previewUrl ? (
            <ChromaKeyerCanvas
              onSample={handleSample}
              originalHeight={media.height}
              originalWidth={media.width}
              previewUrl={previewUrl}
              samples={sampleDetails}
              viewMode={values.viewMode}
            >
              {media.kind === 'video' ? (
                <>
                  <ChromaVideoTimeline
                    disabled={disabledByTask}
                    durationSeconds={media.durationSeconds}
                    estimatedFrameCount={media.estimatedFrameCount}
                    fps={media.fps}
                    height={media.height}
                    onChange={handleSeek}
                    value={values.timestampSeconds}
                    width={media.width}
                  />
                  <FieldError message={errors.timestampSeconds} />
                </>
              ) : null}
            </ChromaKeyerCanvas>
          ) : (
            <div className="chroma-empty-preview">加载本地图片或视频后，可在画面中吸取背景颜色。</div>
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
