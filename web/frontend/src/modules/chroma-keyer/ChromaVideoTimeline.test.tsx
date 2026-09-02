import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ChromaVideoTimeline, getLastVideoFrameTime } from './ChromaVideoTimeline'

const defaultProps = {
  durationSeconds: 12.5,
  estimatedFrameCount: 375,
  fps: 30,
  height: 1080,
  onChange: vi.fn(),
  value: 1.25,
  width: 1920,
}

function ControlledTimeline({
  initialValue,
  onChange,
}: {
  initialValue: number
  onChange: (value: number) => void
}) {
  const [value, setValue] = useState(initialValue)

  return (
    <ChromaVideoTimeline
      {...defaultProps}
      onChange={(nextValue) => {
        onChange(nextValue)
        setValue(nextValue)
      }}
      value={value}
    />
  )
}

describe('ChromaVideoTimeline', () => {
  it('uses frame-accurate range bounds for practical video metadata', () => {
    render(<ChromaVideoTimeline {...defaultProps} />)

    const timeline = screen.getByRole('slider', { name: '视频时间轴' })
    expect(timeline).toHaveAttribute('min', '0')
    expect(timeline).toHaveAttribute('max', String(12.5 - 1 / 30))
    expect(timeline).toHaveAttribute('step', String(1 / 30))
    expect(timeline).toHaveValue('1.25')
  })

  it('never exposes the duration itself and clamps short clips to zero', () => {
    expect(getLastVideoFrameTime(3, 2)).toBe(2.5)
    expect(getLastVideoFrameTime(0.01, 25)).toBe(0)
    expect(getLastVideoFrameTime(1, Number.MAX_VALUE)).toBeLessThan(1)
  })

  it('shows readable current and total timecodes with source metadata', () => {
    render(<ChromaVideoTimeline {...defaultProps} />)

    expect(screen.getByText('00:00:01.250 / 00:00:12.500')).toBeInTheDocument()
    expect(screen.getByText('30 FPS')).toBeInTheDocument()
    expect(screen.getByText('1920 × 1080')).toBeInTheDocument()
    expect(screen.getByText('约 375 帧')).toBeInTheDocument()
  })

  it('preserves normal range change events for pointer input', () => {
    const onChange = vi.fn()
    render(<ChromaVideoTimeline {...defaultProps} onChange={onChange} />)

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1.2833333333333334' },
    })

    expect(onChange).toHaveBeenCalledWith(1.2833333333333334)
  })

  it.each([
    ['ArrowLeft', '{ArrowLeft}', 1.25 - 1 / 30],
    ['ArrowDown', '{ArrowDown}', 1.25 - 1 / 30],
    ['ArrowRight', '{ArrowRight}', 1.25 + 1 / 30],
    ['ArrowUp', '{ArrowUp}', 1.25 + 1 / 30],
    ['Home', '{Home}', 0],
    ['End', '{End}', 12.5 - 1 / 30],
  ])('handles %s as a controlled timeline key', async (_name, key, expected) => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ControlledTimeline initialValue={1.25} onChange={onChange} />)
    const timeline = screen.getByRole('slider', { name: '视频时间轴' })
    timeline.focus()

    await user.keyboard(key)

    expect(onChange).toHaveBeenCalledOnce()
    expect(onChange).toHaveBeenCalledWith(expected)
    expect(Number(timeline.getAttribute('value'))).toBeCloseTo(expected)
  })

  it.each([
    ['ArrowLeft', '{ArrowLeft}', 0],
    ['ArrowDown', '{ArrowDown}', 0],
    ['ArrowRight', '{ArrowRight}', 12.5 - 1 / 30],
    ['ArrowUp', '{ArrowUp}', 12.5 - 1 / 30],
  ])('clamps %s at the timeline boundary', async (_name, key, initialValue) => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ControlledTimeline initialValue={initialValue} onChange={onChange} />)
    const timeline = screen.getByRole('slider', { name: '视频时间轴' })
    timeline.focus()

    await user.keyboard(key)

    expect(onChange).toHaveBeenCalledOnce()
    expect(onChange).toHaveBeenCalledWith(initialValue)
    expect(Number(timeline.getAttribute('value'))).toBeCloseTo(initialValue)
  })

  it('uses explicit overflow-safe containment constraints', () => {
    const stylesheet = document.createElement('style')
    stylesheet.textContent = readFileSync(resolve('src/styles/app.css'), 'utf8')
    document.head.append(stylesheet)
    render(<ChromaVideoTimeline {...defaultProps} />)

    const timeline = getComputedStyle(screen.getByTestId('chroma-video-timeline'))
    const controls = getComputedStyle(screen.getByTestId('chroma-video-timeline-controls'))
    const range = getComputedStyle(screen.getByRole('slider', { name: '视频时间轴' }))
    const metadata = getComputedStyle(screen.getByTestId('chroma-video-timeline-metadata'))

    expect(timeline.maxInlineSize).toBe('100%')
    expect(timeline.minInlineSize).toBe('0')
    expect(controls.display).toBe('grid')
    expect(controls.maxInlineSize).toBe('100%')
    expect(controls.minInlineSize).toBe('0')
    expect(range.maxInlineSize).toBe('100%')
    expect(range.minInlineSize).toBe('0')
    expect(metadata.display).toBe('grid')
    expect(metadata.maxInlineSize).toBe('100%')
    expect(metadata.minInlineSize).toBe('0')
    expect(metadata.overflowWrap).toBe('anywhere')
    stylesheet.remove()
  })
})
