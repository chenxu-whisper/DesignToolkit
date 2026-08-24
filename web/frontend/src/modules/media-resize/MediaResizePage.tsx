import { useEffect, useRef } from 'react'
import { createMediaResizeTask, type MediaResizePayload } from '../../services/apiClient'
import { createClientTaskEvent } from '../../services/taskEventHelpers'
import { useLogStore } from '../../state/useLogStore'
import { useTaskStore } from '../../state/useTaskStore'
import { MediaResizeForm } from './MediaResizeForm'
import { normalizeMediaResizeInputPaths, type MediaResizeValues } from './mediaResizeValidation'

function toPayload(values: MediaResizeValues): MediaResizePayload {
  const inputPaths = normalizeMediaResizeInputPaths(values.inputPath)
  if (inputPaths.length > 1) {
    return {
      input_paths: inputPaths,
      size: values.size.trim(),
    }
  }
  return {
    input_path: inputPaths[0],
    output_path: values.outputPath.trim() || undefined,
    size: values.size.trim(),
  }
}

export function MediaResizePage() {
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
  const showOutcome = kind === 'media_resize' || kind === null

  useEffect(() => {
    return () => {
      mountedRef.current = false
    }
  }, [])

  const handleSubmit = async (values: MediaResizeValues) => {
    if (disabled) {
      return
    }

    clearEvents()
    beginCreate()

    try {
      const response = await createMediaResizeTask(toPayload(values))
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
        kind: 'media_resize',
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
    <section className="module-surface" aria-labelledby="media-resize-title">
      <div className="module-header">
        <h1 id="media-resize-title">媒体尺寸修改</h1>
        {showOutcome && status === 'succeeded' && resultPath ? <p className="task-result">输出：{resultPath}</p> : null}
        {showOutcome && status === 'failed' && error ? (
          <p className="task-error" role="alert">
            失败：{error}
          </p>
        ) : null}
      </div>
      <MediaResizeForm disabled={disabled} onSubmit={handleSubmit} />
    </section>
  )
}
