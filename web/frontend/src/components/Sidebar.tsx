import type { ModuleId, ToolModule } from '../app/types'

type SidebarProps = {
  activeModuleId: ModuleId
  modules: ToolModule[]
  onModuleSelect: (moduleId: ModuleId) => void
}

export function Sidebar({ activeModuleId, modules, onModuleSelect }: SidebarProps) {
  return (
    <nav className="sidebar" aria-label="工具模块">
      {modules.map((module) => {
        const isActive = module.id === activeModuleId

        return (
          <button
            aria-current={isActive ? 'page' : undefined}
            className={isActive ? 'nav-item nav-item-active' : 'nav-item'}
            key={module.id}
            onClick={() => onModuleSelect(module.id)}
            type="button"
          >
            {module.label}
          </button>
        )
      })}
    </nav>
  )
}
