import { create } from 'zustand'
import type { TaskEvent } from '../services/taskTypes'

function elapsedText(details: Record<string, unknown>) {
  const elapsedSeconds = details.elapsed_seconds
  if (typeof elapsedSeconds !== 'number' || !Number.isFinite(elapsedSeconds)) {
    return ''
  }
  return ` 耗时 ${elapsedSeconds.toFixed(2)}s`
}

type LogState = {
  events: TaskEvent[]
  autoScroll: boolean
  appendEvent: (event: TaskEvent) => void
  clearEvents: () => void
  setAutoScroll: (enabled: boolean) => void
  text: () => string
}

export const useLogStore = create<LogState>((set, get) => ({
  events: [],
  autoScroll: true,
  appendEvent: (event) => set((state) => ({ events: [...state.events, event] })),
  clearEvents: () => set({ events: [] }),
  setAutoScroll: (enabled) => set({ autoScroll: enabled }),
  text: () =>
    get()
      .events.map((event) => `[${event.timestamp}] ${event.level} ${event.phase}: ${event.message}${elapsedText(event.details)}`)
      .join('\n'),
}))
