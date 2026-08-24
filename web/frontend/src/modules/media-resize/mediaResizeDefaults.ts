import type { MediaResizeValues } from './mediaResizeValidation'

export function createMediaResizeDefaultValues(): MediaResizeValues {
  return {
    inputPath: '',
    outputPath: '',
    size: '',
  }
}

export const mediaResizeDefaultValues: MediaResizeValues = createMediaResizeDefaultValues()
