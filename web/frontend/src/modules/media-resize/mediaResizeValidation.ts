export type MediaResizeValues = {
  inputPath: string
  outputPath: string
  size: string
}

export type MediaResizeErrors = Partial<Record<keyof MediaResizeValues, string>>

const SIZE_PATTERN = /^[1-9]\d*x[1-9]\d*$/i

export function normalizeMediaResizeInputPaths(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((path) => path.trim())
    .filter((path) => path.length > 0)
}

export function validateMediaResizeValues(values: MediaResizeValues): MediaResizeErrors {
  const errors: MediaResizeErrors = {}
  const inputPaths = normalizeMediaResizeInputPaths(values.inputPath)
  if (inputPaths.length === 0) {
    errors.inputPath = '请输入媒体路径'
  }
  if (!values.size.trim()) {
    errors.size = '请输入目标尺寸'
  } else if (!SIZE_PATTERN.test(values.size.trim())) {
    errors.size = '尺寸格式应为 WIDTHxHEIGHT'
  }
  if (values.outputPath.trim() && inputPaths.length > 1) {
    errors.outputPath = '批量处理时不能指定单个输出路径'
  } else if (values.outputPath.trim() && values.outputPath.trim() === inputPaths[0]) {
    errors.outputPath = '输出路径不能等于输入路径'
  }
  return errors
}
