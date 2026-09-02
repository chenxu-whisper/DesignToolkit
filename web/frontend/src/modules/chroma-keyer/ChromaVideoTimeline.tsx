import type { KeyboardEvent } from 'react'

type ChromaVideoTimelineProps = {
  disabled?: boolean
  durationSeconds: number
  estimatedFrameCount: number
  fps: number
  height: number
  onChange: (timestampSeconds: number) => void
  value: number
  width: number
}

export function getLastVideoFrameTime(durationSeconds: number, fps: number): number {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0 || !Number.isFinite(fps) || fps <= 0) {
    return 0
  }

  return Math.max(0, Math.min(durationSeconds - 1 / fps, nextRepresentableBelow(durationSeconds)))
}

export function ChromaVideoTimeline({
  disabled = false,
  durationSeconds,
  estimatedFrameCount,
  fps,
  height,
  onChange,
  value,
  width,
}: ChromaVideoTimelineProps) {
  const max = getLastVideoFrameTime(durationSeconds, fps)
  const frameStep = 1 / fps
  const clampedValue = Math.min(max, Math.max(0, Number.isFinite(value) ? value : 0))
  const timecode = `${formatTimecode(clampedValue)} / ${formatTimecode(durationSeconds)}`
  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    let nextValue: number

    switch (event.key) {
      case 'ArrowLeft':
      case 'ArrowDown':
        nextValue = clampedValue - frameStep
        break
      case 'ArrowRight':
      case 'ArrowUp':
        nextValue = clampedValue + frameStep
        break
      case 'Home':
        nextValue = 0
        break
      case 'End':
        nextValue = max
        break
      default:
        return
    }

    event.preventDefault()
    onChange(Math.min(max, Math.max(0, nextValue)))
  }

  return (
    <div className="chroma-video-timeline" data-testid="chroma-video-timeline">
      <div
        className="chroma-video-timeline-controls"
        data-testid="chroma-video-timeline-controls"
      >
        <input
          aria-label="视频时间轴"
          aria-valuetext={timecode}
          className="chroma-video-timeline-range"
          disabled={disabled}
          max={max}
          min={0}
          onChange={(event) => onChange(Number(event.target.value))}
          onKeyDown={handleKeyDown}
          step={frameStep}
          type="range"
          value={clampedValue}
        />
        <output className="chroma-video-timecode">{timecode}</output>
      </div>
      <dl className="chroma-video-metadata" data-testid="chroma-video-timeline-metadata">
        <div>
          <dt>帧率</dt>
          <dd>{formatNumber(fps)} FPS</dd>
        </div>
        <div>
          <dt>源尺寸</dt>
          <dd>{width} × {height}</dd>
        </div>
        <div>
          <dt>帧数</dt>
          <dd>约 {estimatedFrameCount} 帧</dd>
        </div>
      </dl>
    </div>
  )
}

function formatTimecode(seconds: number): string {
  const totalMilliseconds = Math.max(0, Math.round(seconds * 1000))
  const milliseconds = totalMilliseconds % 1000
  const totalSeconds = Math.floor(totalMilliseconds / 1000)
  const displaySeconds = totalSeconds % 60
  const totalMinutes = Math.floor(totalSeconds / 60)
  const minutes = totalMinutes % 60
  const hours = Math.floor(totalMinutes / 60)

  return [
    String(hours).padStart(2, '0'),
    String(minutes).padStart(2, '0'),
    `${String(displaySeconds).padStart(2, '0')}.${String(milliseconds).padStart(3, '0')}`,
  ].join(':')
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 }).format(value)
}

function nextRepresentableBelow(value: number): number {
  const buffer = new ArrayBuffer(8)
  const view = new DataView(buffer)
  view.setFloat64(0, value)
  let high = view.getUint32(0)
  let low = view.getUint32(4)

  if (low === 0) {
    high -= 1
    low = 0xffffffff
  } else {
    low -= 1
  }

  view.setUint32(0, high)
  view.setUint32(4, low)
  return view.getFloat64(0)
}
