import type { ChromaVideoOutputFormat } from '../../services/apiClient'

export type ChromaKeyerRgbSample = [red: number, green: number, blue: number]

export type ChromaKeyerValues = {
  inputPath: string
  mediaKind: 'image' | 'video' | null
  alphaSource?: 'chroma_key' | 'source'
  sourceAlphaMode?: 'straight' | 'premultiplied'
  outputFormat: ChromaVideoOutputFormat
  outputPath: string
  outputRoot: string
  samples: ChromaKeyerRgbSample[]
  screenGain: number
  screenBalance: number
  clipBlack: number
  clipWhite: number
  shrinkGrow: number
  feather: number
  despillAmount: number
  timestampSeconds: number
  viewMode: 'final' | 'alpha'
}

export type ChromaKeyerErrors = Partial<Record<keyof ChromaKeyerValues, string>>

function isUnitInterval(value: number): boolean {
  return Number.isFinite(value) && value >= 0 && value <= 1
}

const VIDEO_SUFFIXES = ['.mov', '.mp4', '.m4v', '.webm', '.mkv', '.avi'] as const

export function isVideoPath(path: string): boolean {
  const normalizedPath = path.trim().toLowerCase()
  return VIDEO_SUFFIXES.some((suffix) => normalizedPath.endsWith(suffix))
}

export function validateChromaKeyerPreviewValues(values: ChromaKeyerValues): ChromaKeyerErrors {
  const errors: ChromaKeyerErrors = {}
  const inputPath = values.inputPath.trim()

  if (!inputPath) {
    errors.inputPath = '请输入图片路径'
  }
  if (values.mediaKind === 'video') {
    if (!Number.isFinite(values.timestampSeconds) || values.timestampSeconds < 0) {
      errors.timestampSeconds = '视频时间必须是大于或等于 0 的有限数值'
    }
  }
  if (values.mediaKind === 'video' && values.alphaSource === 'source') {
    return errors
  }
  if (values.samples.length === 0) {
    errors.samples = '请先在画布上吸取需要抠除的背景颜色'
  } else if (values.samples.length !== 1) {
    errors.samples = '只能保留一个背景采样颜色'
  }

  if (!Number.isFinite(values.screenGain) || values.screenGain < 0 || values.screenGain > 2) {
    errors.screenGain = 'Screen Gain 必须是 0 到 2 之间的有限数值'
  }

  const unitIntervalFields = [
    ['screenBalance', 'Screen Balance'],
    ['clipBlack', 'Clip Black'],
    ['clipWhite', 'Clip White'],
    ['despillAmount', 'Despill Amount'],
  ] as const
  for (const [field, label] of unitIntervalFields) {
    if (!isUnitInterval(values[field])) {
      errors[field] = `${label} 必须是 0 到 1 之间的有限数值`
    }
  }

  if (!Number.isFinite(values.shrinkGrow) || !Number.isInteger(values.shrinkGrow)) {
    errors.shrinkGrow = 'Shrink/Grow 必须是有限整数'
  } else if (values.shrinkGrow < -10 || values.shrinkGrow > 10) {
    errors.shrinkGrow = '收缩/扩张必须是 -10 到 10 之间的整数'
  }
  if (!Number.isFinite(values.feather) || !Number.isInteger(values.feather)) {
    errors.feather = 'Feather 必须是有限整数'
  } else if (values.feather < 0 || values.feather > 10) {
    errors.feather = '羽化必须是 0 到 10 之间的整数'
  }
  if (isUnitInterval(values.clipBlack) && isUnitInterval(values.clipWhite) && values.clipBlack >= values.clipWhite) {
    errors.clipBlack = 'Clip Black 必须小于 Clip White'
  }

  return errors
}

export function validateChromaKeyerValues(values: ChromaKeyerValues): ChromaKeyerErrors {
  const errors = validateChromaKeyerPreviewValues(values)
  const inputPath = values.inputPath.trim()

  if (values.mediaKind === 'image' && values.outputPath.trim() === inputPath && inputPath) {
    errors.outputPath = '输出路径不能等于输入路径'
  }
  if (values.mediaKind === 'video') {
    if (values.alphaSource === 'source' && values.outputFormat !== 'packed_alpha_mp4') {
      errors.outputFormat = '视频原始透明通道仅支持 MP4 输出'
    }
    const outputPath = values.outputPath.trim()
    if (values.outputFormat === 'packed_alpha_mp4' && outputPath) {
      if (outputPath === inputPath) {
        errors.outputPath = '输出路径不能等于输入路径'
      } else if (!outputPath.toLowerCase().endsWith('.mp4')) {
        errors.outputPath = 'MP4 输出路径必须以 .mp4 结尾'
      }
    }
    if (values.outputFormat === 'png_sequence' && values.outputRoot.trim() === inputPath && inputPath) {
      errors.outputRoot = '输出目录不能等于输入路径'
    }
  }

  return errors
}
