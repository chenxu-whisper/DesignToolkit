import { useTaskStore } from '../state/useTaskStore'
import { TaskStatusBadge } from './TaskStatusBadge'

export function HeaderBar() {
  const status = useTaskStore((state) => state.status)

  return (
    <header className="header-bar">
      <div className="header-title">DesignToolkit</div>
      <TaskStatusBadge status={status} />
    </header>
  )
}
