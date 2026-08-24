import type { VideoFramesValues } from './videoFramesValidation'

export function createVideoFramesDefaultValues(): VideoFramesValues {
  return {
    inputPath: '',
    outputDir: '',
    imageFormat: 'png',
    size: '',
    samplingMode: 'original',
    fps: '',
    frames: '',
    overwrite: false,
  }
}

export const videoFramesDefaultValues: VideoFramesValues = createVideoFramesDefaultValues()
