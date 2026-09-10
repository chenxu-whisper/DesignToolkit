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
  isPlaying: false,
  onChange: vi.fn(),
  onPlayPause: vi.fn(),
  onRestart: vi.fn(),
  onStepBackward: vi.fn(),
  onStepForward: vi.fn(),
  transportDisabled: false,
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
  it('renders the transport group and switches the play button state', () => {
    const { rerender } = render(<ChromaVideoTimeline {...defaultProps} />)

    expect(screen.getByRole('group', { name: '视频播放控制' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '回到开头' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '上一帧' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '播放' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '下一帧' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '暂停' })).not.toBeInTheDocument()

    rerender(<ChromaVideoTimeline {...defaultProps} isPlaying />)

    expect(screen.getByRole('button', { name: '暂停' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '播放' })).not.toBeInTheDocument()
  })

  it.each([
    ['回到开头', 'onRestart'],
    ['上一帧', 'onStepBackward'],
    ['播放', 'onPlayPause'],
    ['下一帧', 'onStepForward'],
  ] as const)('dispatches only the %s callback', async (label, callbackName) => {
    const user = userEvent.setup()
    const callbacks = {
      onPlayPause: vi.fn(),
      onRestart: vi.fn(),
      onStepBackward: vi.fn(),
      onStepForward: vi.fn(),
    }
    render(<ChromaVideoTimeline {...defaultProps} {...callbacks} />)

    await user.click(screen.getByRole('button', { name: label }))

    expect(callbacks[callbackName]).toHaveBeenCalledOnce()
    for (const [name, callback] of Object.entries(callbacks)) {
      if (name !== callbackName) {
        expect(callback).not.toHaveBeenCalled()
      }
    }
  })

  it('disables every timeline control together', () => {
    render(<ChromaVideoTimeline {...defaultProps} disabled />)

    expect(screen.getByRole('button', { name: '回到开头' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '上一帧' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '播放' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下一帧' })).toBeDisabled()
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toBeDisabled()
  })

  it('disables transport controls independently from the timeline range', () => {
    render(<ChromaVideoTimeline {...defaultProps} transportDisabled />)

    expect(screen.getByRole('button', { name: '回到开头' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '上一帧' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '播放' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下一帧' })).toBeDisabled()
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toBeEnabled()
  })

  it('uses the requested Lucide transport icons and matching titles', () => {
    render(<ChromaVideoTimeline {...defaultProps} />)

    for (const [label, iconClass] of [
      ['回到开头', 'lucide-rotate-ccw'],
      ['上一帧', 'lucide-step-back'],
      ['播放', 'lucide-play'],
      ['下一帧', 'lucide-step-forward'],
    ]) {
      const button = screen.getByRole('button', { name: label })
      expect(button).toHaveAttribute('title', label)
      expect(button).toHaveClass('icon-button')
      expect(button.querySelector('svg')).toHaveClass(iconClass)
      expect(button.querySelector('svg')).toHaveAttribute('width', '18')
      expect(button.querySelector('svg')).toHaveAttribute('height', '18')
    }
  })

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

  it('associates an error with the range without replacing its timecode value text', () => {
    render(
      <ChromaVideoTimeline
        {...defaultProps}
        errorId="chroma-video-timestamp-error"
        invalid
      />,
    )

    expect(screen.getByRole('slider', { name: '视频时间轴' })).toHaveAttribute(
      'aria-describedby',
      'chroma-video-timestamp-error',
    )
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(screen.getByRole('slider', { name: '视频时间轴' })).toHaveAttribute(
      'aria-valuetext',
      '00:00:01.250 / 00:00:12.500',
    )
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
    stylesheet.textContent = readFileSync(resolve('src/styles/modules.css'), 'utf8')
    document.head.append(stylesheet)
    render(<ChromaVideoTimeline {...defaultProps} />)

    const timeline = getComputedStyle(screen.getByTestId('chroma-video-timeline'))
    const controls = getComputedStyle(screen.getByTestId('chroma-video-timeline-controls'))
    const transport = getComputedStyle(screen.getByTestId('chroma-video-transport'))
    const transportButtons = screen.getAllByRole('button')
    const range = getComputedStyle(screen.getByRole('slider', { name: '视频时间轴' }))
    const metadata = getComputedStyle(screen.getByTestId('chroma-video-timeline-metadata'))

    expect(timeline.maxInlineSize).toBe('100%')
    expect(timeline.minInlineSize).toBe('0')
    expect(controls.display).toBe('grid')
    expect(controls.gridTemplateColumns).toBe('auto minmax(0, 1fr) minmax(0, 176px)')
    expect(controls.maxInlineSize).toBe('100%')
    expect(controls.minInlineSize).toBe('0')
    expect(transport.display).toBe('flex')
    expect(transport.flexShrink).toBe('0')
    for (const button of transportButtons) {
      const buttonStyle = getComputedStyle(button)
      expect(buttonStyle.blockSize).toBe('32px')
      expect(buttonStyle.inlineSize).toBe('32px')
      expect(buttonStyle.minBlockSize).toBe('32px')
      expect(buttonStyle.minInlineSize).toBe('32px')
    }
    expect(range.maxInlineSize).toBe('100%')
    expect(range.minInlineSize).toBe('0')
    expect(metadata.display).toBe('grid')
    expect(metadata.maxInlineSize).toBe('100%')
    expect(metadata.minInlineSize).toBe('0')
    expect(metadata.overflowWrap).toBe('anywhere')

    const mobileMediaRule = Array.from(stylesheet.sheet?.cssRules ?? []).find(
      (rule) => 'conditionText' in rule && rule.conditionText === '(max-width: 759px)',
    ) as CSSMediaRule | undefined
    expect(mobileMediaRule).toBeDefined()
    const mobileRules = Array.from(mobileMediaRule?.cssRules ?? [])
    const mobileControlsRule = mobileRules.find(
      (rule) => 'selectorText' in rule
        && rule.selectorText === '.chroma-video-timeline-controls',
    ) as CSSStyleRule | undefined
    const mobileRangeRule = mobileRules.find(
      (rule) => 'selectorText' in rule
        && rule.selectorText === '.chroma-video-timeline-range',
    ) as CSSStyleRule | undefined
    const mobileTransportRule = mobileRules.find(
      (rule) => 'selectorText' in rule
        && rule.selectorText === '.chroma-video-transport',
    ) as CSSStyleRule | undefined
    const mobileTimecodeRule = mobileRules.find(
      (rule) => 'selectorText' in rule
        && rule.selectorText === '.chroma-video-timecode',
    ) as CSSStyleRule | undefined

    const mobileGridAreas = mobileControlsRule?.style.getPropertyValue('grid-template-areas')
    expect(mobileGridAreas).toContain('"transport timecode"')
    expect(mobileGridAreas).toContain('"range range"')
    expect(mobileControlsRule?.style.getPropertyValue('grid-template-columns')).toBe(
      'auto minmax(0, 1fr)',
    )
    expect(mobileTransportRule?.style.getPropertyValue('grid-area')).toBe('transport')
    expect(mobileRangeRule?.style.getPropertyValue('grid-area')).toBe('range')
    expect(mobileTimecodeRule?.style.getPropertyValue('grid-area')).toBe('timecode')
    expect(mobileTimecodeRule?.style.getPropertyValue('text-align')).toBe('right')
    stylesheet.remove()
  })
})
