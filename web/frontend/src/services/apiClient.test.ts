import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createMediaResizeTask,
  createVideoFramesTask,
  getTask,
  pickLocalDirectory,
  pickLocalFiles,
  pickLocalSaveFile,
  resolveDroppedFilePaths,
} from './apiClient'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('apiClient', () => {
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
