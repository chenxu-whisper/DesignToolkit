export type TaskKind = 'video_frames' | 'media_resize'
export type TaskStatus = 'idle' | 'queued' | 'running' | 'succeeded' | 'failed'
export type LogLevel = 'info' | 'progress' | 'success' | 'error'
export type LogPhase = 'created' | 'validating' | 'preparing' | 'running' | 'completed' | 'failed'

export type TaskEvent = {
  task_id: string
  timestamp: string
  level: LogLevel
  phase: LogPhase
  message: string
  details: Record<string, unknown>
}
