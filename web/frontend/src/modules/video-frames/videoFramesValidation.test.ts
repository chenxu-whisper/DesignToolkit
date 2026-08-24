import { describe, expect, it } from 'vitest'
import {
  normalizeVideoInputPaths,
  validateVideoFramesValues,
  type VideoFramesValues,
} from './videoFramesValidation'

const baseValues: VideoFramesValues = {
  inputPath: 'a.webm',
  outputDir: '',
  imageFormat: 'png',
  size: '',
  samplingMode: 'original',
  fps: '',
  frames: '',
  overwrite: false,
}

describe('validateVideoFramesValues', () => {
  it('requires input path', () => {
    expect(validateVideoFramesValues({ ...baseValues, inputPath: '' }).inputPath).toBe('请输入视频路径')
  })

  it('accepts newline-separated input paths', () => {
    const values = { ...baseValues, inputPath: ' C:\\clips\\a.mp4 \n\nD:\\clips\\b.webm ' }

    expect(validateVideoFramesValues(values).inputPath).toBeUndefined()
    expect(normalizeVideoInputPaths(values.inputPath)).toEqual(['C:\\clips\\a.mp4', 'D:\\clips\\b.webm'])
  })

  it('rejects invalid size', () => {
    expect(validateVideoFramesValues({ ...baseValues, size: 'bad' }).size).toBe('尺寸格式应为 WIDTHxHEIGHT')
  })

  it('rejects non-numeric fps values', () => {
    expect(validateVideoFramesValues({ ...baseValues, samplingMode: 'fps', fps: 'abc' }).fps).toBe('FPS 必须大于 0')
  })

  it.each(['', '   ', '0', '-1'])('rejects non-positive fps value "%s"', (fps) => {
    expect(validateVideoFramesValues({ ...baseValues, samplingMode: 'fps', fps }).fps).toBe('FPS 必须大于 0')
  })

  it('accepts positive decimal fps values', () => {
    expect(validateVideoFramesValues({ ...baseValues, samplingMode: 'fps', fps: '0.5' }).fps).toBeUndefined()
  })

  it('rejects non-numeric frame counts', () => {
    expect(validateVideoFramesValues({ ...baseValues, samplingMode: 'frames', frames: 'abc' }).frames).toBe('总帧数必须大于 0')
  })

  it('rejects decimal frame counts', () => {
    expect(validateVideoFramesValues({ ...baseValues, samplingMode: 'frames', frames: '1.5' }).frames).toBe('总帧数必须大于 0')
  })

  it('accepts positive integer frame counts', () => {
    expect(validateVideoFramesValues({ ...baseValues, samplingMode: 'frames', frames: '2' }).frames).toBeUndefined()
  })
})
