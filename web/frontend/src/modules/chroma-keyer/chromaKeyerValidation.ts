export type ChromaKeyerRgbSample = [red: number, green: number, blue: number]

export type ChromaKeyerValues = {
  inputPath: string
  outputPath: string
  samples: ChromaKeyerRgbSample[]
  tolerance: number
  softness: number
  clipBlack: number
  clipWhite: number
  shrinkGrow: number
  feather: number
  despillAmount: number
  viewMode: 'final' | 'alpha'
}

export type ChromaKeyerErrors = Partial<Record<keyof ChromaKeyerValues, string>>

function isUnitInterval(value: number): boolean {
  return Number.isFinite(value) && value >= 0 && value <= 1
}

export function validateChromaKeyerValues(values: ChromaKeyerValues): ChromaKeyerErrors {
  const errors: ChromaKeyerErrors = {}

  if (!values.inputPath.trim()) {
    errors.inputPath = '请输入图片路径'
  }
  if (values.samples.length === 0) {
    errors.samples = '请先在画布上吸取需要抠除的背景颜色'
  }

  const unitIntervalFields = [
    ['tolerance', 'Tolerance'],
    ['softness', 'Softness'],
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
  }
  if (!Number.isFinite(values.feather) || !Number.isInteger(values.feather)) {
    errors.feather = 'Feather 必须是有限整数'
  }
  if (isUnitInterval(values.clipBlack) && isUnitInterval(values.clipWhite) && values.clipBlack >= values.clipWhite) {
    errors.clipBlack = 'Clip Black 必须小于 Clip White'
  }

  return errors
}
