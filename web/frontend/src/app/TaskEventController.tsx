import { useEffect, useState } from 'react'
import { getTask, type TaskResponse } from '../services/apiClient'
import { createClientTaskEvent, eventErrorMessage, outputPathFromEvent } from '../services/taskEventHelpers'
import { subscribeTaskEvents } from '../services/taskEvents'
import type { TaskEvent } from '../services/taskTypes'
import { useLogStore } from '../state/useLogStore'
import { useTaskStore } from '../state/useTaskStore'

function isTerminalSuccess(event: TaskEvent): boolean {
  return event.phase === 'completed'
}

function isTerminalFailure(event: TaskEvent): boolean {
  return event.phase === 'failed'
}

function outputPathFromTask(task: TaskResponse): string {
  return task.output_path ?? ''
}

export function TaskEventController() {
  const [reconnectAttempt, setReconnectAttempt] = useState(0)
  const appendEvent = useLogStore((state) => state.appendEvent)
  const currentTaskId = useTaskStore((state) => state.currentTaskId)
  const status = useTaskStore((state) => state.status)
  const completeTask = useTaskStore((state) => state.completeTask)
  const failTask = useTaskStore((state) => state.failTask)
  const startTask = useTaskStore((state) => state.startTask)

  useEffect(() => {
    if (!currentTaskId || (status !== 'queued' && status !== 'running')) {
      return undefined
    }

    let cancelled = false
    const isCurrentTask = (eventTaskId: string): boolean => useTaskStore.getState().currentTaskId === eventTaskId

    const reconcileStreamError = async (streamError: Event | Error): Promise<void> => {
      const message = eventErrorMessage(streamError)
      try {
        const task = await getTask(currentTaskId)
        if (cancelled || !isCurrentTask(currentTaskId)) {
          return
        }

        if (task.status === 'failed') {
          const taskError = task.error || message
          appendEvent(createClientTaskEvent(currentTaskId, 'error', 'failed', taskError))
          failTask({ taskId: currentTaskId, error: taskError })
          return
        }

        if (task.status === 'succeeded') {
          const outputPath = outputPathFromTask(task)
          appendEvent(createClientTaskEvent(currentTaskId, 'success', 'completed', '任务已完成', { output_path: outputPath }))
          completeTask({ taskId: currentTaskId, resultPath: outputPath })
          return
        }

        startTask({
          taskId: currentTaskId,
          kind: task.kind,
          status: task.status === 'queued' ? 'queued' : 'running',
        })
        appendEvent(createClientTaskEvent(currentTaskId, 'error', 'running', `${message}，正在重新连接`))
        setReconnectAttempt((attempt) => attempt + 1)
      } catch {
        if (cancelled || !isCurrentTask(currentTaskId)) {
          return
        }

        const unavailableMessage = `${message}，任务状态不可用，请刷新页面或重新提交`
        appendEvent(createClientTaskEvent(currentTaskId, 'error', 'failed', unavailableMessage))
        failTask({ taskId: currentTaskId, error: unavailableMessage })
      }
    }

    const unsubscribe = subscribeTaskEvents(
      currentTaskId,
      (event) => {
        if (event.task_id !== currentTaskId || !isCurrentTask(currentTaskId)) {
          return
        }

        appendEvent(event)

        if (isTerminalFailure(event)) {
          failTask({ taskId: currentTaskId, error: event.message })
        } else if (isTerminalSuccess(event)) {
          completeTask({ taskId: currentTaskId, resultPath: outputPathFromEvent(event) })
        }
      },
      (streamError) => {
        if (!isCurrentTask(currentTaskId)) {
          return
        }

        void reconcileStreamError(streamError)
      },
    )

    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [appendEvent, completeTask, currentTaskId, failTask, reconnectAttempt, startTask, status])

  return null
}
