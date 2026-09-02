import { fireEvent, render, screen } from '@testing-library/react'
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

  it('reports keyboard-driven range changes through normal input events', () => {
    const onChange = vi.fn()
    render(<ChromaVideoTimeline {...defaultProps} onChange={onChange} />)

    fireEvent.change(screen.getByRole('slider', { name: '视频时间轴' }), {
      target: { value: '1.2833333333333334' },
    })

    expect(onChange).toHaveBeenCalledWith(1.2833333333333334)
  })

  it('uses one compact, contained layout with stable control dimensions', () => {
    render(<ChromaVideoTimeline {...defaultProps} />)

    expect(screen.getByTestId('chroma-video-timeline')).toHaveClass('chroma-video-timeline')
    expect(screen.getByTestId('chroma-video-timeline-controls')).toHaveClass(
      'chroma-video-timeline-controls',
    )
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toHaveClass(
      'chroma-video-timeline-range',
    )
  })
})
