import { useEffect, useRef } from 'react'
import { hostedMode } from '../../app/runtimeConfig'
import { TaskResult } from '../../components/TaskResult'
import { createVideoFramesTask, type VideoFramesPayload } from '../../services/apiClient'
import { createClientTaskEvent } from '../../services/taskEventHelpers'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { VideoFramesForm } from './VideoFramesForm'
import { normalizeVideoInputPaths, type VideoFramesValues } from './videoFramesValidation'

function toPayload(values: VideoFramesValues): VideoFramesPayload {
  const inputPaths = normalizeVideoInputPaths(values.inputPath)
  const sharedPayload = {
    output_dir: values.outputDir.trim() || undefined,
    image_format: values.imageFormat,
    size: values.size.trim() || undefined,
    fps: values.samplingMode === 'fps' ? Number(values.fps) : undefined,
    frames: values.samplingMode === 'frames' ? Number(values.frames) : undefined,
    overwrite: values.overwrite,
  }
  if (inputPaths.length === 1) {
    return {
      input_path: inputPaths[0],
      ...sharedPayload,
    }
  }
  return {
    input_paths: inputPaths,
    ...sharedPayload,
  }
}

export function VideoFramesPage() {
  const status = useTaskStore((state) => state.status)
  const kind = useTaskStore((state) => state.kind)
  const resultPath = useTaskStore((state) => state.resultPath)
  const error = useTaskStore((state) => state.error)
  const isCreating = useTaskStore((state) => state.isCreating)
  const startTask = useTaskStore((state) => state.startTask)
  const failTask = useTaskStore((state) => state.failTask)
  const resetTask = useTaskStore((state) => state.resetTask)
  const beginCreate = useTaskStore((state) => state.beginCreate)
  const endCreate = useTaskStore((state) => state.endCreate)
  const appendEvent = useLogStore((state) => state.appendEvent)
  const clearEvents = useLogStore((state) => state.clearEvents)
  const mountedRef = useRef(true)
  const disabled = status === 'queued' || status === 'running' || isCreating
  const showOutcome = kind === 'video_frames' || kind === null

  useEffect(() => {
    return () => {
      mountedRef.current = false
    }
  }, [])

  const handleSubmit = async (values: VideoFramesValues) => {
    if (disabled) {
      return
    }

    clearEvents()
    beginCreate()

    try {
      const response = await createVideoFramesTask(toPayload(values))
      if (!mountedRef.current) {
        return
      }

      const state = useTaskStore.getState()
      if (state.currentTaskId !== null && (state.status === 'queued' || state.status === 'running')) {
        return
      }

      resetTask()
      startTask({
        taskId: response.task_id,
        kind: 'video_frames',
        status: response.status === 'queued' ? 'queued' : 'running',
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
      appendEvent(createClientTaskEvent('client', 'error', 'failed', message))
      resetTask()
      failTask({ error: message })
    } finally {
      endCreate()
    }
  }

  return (
    <section className="module-surface" aria-labelledby="video-frames-title">
      <div className="module-header">
        <h1 id="video-frames-title">视频转序列</h1>
        {showOutcome && status === 'succeeded' && resultPath ? (
          <TaskResult hosted={hostedMode} value={resultPath} />
        ) : null}
        {showOutcome && status === 'failed' && error ? (
          <p className="task-error" role="alert">
            失败：{error}
          </p>
        ) : null}
      </div>
      <VideoFramesForm disabled={disabled} onSubmit={handleSubmit} />
    </section>
  )
}
