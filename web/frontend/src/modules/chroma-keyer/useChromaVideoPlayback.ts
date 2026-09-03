import { useCallback, useEffect, useRef, useState } from 'react'
import { getLastVideoFrameTime } from './ChromaVideoTimeline'

export type PlaybackFrame = {
  previewUrl: string
  timestampSeconds: number
}

export type UseChromaVideoPlaybackOptions = {
  canPlay: boolean
  durationSeconds: number
  fps: number
  renderedTimestampSeconds: number | null
  requestFrame: (timestampSeconds: number) => Promise<PlaybackFrame>
  onError: (error: unknown) => void
  onFrame: (frame: PlaybackFrame) => void
  onPause: (renderedTimestampSeconds: number | null) => void
  onTargetChange: (timestampSeconds: number) => void
}

export type PausePlaybackOptions = {
  synchronize?: boolean
}

export function quantizePlaybackTimestamp(
  timestampSeconds: number,
  fps: number,
  durationSeconds: number,
): number {
  if (
    !Number.isFinite(timestampSeconds)
    || !Number.isFinite(fps)
    || fps <= 0
    || !Number.isFinite(durationSeconds)
    || durationSeconds <= 0
  ) {
    return 0
  }

  const max = getLastVideoFrameTime(durationSeconds, fps)
  const frameIndex = Math.floor(Math.max(0, timestampSeconds) * fps + 1e-9)
  return Math.min(max, frameIndex / fps)
}

export function useChromaVideoPlayback(options: UseChromaVideoPlaybackOptions) {
  const [isPlaying, setIsPlaying] = useState(false)
  const optionsRef = useRef(options)
  const isPlayingRef = useRef(false)
  const mountedRef = useRef(true)
  const animationFrameRef = useRef<number | null>(null)
  const generationRef = useRef(0)
  const requestInFlightRef = useRef<symbol | null>(null)
  const targetRef = useRef(0)
  const renderedTimestampRef = useRef<number | null>(
    options.renderedTimestampSeconds,
  )
  const renderedTimestampOptionRef = useRef(options.renderedTimestampSeconds)
  const startWallTimeRef = useRef(0)
  const startTimestampRef = useRef(0)

  optionsRef.current = options
  if (!Object.is(
    renderedTimestampOptionRef.current,
    options.renderedTimestampSeconds,
  )) {
    renderedTimestampOptionRef.current = options.renderedTimestampSeconds
    renderedTimestampRef.current = options.renderedTimestampSeconds
  }

  const cancelScheduledAnimation = useCallback(() => {
    if (animationFrameRef.current !== null) {
      cancelAnimationFrame(animationFrameRef.current)
      animationFrameRef.current = null
    }
  }, [])

  const stopPlayback = useCallback((
    synchronize: boolean,
    expectedGeneration?: number,
  ) => {
    if (
      !isPlayingRef.current
      || (
        expectedGeneration !== undefined
        && generationRef.current !== expectedGeneration
      )
    ) {
      return false
    }

    generationRef.current += 1
    isPlayingRef.current = false
    cancelScheduledAnimation()
    if (mountedRef.current) {
      setIsPlaying(false)
      if (synchronize) {
        optionsRef.current.onPause(renderedTimestampRef.current)
      }
    }
    return true
  }, [cancelScheduledAnimation])

  const dispatchLatestTarget = useCallback(async function dispatch(
    generation: number,
  ): Promise<void> {
    if (
      !mountedRef.current
      || !isPlayingRef.current
      || generationRef.current !== generation
      || requestInFlightRef.current !== null
    ) {
      return
    }

    const requestedTarget = targetRef.current
    const requestToken = Symbol('playback-request')
    requestInFlightRef.current = requestToken

    try {
      const frame = await optionsRef.current.requestFrame(requestedTarget)
      if (
        !mountedRef.current
        || !isPlayingRef.current
        || generationRef.current !== generation
      ) {
        return
      }

      renderedTimestampRef.current = frame.timestampSeconds
      optionsRef.current.onFrame(frame)

      const { durationSeconds, fps } = optionsRef.current
      const lastFrameTimestamp = getLastVideoFrameTime(durationSeconds, fps)
      if (requestedTarget >= lastFrameTimestamp) {
        stopPlayback(true, generation)
      }
    } catch (error) {
      if (
        !mountedRef.current
        || !isPlayingRef.current
        || generationRef.current !== generation
      ) {
        return
      }

      if (stopPlayback(true, generation)) {
        optionsRef.current.onError(error)
      }
    } finally {
      if (requestInFlightRef.current === requestToken) {
        requestInFlightRef.current = null
        if (
          mountedRef.current
          && isPlayingRef.current
          && (
            generationRef.current !== generation
            || targetRef.current > requestedTarget
          )
        ) {
          void dispatch(generationRef.current)
        }
      }
    }
  }, [stopPlayback])

  const scheduleAnimation = useCallback(function schedule(generation: number) {
    if (
      !mountedRef.current
      || !isPlayingRef.current
      || generationRef.current !== generation
    ) {
      return
    }

    animationFrameRef.current = requestAnimationFrame((now) => {
      animationFrameRef.current = null
      if (
        !mountedRef.current
        || !isPlayingRef.current
        || generationRef.current !== generation
      ) {
        return
      }

      const { durationSeconds, fps } = optionsRef.current
      const nextTarget = quantizePlaybackTimestamp(
        startTimestampRef.current + (now - startWallTimeRef.current) / 1000,
        fps,
        durationSeconds,
      )
      const targetAdvanced = nextTarget > targetRef.current
      targetRef.current = nextTarget
      optionsRef.current.onTargetChange(nextTarget)

      if (targetAdvanced && !requestInFlightRef.current) {
        void dispatchLatestTarget(generation)
      }
      schedule(generation)
    })
  }, [dispatchLatestTarget])

  const pause = useCallback((pauseOptions?: PausePlaybackOptions) => {
    stopPlayback(pauseOptions?.synchronize ?? true)
  }, [stopPlayback])

  const play = useCallback(() => {
    if (isPlayingRef.current) {
      return
    }

    const {
      canPlay,
      durationSeconds,
      fps,
    } = optionsRef.current
    if (
      !canPlay
      || !Number.isFinite(durationSeconds)
      || durationSeconds <= 0
      || !Number.isFinite(fps)
      || fps <= 0
    ) {
      return
    }

    const lastFrameTimestamp = getLastVideoFrameTime(durationSeconds, fps)
    const renderedTimestamp = quantizePlaybackTimestamp(
      renderedTimestampRef.current ?? 0,
      fps,
      durationSeconds,
    )
    const startTimestamp = renderedTimestamp >= lastFrameTimestamp
      ? 0
      : renderedTimestamp
    const generation = generationRef.current + 1

    generationRef.current = generation
    targetRef.current = startTimestamp
    startTimestampRef.current = startTimestamp
    startWallTimeRef.current = performance.now()
    isPlayingRef.current = true
    setIsPlaying(true)
    optionsRef.current.onTargetChange(startTimestamp)
    void dispatchLatestTarget(generation)
    scheduleAnimation(generation)
  }, [dispatchLatestTarget, scheduleAnimation])

  const togglePlayback = useCallback(() => {
    if (isPlayingRef.current) {
      pause()
    } else {
      play()
    }
  }, [pause, play])

  useEffect(() => {
    if (!options.canPlay) {
      pause()
    }
  }, [options.canPlay, pause])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      generationRef.current += 1
      isPlayingRef.current = false
      cancelScheduledAnimation()
    }
  }, [cancelScheduledAnimation])

  return {
    isPlaying,
    pause,
    play,
    togglePlayback,
  }
}
