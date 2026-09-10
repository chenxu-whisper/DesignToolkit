import {
  CircleCheck,
  CircleDashed,
  CircleX,
  Clock3,
  LoaderCircle,
  type LucideIcon,
} from 'lucide-react'
import type { TaskStatus } from '../services/taskTypes'

const STATUS_LABELS: Record<TaskStatus, string> = {
  idle: '空闲',
  queued: '排队中',
  running: '运行中',
  succeeded: '完成',
  failed: '失败',
}

const STATUS_ICONS: Record<TaskStatus, LucideIcon> = {
  failed: CircleX,
  idle: CircleDashed,
  queued: Clock3,
  running: LoaderCircle,
  succeeded: CircleCheck,
}

type TaskStatusBadgeProps = {
  status: TaskStatus
}

export function TaskStatusBadge({ status }: TaskStatusBadgeProps) {
  const label = STATUS_LABELS[status]
  const Icon = STATUS_ICONS[status]

  return (
    <span
      aria-label={`任务状态：${label}`}
      aria-live="polite"
      className={`task-status task-status-${status}`}
      role="status"
    >
      <Icon
        aria-hidden="true"
        className={status === 'running' ? 'task-status-icon task-status-icon-spinning' : 'task-status-icon'}
        size={14}
      />
      <span>{label}</span>
    </span>
  )
}
