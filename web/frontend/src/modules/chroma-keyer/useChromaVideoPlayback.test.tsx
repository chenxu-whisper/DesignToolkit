import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  quantizePlaybackTimestamp,
  type PlaybackFrame,
  useChromaVideoPlayback,
} from './useChromaVideoPlayback'

type Deferred<T> = {
  promise: Promise<T>
  reject: (error: unknown) => void
  resolve: (value: T) => void
}

type PlaybackOptions = Parameters<typeof useChromaVideoPlayback>[0]

function createDeferred<T>(): Deferred<T> {
  let reject!: (error: unknown) => void
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function createFrame(timestampSeconds: number): PlaybackFrame {
  return {
    previewUrl: `/preview-${timestampSeconds}.png`,
    timestampSeconds,
  }
}

function createOptions(
  overrides: Partial<PlaybackOptions> = {},
): PlaybackOptions {
  return {
    canPlay: true,
    durationSeconds: 3,
    fps: 4,
    renderedTimestampSeconds: 0,
    requestFrame: vi.fn(async (timestampSeconds) => createFrame(timestampSeconds)),
    onError: vi.fn(),
    onFrame: vi.fn(),
    onPause: vi.fn(),
    onTargetChange: vi.fn(),
    ...overrides,
  }
}

describe('quantizePlaybackTimestamp', () => {
  it('floors timestamps to the source frame grid and clamps to the last frame', () => {
    expect(quantizePlaybackTimestamp(0.74, 4, 3)).toBe(0.5)
    expect(quantizePlaybackTimestamp(-1, 4, 3)).toBe(0)
    expect(quantizePlaybackTimestamp(10, 4, 3)).toBe(2.75)
  })

  it.each([
    [1, 0, 3],
    [1, Number.NaN, 3],
    [1, 4, 0],
    [1, 4, Number.POSITIVE_INFINITY],
    [Number.NaN, 4, 3],
  ])('returns zero for invalid metadata (%s, %s, %s)', (timestamp, fps, duration) => {
    expect(quantizePlaybackTimestamp(timestamp, fps, duration)).toBe(0)
  })
})

describe('useChromaVideoPlayback', () => {
  let animationCallbacks: Map<number, FrameRequestCallback>
  let nextAnimationHandle: number
  let now: number

  beforeEach(() => {
    vi.useFakeTimers()
    animationCallbacks = new Map()
    nextAnimationHandle = 1
    now = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    vi.stubGlobal(
      'requestAnimationFrame',
      vi.fn((callback: FrameRequestCallback) => {
        const handle = nextAnimationHandle
        nextAnimationHandle += 1
        animationCallbacks.set(handle, callback)
        return handle
      }),
    )
    vi.stubGlobal(
      'cancelAnimationFrame',
      vi.fn((handle: number) => {
        animationCallbacks.delete(handle)
      }),
    )
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  function runAnimationFrame(elapsedMilliseconds: number) {
    now = 1000 + elapsedMilliseconds
    const callbacks = [...animationCallbacks.values()]
    animationCallbacks.clear()
    act(() => {
      for (const callback of callbacks) {
        callback(now)
      }
    })
  }

  async function settle() {
    await act(async () => {
      await Promise.resolve()
    })
  }

  it('dispatches and accepts playback frames after StrictMode effect replay', async () => {
    const requestFrame = vi.fn(async (timestampSeconds) => createFrame(timestampSeconds))
    const onFrame = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({ requestFrame, onFrame })), {
      reactStrictMode: true,
    })

    act(() => {
      result.current.play()
    })

    expect(requestFrame).toHaveBeenCalledWith(0)
    await settle()
    expect(onFrame).toHaveBeenCalledWith(createFrame(0))

    runAnimationFrame(300)
    await settle()

    expect(requestFrame).toHaveBeenLastCalledWith(0.25)
    expect(onFrame).toHaveBeenLastCalledWith(createFrame(0.25))
  })

  it('starts from the last rendered timestamp', async () => {
    const requestFrame = vi.fn(async (timestampSeconds) => createFrame(timestampSeconds))
    const onFrame = vi.fn()
    const onTargetChange = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        renderedTimestampSeconds: 1.25,
        requestFrame,
        onFrame,
        onTargetChange,
      })),
    )

    act(() => {
      result.current.play()
    })

    expect(result.current.isPlaying).toBe(true)
    expect(onTargetChange).toHaveBeenCalledWith(1.25)
    expect(requestFrame).toHaveBeenCalledWith(1.25)

    await settle()

    expect(onFrame).toHaveBeenCalledWith(createFrame(1.25))
  })

  it('quantizes elapsed wall-clock progress to the source frame grid', async () => {
    const requestFrame = vi.fn(async (timestampSeconds) => createFrame(timestampSeconds))
    const onTargetChange = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        renderedTimestampSeconds: 0.5,
        requestFrame,
        onTargetChange,
      })),
    )

    act(() => {
      result.current.play()
    })
    await settle()
    runAnimationFrame(390)

    expect(onTargetChange).toHaveBeenLastCalledWith(0.75)
    expect(requestFrame).toHaveBeenLastCalledWith(0.75)
  })

  it('waits for the quantized target to advance before requesting another frame', async () => {
    const requestFrame = vi.fn(async (timestampSeconds) => createFrame(timestampSeconds))
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({ requestFrame })),
    )

    act(() => {
      result.current.play()
    })
    await settle()

    for (const elapsedMilliseconds of [16, 100, 249]) {
      runAnimationFrame(elapsedMilliseconds)
      await settle()
    }

    expect(requestFrame).toHaveBeenCalledOnce()

    runAnimationFrame(250)

    expect(requestFrame).toHaveBeenCalledTimes(2)
    expect(requestFrame).toHaveBeenLastCalledWith(0.25)
  })

  it('keeps one request in flight and dispatches only the latest advanced target', async () => {
    const firstRequest = createDeferred<PlaybackFrame>()
    const secondRequest = createDeferred<PlaybackFrame>()
    const requestFrame = vi.fn()
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(secondRequest.promise)
    const onTargetChange = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({ requestFrame, onTargetChange })),
    )

    act(() => {
      result.current.play()
    })
    runAnimationFrame(300)
    runAnimationFrame(800)
    runAnimationFrame(1300)

    expect(requestFrame).toHaveBeenCalledTimes(1)
    expect(onTargetChange.mock.calls.map(([timestamp]) => timestamp)).toEqual([
      0,
      0.25,
      0.75,
      1.25,
    ])

    firstRequest.resolve(createFrame(0))
    await settle()

    expect(requestFrame).toHaveBeenCalledTimes(2)
    expect(requestFrame).toHaveBeenLastCalledWith(1.25)

    secondRequest.resolve(createFrame(1.25))
    await settle()
  })

  it('does not start a replay request while the prior generation request is pending', () => {
    const pendingRequest = createDeferred<PlaybackFrame>()
    const requestFrame = vi.fn(() => pendingRequest.promise)
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({ requestFrame })),
    )

    act(() => {
      result.current.play()
      result.current.pause()
      result.current.play()
    })
    runAnimationFrame(800)

    expect(requestFrame).toHaveBeenCalledOnce()
  })

  it('ignores a stale frame and dispatches the replay latest target once', async () => {
    const firstRequest = createDeferred<PlaybackFrame>()
    const replayRequest = createDeferred<PlaybackFrame>()
    const requestFrame = vi.fn()
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(replayRequest.promise)
    const onFrame = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({ requestFrame, onFrame })),
    )

    act(() => {
      result.current.play()
      result.current.pause()
      result.current.play()
    })
    runAnimationFrame(800)
    firstRequest.resolve(createFrame(0))
    await settle()

    expect(onFrame).not.toHaveBeenCalled()
    expect(requestFrame).toHaveBeenCalledTimes(2)
    expect(requestFrame).toHaveBeenLastCalledWith(0.75)

    replayRequest.resolve(createFrame(0.75))
    await settle()
  })

  it('ignores a stale error and dispatches the replay latest target once', async () => {
    const firstRequest = createDeferred<PlaybackFrame>()
    const replayRequest = createDeferred<PlaybackFrame>()
    const requestFrame = vi.fn()
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(replayRequest.promise)
    const onError = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({ requestFrame, onError })),
    )

    act(() => {
      result.current.play()
      result.current.pause()
      result.current.play()
    })
    runAnimationFrame(800)
    firstRequest.reject(new Error('stale decode failure'))
    await settle()

    expect(onError).not.toHaveBeenCalled()
    expect(result.current.isPlaying).toBe(true)
    expect(requestFrame).toHaveBeenCalledTimes(2)
    expect(requestFrame).toHaveBeenLastCalledWith(0.75)

    replayRequest.resolve(createFrame(0.75))
    await settle()
  })

  it('does not let stale settlement clear a newer pending request', async () => {
    const firstRequest = createDeferred<PlaybackFrame>()
    const secondRequest = createDeferred<PlaybackFrame>()
    const thirdRequest = createDeferred<PlaybackFrame>()
    const requestFrame = vi.fn()
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(secondRequest.promise)
      .mockReturnValueOnce(thirdRequest.promise)
    const onFrame = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({ requestFrame, onFrame })),
    )

    act(() => {
      result.current.play()
      result.current.pause()
      result.current.play()
    })
    runAnimationFrame(800)
    firstRequest.resolve(createFrame(0))
    await settle()

    expect(requestFrame).toHaveBeenCalledTimes(2)
    expect(requestFrame).toHaveBeenLastCalledWith(0.75)

    act(() => {
      result.current.pause()
      result.current.play()
    })
    runAnimationFrame(1300)

    expect(requestFrame).toHaveBeenCalledTimes(2)

    secondRequest.resolve(createFrame(0.75))
    await settle()

    expect(onFrame).not.toHaveBeenCalled()
    expect(requestFrame).toHaveBeenCalledTimes(3)
    expect(requestFrame).toHaveBeenLastCalledWith(0.5)

    runAnimationFrame(1800)

    expect(requestFrame).toHaveBeenCalledTimes(3)

    thirdRequest.resolve(createFrame(0.5))
    await settle()
  })

  it('pauses on the last rendered frame and ignores a stale response', async () => {
    const pendingRequest = createDeferred<PlaybackFrame>()
    const requestFrame = vi.fn(() => pendingRequest.promise)
    const onFrame = vi.fn()
    const onPause = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        renderedTimestampSeconds: 0.5,
        requestFrame,
        onFrame,
        onPause,
      })),
    )

    act(() => {
      result.current.play()
      result.current.pause()
    })

    expect(result.current.isPlaying).toBe(false)
    expect(animationCallbacks.size).toBe(0)
    expect(onPause).toHaveBeenCalledOnce()
    expect(onPause).toHaveBeenCalledWith(0.5)

    pendingRequest.resolve(createFrame(0.5))
    await settle()
    runAnimationFrame(500)

    expect(onFrame).not.toHaveBeenCalled()
    expect(requestFrame).toHaveBeenCalledOnce()
  })

  it('can pause without synchronizing the rendered timestamp', () => {
    const pendingRequest = createDeferred<PlaybackFrame>()
    const onPause = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        requestFrame: vi.fn(() => pendingRequest.promise),
        onPause,
      })),
    )

    act(() => {
      result.current.play()
      result.current.pause({ synchronize: false })
    })

    expect(result.current.isPlaying).toBe(false)
    expect(onPause).not.toHaveBeenCalled()
  })

  it('pauses exactly once and reports a current request failure', async () => {
    const pendingRequest = createDeferred<PlaybackFrame>()
    const error = new Error('decode failed')
    const onError = vi.fn()
    const onPause = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        renderedTimestampSeconds: 0.25,
        requestFrame: vi.fn(() => pendingRequest.promise),
        onError,
        onPause,
      })),
    )

    act(() => {
      result.current.play()
    })
    pendingRequest.reject(error)
    await settle()

    expect(result.current.isPlaying).toBe(false)
    expect(onPause).toHaveBeenCalledOnce()
    expect(onPause).toHaveBeenCalledWith(0.25)
    expect(onError).toHaveBeenCalledOnce()
    expect(onError).toHaveBeenCalledWith(error)
  })

  it('renders the last frame before automatically pausing', async () => {
    const finalRequest = createDeferred<PlaybackFrame>()
    const replayRequest = createDeferred<PlaybackFrame>()
    const requestFrame = vi.fn()
      .mockResolvedValueOnce(createFrame(2.5))
      .mockReturnValueOnce(finalRequest.promise)
      .mockReturnValueOnce(replayRequest.promise)
    const onFrame = vi.fn()
    const onPause = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        durationSeconds: 3,
        renderedTimestampSeconds: 2.5,
        requestFrame,
        onFrame,
        onPause,
      })),
    )

    act(() => {
      result.current.play()
    })
    await settle()
    runAnimationFrame(300)

    expect(requestFrame).toHaveBeenLastCalledWith(2.75)
    expect(result.current.isPlaying).toBe(true)
    expect(onPause).not.toHaveBeenCalled()

    finalRequest.resolve(createFrame(2.75))
    await settle()

    expect(onFrame).toHaveBeenLastCalledWith(createFrame(2.75))
    expect(result.current.isPlaying).toBe(false)
    expect(onPause).toHaveBeenCalledOnce()
    expect(onPause).toHaveBeenCalledWith(2.75)

    act(() => {
      result.current.play()
    })

    expect(requestFrame).toHaveBeenLastCalledWith(0)

    replayRequest.resolve(createFrame(0))
    await settle()
  })

  it('restarts from zero when play is pressed on the last frame', () => {
    const requestFrame = vi.fn(() => createDeferred<PlaybackFrame>().promise)
    const onTargetChange = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        renderedTimestampSeconds: 2.75,
        requestFrame,
        onTargetChange,
      })),
    )

    act(() => {
      result.current.play()
    })

    expect(onTargetChange).toHaveBeenCalledWith(0)
    expect(requestFrame).toHaveBeenCalledWith(0)
  })

  it('does not start with invalid playback metadata', () => {
    const requestFrame = vi.fn()
    const onTargetChange = vi.fn()
    const { result } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        durationSeconds: 0,
        requestFrame,
        onTargetChange,
      })),
    )

    act(() => {
      result.current.play()
    })

    expect(result.current.isPlaying).toBe(false)
    expect(requestFrame).not.toHaveBeenCalled()
    expect(onTargetChange).not.toHaveBeenCalled()
    expect(animationCallbacks.size).toBe(0)
  })

  it('stops when canPlay becomes false and ignores the pending response', async () => {
    const pendingRequest = createDeferred<PlaybackFrame>()
    const onFrame = vi.fn()
    const onPause = vi.fn()
    const options = createOptions({
      requestFrame: vi.fn(() => pendingRequest.promise),
      onFrame,
      onPause,
    })
    const { result, rerender } = renderHook(
      ({ canPlay }) => useChromaVideoPlayback({ ...options, canPlay }),
      { initialProps: { canPlay: true } },
    )

    act(() => {
      result.current.play()
    })
    rerender({ canPlay: false })

    expect(result.current.isPlaying).toBe(false)
    expect(onPause).toHaveBeenCalledOnce()
    expect(onPause).toHaveBeenCalledWith(0)
    expect(animationCallbacks.size).toBe(0)

    rerender({ canPlay: false })
    rerender({ canPlay: false })

    expect(onPause).toHaveBeenCalledOnce()

    pendingRequest.resolve(createFrame(0))
    await settle()

    expect(onFrame).not.toHaveBeenCalled()
  })

  it('reads updated callbacks and metadata while playback is active', async () => {
    const firstRequest = createDeferred<PlaybackFrame>()
    const secondRequest = createDeferred<PlaybackFrame>()
    const initialOnFrame = vi.fn()
    const latestOnFrame = vi.fn()
    const latestOnTargetChange = vi.fn()
    const latestOnPause = vi.fn()
    const initialOptions = createOptions({
      requestFrame: vi.fn(() => firstRequest.promise),
      onFrame: initialOnFrame,
    })
    const latestRequestFrame = vi.fn(() => secondRequest.promise)
    const { result, rerender } = renderHook(
      (options: PlaybackOptions) => useChromaVideoPlayback(options),
      { initialProps: initialOptions },
    )

    act(() => {
      result.current.play()
    })
    rerender({
      ...initialOptions,
      durationSeconds: 2,
      fps: 2,
      requestFrame: latestRequestFrame,
      onFrame: latestOnFrame,
      onPause: latestOnPause,
      onTargetChange: latestOnTargetChange,
    })
    runAnimationFrame(700)
    firstRequest.resolve(createFrame(0))
    await settle()

    expect(initialOnFrame).not.toHaveBeenCalled()
    expect(latestOnFrame).toHaveBeenCalledWith(createFrame(0))
    expect(latestOnTargetChange).toHaveBeenCalledWith(0.5)
    expect(latestRequestFrame).toHaveBeenCalledWith(0.5)

    act(() => {
      result.current.pause()
    })
    expect(latestOnPause).toHaveBeenCalledWith(0)

    secondRequest.resolve(createFrame(0.5))
    await settle()
  })

  it('cancels animation work on unmount and ignores a pending frame', async () => {
    const pendingRequest = createDeferred<PlaybackFrame>()
    const onFrame = vi.fn()
    const onPause = vi.fn()
    const requestFrame = vi.fn(() => pendingRequest.promise)
    const { result, unmount } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        requestFrame,
        onFrame,
        onPause,
      })),
    )

    act(() => {
      result.current.play()
    })
    unmount()

    expect(animationCallbacks.size).toBe(0)

    pendingRequest.resolve(createFrame(0))
    await settle()
    runAnimationFrame(500)

    expect(requestFrame).toHaveBeenCalledOnce()
    expect(onFrame).not.toHaveBeenCalled()
    expect(onPause).not.toHaveBeenCalled()
  })

  it('ignores a pending failure after unmount', async () => {
    const pendingRequest = createDeferred<PlaybackFrame>()
    const onError = vi.fn()
    const { result, unmount } = renderHook(() =>
      useChromaVideoPlayback(createOptions({
        requestFrame: vi.fn(() => pendingRequest.promise),
        onError,
      })),
    )

    act(() => {
      result.current.play()
    })
    unmount()

    pendingRequest.reject(new Error('late failure'))
    await settle()

    expect(onError).not.toHaveBeenCalled()
  })
})
