import type { TaskEvent } from './taskTypes'

export function subscribeTaskEvents(
  taskId: string,
  onEvent: (event: TaskEvent) => void,
  onError: (error: Event | Error) => void,
): () => void {
  const source = new EventSource(`/api/tasks/${taskId}/events`)
  let closed = false

  const close = () => {
    if (closed) {
      return
    }
    closed = true
    source.close()
  }

  source.onmessage = (message) => {
    if (closed) {
      return
    }

    let event: TaskEvent
    try {
      event = JSON.parse(message.data) as TaskEvent
    } catch (error) {
      onError(error instanceof Error ? error : new Error('Failed to parse task event'))
      close()
      return
    }

    onEvent(event)
    if (event.phase === 'completed' || event.phase === 'failed') {
      close()
    }
  }
  source.onerror = (error) => {
    if (closed) {
      return
    }
    onError(error)
    close()
  }
  return close
}
