import type { TaskKind, TaskStatus } from './taskTypes'

type TaskCreateResponse = {
  task_id: string
  status: Exclude<TaskStatus, 'idle'>
}

export type TaskResponse = {
  task_id: string
  kind: TaskKind
  status: Exclude<TaskStatus, 'idle'>
  created_at: string
  started_at: string | null
  finished_at: string | null
  input_path: string
  output_path: string | null
  error: string | null
}

type ApiErrorResponse = {
  error?: {
    code: string
    message: string
  }
}

type VideoFramesBasePayload = {
  output_dir?: string
  image_format: 'png' | 'jpg' | 'jpeg'
  size?: string
  fps?: number
  frames?: number
  overwrite: boolean
}

export type VideoFramesPayload = VideoFramesBasePayload &
  (
    | {
        input_path: string
        input_paths?: never
      }
    | {
        input_path?: never
        input_paths: readonly string[]
      }
  )

type MediaResizeBasePayload = {
  size: string
}

export type MediaResizePayload = MediaResizeBasePayload &
  (
    | {
        input_path: string
        input_paths?: never
        output_path?: string
      }
    | {
        input_path?: never
        input_paths: readonly string[]
        output_path?: never
      }
  )

export type DroppedFilePathResolveResponse = {
  paths: string[]
  unresolved: string[]
}

export type LocalPathPickResponse = {
  paths: string[]
  cancelled: boolean
}

export type LocalFileKind = 'video' | 'media' | 'any'

async function readJson<T>(response: Response): Promise<ApiErrorResponse | T | null> {
  try {
    return (await response.json()) as ApiErrorResponse | T
  } catch (error) {
    if (!response.ok) {
      return null
    }
    throw error
  }
}

async function request<T>(path: string, payload: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await readJson<T>(response)

  if (!response.ok) {
    const apiMessage =
      body && typeof body === 'object' && 'error' in body ? (body as ApiErrorResponse).error?.message : undefined
    throw new Error(apiMessage ?? `Request failed: ${response.status}`)
  }

  return body as T
}

async function requestGet<T>(path: string): Promise<T> {
  const response = await fetch(path)
  const body = await readJson<T>(response)

  if (!response.ok) {
    const apiMessage =
      body && typeof body === 'object' && 'error' in body ? (body as ApiErrorResponse).error?.message : undefined
    throw new Error(apiMessage ?? `Request failed: ${response.status}`)
  }

  return body as T
}

export function createVideoFramesTask(payload: VideoFramesPayload): Promise<TaskCreateResponse> {
  return request<TaskCreateResponse>('/api/tasks/video-frames', payload)
}

export function createMediaResizeTask(payload: MediaResizePayload): Promise<TaskCreateResponse> {
  return request<TaskCreateResponse>('/api/tasks/media-resize', payload)
}

export function getTask(taskId: string): Promise<TaskResponse> {
  return requestGet<TaskResponse>(`/api/tasks/${encodeURIComponent(taskId)}`)
}

export function resolveDroppedFilePaths(names: readonly string[]): Promise<DroppedFilePathResolveResponse> {
  return request<DroppedFilePathResolveResponse>('/api/local-paths/resolve-dropped-files', { names })
}

export function pickLocalFiles({
  fileKind,
  multiple,
  title,
}: {
  fileKind: LocalFileKind
  multiple: boolean
  title: string
}): Promise<LocalPathPickResponse> {
  return request<LocalPathPickResponse>('/api/local-paths/pick-files', {
    multiple,
    file_kind: fileKind,
    title,
  })
}

export function pickLocalDirectory({ title }: { title: string }): Promise<LocalPathPickResponse> {
  return request<LocalPathPickResponse>('/api/local-paths/pick-directory', { title })
}

export function pickLocalSaveFile({
  fileKind,
  title,
}: {
  fileKind: Exclude<LocalFileKind, 'video'>
  title: string
}): Promise<LocalPathPickResponse> {
  return request<LocalPathPickResponse>('/api/local-paths/pick-save-file', {
    file_kind: fileKind,
    title,
  })
}
