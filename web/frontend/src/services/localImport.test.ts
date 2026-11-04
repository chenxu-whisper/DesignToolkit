import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as apiClient from './apiClient'

class ImportRequest {
  static requests: ImportRequest[] = []
  status = 201
  response: unknown = null
  responseType = ''
  headers = new Map<string, string>()
  listeners = new Map<string, () => void>()
  progress?: (event: { lengthComputable: boolean; loaded: number; total: number }) => void
  upload = {
    addEventListener: (_type: string, listener: NonNullable<ImportRequest['progress']>) => {
      this.progress = listener
    },
  }
  open = vi.fn()
  send = vi.fn()
  abort = vi.fn(() => this.listeners.get('abort')?.())

  constructor() {
    ImportRequest.requests.push(this)
  }

  setRequestHeader(name: string, value: string) {
    this.headers.set(name, value)
  }

  addEventListener(type: string, listener: () => void) {
    this.listeners.set(type, listener)
  }

  complete(path: string, filename = 'clip.mp4', size = 5) {
    this.response = { path, filename, size }
    this.listeners.get('load')?.()
  }
}

beforeEach(() => {
  ImportRequest.requests = []
  vi.stubGlobal('XMLHttpRequest', ImportRequest)
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('importDroppedFiles', () => {
  it('sends actual binary File bytes with an encoded name and 32-hex cleanup identity', async () => {
    expect(apiClient).toHaveProperty('importDroppedFiles', expect.any(Function))
    const file = new File(['video'], 'outside clip%.mp4', { type: 'video/mp4' })
    const progress = vi.fn()
    const pending = apiClient.importDroppedFiles([file], progress)
    const request = ImportRequest.requests[0]

    expect(request.open).toHaveBeenCalledWith('POST', '/api/local-paths/import-files')
    expect(request.send).toHaveBeenCalledWith(file)
    expect(request.responseType).toBe('json')
    expect(request.headers.get('X-Local-Import-Id')).toMatch(/^[a-f0-9]{32}$/)
    expect(request.headers.get('X-Upload-Filename')).toBe(encodeURIComponent(file.name))
    expect(request.headers.get('Content-Type')).toBe('video/mp4')
    request.progress?.({ lengthComputable: true, loaded: 2, total: 5 })
    expect(progress).toHaveBeenLastCalledWith(40)
    request.complete('E:\\workspace\\.temp\\local-imports\\clip.mp4', file.name)

    await expect(pending).resolves.toEqual(['E:\\workspace\\.temp\\local-imports\\clip.mp4'])
    expect(progress).toHaveBeenLastCalledWith(100)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('returns ordered paths and aggregates progress across files', async () => {
    const progress = vi.fn()
    const pending = apiClient.importDroppedFiles(
      [new File(['a'], 'a.png'), new File(['b'], 'b.png')],
      progress,
    )
    const first = ImportRequest.requests[0]
    expect(first.headers.get('Content-Type')).toBe('application/octet-stream')
    first.complete('E:\\imports\\a.png', 'a.png', 1)
    await vi.waitFor(() => expect(ImportRequest.requests).toHaveLength(2))
    const second = ImportRequest.requests[1]
    expect(second.headers.get('X-Local-Import-Id')).not.toBe(first.headers.get('X-Local-Import-Id'))
    second.progress?.({ lengthComputable: true, loaded: 1, total: 2 })
    expect(progress).toHaveBeenLastCalledWith(75)
    second.complete('E:\\imports\\b.png', 'b.png', 1)
    await expect(pending).resolves.toEqual(['E:\\imports\\a.png', 'E:\\imports\\b.png'])
  })

  it('cleans all attempted IDs on batch failure, never using original or returned paths for DELETE', async () => {
    const pending = apiClient.importDroppedFiles(
      [new File(['a'], 'a.png'), new File(['b'], 'b.png'), new File(['c'], 'c.png')],
      vi.fn(),
    )
    const rejected = expect(pending).rejects.toThrow('Import too large')
    ImportRequest.requests[0].complete('C:\\original\\a.png', 'a.png', 1)
    await vi.waitFor(() => expect(ImportRequest.requests).toHaveLength(2))
    ImportRequest.requests[1].status = 413
    ImportRequest.requests[1].response = { error: { code: 'too_large', message: 'Import too large' } }
    ImportRequest.requests[1].listeners.get('load')?.()
    await rejected

    expect(ImportRequest.requests).toHaveLength(2)
    expect(fetch).toHaveBeenCalledTimes(2)
    for (const request of ImportRequest.requests) {
      expect(fetch).toHaveBeenCalledWith(
        `/api/local-paths/import-files/${request.headers.get('X-Local-Import-Id')}`,
        { method: 'DELETE' },
      )
    }
  })

  it.each(['error', 'timeout'])('cleans an import with a lost %s response', async (event) => {
    const pending = apiClient.importDroppedFiles([new File(['a'], 'a.png')], vi.fn())
    const rejected = expect(pending).rejects.toThrow()
    const request = ImportRequest.requests[0]
    request.listeners.get(event)?.()
    await rejected
    expect(fetch).toHaveBeenCalledWith(
      `/api/local-paths/import-files/${request.headers.get('X-Local-Import-Id')}`,
      { method: 'DELETE' },
    )
  })

  it('aborts transport and cleans both completed and in-flight imports', async () => {
    const controller = new AbortController()
    const progress = vi.fn()
    const pending = apiClient.importDroppedFiles(
      [new File(['a'], 'a.png'), new File(['b'], 'b.png')],
      progress,
      controller.signal,
    )
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    ImportRequest.requests[0].complete('E:\\imports\\a.png', 'a.png', 1)
    await vi.waitFor(() => expect(ImportRequest.requests).toHaveLength(2))
    const second = ImportRequest.requests[1]
    controller.abort()
    const count = progress.mock.calls.length
    second.progress?.({ lengthComputable: true, loaded: 1, total: 1 })
    second.complete('E:\\imports\\b.png', 'b.png', 1)
    await rejected
    expect(second.abort).toHaveBeenCalledOnce()
    expect(progress).toHaveBeenCalledTimes(count)
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('does not start transport or cleanup when already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(apiClient.importDroppedFiles(
      [new File(['a'], 'a.png')], vi.fn(), controller.signal,
    )).rejects.toMatchObject({ name: 'AbortError' })
    expect(ImportRequest.requests).toHaveLength(0)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('returns no paths for an empty batch', async () => {
    await expect(apiClient.importDroppedFiles([], vi.fn())).resolves.toEqual([])
    expect(ImportRequest.requests).toHaveLength(0)
  })

  it.each([
    { status: 200, response: { path: 'E:\\imports\\a.png', filename: 'a.png', size: 1 } },
    { status: 201, response: null },
  ])('rejects an invalid success response and cleans its client identity: %j', async (result) => {
    const pending = apiClient.importDroppedFiles([new File(['a'], 'a.png')], vi.fn())
    const rejected = expect(pending).rejects.toThrow()
    Object.assign(ImportRequest.requests[0], result)
    ImportRequest.requests[0].listeners.get('load')?.()
    await rejected
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('retries transient cleanup errors and reports persistent cleanup with the original cause', async () => {
    vi.mocked(fetch)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const pending = apiClient.importDroppedFiles([new File(['a'], 'a.png')], vi.fn())
    const rejected = expect(pending).rejects.toThrow('Import denied')
    ImportRequest.requests[0].status = 400
    ImportRequest.requests[0].response = { error: { code: 'denied', message: 'Import denied' } }
    ImportRequest.requests[0].listeners.get('load')?.()
    await rejected
    expect(fetch).toHaveBeenCalledTimes(3)

    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 403 }))
    const next = apiClient.importDroppedFiles([new File(['b'], 'b.png')], vi.fn())
    const nextRejected = expect(next).rejects.toMatchObject({
      name: 'AggregateError',
      cause: expect.objectContaining({ message: 'Import denied' }),
    })
    ImportRequest.requests[1].status = 400
    ImportRequest.requests[1].response = { error: { code: 'denied', message: 'Import denied' } }
    ImportRequest.requests[1].listeners.get('load')?.()
    await nextRejected
  })
})
