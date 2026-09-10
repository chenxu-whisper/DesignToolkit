import { type CSSProperties, useEffect, useState } from 'react'
import { HeaderBar } from '../components/HeaderBar'
import { LogPanel } from '../components/LogPanel'
import { Sidebar } from '../components/Sidebar'
import { toolModules } from './modules'
import { TaskEventController } from './TaskEventController'
import type { ModuleId } from './types'

const LOG_COLLAPSED_KEY = 'designtoolkit-log-collapsed'
const LOG_HEIGHT_KEY = 'designtoolkit-log-height'
const DEFAULT_LOG_HEIGHT = 180

function readLogCollapsed() {
  const stored = window.localStorage.getItem(LOG_COLLAPSED_KEY)
  return stored === null ? true : stored === 'true'
}

function readLogHeight() {
  const stored = Number(window.localStorage.getItem(LOG_HEIGHT_KEY))
  return Number.isFinite(stored) && stored >= 120 ? stored : DEFAULT_LOG_HEIGHT
}

export function AppShell() {
  const [activeModuleId, setActiveModuleId] = useState<ModuleId>('video_frames')
  const [logsCollapsed, setLogsCollapsed] = useState(readLogCollapsed)
  const [logHeight, setLogHeight] = useState(readLogHeight)
  const activeModule = toolModules.find((item) => item.id === activeModuleId) ?? toolModules[0]
  const ActiveComponent = activeModule.component

  useEffect(() => {
    window.localStorage.setItem(LOG_COLLAPSED_KEY, String(logsCollapsed))
  }, [logsCollapsed])

  useEffect(() => {
    window.localStorage.setItem(LOG_HEIGHT_KEY, String(logHeight))
  }, [logHeight])

  return (
    <div
      className="app-shell"
      style={{
        '--log-panel-height': `${logsCollapsed ? 44 : logHeight}px`,
      } as CSSProperties}
    >
      <HeaderBar
        activeModule={activeModule}
        logsCollapsed={logsCollapsed}
        onToggleLogs={() => setLogsCollapsed((current) => !current)}
      />
      <TaskEventController />
      <Sidebar activeModuleId={activeModuleId} modules={toolModules} onModuleSelect={setActiveModuleId} />
      <main className="workspace">
        <ActiveComponent />
      </main>
      <LogPanel
        collapsed={logsCollapsed}
        height={logHeight}
        onCollapsedChange={setLogsCollapsed}
        onHeightChange={setLogHeight}
      />
    </div>
  )
}
