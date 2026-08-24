export type VideoFramesValues = {
  inputPath: string
  outputDir: string
  imageFormat: 'png' | 'jpg' | 'jpeg'
  size: string
  samplingMode: 'original' | 'fps' | 'frames'
  fps: string
  frames: string
  overwrite: boolean
}

export type VideoFramesErrors = Partial<Record<keyof VideoFramesValues, string>>

const SIZE_PATTERN = /^[1-9]\d*x[1-9]\d*$/i

export function normalizeVideoInputPaths(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((path) => path.trim())
    .filter((path) => path.length > 0)
}

export function validateVideoFramesValues(values: VideoFramesValues): VideoFramesErrors {
  const errors: VideoFramesErrors = {}
  if (normalizeVideoInputPaths(values.inputPath).length === 0) {
    errors.inputPath = '请输入视频路径'
  }
  if (values.size.trim() && !SIZE_PATTERN.test(values.size.trim())) {
    errors.size = '尺寸格式应为 WIDTHxHEIGHT'
  }
  if (values.samplingMode === 'fps') {
    const fps = Number(values.fps.trim())
    if (!Number.isFinite(fps) || fps <= 0) {
      errors.fps = 'FPS 必须大于 0'
    }
  }
  if (values.samplingMode === 'frames') {
    const frames = Number(values.frames.trim())
    if (!Number.isFinite(frames) || frames <= 0 || !Number.isInteger(frames)) {
      errors.frames = '总帧数必须大于 0'
    }
  }
  return errors
}
