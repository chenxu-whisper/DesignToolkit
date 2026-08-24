import { describe, expect, it } from 'vitest'
import { createVideoFramesDefaultValues, videoFramesDefaultValues } from './videoFramesDefaults'

describe('videoFramesDefaultValues', () => {
  it('uses the initial video frame form values', () => {
    expect(videoFramesDefaultValues).toEqual({
      inputPath: '',
      outputDir: '',
      imageFormat: 'png',
      size: '',
      samplingMode: 'original',
      fps: '',
      frames: '',
      overwrite: false,
    })
  })

  it('creates independent default value objects', () => {
    const firstValues = createVideoFramesDefaultValues()
    firstValues.inputPath = 'changed.webm'

    expect(createVideoFramesDefaultValues().inputPath).toBe('')
  })
})
