import type { ModuleId, ToolModule } from '../app/types'

type SidebarProps = {
  activeModuleId: ModuleId
  disabled?: boolean
  modules: ToolModule[]
  onModuleSelect: (moduleId: ModuleId) => void
}

export function Sidebar({
  activeModuleId,
  disabled = false,
  modules,
  onModuleSelect,
}: SidebarProps) {
  return (
    <nav className="sidebar tool-rail" aria-label="工具模块">
      {modules.map((module) => {
        const isActive = module.id === activeModuleId
        const Icon = module.icon

        return (
          <button
            aria-current={isActive ? 'page' : undefined}
            className={isActive ? 'nav-item nav-item-active' : 'nav-item'}
            disabled={disabled}
            key={module.id}
            onClick={() => onModuleSelect(module.id)}
            title={module.description}
            type="button"
          >
            <Icon aria-hidden="true" className="nav-item-icon" size={19} />
            <span className="nav-item-label">{module.label}</span>
          </button>
        )
      })}
    </nav>
  )
}
