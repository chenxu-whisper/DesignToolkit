import type { TaskKind, TaskStatus } from './taskTypes'

export type TaskCreateResponse = {
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
export type ChromaViewMode = 'final' | 'alpha'

export type ChromaKeyerParams = {
  screenGain: number
  screenBalance: number
  clipBlack: number
  clipWhite: number
  shrinkGrow: number
  feather: number
  despillAmount: number
}

export type ChromaKeyerPayload = {
  inputPath: string
  samples: readonly [ChromaKeyerRgbSample]
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
  viewMode: ChromaViewMode
}

export type ChromaPreviewResponse = {
  preview_url: string
}

export type ChromaVideoInspectRequest = ChromaImageInspectRequest

export type ChromaVideoInspectResponse = ChromaImageInspectResponse & {
  duration_seconds: number
  fps: number
  estimated_frame_count: number
  timestamp_seconds: number
}

export type ChromaVideoFrameRequest = {
  inputPath: string
  timestampSeconds: number
}

export type ChromaVideoFrameResponse = {
  preview_url: string
  timestamp_seconds: number
}

export type ChromaVideoSampleRequest = ChromaImageSampleRequest & {
  timestampSeconds: number
}

export type ChromaVideoSampleResponse = ChromaImageSampleResponse & {
  timestamp_seconds: number
}

export type ChromaAlphaSource = 'chroma_key' | 'source'
export type SourceAlphaMode = 'straight' | 'premultiplied'

export type ChromaVideoBasePayload =
  | (Omit<ChromaKeyerPayload, 'outputPath'> & {
      alphaSource?: 'chroma_key'
      sourceAlphaMode?: SourceAlphaMode
    })
  | {
      inputPath: string
      alphaSource: 'source'
      sourceAlphaMode?: SourceAlphaMode
      samples?: readonly ChromaKeyerRgbSample[]
      params?: ChromaKeyerParams
    }

export type ChromaVideoPreviewPayload = ChromaVideoBasePayload & {
  timestampSeconds: number
  viewMode: ChromaViewMode
}

export type ChromaVideoPreviewResponse = ChromaPreviewResponse & {
  timestamp_seconds: number
}

export type ChromaVideoOutputFormat = 'png_sequence' | 'packed_alpha_mp4'

export type ChromaVideoKeyerPayload = ChromaVideoBasePayload & {
  outputFormat?: ChromaVideoOutputFormat
  outputPath?: string
  outputRoot?: string
}

export type DroppedFilePathResolveResponse = {
  paths: string[]
  unresolved: string[]
}

export type LocalPathPickResponse = {
  paths: string[]
  cancelled: boolean
}

export type UploadResponse = {
  reference: string
  filename: string
  size: number
}

type LocalImportResponse = {
  path: string
  filename: string
  size: number
}

type UploadedAssetDeletionTarget = {
  assetId: string
  generation?: string
}

export type LocalFileKind = 'video' | 'media' | 'image' | 'any'
type LocalSaveFileKind = LocalFileKind

const pendingUploadCleanups = new Map<string, UploadedAssetDeletionTarget>()
let pendingCleanupDrain: Promise<void> | null = null

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

export async function importDroppedFiles(
  files: readonly File[],
  onProgress: (percentage: number) => void,
  signal?: AbortSignal,
): Promise<readonly string[]> {
  const paths: string[] = []
  const attemptedIds: string[] = []
  try {
    for (let index = 0; index < files.length; index += 1) {
      throwIfUploadAborted(signal)
      const importId = createUploadId()
      // Record before sending: the server may finish even if its response is lost.
      attemptedIds.push(importId)
      const response = await importLocalFile(files[index], importId, (fraction) => {
        onProgress(Math.round(((index + fraction) / files.length) * 100))
      }, signal)
      throwIfUploadAborted(signal)
      paths.push(response.path)
    }
    return paths
  } catch (error) {
    const cleanupResults = await Promise.allSettled(attemptedIds.map(deleteLocalImport))
    const cleanupErrors = cleanupResults.flatMap((result) =>
      result.status === 'rejected' ? [result.reason] : [],
    )
    if (cleanupErrors.length > 0) {
      const message = error instanceof Error && error.message ? error.message : '文件导入失败'
      throw new AggregateError(
        cleanupErrors,
        `${message}；${cleanupErrors.length} 个导入文件清理失败`,
        { cause: error },
      )
    }
    throw error
  }
}

async function deleteLocalImport(importId: string): Promise<void> {
  const url = `/api/local-paths/import-files/${importId}`
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    let response: Response
    try {
      response = await fetch(url, { method: 'DELETE' })
    } catch (error) {
      if (attempt === 3) {
        throw new Error(`Failed to delete local import ${importId}: network error`, { cause: error })
      }
      continue
    }
    if (response.status === 204 || response.status === 404) {
      return
    }
    if ((response.status !== 409 && response.status < 500) || attempt === 3) {
      throw new Error(`Failed to delete local import ${importId}: ${response.status}`)
    }
  }
}

function importLocalFile(
  file: File,
  importId: string,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<LocalImportResponse> {
  return new Promise((resolve, reject) => {
    throwIfUploadAborted(signal)
    const request = new XMLHttpRequest()
    let settled = false
    const settle = (callback: () => void) => {
      if (settled) return
      settled = true
      signal?.removeEventListener('abort', abortRequest)
      callback()
    }
    const abortRequest = () => {
      request.abort()
      settle(() => reject(uploadAbortError(signal)))
    }
    request.open('POST', '/api/local-paths/import-files')
    request.responseType = 'json'
    request.setRequestHeader('Content-Type', file.type || 'application/octet-stream')
    request.setRequestHeader('X-Upload-Filename', encodeURIComponent(file.name))
    request.setRequestHeader('X-Local-Import-Id', importId)
    signal?.addEventListener('abort', abortRequest, { once: true })
    request.upload.addEventListener('progress', (event) => {
      if (!settled && !signal?.aborted && event.lengthComputable && event.total > 0) {
        onProgress(event.loaded / event.total)
      }
    })
    request.addEventListener('load', () => {
      if (settled) return
      if (signal?.aborted) {
        settle(() => reject(uploadAbortError(signal)))
        return
      }
      const body = request.response as ApiErrorResponse | LocalImportResponse | null
      if (request.status !== 201) {
        const message = body && 'error' in body ? body.error?.message : undefined
        settle(() => reject(new Error(message ?? `Request failed: ${request.status}`)))
        return
      }
      if (
        !body || !('path' in body) || typeof body.path !== 'string' || !body.path
        || typeof body.filename !== 'string' || !body.filename
        || typeof body.size !== 'number' || !Number.isSafeInteger(body.size) || body.size <= 0
      ) {
        settle(() => reject(new Error('Invalid local import response')))
        return
      }
      settle(() => {
        onProgress(1)
        resolve(body)
      })
    })
    request.addEventListener('error', () => settle(() => reject(new Error('文件导入失败'))))
    request.addEventListener('timeout', () => settle(() => reject(new Error('文件导入超时'))))
    request.addEventListener('abort', () => settle(() => reject(uploadAbortError(signal))))
    request.send(file)
  })
}

export async function uploadFiles(
  files: readonly File[],
  onProgress: (percentage: number) => void,
  signal?: AbortSignal,
): Promise<readonly string[]> {
  if (pendingUploadCleanups.size > 0) {
    await drainPendingUploadCleanups()
  }
  const uploaded: UploadResponse[] = []
  const cleanupTargets: UploadedAssetDeletionTarget[] = []
  try {
    for (let index = 0; index < files.length; index += 1) {
      throwIfUploadAborted(signal)
      const file = files[index]
      const uploadId = createUploadId()
      const generation = createUploadGeneration()
      cleanupTargets.push({ assetId: uploadId, generation })
      const response = await uploadFile(file, uploadId, generation, (fileProgress) => {
        onProgress(Math.round(((index + fileProgress) / files.length) * 100))
      }, signal)
      uploaded.push(response)
      throwIfUploadAborted(signal)
    }
    if (pendingUploadCleanups.size > 0) {
      await drainPendingUploadCleanups()
    }
    return uploaded.map(({ reference }) => reference)
  } catch (error) {
    const cleanupResults = await Promise.allSettled(
      cleanupTargets.map((target) => deleteUploadedAssetTarget(target)),
    )
    const cleanupErrors: unknown[] = []
    cleanupResults.forEach((result, index) => {
      const target = cleanupTargets[index]
      const key = uploadedAssetDeletionTargetKey(target)
      if (result.status === 'fulfilled') {
        pendingUploadCleanups.delete(key)
      } else {
        pendingUploadCleanups.set(key, target)
        cleanupErrors.push(result.reason)
      }
    })
    if (cleanupErrors.length > 0) {
      const uploadMessage =
        error instanceof Error && error.message ? error.message : '文件上传失败'
      throw new AggregateError(
        cleanupErrors,
        `${uploadMessage}；${cleanupErrors.length} 个已上传文件清理失败`,
        { cause: error },
      )
    }
    throw error
  }
}

export async function deleteUploadedAsset(
  assetIdOrReference: string,
): Promise<void> {
  const target = uploadedAssetDeletionTarget(assetIdOrReference)
  const key = uploadedAssetDeletionTargetKey(target)
  try {
    await deleteUploadedAssetTarget(target)
    pendingUploadCleanups.delete(key)
  } catch (error) {
    pendingUploadCleanups.set(key, target)
    throw error
  }
}

async function deleteUploadedAssetTarget({
  assetId,
  generation,
}: UploadedAssetDeletionTarget): Promise<void> {
  const generationQuery = generation === undefined
    ? ''
    : `?generation=${encodeURIComponent(generation)}`
  const url = `/api/uploads/${encodeURIComponent(assetId)}${generationQuery}`
  const maximumAttempts = 3
  for (let attempt = 1; attempt <= maximumAttempts; attempt += 1) {
    let response: Response
    try {
      response = await fetch(url, { method: 'DELETE' })
    } catch (error) {
      if (attempt === maximumAttempts) {
        throw new Error(
          `Failed to delete uploaded asset ${assetId}: network error`,
          { cause: error },
        )
      }
      continue
    }
    if (response.ok || response.status === 404) {
      return
    }
    const failure = new Error(
      `Failed to delete uploaded asset ${assetId}: ${response.status}`,
    )
    if (
      (response.status !== 409 && response.status < 500)
      || attempt === maximumAttempts
    ) {
      throw failure
    }
  }
}

async function drainPendingUploadCleanups(): Promise<void> {
  if (pendingCleanupDrain) {
    await pendingCleanupDrain
    return
  }
  const targets = Array.from(pendingUploadCleanups.entries())
  if (targets.length === 0) {
    return
  }
  pendingCleanupDrain = (async () => {
    const results = await Promise.allSettled(
      targets.map(([, target]) => deleteUploadedAssetTarget(target)),
    )
    results.forEach((result, index) => {
      if (result.status !== 'fulfilled') {
        return
      }
      const [key, target] = targets[index]
      if (pendingUploadCleanups.get(key) === target) {
        pendingUploadCleanups.delete(key)
      }
    })
  })()
  try {
    await pendingCleanupDrain
  } finally {
    pendingCleanupDrain = null
  }
}

function uploadedAssetDeletionTarget(value: string): UploadedAssetDeletionTarget {
  const parts = value.split(':')
  if (
    parts[0] === 'asset'
    && (parts.length === 3 || parts.length === 4)
    && parts[1]
  ) {
    return {
      assetId: parts[1],
      generation: parts.length === 4 && parts[2] ? parts[2] : undefined,
    }
  }
  return { assetId: value }
}

function uploadedAssetDeletionTargetKey({
  assetId,
  generation,
}: UploadedAssetDeletionTarget): string {
  return `${assetId}:${generation ?? ''}`
}

function uploadFile(
  file: File,
  uploadId: string,
  generation: string,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    let settled = false
    const settle = (callback: () => void) => {
      if (settled) {
        return
      }
      settled = true
      signal?.removeEventListener('abort', abortRequest)
      callback()
    }
    const abortRequest = () => request.abort()

    if (signal?.aborted) {
      reject(uploadAbortError(signal))
      return
    }

    request.open('POST', '/api/uploads')
    request.responseType = 'json'
    request.setRequestHeader('Content-Type', file.type || 'application/octet-stream')
    request.setRequestHeader('X-Upload-Filename', encodeURIComponent(file.name))
    request.setRequestHeader('X-Upload-Id', uploadId)
    request.setRequestHeader('X-Upload-Generation', generation)
    signal?.addEventListener('abort', abortRequest, { once: true })
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(event.loaded / event.total)
      }
    })
    request.addEventListener('load', () => {
      const body = request.response as ApiErrorResponse | UploadResponse | null
      if (request.status < 200 || request.status >= 300) {
        const message =
          body && typeof body === 'object' && 'error' in body
            ? body.error?.message
            : undefined
        settle(() => reject(new Error(message ?? `Request failed: ${request.status}`)))
        return
      }
      onProgress(1)
      settle(() => resolve(body as UploadResponse))
    })
    request.addEventListener('error', () => {
      settle(() => reject(new Error('文件上传失败')))
    })
    request.addEventListener('abort', () => {
      settle(() => reject(uploadAbortError(signal)))
    })
    request.send(file)
  })
}

function createUploadId(): string {
  return crypto.randomUUID().replaceAll('-', '')
}

function createUploadGeneration(): string {
  return crypto.randomUUID().replaceAll('-', '')
}

export function resetUploadCleanupQueueForTests(): void {
  pendingUploadCleanups.clear()
  pendingCleanupDrain = null
}

function throwIfUploadAborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw uploadAbortError(signal)
  }
}

function uploadAbortError(signal?: AbortSignal): Error {
  return signal?.reason instanceof Error
    ? signal.reason
    : new DOMException('文件上传已取消', 'AbortError')
}

export function createMediaResizeTask(payload: MediaResizePayload): Promise<TaskCreateResponse> {
  return request<TaskCreateResponse>('/api/tasks/media-resize', payload)
}

function chromaParamsToWire(params: ChromaKeyerParams) {
  return {
    screen_gain: params.screenGain,
    screen_balance: params.screenBalance,
    clip_black: params.clipBlack,
    clip_white: params.clipWhite,
    shrink_grow: params.shrinkGrow,
    feather: params.feather,
    despill_amount: params.despillAmount,
  }
}

function chromaBasePayloadToWire(payload: Omit<ChromaKeyerPayload, 'outputPath'>) {
  return {
    input_path: payload.inputPath,
    samples: payload.samples,
    params: chromaParamsToWire(payload.params),
  }
}

function chromaPayloadToWire(payload: ChromaKeyerPayload) {
  const outputPath = payload.outputPath?.trim()
  return {
    ...chromaBasePayloadToWire(payload),
    ...(outputPath ? { output_path: outputPath } : {}),
  }
}

function chromaPreviewPayloadToWire(payload: ChromaPreviewPayload) {
  return {
    ...chromaBasePayloadToWire(payload),
    view_mode: payload.viewMode,
  }
}

function chromaVideoPreviewPayloadToWire(payload: ChromaVideoPreviewPayload) {
  const { input_path, ...previewPayload } = chromaVideoBasePayloadToWire(payload)
  return {
    input_path,
    timestamp_seconds: payload.timestampSeconds,
    ...previewPayload,
    view_mode: payload.viewMode,
  }
}

function chromaVideoBasePayloadToWire(payload: ChromaVideoBasePayload) {
  if (payload.alphaSource === 'source') {
    return {
      input_path: payload.inputPath,
      alpha_source: 'source',
      source_alpha_mode: payload.sourceAlphaMode ?? 'straight',
      samples: [],
    }
  }
  return chromaBasePayloadToWire(payload)
}

function chromaVideoKeyerPayloadToWire(payload: ChromaVideoKeyerPayload) {
  const { input_path, ...keyerPayload } = chromaVideoBasePayloadToWire(payload)
  const outputRoot = payload.outputRoot?.trim()
  const outputPath = payload.outputPath?.trim()
  return {
    input_path,
    ...(payload.outputFormat ? { output_format: payload.outputFormat } : {}),
    ...(outputPath ? { output_path: outputPath } : {}),
    ...(outputRoot ? { output_root: outputRoot } : {}),
    ...keyerPayload,
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

export function inspectChromaVideo(payload: ChromaVideoInspectRequest): Promise<ChromaVideoInspectResponse> {
  return request<ChromaVideoInspectResponse>('/api/chroma-keyer/videos/inspect', {
    input_path: payload.inputPath,
  })
}

export function createChromaVideoFrame(payload: ChromaVideoFrameRequest): Promise<ChromaVideoFrameResponse> {
  return request<ChromaVideoFrameResponse>('/api/chroma-keyer/videos/frame', {
    input_path: payload.inputPath,
    timestamp_seconds: payload.timestampSeconds,
  })
}

export function sampleChromaVideo(payload: ChromaVideoSampleRequest): Promise<ChromaVideoSampleResponse> {
  return request<ChromaVideoSampleResponse>('/api/chroma-keyer/videos/sample', {
    input_path: payload.inputPath,
    timestamp_seconds: payload.timestampSeconds,
    x: payload.x,
    y: payload.y,
    preview_width: payload.previewWidth,
    preview_height: payload.previewHeight,
  })
}

export function createChromaPreview(payload: ChromaPreviewPayload): Promise<ChromaPreviewResponse> {
  return request<ChromaPreviewResponse>('/api/chroma-keyer/preview', chromaPreviewPayloadToWire(payload))
}

export function createChromaVideoPreview(payload: ChromaVideoPreviewPayload): Promise<ChromaVideoPreviewResponse> {
  return request<ChromaVideoPreviewResponse>(
    '/api/chroma-keyer/videos/preview',
    chromaVideoPreviewPayloadToWire(payload),
  )
}

export function createChromaKeyerTask(payload: ChromaKeyerPayload): Promise<TaskCreateResponse> {
  return request<TaskCreateResponse>('/api/tasks/chroma-keyer', chromaPayloadToWire(payload))
}

export function createChromaVideoKeyerTask(payload: ChromaVideoKeyerPayload): Promise<TaskCreateResponse> {
  return request<TaskCreateResponse>('/api/tasks/chroma-keyer-video', chromaVideoKeyerPayloadToWire(payload))
}

export function getTask(taskId: string): Promise<TaskResponse> {
  return requestGet<TaskResponse>(`/api/tasks/${encodeURIComponent(taskId)}`)
}

export function resolveDroppedFilePaths(names: readonly string[]): Promise<DroppedFilePathResolveResponse> {
  return request<DroppedFilePathResolveResponse>('/api/local-paths/resolve-dropped-files', { names })
}

export function pickLocalFiles({
  allowedSuffixes,
  fileKind,
  multiple,
  title,
}: {
  allowedSuffixes?: readonly string[]
  fileKind: LocalFileKind
  multiple: boolean
  title: string
}): Promise<LocalPathPickResponse> {
  return request<LocalPathPickResponse>('/api/local-paths/pick-files', {
    allowed_suffixes: allowedSuffixes,
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
