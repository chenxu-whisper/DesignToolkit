import {
  Check,
  MonitorCog,
  Moon,
  PanelBottomClose,
  PanelBottomOpen,
  PanelsTopLeft,
  Sun,
} from 'lucide-react'
import {
  type KeyboardEvent,
  useEffect,
  useRef,
  useState,
} from 'react'
import type { ToolModule } from '../app/types'
import {
  type ThemePreference,
  useTheme,
} from '../app/theme/ThemeProvider'
import { useTaskStore } from '../state/useTaskStore'
import { TaskStatusBadge } from './TaskStatusBadge'

type HeaderBarProps = {
  activeModule: ToolModule
  logsCollapsed: boolean
  onToggleLogs: () => void
}

const themeOptions: Array<{
  icon: typeof MonitorCog
  label: string
  value: ThemePreference
}> = [
  { icon: MonitorCog, label: '跟随系统', value: 'system' },
  { icon: Sun, label: '浅色', value: 'light' },
  { icon: Moon, label: '深色', value: 'dark' },
]

export function HeaderBar({
  activeModule,
  logsCollapsed,
  onToggleLogs,
}: HeaderBarProps) {
  const status = useTaskStore((state) => state.status)
  const { preference, resolvedTheme, setPreference } = useTheme()
  const [themeMenuOpen, setThemeMenuOpen] = useState(false)
  const themeMenuItemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const themeTriggerRef = useRef<HTMLButtonElement>(null)
  const ThemeIcon = resolvedTheme === 'dark' ? Moon : Sun

  useEffect(() => {
    if (!themeMenuOpen) {
      return
    }
    const selectedIndex = themeOptions.findIndex((option) => option.value === preference)
    themeMenuItemRefs.current[selectedIndex]?.focus()
  }, [preference, themeMenuOpen])

  const closeThemeMenu = () => {
    setThemeMenuOpen(false)
    themeTriggerRef.current?.focus()
  }

  const selectTheme = (nextPreference: ThemePreference) => {
    setPreference(nextPreference)
    closeThemeMenu()
  }

  const handleThemeMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = themeMenuItemRefs.current.filter(
      (item): item is HTMLButtonElement => item !== null,
    )
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement)
    let nextIndex: number | null = null

    if (event.key === 'ArrowDown') {
      nextIndex = (currentIndex + 1) % items.length
    } else if (event.key === 'ArrowUp') {
      nextIndex = (currentIndex - 1 + items.length) % items.length
    } else if (event.key === 'Home') {
      nextIndex = 0
    } else if (event.key === 'End') {
      nextIndex = items.length - 1
    } else if (event.key === 'Escape') {
      event.preventDefault()
      closeThemeMenu()
      return
    }

    if (nextIndex !== null) {
      event.preventDefault()
      items[nextIndex]?.focus()
    }
  }

  return (
    <header className="header-bar command-bar">
      <div className="brand-lockup">
        <span aria-hidden="true" className="brand-mark">
          <PanelsTopLeft size={17} />
        </span>
        <span className="brand-name">DesignToolkit</span>
      </div>
      <div className="command-context">
        <span className="command-context-title">{activeModule.label}</span>
        <span className="command-context-description">{activeModule.description}</span>
      </div>
      <div className="command-actions">
        <TaskStatusBadge status={status} />
        <div className="theme-menu">
          <button
            aria-expanded={themeMenuOpen}
            aria-haspopup="menu"
            aria-label="主题设置"
            className="icon-button command-icon-button"
            onClick={() => setThemeMenuOpen((current) => !current)}
            ref={themeTriggerRef}
            title="主题设置"
            type="button"
          >
            <ThemeIcon aria-hidden="true" size={17} />
          </button>
          {themeMenuOpen ? (
            <div
              aria-label="主题设置"
              className="theme-menu-panel"
              onKeyDown={handleThemeMenuKeyDown}
              role="menu"
            >
              {themeOptions.map((option, index) => {
                const Icon = option.icon
                const selected = preference === option.value
                return (
                  <button
                    aria-checked={selected}
                    className="theme-menu-item"
                    key={option.value}
                    onClick={() => selectTheme(option.value)}
                    ref={(element) => {
                      themeMenuItemRefs.current[index] = element
                    }}
                    role="menuitemradio"
                    type="button"
                  >
                    <Icon aria-hidden="true" size={16} />
                    <span>{option.label}</span>
                    {selected ? <Check aria-hidden="true" className="theme-menu-check" size={15} /> : null}
                  </button>
                )
              })}
            </div>
          ) : null}
        </div>
        <button
          aria-controls="task-log-panel"
          aria-expanded={!logsCollapsed}
          aria-label={logsCollapsed ? '展开任务日志' : '折叠任务日志'}
          className="icon-button command-icon-button"
          onClick={onToggleLogs}
          title={logsCollapsed ? '展开任务日志' : '折叠任务日志'}
          type="button"
        >
          {logsCollapsed
            ? <PanelBottomOpen aria-hidden="true" size={17} />
            : <PanelBottomClose aria-hidden="true" size={17} />}
        </button>
      </div>
    </header>
  )
}
