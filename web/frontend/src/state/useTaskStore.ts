import { create } from 'zustand'
import type { TaskKind, TaskStatus } from '../services/taskTypes'

type StartTaskInput = {
  taskId: string
  kind: TaskKind
  status?: Extract<TaskStatus, 'queued' | 'running'>
}

type CompleteTaskInput = {
  taskId: string
  resultPath: string
}

type FailTaskInput = {
  taskId?: string | null
  error: string
}

type TaskState = {
  currentTaskId: string | null
  kind: TaskKind | null
  status: TaskStatus
  resultPath: string | null
  error: string | null
  isCreating: boolean
  startTask: (input: StartTaskInput) => void
  completeTask: (input: CompleteTaskInput) => void
  failTask: (input: FailTaskInput) => void
  resetTask: () => void
  beginCreate: () => void
  endCreate: () => void
}

export const useTaskStore = create<TaskState>((set) => ({
  currentTaskId: null,
  kind: null,
  status: 'idle',
  resultPath: null,
  error: null,
  isCreating: false,
  startTask: ({ taskId, kind, status = 'running' }) =>
    set({ currentTaskId: taskId, kind, status, resultPath: null, error: null }),
  completeTask: ({ taskId, resultPath }) =>
    set((state) => (state.currentTaskId === taskId ? { status: 'succeeded', resultPath, error: null } : {})),
  failTask: ({ taskId = null, error }) =>
    set((state) => {
      if (taskId !== null && state.currentTaskId !== taskId) {
        return {}
      }
      if (taskId === null && state.currentTaskId !== null) {
        return {}
      }
      return { status: 'failed', resultPath: null, error }
    }),
  resetTask: () => set({ currentTaskId: null, kind: null, status: 'idle', resultPath: null, error: null, isCreating: false }),
  beginCreate: () => set({ isCreating: true }),
  endCreate: () => set({ isCreating: false }),
}))
