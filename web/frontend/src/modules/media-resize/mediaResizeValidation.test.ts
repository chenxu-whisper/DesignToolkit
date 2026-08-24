import { describe, expect, it } from 'vitest'
import { normalizeMediaResizeInputPaths, validateMediaResizeValues } from './mediaResizeValidation'

describe('validateMediaResizeValues', () => {
  it('requires input path and size', () => {
    const errors = validateMediaResizeValues({ inputPath: '', outputPath: '', size: '' })

    expect(errors.inputPath).toBe('请输入媒体路径')
    expect(errors.size).toBe('请输入目标尺寸')
  })

  it('rejects equal input and output paths', () => {
    const errors = validateMediaResizeValues({ inputPath: 'a.png', outputPath: 'a.png', size: '250x250' })

    expect(errors.outputPath).toBe('输出路径不能等于输入路径')
  })

  it('normalizes newline-separated media input paths', () => {
    expect(normalizeMediaResizeInputPaths(' a.png\n\n b.mp4 ')).toEqual(['a.png', 'b.mp4'])
  })

  it('rejects a single output path for multiple input paths', () => {
    const errors = validateMediaResizeValues({
      inputPath: 'a.png\nb.mp4',
      outputPath: 'small.png',
      size: '250x250',
    })

    expect(errors.outputPath).toBe('批量处理时不能指定单个输出路径')
  })
})
