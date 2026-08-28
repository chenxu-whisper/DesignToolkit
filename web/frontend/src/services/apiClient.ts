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

export type ChromaKeyerRgbSample = readonly [red: number, green: number, blue: number]

export type ChromaKeyerParams = {
  tolerance: number
  softness: number
  clipBlack: number
  clipWhite: number
  shrinkGrow: number
  feather: number
  despillAmount: number
}

export type ChromaKeyerPayload = {
  inputPath: string
  samples: readonly [ChromaKeyerRgbSample, ...ChromaKeyerRgbSample[]]
  params: ChromaKeyerParams
  outputPath?: string
}

export type ChromaImageInspectRequest = {
  inputPath: string
}

export type ChromaImageInspectResponse = {
  input_path: string
  width: number
  height: number
  preview_width: number
  preview_height: number
  preview_url: string
}

export type ChromaImageSampleRequest = {
  inputPath: string
  x: number
  y: number
  previewWidth: number
  previewHeight: number
}

export type ChromaImageSampleResponse = {
  rgb: ChromaKeyerRgbSample
  hex: string
  source_x: number
  source_y: number
}

export type ChromaPreviewPayload = Omit<ChromaKeyerPayload, 'outputPath'> & {
  viewMode: 'final' | 'alpha'
}

export type ChromaPreviewResponse = {
  preview_url: string
}

export type DroppedFilePathResolveResponse = {
  paths: string[]
  unresolved: string[]
}

export type LocalPathPickResponse = {
  paths: string[]
  cancelled: boolean
}

export type LocalFileKind = 'video' | 'media' | 'image' | 'any'
type LocalSaveFileKind = 'media' | 'any'

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

function chromaParamsToWire(params: ChromaKeyerParams) {
  return {
    tolerance: params.tolerance,
    softness: params.softness,
    clip_black: params.clipBlack,
    clip_white: params.clipWhite,
    shrink_grow: params.shrinkGrow,
    feather: params.feather,
    despill_amount: params.despillAmount,
  }
}

function chromaPayloadToWire(payload: ChromaKeyerPayload) {
  const outputPath = payload.outputPath?.trim()
  return {
    input_path: payload.inputPath,
    samples: payload.samples,
    params: chromaParamsToWire(payload.params),
    ...(outputPath ? { output_path: outputPath } : {}),
  }
}

export function inspectChromaImage(payload: ChromaImageInspectRequest): Promise<ChromaImageInspectResponse> {
  return request<ChromaImageInspectResponse>('/api/chroma-keyer/images/inspect', {
    input_path: payload.inputPath,
  })
}

export function sampleChromaImage(payload: ChromaImageSampleRequest): Promise<ChromaImageSampleResponse> {
  return request<ChromaImageSampleResponse>('/api/chroma-keyer/images/sample', {
    input_path: payload.inputPath,
    x: payload.x,
    y: payload.y,
    preview_width: payload.previewWidth,
    preview_height: payload.previewHeight,
  })
}

export function createChromaPreview(payload: ChromaPreviewPayload): Promise<ChromaPreviewResponse> {
  return request<ChromaPreviewResponse>('/api/chroma-keyer/preview', {
    ...chromaPayloadToWire(payload),
    view_mode: payload.viewMode,
  })
}

export function createChromaKeyerTask(payload: ChromaKeyerPayload): Promise<TaskCreateResponse> {
  return request<TaskCreateResponse>('/api/tasks/chroma-keyer', chromaPayloadToWire(payload))
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
  fileKind: LocalSaveFileKind
  title: string
}): Promise<LocalPathPickResponse> {
  return request<LocalPathPickResponse>('/api/local-paths/pick-save-file', {
    file_kind: fileKind,
    title,
  })
}
