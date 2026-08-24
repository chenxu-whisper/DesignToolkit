import { useState } from 'react'
import { HeaderBar } from '../components/HeaderBar'
import { LogPanel } from '../components/LogPanel'
import { Sidebar } from '../components/Sidebar'
import { toolModules } from './modules'
import { TaskEventController } from './TaskEventController'
import type { ModuleId } from './types'

export function AppShell() {
  const [activeModuleId, setActiveModuleId] = useState<ModuleId>('video_frames')
  const activeModule = toolModules.find((item) => item.id === activeModuleId) ?? toolModules[0]
  const ActiveComponent = activeModule.component

  return (
    <div className="app-shell">
      <HeaderBar />
      <TaskEventController />
      <Sidebar activeModuleId={activeModuleId} modules={toolModules} onModuleSelect={setActiveModuleId} />
      <main className="workspace">
        <ActiveComponent />
      </main>
      <LogPanel />
    </div>
  )
}
