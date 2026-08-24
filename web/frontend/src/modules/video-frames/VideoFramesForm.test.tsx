import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { VideoFramesForm } from './VideoFramesForm'

describe('VideoFramesForm', () => {
  it('renders video frame fields', () => {
    render(<VideoFramesForm disabled={false} onSubmit={vi.fn()} />)

    expect(screen.getByLabelText('输入视频路径')).toBeInTheDocument()
    expect(screen.getByLabelText('输出目录')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始导出' })).toBeInTheDocument()
  })

  it('uses a multiline input for batch video paths', () => {
    render(<VideoFramesForm disabled={false} onSubmit={vi.fn()} />)

    expect(screen.getByLabelText('输入视频路径').tagName).toBe('TEXTAREA')
    expect(screen.getByRole('button', { name: '指定输入视频路径' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '指定输出目录' })).toBeInTheDocument()
  })
})
