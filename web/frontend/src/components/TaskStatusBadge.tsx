import type { TaskStatus } from '../services/taskTypes'

const STATUS_LABELS: Record<TaskStatus, string> = {
  idle: '空闲',
  queued: '排队中',
  running: '运行中',
  succeeded: '完成',
  failed: '失败',
}

type TaskStatusBadgeProps = {
  status: TaskStatus
}

export function TaskStatusBadge({ status }: TaskStatusBadgeProps) {
  const label = STATUS_LABELS[status]

  return (
    <span className={`task-status task-status-${status}`} aria-label={`任务状态：${label}`}>
      {label}
    </span>
  )
}
