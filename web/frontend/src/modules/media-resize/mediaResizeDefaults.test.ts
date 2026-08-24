import { describe, expect, it } from 'vitest'
import { createMediaResizeDefaultValues, mediaResizeDefaultValues } from './mediaResizeDefaults'

describe('mediaResizeDefaultValues', () => {
  it('uses the initial media resize form values', () => {
    expect(mediaResizeDefaultValues).toEqual({
      inputPath: '',
      outputPath: '',
      size: '',
    })
  })

  it('creates independent default value objects', () => {
    const firstValues = createMediaResizeDefaultValues()
    firstValues.inputPath = 'changed.png'

    expect(createMediaResizeDefaultValues().inputPath).toBe('')
  })
})
