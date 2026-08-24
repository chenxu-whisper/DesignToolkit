import type { TaskEvent } from './taskTypes'

export function createClientTaskEvent(
  taskId: string,
  level: TaskEvent['level'],
  phase: TaskEvent['phase'],
  message: string,
  details: Record<string, unknown> = {},
): TaskEvent {
  return {
    task_id: taskId,
    timestamp: new Date().toISOString(),
    level,
    phase,
    message,
    details,
  }
}

export function eventErrorMessage(error: Event | Error): string {
  return error instanceof Error && error.message ? error.message : '日志连接中断'
}

export function outputPathFromEvent(event: TaskEvent): string {
  const outputPath = event.details.output_path
  return typeof outputPath === 'string' ? outputPath : event.message || ''
}
