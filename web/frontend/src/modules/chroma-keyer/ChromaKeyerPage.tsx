import { useEffect, useRef, useState } from 'react'
import { Eraser, Pipette, SlidersHorizontal, Trash2 } from 'lucide-react'
import { hostedMode } from '../../app/runtimeConfig'
import { useMediaQuery } from '../../app/useMediaQuery'
import { TaskResult } from '../../components/TaskResult'
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
  type ChromaVideoBasePayload,
  type ChromaVideoKeyerPayload,
} from '../../services/apiClient'
import { FieldError } from '../../components/FieldError'
import { createClientTaskEvent } from '../../services/taskEventHelpers'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { ChromaKeyerCanvas, type ChromaCanvasSample } from './ChromaKeyerCanvas'
import { createChromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { ChromaKeyerForm } from './ChromaKeyerForm'
import { ChromaInspector } from './ChromaInspector'
import { ChromaVideoTimeline, getLastVideoFrameTime } from './ChromaVideoTimeline'
import { useChromaVideoPlayback } from './useChromaVideoPlayback'
import {
  isVideoPath,
  validateChromaKeyerPreviewValues,
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

const keyedPreviewFields = ['samples', ...previewParameterFields] as const
const videoTimestampErrorId = 'chroma-video-timestamp-error'

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
    ...toVideoBasePayload(values),
    outputFormat: values.outputFormat,
    ...(!hostedMode && values.outputFormat === 'packed_alpha_mp4' && values.outputPath.trim()
      ? { outputPath: values.outputPath.trim() } : {}),
    ...(!hostedMode && values.outputFormat === 'png_sequence' && values.outputRoot.trim()
      ? { outputRoot: values.outputRoot.trim() } : {}),
  }
}

function isSourceAlpha(values: ChromaKeyerValues): boolean {
  return values.mediaKind === 'video' && values.alphaSource === 'source'
}

function toVideoBasePayload(values: ChromaKeyerValues): ChromaVideoBasePayload {
  if (isSourceAlpha(values)) {
    return {
      inputPath: values.inputPath.trim(),
      alphaSource: 'source',
      sourceAlphaMode: values.sourceAlphaMode ?? 'straight',
      samples: [],
    }
  }
  return toBasePayload(values)
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
  const [viewportKey, setViewportKey] = useState(0)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [sampleDetails, setSampleDetails] = useState<ChromaCanvasSample[]>([])
  const [exportError, setExportError] = useState<string | null>(null)
  const [isRequestPending, setIsRequestPending] = useState(false)
  const [pendingPreviewSequence, setPendingPreviewSequence] = useState<number | null>(null)
  const [isUploadPending, setIsUploadPending] = useState(false)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [renderedTimestampSeconds, setRenderedTimestampSeconds] = useState<number | null>(null)
  const compactInspector = useMediaQuery('(max-width: 1179px)')
  const inspectorTriggerRef = useRef<HTMLButtonElement>(null)
  const mountedRef = useRef(true)
  const inspectingPathRef = useRef<string | null>(null)
  const uploadPendingRef = useRef(false)
  const requestSequenceRef = useRef(0)
  const previewSequenceRef = useRef(0)
  const renderedTimestampRef = useRef<number | null>(null)
  const seekTimeoutRef = useRef<number | null>(null)
  const lastViewModeRef = useRef(values.viewMode)
  const lastAlphaSettingsRef = useRef(`${values.alphaSource}:${values.sourceAlphaMode}`)
  const mediaRef = useRef(media)
  const valuesRef = useRef(values)
  mediaRef.current = media
  valuesRef.current = values
  const disabledByTask = status === 'queued' || status === 'running' || isCreating
  const disabled = disabledByTask || isRequestPending
  const sourceAlpha = isSourceAlpha(values)
  const keyedVideoSeekInvalid = media?.kind === 'video'
    && values.samples.length > 0
    && !areKeyedPreviewInputsValid(values)
  const videoTransportAvailable = media?.kind === 'video'
    && Number.isFinite(media.durationSeconds)
    && media.durationSeconds > 0
    && Number.isFinite(media.fps)
    && media.fps > 0
  const canPlayVideo = Boolean(
    videoTransportAvailable
    && !disabled
    && pendingPreviewSequence === null
    && inspectingPathRef.current === null
    && !keyedVideoSeekInvalid,
  )
  const videoPreviewOutOfDate = media?.kind === 'video'
    && renderedTimestampSeconds !== values.timestampSeconds
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
    setPendingPreviewSequence(null)
  }

  const handleUploadPendingChange = (pending: boolean) => {
    uploadPendingRef.current = pending
    if (mountedRef.current) {
      setIsUploadPending(pending)
    }
  }

  const updateValues = (nextValues: ChromaKeyerValues) => {
    const currentValues = valuesRef.current
    const didChangePath = nextValues.inputPath !== currentValues.inputPath
    if (didChangePath) {
      stopVideoPlayback({ synchronize: false })
      invalidateRequests()
      if (seekTimeoutRef.current !== null) {
        window.clearTimeout(seekTimeoutRef.current)
        seekTimeoutRef.current = null
      }
      setMedia(null)
      mediaRef.current = null
      setPreviewUrl(null)
      renderedTimestampRef.current = null
      setRenderedTimestampSeconds(null)
      setSampleDetails([])
      const resetValues = {
        ...nextValues,
        mediaKind: null,
        alphaSource: 'chroma_key',
        sourceAlphaMode: 'straight',
        outputFormat: 'packed_alpha_mp4',
        outputPath: '',
        outputRoot: '',
        samples: [],
        timestampSeconds: 0,
      } satisfies ChromaKeyerValues
      valuesRef.current = resetValues
      setValues(resetValues)
      setErrors({})
      return
    }
    const alphaSettingsChanged = nextValues.alphaSource !== currentValues.alphaSource
      || nextValues.sourceAlphaMode !== currentValues.sourceAlphaMode
    const playbackInputChanged = (
      alphaSettingsChanged
      || nextValues.samples !== currentValues.samples
      || nextValues.viewMode !== currentValues.viewMode
      || previewParameterFields.some(
        (field) => nextValues[field] !== currentValues[field],
      )
    )
    const wasPlaying = playbackInputChanged && playback.isPlaying
    if (playbackInputChanged) {
      stopVideoPlayback()
    }
    if (alphaSettingsChanged || (isSourceAlpha(currentValues) && nextValues.viewMode !== currentValues.viewMode)) {
      invalidateRequests()
      if (seekTimeoutRef.current !== null) {
        window.clearTimeout(seekTimeoutRef.current)
        seekTimeoutRef.current = null
      }
      setIsRequestPending(false)
      setErrors({})
    }
    const sourceSafeValues = isSourceAlpha(nextValues)
      ? { ...nextValues, outputFormat: 'packed_alpha_mp4' as const }
      : nextValues
    const resolvedNextValues = (
      wasPlaying
      && renderedTimestampRef.current !== null
    )
      ? { ...sourceSafeValues, timestampSeconds: renderedTimestampRef.current }
      : sourceSafeValues
    valuesRef.current = resolvedNextValues
    setValues(resolvedNextValues)
    if (isPreviewValid(currentValues) && !isPreviewValid(resolvedNextValues)) {
      previewSequenceRef.current += 1
      setPendingPreviewSequence(null)
    }
    const validationErrors = validateChromaKeyerPreviewValues(resolvedNextValues)
    setErrors((current) => {
      const nextErrors = { ...current }
      for (const field of previewParameterFields) {
        nextErrors[field] = validationErrors[field]
      }
      return nextErrors
    })
    if (
      alphaSettingsChanged
      && mediaRef.current?.kind === 'video'
      && !isSourceAlpha(resolvedNextValues)
      && resolvedNextValues.samples.length === 0
    ) {
      requestVideoTimestamp(resolvedNextValues.timestampSeconds, 0)
    }
  }

  const setRenderedVideoTimestamp = (
    video: InspectedVideo,
    timestampSeconds: number,
  ) => {
    const resolvedTimestamp = Math.min(
      getLastVideoFrameTime(video.durationSeconds, video.fps),
      Math.max(0, timestampSeconds),
    )
    renderedTimestampRef.current = resolvedTimestamp
    setRenderedTimestampSeconds(resolvedTimestamp)
    return resolvedTimestamp
  }

  const synchronizeRenderedVideoTimestamp = (
    video: InspectedVideo,
    timestampSeconds: number,
  ) => {
    const resolvedTimestamp = setRenderedVideoTimestamp(video, timestampSeconds)
    setValues((current) => {
      const resolvedValues = { ...current, timestampSeconds: resolvedTimestamp }
      valuesRef.current = resolvedValues
      return resolvedValues
    })
  }

  const rollbackRequestedTimestamp = () => {
    const renderedTimestamp = renderedTimestampRef.current
    if (renderedTimestamp === null) {
      return
    }
    setValues((current) => {
      const rolledBackValues = { ...current, timestampSeconds: renderedTimestamp }
      valuesRef.current = rolledBackValues
      return rolledBackValues
    })
  }

  const requestPlaybackFrame = async (timestampSeconds: number) => {
    const playbackMedia = mediaRef.current
    const playbackValues = valuesRef.current
    if (!playbackMedia || playbackMedia.kind !== 'video') {
      throw new Error('视频未加载')
    }

    const response = isSourceAlpha(playbackValues) || playbackValues.samples.length === 1
      ? await createChromaVideoPreview({
          ...toVideoBasePayload(playbackValues),
          timestampSeconds,
          viewMode: playbackValues.viewMode,
        })
      : await createChromaVideoFrame({
          inputPath: playbackMedia.inputPath,
          timestampSeconds,
        })

    return {
      previewUrl: response.preview_url,
      timestampSeconds: response.timestamp_seconds,
    }
  }

  const playback = useChromaVideoPlayback({
    canPlay: canPlayVideo,
    durationSeconds: media?.kind === 'video' ? media.durationSeconds : 0,
    fps: media?.kind === 'video' ? media.fps : 0,
    renderedTimestampSeconds,
    requestFrame: requestPlaybackFrame,
    onError: (error) => {
      setErrors((current) => ({
        ...current,
        timestampSeconds: error instanceof Error && error.message
          ? error.message
          : '无法播放视频',
      }))
    },
    onFrame: (frame) => {
      const playbackMedia = mediaRef.current
      if (!playbackMedia || playbackMedia.kind !== 'video') {
        return
      }

      setPreviewUrl(frame.previewUrl)
      setRenderedVideoTimestamp(playbackMedia, frame.timestampSeconds)
      if (!isSourceAlpha(valuesRef.current) && valuesRef.current.samples.length === 0) {
        setMedia((current) => {
          if (!current || current.kind !== 'video' || current.inputPath !== playbackMedia.inputPath) {
            return current
          }
          const nextMedia = { ...current, previewUrl: frame.previewUrl }
          mediaRef.current = nextMedia
          return nextMedia
        })
      }
    },
    onPause: (timestampSeconds) => {
      const playbackMedia = mediaRef.current
      if (
        playbackMedia
        && playbackMedia.kind === 'video'
        && timestampSeconds !== null
      ) {
        synchronizeRenderedVideoTimestamp(playbackMedia, timestampSeconds)
      }
    },
    onTargetChange: (timestampSeconds) => {
      setValues((current) => {
        const nextValues = { ...current, timestampSeconds }
        valuesRef.current = nextValues
        return nextValues
      })
    },
  })

  const stopVideoPlayback = (options?: { synchronize?: boolean }) => {
    playback.pause({ synchronize: options?.synchronize ?? true })
  }

  const requestPreview = async (
    previewValues: ChromaKeyerValues,
    actionSequence: number,
  ) => {
    const previewMedia = mediaRef.current
    if (
      !previewMedia
      || previewMedia.inputPath !== previewValues.inputPath.trim()
      || actionSequence !== requestSequenceRef.current
      || !isPreviewValid(previewValues)
    ) {
      return
    }
    const previewSequence = ++previewSequenceRef.current
    setPendingPreviewSequence(previewSequence)

    try {
      const response = previewMedia.kind === 'video'
        ? await createChromaVideoPreview({
            ...toVideoBasePayload(previewValues),
            timestampSeconds: previewValues.timestampSeconds,
            viewMode: previewValues.viewMode,
          })
        : await createChromaPreview({
            ...toBasePayload(previewValues),
            viewMode: previewValues.viewMode,
          })
      if (mountedRef.current && previewSequence === previewSequenceRef.current) {
        setPreviewUrl(response.preview_url)
        if (
          previewMedia.kind === 'video'
          && 'timestamp_seconds' in response
          && typeof response.timestamp_seconds === 'number'
        ) {
          synchronizeRenderedVideoTimestamp(previewMedia, response.timestamp_seconds)
        }
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
        if (previewMedia.kind === 'video') {
          rollbackRequestedTimestamp()
        }
        setErrors((current) => ({
          ...current,
          samples: error instanceof Error && error.message ? error.message : '无法生成预览',
        }))
      }
    } finally {
      if (mountedRef.current && previewSequence === previewSequenceRef.current) {
        setPendingPreviewSequence(null)
      }
    }
  }

  useEffect(() => {
    const isViewModeChange = lastViewModeRef.current !== values.viewMode
    lastViewModeRef.current = values.viewMode
    const alphaSettings = `${values.alphaSource}:${values.sourceAlphaMode}`
    const isAlphaSettingsChange = lastAlphaSettingsRef.current !== alphaSettings
    lastAlphaSettingsRef.current = alphaSettings
    if (!media || !isPreviewValid(values)) {
      return
    }

    const actionSequence = requestSequenceRef.current
    if (isViewModeChange || isAlphaSettingsChange) {
      void requestPreview(values, actionSequence)
      return
    }

    const timeout = window.setTimeout(() => {
      void requestPreview(values, actionSequence)
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
    values.alphaSource,
    values.sourceAlphaMode,
  ])

  const handleInspect = async (committedPath: string) => {
    const inputPath = committedPath.trim()
    if (!inputPath) {
      setErrors((current) => ({ ...current, inputPath: '请输入图片路径' }))
      return
    }
    if (inspectingPathRef.current === inputPath) {
      return
    }

    stopVideoPlayback()
    const mediaKind = isVideoPath(inputPath) ? 'video' : 'image'
    const preserveSourceAlpha = mediaRef.current?.inputPath === inputPath
      && isSourceAlpha(valuesRef.current)
    const requestSequence = ++requestSequenceRef.current
    previewSequenceRef.current += 1
    setPendingPreviewSequence(null)
    inspectingPathRef.current = inputPath
    setIsRequestPending(true)
    setErrors({})
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
        const renderedTimestamp = Math.min(
          getLastVideoFrameTime(response.duration_seconds, response.fps),
          Math.max(0, response.timestamp_seconds),
        )
        setViewportKey((current) => current + 1)
        mediaRef.current = inspectedVideo
        setMedia(inspectedVideo)
        if (!preserveSourceAlpha) {
          setPreviewUrl(response.preview_url)
        }
        renderedTimestampRef.current = renderedTimestamp
        setRenderedTimestampSeconds(renderedTimestamp)
        setValues((current) => {
          const nextValues = {
            ...current,
            mediaKind: 'video' as const,
            alphaSource: preserveSourceAlpha ? current.alphaSource : 'chroma_key' as const,
            sourceAlphaMode: preserveSourceAlpha ? current.sourceAlphaMode : 'straight' as const,
            samples: [],
            timestampSeconds: renderedTimestamp,
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
        setViewportKey((current) => current + 1)
        mediaRef.current = inspectedImage
        setMedia(inspectedImage)
        setPreviewUrl(response.preview_url)
        renderedTimestampRef.current = null
        setRenderedTimestampSeconds(null)
        setValues((current) => {
          const nextValues = {
            ...current,
            mediaKind: 'image' as const,
            alphaSource: 'chroma_key' as const,
            sourceAlphaMode: 'straight' as const,
            outputFormat: 'png_sequence' as const,
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
        const currentMedia = mediaRef.current
        const currentValues = valuesRef.current
        if (
          currentMedia
          && currentMedia.inputPath === inputPath
          && currentValues.inputPath.trim() === inputPath
          && isPreviewValid(currentValues)
        ) {
          void requestPreview(currentValues, requestSequence)
        }
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

  const requestVideoTimestamp = (
    timestampSeconds: number,
    delayMilliseconds: number,
  ) => {
    const currentMedia = mediaRef.current
    const currentValues = valuesRef.current
    if (
      !currentMedia
      || currentMedia.kind !== 'video'
      || disabledByTask
      || inspectingPathRef.current !== null
      || (
        currentValues.samples.length > 0
        && !areKeyedPreviewInputsValid(currentValues)
      )
    ) {
      return
    }

    const timestamp = Math.min(
      getLastVideoFrameTime(currentMedia.durationSeconds, currentMedia.fps),
      Math.max(0, timestampSeconds),
    )
    const requestSequence = ++requestSequenceRef.current
    const previewSequence = ++previewSequenceRef.current
    const nextValues = { ...valuesRef.current, timestampSeconds: timestamp }
    setPendingPreviewSequence(
      isPreviewValid(nextValues) ? previewSequence : null,
    )
    valuesRef.current = nextValues
    setValues(nextValues)
    setIsRequestPending(false)
    setErrors((current) => ({ ...current, timestampSeconds: undefined }))

    if (seekTimeoutRef.current !== null) {
      window.clearTimeout(seekTimeoutRef.current)
    }
    const dispatchRequest = async () => {
      seekTimeoutRef.current = null
      if (requestSequence !== requestSequenceRef.current) {
        return
      }

      const seekMedia = mediaRef.current
      const seekValues = valuesRef.current
      if (!seekMedia || seekMedia.kind !== 'video' || seekMedia.inputPath !== currentMedia.inputPath) {
        if (previewSequence === previewSequenceRef.current) {
          setPendingPreviewSequence(null)
        }
        return
      }

      try {
        const usesPreview = isSourceAlpha(seekValues) || seekValues.samples.length === 1
        if (usesPreview && !areKeyedPreviewInputsValid(seekValues)) {
          rollbackRequestedTimestamp()
          return
        }
        const response = usesPreview
          ? await createChromaVideoPreview({
              ...toVideoBasePayload(seekValues),
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

        setPreviewUrl(response.preview_url)
        synchronizeRenderedVideoTimestamp(seekMedia, response.timestamp_seconds)
        if (!usesPreview) {
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
          rollbackRequestedTimestamp()
          setErrors((current) => ({
            ...current,
            timestampSeconds: error instanceof Error && error.message ? error.message : '无法加载视频帧',
          }))
        }
      } finally {
        if (mountedRef.current && previewSequence === previewSequenceRef.current) {
          setPendingPreviewSequence(null)
        }
      }
    }

    if (delayMilliseconds === 0) {
      void dispatchRequest()
      return
    }
    seekTimeoutRef.current = window.setTimeout(
      () => void dispatchRequest(),
      delayMilliseconds,
    )
  }

  const handleSeek = (timestampSeconds: number) => {
    stopVideoPlayback()
    requestVideoTimestamp(timestampSeconds, 100)
  }

  const handlePlayPause = () => {
    if (playback.isPlaying) {
      stopVideoPlayback()
      return
    }
    if (seekTimeoutRef.current !== null) {
      window.clearTimeout(seekTimeoutRef.current)
      seekTimeoutRef.current = null
    }
    invalidateRequests()
    setErrors((current) => ({ ...current, timestampSeconds: undefined }))
    playback.play()
  }

  const handleRestart = () => {
    stopVideoPlayback()
    requestVideoTimestamp(0, 0)
  }

  const handleStep = (direction: -1 | 1) => {
    const stepBase = playback.isPlaying
      ? renderedTimestampRef.current ?? valuesRef.current.timestampSeconds
      : valuesRef.current.timestampSeconds
    stopVideoPlayback()
    const currentMedia = mediaRef.current
    if (!currentMedia || currentMedia.kind !== 'video') {
      return
    }
    requestVideoTimestamp(
      stepBase + direction / currentMedia.fps,
      0,
    )
  }

  const handleSample = async (point: { x: number; y: number; previewWidth: number; previewHeight: number }) => {
    if (!media || disabled || uploadPendingRef.current || isSourceAlpha(valuesRef.current)) {
      return
    }
    stopVideoPlayback()

    const renderedTimestamp = renderedTimestampRef.current
    if (
      media.kind === 'video'
      && (renderedTimestamp === null || renderedTimestamp !== valuesRef.current.timestampSeconds)
    ) {
      return
    }

    const requestSequence = ++requestSequenceRef.current
    if (media.kind === 'video') {
      previewSequenceRef.current += 1
      setPendingPreviewSequence(null)
    }
    setIsRequestPending(true)
    try {
      const response = media.kind === 'video'
        ? await sampleChromaVideo({
            inputPath: media.inputPath,
            timestampSeconds: renderedTimestamp as number,
            ...point,
          })
        : await sampleChromaImage({ inputPath: media.inputPath, ...point })
      if (mountedRef.current && requestSequence === requestSequenceRef.current) {
        previewSequenceRef.current += 1
        const sampledTimestamp = media.kind === 'video'
          && 'timestamp_seconds' in response
          && typeof response.timestamp_seconds === 'number'
          ? Math.min(
              getLastVideoFrameTime(media.durationSeconds, media.fps),
              Math.max(0, response.timestamp_seconds),
            )
          : null
        if (sampledTimestamp !== null) {
          renderedTimestampRef.current = sampledTimestamp
          setRenderedTimestampSeconds(sampledTimestamp)
        }
        setValues((current) => {
          const nextValues = {
            ...current,
            samples: [[...response.rgb] as [number, number, number]],
            timestampSeconds: sampledTimestamp ?? current.timestampSeconds,
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
    stopVideoPlayback()
    const currentMedia = mediaRef.current
    const requestSequence = ++requestSequenceRef.current
    previewSequenceRef.current += 1
    setPendingPreviewSequence(null)
    const timestampSeconds = currentMedia?.kind === 'video'
      ? renderedTimestampRef.current ?? valuesRef.current.timestampSeconds
      : valuesRef.current.timestampSeconds
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
      synchronizeRenderedVideoTimestamp(currentMedia, response.timestamp_seconds)
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
        rollbackRequestedTimestamp()
        setErrors((current) => ({
          ...current,
          timestampSeconds: error instanceof Error && error.message ? error.message : '无法加载视频帧',
        }))
      }
    })
  }

  const removeSample = clearSamples

  const handleSubmit = async () => {
    if (disabled || uploadPendingRef.current) {
      return
    }

    stopVideoPlayback()
    const submissionValues = valuesRef.current
    const nextErrors = validateChromaKeyerValues(submissionValues)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) {
      return
    }

    setExportError(null)
    beginCreate()
    try {
      const response = submissionValues.mediaKind === 'video'
        ? await createChromaVideoKeyerTask(toVideoPayload(submissionValues))
        : await createChromaKeyerTask(toPayload(submissionValues))
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
      <div className="module-header chroma-module-header">
        <h1 id="chroma-keyer-title">
          <Pipette aria-hidden="true" size={22} />
          色键抠图
        </h1>
        {compactInspector ? (
          <button
            aria-haspopup="dialog"
            className="secondary-action-button chroma-inspector-trigger"
            onClick={() => setInspectorOpen(true)}
            ref={inspectorTriggerRef}
            type="button"
          >
            <SlidersHorizontal aria-hidden="true" size={17} />
            打开参数检查器
          </button>
        ) : null}
        {showOutcome && status === 'succeeded' && resultPath ? (
          <TaskResult hosted={hostedMode} value={resultPath} />
        ) : null}
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

      <div className="chroma-editor chroma-workbench">
        <div className="chroma-editor-main">
          {media && previewUrl ? (
            <ChromaKeyerCanvas
              disabled={
                disabledByTask
                || sourceAlpha
                || isUploadPending
                || playback.isPlaying
                || videoPreviewOutOfDate
              }
              onSample={handleSample}
              originalHeight={media.height}
              originalWidth={media.width}
              previewUrl={previewUrl}
              samples={sampleDetails}
              viewMode={values.viewMode}
              viewportKey={viewportKey}
            >
              {media.kind === 'video' ? (
                <>
                  <ChromaVideoTimeline
                    disabled={
                      disabledByTask
                      || inspectingPathRef.current !== null
                      || keyedVideoSeekInvalid
                    }
                    durationSeconds={media.durationSeconds}
                    errorId={videoTimestampErrorId}
                    estimatedFrameCount={media.estimatedFrameCount}
                    fps={media.fps}
                    height={media.height}
                    invalid={Boolean(errors.timestampSeconds)}
                    isPlaying={playback.isPlaying}
                    onChange={handleSeek}
                    onPlayPause={handlePlayPause}
                    onRestart={handleRestart}
                    onStepBackward={() => handleStep(-1)}
                    onStepForward={() => handleStep(1)}
                    transportDisabled={!canPlayVideo}
                    value={values.timestampSeconds}
                    width={media.width}
                  />
                  <FieldError id={videoTimestampErrorId} message={errors.timestampSeconds} />
                </>
              ) : null}
            </ChromaKeyerCanvas>
          ) : (
            <div className="chroma-empty-preview">加载本地图片或视频后，可在画面中吸取背景颜色。</div>
          )}
          {!sourceAlpha ? (
          <>
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
          </>
          ) : null}
          <FieldError message={errors.samples} />
        </div>
        <ChromaInspector
          compact={compactInspector}
          onOpenChange={setInspectorOpen}
          open={inspectorOpen}
          triggerRef={inspectorTriggerRef}
        >
          <ChromaKeyerForm
            disabled={disabled}
            errors={errors}
            isUploadPending={isUploadPending}
            onChange={updateValues}
            onInputPathCommit={(path) => void handleInspect(path)}
            onSubmit={() => void handleSubmit()}
            onUploadPendingChange={handleUploadPendingChange}
            values={values}
          />
        </ChromaInspector>
      </div>
    </section>
  )
}

function isPreviewValid(values: ChromaKeyerValues): boolean {
  return Object.keys(validateChromaKeyerPreviewValues(values)).length === 0
}

function areKeyedPreviewInputsValid(values: ChromaKeyerValues): boolean {
  const validationErrors = validateChromaKeyerPreviewValues(values)
  return keyedPreviewFields.every((field) => validationErrors[field] === undefined)
}
