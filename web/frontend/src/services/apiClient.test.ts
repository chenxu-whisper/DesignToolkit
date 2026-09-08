import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest'
import {
  createChromaKeyerTask,
  createChromaPreview,
  createChromaVideoFrame,
  createChromaVideoKeyerTask,
  createChromaVideoPreview,
  createMediaResizeTask,
  createVideoFramesTask,
  deleteUploadedAsset,
  getTask,
  inspectChromaImage,
  inspectChromaVideo,
  pickLocalDirectory,
  pickLocalFiles,
  pickLocalSaveFile,
  resolveDroppedFilePaths,
  sampleChromaImage,
  sampleChromaVideo,
  uploadFiles,
  type ChromaKeyerPayload,
  type ChromaKeyerRgbSample,
  type ChromaPreviewPayload,
  type ChromaVideoFrameRequest,
  type ChromaVideoInspectRequest,
  type ChromaVideoKeyerPayload,
  type ChromaVideoPreviewPayload,
  type ChromaVideoSampleRequest,
  type TaskCreateResponse,
} from './apiClient'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('apiClient', () => {
  it('deletes a four-part asset reference with an encoded generation URL', async () => {
    const cleanup = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 204 }))
    const assetId = 'asset/id with space'
    const generation = 'generation/value'

    await deleteUploadedAsset(
      `asset:${assetId}:${generation}:素材%3A100%25.png`,
    )

    expect(cleanup).toHaveBeenCalledWith(
      `/api/uploads/${encodeURIComponent(assetId)}?generation=${encodeURIComponent(generation)}`,
      { method: 'DELETE' },
    )
  })

  it('uploads files sequentially as raw bodies with aggregate progress', async () => {
    const requests: MockUploadRequest[] = []
    vi.spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValueOnce('11111111-1111-1111-1111-111111111111')
      .mockReturnValueOnce('22222222-2222-2222-2222-222222222222')

    class MockUploadRequest {
      method = ''
      url = ''
      responseType = ''
      status = 201
      response: unknown = null
      headers = new Map<string, string>()
      sentBody: Document | XMLHttpRequestBodyInit | null = null
      listeners = new Map<string, () => void>()
      uploadListeners = new Map<string, (event: ProgressEvent) => void>()
      upload = {
        addEventListener: (
          type: string,
          listener: (event: ProgressEvent) => void,
        ) => this.uploadListeners.set(type, listener),
      }

      constructor() {
        requests.push(this)
      }

      open(method: string, url: string) {
        this.method = method
        this.url = url
      }

      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value)
      }

      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, listener)
      }

      send(body: Document | XMLHttpRequestBodyInit | null) {
        this.sentBody = body
      }
    }

    vi.stubGlobal('XMLHttpRequest', MockUploadRequest)
    const progress: number[] = []
    const first = new File(['first'], 'first image.png', { type: 'image/png' })
    const second = new File(['second'], '第二张.png')
    const pending = uploadFiles([first, second], (value) => progress.push(value))

    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('POST')
    expect(requests[0].url).toBe('/api/uploads')
    expect(requests[0].headers.get('Content-Type')).toBe('image/png')
    expect(requests[0].headers.get('X-Upload-Filename')).toBe(
      encodeURIComponent(first.name),
    )
    expect(requests[0].headers.get('X-Upload-Id')).toBe('1'.repeat(32))
    expect(requests[0].sentBody).toBe(first)

    requests[0].uploadListeners.get('progress')?.(
      new ProgressEvent('progress', { lengthComputable: true, loaded: 2, total: 5 }),
    )
    requests[0].response = {
      filename: first.name,
      reference: 'asset:first',
      size: first.size,
    }
    requests[0].listeners.get('load')?.()
    await Promise.resolve()

    expect(requests).toHaveLength(2)
    expect(requests[1].headers.get('Content-Type')).toBe(
      'application/octet-stream',
    )
    expect(requests[1].headers.get('X-Upload-Filename')).toBe(
      encodeURIComponent(second.name),
    )
    expect(requests[1].headers.get('X-Upload-Id')).toBe('2'.repeat(32))
    expect(requests[1].sentBody).toBe(second)

    requests[1].response = {
      filename: second.name,
      reference: 'asset:second',
      size: second.size,
    }
    requests[1].listeners.get('load')?.()

    await expect(pending).resolves.toEqual(['asset:first', 'asset:second'])
    expect(progress).toEqual([20, 50, 100])
  })

  it('aborts an in-flight upload when its signal is cancelled', async () => {
    const requests: MockUploadRequest[] = []
    const uploadUuid = '11111111-1111-1111-1111-111111111111'
    const uploadId = uploadUuid.replaceAll('-', '')

    class MockUploadRequest {
      response: unknown = null
      status = 0
      headers = new Map<string, string>()
      listeners = new Map<string, () => void>()
      upload = {
        addEventListener: () => undefined,
      }
      abort = vi.fn(() => this.listeners.get('abort')?.())

      constructor() {
        requests.push(this)
      }

      open() {}

      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value)
      }

      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, listener)
      }

      send() {}
    }

    vi.stubGlobal('XMLHttpRequest', MockUploadRequest)
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue(uploadUuid)
    const cleanup = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 404 }))
    const controller = new AbortController()
    const pending = uploadFiles(
      [new File(['image'], 'image.png')],
      () => undefined,
      controller.signal,
    )

    controller.abort()

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(requests[0].abort).toHaveBeenCalledOnce()
    expect(requests[0].headers.get('X-Upload-Id')).toBe(uploadId)
    expect(cleanup).toHaveBeenCalledOnce()
    expect(cleanup).toHaveBeenCalledWith(`/api/uploads/${uploadId}`, {
      method: 'DELETE',
    })
  })

  it('deletes a committed upload when cancellation wins the response race', async () => {
    const requests: MockUploadRequest[] = []
    const uploadUuid = '11111111-1111-1111-1111-111111111111'
    const assetId = uploadUuid.replaceAll('-', '')
    const generation = '2'.repeat(32)

    class MockUploadRequest {
      response: unknown = null
      status = 201
      headers = new Map<string, string>()
      listeners = new Map<string, () => void>()
      upload = {
        addEventListener: () => undefined,
      }
      abort = vi.fn()

      constructor() {
        requests.push(this)
      }

      open() {}

      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value)
      }

      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, listener)
      }

      send() {}
    }

    vi.stubGlobal('XMLHttpRequest', MockUploadRequest)
    vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue(uploadUuid)
    const cleanup = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 204 }))
    const controller = new AbortController()
    const pending = uploadFiles(
      [new File(['image'], 'image.png')],
      () => undefined,
      controller.signal,
    )

    requests[0].response = {
      filename: 'image.png',
      reference: `asset:${assetId}:${generation}:image.png`,
      size: 5,
    }
    requests[0].listeners.get('load')?.()
    controller.abort()

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(requests[0].headers.get('X-Upload-Id')).toBe(assetId)
    expect(cleanup).toHaveBeenCalledOnce()
    expect(cleanup).toHaveBeenCalledWith(
      `/api/uploads/${assetId}?generation=${generation}`,
      { method: 'DELETE' },
    )
  })

  it('retries network and transient server cleanup failures without masking the upload error', async () => {
    const requests: MockUploadRequest[] = []
    const firstId = '1'.repeat(32)
    const secondId = '2'.repeat(32)
    const firstGeneration = '3'.repeat(32)

    class MockUploadRequest {
      status = 201
      response: unknown = null
      listeners = new Map<string, () => void>()
      upload = {
        addEventListener: () => undefined,
      }

      constructor() {
        requests.push(this)
      }

      open() {}

      setRequestHeader() {}

      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, listener)
      }

      send() {}
    }

    vi.stubGlobal('XMLHttpRequest', MockUploadRequest)
    vi.spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValueOnce('11111111-1111-1111-1111-111111111111')
      .mockReturnValueOnce('22222222-2222-2222-2222-222222222222')
    const cleanupAttempts = new Map<string, number>()
    const cleanup = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(async (input) => {
        const url = new URL(String(input), 'http://localhost')
        const assetId = decodeURIComponent(url.pathname.split('/').at(-1) ?? '')
        const attempt = (cleanupAttempts.get(assetId) ?? 0) + 1
        cleanupAttempts.set(assetId, attempt)
        if (assetId === firstId && attempt === 1) {
          throw new Error('network unavailable')
        }
        if (assetId === firstId && attempt === 2) {
          return new Response(null, { status: 503 })
        }
        if (assetId === secondId && attempt === 1) {
          return new Response(null, { status: 409 })
        }
        return new Response(null, { status: 204 })
      })
    const pending = uploadFiles(
      [new File(['first'], 'first.png'), new File(['second'], 'second.png')],
      () => undefined,
    )

    requests[0].response = {
      filename: 'first.png',
      reference: `asset:${firstId}:${firstGeneration}:first.png`,
      size: 5,
    }
    requests[0].listeners.get('load')?.()
    await Promise.resolve()
    requests[1].status = 413
    requests[1].response = {
      error: { code: 'upload_too_large', message: 'Second upload rejected' },
    }
    requests[1].listeners.get('load')?.()

    await expect(pending).rejects.toThrow('Second upload rejected')
    expect(cleanup).toHaveBeenCalledTimes(5)
    expect(cleanup).toHaveBeenCalledWith(
      `/api/uploads/${firstId}?generation=${firstGeneration}`,
      { method: 'DELETE' },
    )
    expect(cleanup).toHaveBeenCalledWith(`/api/uploads/${secondId}`, {
      method: 'DELETE',
    })
  })

  it('reports a persistent cleanup failure while preserving the upload error as cause', async () => {
    const requests: MockUploadRequest[] = []
    const firstId = '1'.repeat(32)
    const secondId = '2'.repeat(32)
    const thirdId = '3'.repeat(32)

    class MockUploadRequest {
      status = 201
      response: unknown = null
      listeners = new Map<string, () => void>()
      upload = {
        addEventListener: () => undefined,
      }

      constructor() {
        requests.push(this)
      }

      open() {}

      setRequestHeader() {}

      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, listener)
      }

      send() {}
    }

    vi.stubGlobal('XMLHttpRequest', MockUploadRequest)
    vi.spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValueOnce('11111111-1111-1111-1111-111111111111')
      .mockReturnValueOnce('22222222-2222-2222-2222-222222222222')
      .mockReturnValueOnce('33333333-3333-3333-3333-333333333333')
    const cleanup = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 503 }))
    const pending = uploadFiles(
      [
        new File(['first'], 'first.png'),
        new File(['second'], 'second.png'),
        new File(['third'], 'third.png'),
      ],
      () => undefined,
    )

    requests[0].response = {
      filename: 'first.png',
      reference: `asset:${firstId}:first.png`,
      size: 5,
    }
    requests[0].listeners.get('load')?.()
    await Promise.resolve()
    requests[1].response = {
      filename: 'second.png',
      reference: `asset:${secondId}:second.png`,
      size: 6,
    }
    requests[1].listeners.get('load')?.()
    await Promise.resolve()
    requests[2].status = 413
    requests[2].response = {
      error: { code: 'upload_too_large', message: 'Third upload rejected' },
    }
    requests[2].listeners.get('load')?.()

    const error = await pending.catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(AggregateError)
    expect(error).toMatchObject({
      message: expect.stringContaining('Third upload rejected'),
      cause: expect.objectContaining({ message: 'Third upload rejected' }),
      errors: [
        expect.objectContaining({
          message: expect.stringContaining(`Failed to delete uploaded asset ${firstId}: 503`),
        }),
        expect.objectContaining({
          message: expect.stringContaining(`Failed to delete uploaded asset ${secondId}: 503`),
        }),
        expect.objectContaining({
          message: expect.stringContaining(`Failed to delete uploaded asset ${thirdId}: 503`),
        }),
      ],
    })
    expect(cleanup).toHaveBeenCalledTimes(9)
  })

  it('treats a missing uploaded asset as an idempotent cleanup success', async () => {
    const requests: MockUploadRequest[] = []
    vi.spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValueOnce('11111111-1111-1111-1111-111111111111')
      .mockReturnValueOnce('22222222-2222-2222-2222-222222222222')

    class MockUploadRequest {
      status = 201
      response: unknown = null
      listeners = new Map<string, () => void>()
      upload = {
        addEventListener: () => undefined,
      }

      constructor() {
        requests.push(this)
      }

      open() {}

      setRequestHeader() {}

      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, listener)
      }

      send() {}
    }

    vi.stubGlobal('XMLHttpRequest', MockUploadRequest)
    const cleanup = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 404 }))
    const pending = uploadFiles(
      [new File(['first'], 'first.png'), new File(['second'], 'second.png')],
      () => undefined,
    )

    requests[0].response = {
      filename: 'first.png',
      reference: `asset:${'1'.repeat(32)}:first.png`,
      size: 5,
    }
    requests[0].listeners.get('load')?.()
    await Promise.resolve()
    requests[1].status = 413
    requests[1].response = {
      error: { code: 'upload_too_large', message: 'Second upload rejected' },
    }
    requests[1].listeners.get('load')?.()

    const error = await pending.catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(Error)
    expect(error).not.toBeInstanceOf(AggregateError)
    expect(error).toMatchObject({ message: 'Second upload rejected' })
    expect(cleanup).toHaveBeenCalledTimes(2)
  })

  it('models chroma payloads with exactly one RGB sample', () => {
    expectTypeOf<ChromaKeyerPayload['samples']>().toEqualTypeOf<readonly [ChromaKeyerRgbSample]>()
    expectTypeOf<ChromaVideoKeyerPayload['samples']>().toEqualTypeOf<readonly [ChromaKeyerRgbSample]>()
    expectTypeOf<ChromaKeyerPayload>().not.toMatchTypeOf<ChromaVideoKeyerPayload>()
    expectTypeOf<ChromaVideoKeyerPayload>().toMatchTypeOf<Parameters<typeof createChromaVideoKeyerTask>[0]>()
    expectTypeOf<ChromaVideoPreviewPayload['viewMode']>().toEqualTypeOf<'final' | 'alpha'>()
    expectTypeOf<Parameters<typeof inspectChromaVideo>[0]>().toEqualTypeOf<ChromaVideoInspectRequest>()
    expectTypeOf<Parameters<typeof createChromaVideoFrame>[0]>().toEqualTypeOf<ChromaVideoFrameRequest>()
    expectTypeOf<Parameters<typeof sampleChromaVideo>[0]>().toEqualTypeOf<ChromaVideoSampleRequest>()
  })

  it('posts video frame tasks', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ task_id: 'task-1', status: 'queued' })))
    const payload = {
      input_path: 'clip.webm',
      image_format: 'png',
      overwrite: false,
    } as const

    const result = await createVideoFramesTask(payload)

    expect(result.task_id).toBe('task-1')
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/video-frames', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  })

  it('posts batch video frame tasks', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ task_id: 'task-batch', status: 'queued' })))
    const payload = {
      input_paths: ['clip-a.webm', 'clip-b.webm'],
      image_format: 'png',
      overwrite: false,
    } as const

    const result = await createVideoFramesTask(payload)

    expect(result.task_id).toBe('task-batch')
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/video-frames', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  })

  it('posts media resize tasks', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ task_id: 'task-2', status: 'queued' })))
    const payload = {
      input_path: 'photo.png',
      output_path: 'photo-small.png',
      size: '100x100',
    }

    const result = await createMediaResizeTask(payload)

    expect(result.task_id).toBe('task-2')
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/media-resize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  })

  it('posts batch media resize tasks', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ task_id: 'task-media-batch', status: 'queued' })))
    const payload = {
      input_paths: ['photo-a.png', 'clip-b.mp4'],
      size: '100x100',
    } as const

    const result = await createMediaResizeTask(payload)

    expect(result.task_id).toBe('task-media-batch')
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/media-resize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  })

  it('inspects a chroma keyer image', async () => {
    const body = {
      input_path: 'screen.png',
      width: 1920,
      height: 1080,
      preview_width: 1280,
      preview_height: 720,
      preview_url: '/api/chroma-keyer/previews/inspect-1',
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))

    await expect(inspectChromaImage({ inputPath: 'screen.png' })).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/images/inspect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input_path: 'screen.png' }),
    })
  })

  it('samples a chroma keyer image at preview coordinates', async () => {
    const body = { rgb: [42, 180, 72], hex: '#2AB448', source_x: 630, source_y: 405 }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))

    await expect(
      sampleChromaImage({
        inputPath: 'screen.png',
        x: 0.5,
        y: 0.75,
        previewWidth: 1280,
        previewHeight: 720,
      }),
    ).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/images/sample', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.png',
        x: 0.5,
        y: 0.75,
        preview_width: 1280,
        preview_height: 720,
      }),
    })
  })

  it('inspects a chroma keyer video and returns metadata with the resolved timestamp', async () => {
    const body = {
      input_path: 'screen.mp4',
      width: 1920,
      height: 1080,
      preview_width: 1280,
      preview_height: 720,
      preview_url: '/api/chroma-keyer/previews/video-inspect-1',
      duration_seconds: 12.5,
      fps: 30,
      estimated_frame_count: 375,
      timestamp_seconds: 0,
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))

    await expect(inspectChromaVideo({ inputPath: 'screen.mp4' })).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/videos/inspect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input_path: 'screen.mp4' }),
    })
  })

  it('loads an unkeyed video frame at a timestamp', async () => {
    const body = {
      preview_url: '/api/chroma-keyer/previews/video-frame-1',
      timestamp_seconds: 1.25,
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))

    await expect(createChromaVideoFrame({ inputPath: 'screen.mp4', timestampSeconds: 1.3 })).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/videos/frame', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.mp4',
        timestamp_seconds: 1.3,
      }),
    })
  })

  it('samples a chroma keyer video at preview coordinates and a timestamp', async () => {
    const body = {
      rgb: [42, 180, 72],
      hex: '#2AB448',
      source_x: 630,
      source_y: 405,
      timestamp_seconds: 1.25,
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))

    await expect(
      sampleChromaVideo({
        inputPath: 'screen.mp4',
        timestampSeconds: 1.3,
        x: 0.5,
        y: 0.75,
        previewWidth: 1280,
        previewHeight: 720,
      }),
    ).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/videos/sample', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.mp4',
        timestamp_seconds: 1.3,
        x: 0.5,
        y: 0.75,
        preview_width: 1280,
        preview_height: 720,
      }),
    })
  })

  it('posts keyed video previews with timestamp and nested snake case params', async () => {
    const body = {
      preview_url: '/api/chroma-keyer/previews/video-final-1',
      timestamp_seconds: 1.25,
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))
    const payload: ChromaVideoPreviewPayload = {
      inputPath: 'screen.mp4',
      timestampSeconds: 1.3,
      samples: [[42, 180, 72]],
      params: {
        screenGain: 1.25,
        screenBalance: 0.35,
        clipBlack: 0,
        clipWhite: 1,
        shrinkGrow: 0,
        feather: 0,
        despillAmount: 0.5,
      },
      viewMode: 'final',
    }

    await expect(createChromaVideoPreview(payload)).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/videos/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.mp4',
        timestamp_seconds: 1.3,
        samples: [[42, 180, 72]],
        params: {
          screen_gain: 1.25,
          screen_balance: 0.35,
          clip_black: 0,
          clip_white: 1,
          shrink_grow: 0,
          feather: 0,
          despill_amount: 0.5,
        },
        view_mode: 'final',
      }),
    })
  })

  it('creates chroma video tasks with output_root instead of image output_path', async () => {
    const body: TaskCreateResponse = { task_id: 'task-video-chroma-1', status: 'queued' }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))
    const payload: ChromaVideoKeyerPayload = {
      inputPath: 'screen.mp4',
      outputRoot: 'E:\\exports',
      samples: [[42, 180, 72]],
      params: {
        screenGain: 1.25,
        screenBalance: 0.35,
        clipBlack: 0,
        clipWhite: 1,
        shrinkGrow: 0,
        feather: 0,
        despillAmount: 0.5,
      },
    }

    await expect(createChromaVideoKeyerTask(payload)).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/chroma-keyer-video', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.mp4',
        output_root: 'E:\\exports',
        samples: [[42, 180, 72]],
        params: {
          screen_gain: 1.25,
          screen_balance: 0.35,
          clip_black: 0,
          clip_white: 1,
          shrink_grow: 0,
          feather: 0,
          despill_amount: 0.5,
        },
      }),
    })
    expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string)).not.toHaveProperty('output_path')
  })

  it('posts canonical chroma preview requests with nested snake case params', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ preview_url: '/api/chroma-keyer/previews/final-1' })))
    const payload = {
      inputPath: 'screen.png',
      samples: [[42, 180, 72]] as const,
      params: {
        screenGain: 1.25,
        screenBalance: 0.35,
        clipBlack: 0,
        clipWhite: 1,
        shrinkGrow: 0,
        feather: 0,
        despillAmount: 0.5,
      },
      viewMode: 'final' as const,
    }

    await expect(createChromaPreview(payload)).resolves.toEqual({
      preview_url: '/api/chroma-keyer/previews/final-1',
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.png',
        samples: [[42, 180, 72]],
        params: {
          screen_gain: 1.25,
          screen_balance: 0.35,
          clip_black: 0,
          clip_white: 1,
          shrink_grow: 0,
          feather: 0,
          despill_amount: 0.5,
        },
        view_mode: 'final',
      }),
    })
  })

  it('omits export-only output paths from chroma preview requests', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ preview_url: '/api/chroma-keyer/previews/final-2' })))
    const payload: ChromaPreviewPayload & { outputPath: string } = {
      inputPath: 'screen.png',
      outputPath: 'screen_keyed.png',
      samples: [[42, 180, 72]],
      params: {
        screenGain: 1.25,
        screenBalance: 0.35,
        clipBlack: 0,
        clipWhite: 1,
        shrinkGrow: 0,
        feather: 0,
        despillAmount: 0.5,
      },
      viewMode: 'final',
    }

    await createChromaPreview(payload)

    expect(fetchMock).toHaveBeenCalledWith('/api/chroma-keyer/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.png',
        samples: [[42, 180, 72]],
        params: {
          screen_gain: 1.25,
          screen_balance: 0.35,
          clip_black: 0,
          clip_white: 1,
          shrink_grow: 0,
          feather: 0,
          despill_amount: 0.5,
        },
        view_mode: 'final',
      }),
    })
  })

  it('omits blank output paths from chroma keyer task requests', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ task_id: 'task-chroma-1', status: 'queued' })))

    await expect(
      createChromaKeyerTask({
        inputPath: 'screen.png',
        outputPath: '   ',
        samples: [[42, 180, 72]],
        params: {
          screenGain: 1.25,
          screenBalance: 0.35,
          clipBlack: 0,
          clipWhite: 1,
          shrinkGrow: 0,
          feather: 0,
          despillAmount: 0.5,
        },
      }),
    ).resolves.toEqual({ task_id: 'task-chroma-1', status: 'queued' })
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/chroma-keyer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_path: 'screen.png',
        samples: [[42, 180, 72]],
        params: {
          screen_gain: 1.25,
          screen_balance: 0.35,
          clip_black: 0,
          clip_white: 1,
          shrink_grow: 0,
          feather: 0,
          despill_amount: 0.5,
        },
      }),
    })
  })

  it('resolves dropped file names to local paths', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          paths: ['E:\\project\\assets\\video\\clip-a.mp4'],
          unresolved: ['missing.mp4'],
        }),
      ),
    )

    const result = await resolveDroppedFilePaths(['clip-a.mp4', 'missing.mp4'])

    expect(result).toEqual({
      paths: ['E:\\project\\assets\\video\\clip-a.mp4'],
      unresolved: ['missing.mp4'],
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/local-paths/resolve-dropped-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: ['clip-a.mp4', 'missing.mp4'] }),
    })
  })

  it('picks local files through the native backend picker', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          paths: ['E:\\project\\assets\\video\\clip-a.mp4'],
          cancelled: false,
        }),
      ),
    )

    const result = await pickLocalFiles({
      multiple: true,
      fileKind: 'video',
      title: '选择视频',
    })

    expect(result).toEqual({
      paths: ['E:\\project\\assets\\video\\clip-a.mp4'],
      cancelled: false,
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/local-paths/pick-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ multiple: true, file_kind: 'video', title: '选择视频' }),
    })
  })

  it('serializes optional local file suffix constraints for the native backend picker', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ paths: [], cancelled: true })),
    )
    const allowedSuffixes = ['.mp4', '.webm', '.png'] as const

    await pickLocalFiles({
      allowedSuffixes,
      multiple: false,
      fileKind: 'media',
      title: '选择受支持的媒体',
    })

    expect(fetchMock).toHaveBeenCalledWith('/api/local-paths/pick-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        allowed_suffixes: allowedSuffixes,
        multiple: false,
        file_kind: 'media',
        title: '选择受支持的媒体',
      }),
    })
  })

  it('picks local directories through the native backend picker', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          paths: ['E:\\project\\frames'],
          cancelled: false,
        }),
      ),
    )

    const result = await pickLocalDirectory({ title: '选择输出目录' })

    expect(result).toEqual({
      paths: ['E:\\project\\frames'],
      cancelled: false,
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/local-paths/pick-directory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '选择输出目录' }),
    })
  })

  it('picks local save-file paths through the native backend picker', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          paths: ['E:\\project\\frames\\small.png'],
          cancelled: false,
        }),
      ),
    )

    const result = await pickLocalSaveFile({
      fileKind: 'media',
      title: '选择输出路径',
    })

    expect(result).toEqual({
      paths: ['E:\\project\\frames\\small.png'],
      cancelled: false,
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/local-paths/pick-save-file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_kind: 'media', title: '选择输出路径' }),
    })
  })

  it('accepts only backend-supported save-file kinds', () => {
    type SaveFilePickerOptions = Parameters<typeof pickLocalSaveFile>[0]

    expectTypeOf<{ fileKind: 'image'; title: string }>().toMatchTypeOf<SaveFilePickerOptions>()
    expectTypeOf<{ fileKind: 'media'; title: string }>().toMatchTypeOf<SaveFilePickerOptions>()
    expectTypeOf<{ fileKind: 'any'; title: string }>().toMatchTypeOf<SaveFilePickerOptions>()
    expectTypeOf<{ fileKind: 'video'; title: string }>().not.toMatchTypeOf<SaveFilePickerOptions>()
  })

  it('fetches task status by id', async () => {
    const body = {
      task_id: 'task-1',
      kind: 'video_frames',
      status: 'succeeded',
      created_at: '2026-08-20T00:00:00Z',
      started_at: '2026-08-20T00:00:01Z',
      finished_at: '2026-08-20T00:00:02Z',
      input_path: 'clip.webm',
      output_path: 'frames',
      error: null,
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body)))

    await expect(getTask('task-1')).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/task-1')
  })


  it('throws user-readable API errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: { code: 'bad_size', message: 'Invalid size' } }), { status: 400 }),
    )

    await expect(createMediaResizeTask({ input_path: 'photo.png', size: 'bad' })).rejects.toThrow('Invalid size')
  })

  it('throws status errors for non-JSON error responses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('service unavailable', { status: 502 }))

    await expect(createMediaResizeTask({ input_path: 'photo.png', size: '100x100' })).rejects.toThrow(
      'Request failed: 502',
    )
  })
})
