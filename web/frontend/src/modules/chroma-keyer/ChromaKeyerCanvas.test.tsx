import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ChromaKeyerCanvas } from './ChromaKeyerCanvas'

describe('ChromaKeyerCanvas', () => {
  it('submits click coordinates relative to the rendered preview', async () => {
    const onSample = vi.fn()

    render(<ChromaKeyerCanvas previewUrl="/preview.png" onSample={onSample} samples={[]} />)

    const image = screen.getByRole('img', { name: '抠像预览' })
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue({
      bottom: 220,
      height: 200,
      left: 10,
      right: 410,
      toJSON: () => ({}),
      top: 20,
      width: 400,
      x: 10,
      y: 20,
    })

    fireEvent.click(image, { clientX: 210, clientY: 120 })

    expect(onSample).toHaveBeenCalledWith({ x: 200, y: 100, previewHeight: 200, previewWidth: 400 })
  })

  it('maps returned original coordinates to stable numbered markers', () => {
    render(
      <ChromaKeyerCanvas
        onSample={vi.fn()}
        originalHeight={400}
        originalWidth={800}
        previewUrl="/preview.png"
        samples={[{ hex: '#2AB448', sourceX: 200, sourceY: 100 }]}
      />,
    )

    expect(screen.getByLabelText('采样点 1')).toHaveStyle({ left: '25%', top: '25%' })
  })
})
