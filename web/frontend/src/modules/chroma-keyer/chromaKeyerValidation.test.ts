import { describe, expect, it } from 'vitest'
import { createChromaKeyerDefaultValues, chromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { isVideoPath, validateChromaKeyerPreviewValues, validateChromaKeyerValues } from './chromaKeyerValidation'

describe('chromaKeyerDefaultValues', () => {
  it('uses the initial chroma keyer form values', () => {
    expect(chromaKeyerDefaultValues).toEqual({
      inputPath: '',
      mediaKind: null,
      alphaSource: 'chroma_key',
      sourceAlphaMode: 'straight',
      outputFormat: 'packed_alpha_mp4',
      outputPath: '',
      outputRoot: '',
      samples: [],
      screenGain: 1,
      screenBalance: 0.5,
      clipBlack: 0,
      clipWhite: 1,
      shrinkGrow: 0,
      feather: 0,
      despillAmount: 1,
      timestampSeconds: 0,
      viewMode: 'final',
    })
  })

  it('creates independent sample lists', () => {
    const firstValues = createChromaKeyerDefaultValues()
    firstValues.samples.push([42, 180, 72])

    expect(createChromaKeyerDefaultValues().samples).toEqual([])
  })
})

describe('isVideoPath', () => {
  it.each(['clip.mov', 'clip.mp4', 'clip.m4v', 'clip.webm', 'clip.mkv', 'clip.avi'])(
    'recognizes the supported %s suffix',
    (path) => {
      expect(isVideoPath(path)).toBe(true)
    },
  )

  it.each(['CLIP.MOV', 'Clip.Mp4', 'clip.M4V', 'clip.WeBm', 'clip.MkV', 'clip.AvI'])(
    'normalizes case for %s',
    (path) => {
      expect(isVideoPath(path)).toBe(true)
    },
  )

  it.each(['screen.png', 'clip.mp4.png', 'clip', 'clip.mp4.backup'])('rejects unsupported path %s', (path) => {
    expect(isVideoPath(path)).toBe(false)
  })
})

describe('validateChromaKeyerPreviewValues', () => {
  it('previews native video alpha without samples or valid chroma parameters', () => {
    expect(validateChromaKeyerPreviewValues({
      ...createChromaKeyerDefaultValues(),
      inputPath: 'native.mov',
      mediaKind: 'video',
      alphaSource: 'source',
      screenGain: Number.NaN,
      clipBlack: 1,
      clipWhite: 0,
    })).toEqual({})
  })

  it('does not bypass image sampling for stale source values', () => {
    expect(validateChromaKeyerPreviewValues({
      ...createChromaKeyerDefaultValues(),
      inputPath: 'image.png',
      mediaKind: 'image',
      alphaSource: 'source',
    })).toHaveProperty('samples')
  })
  it.each([
    ['image', 'packed_alpha_mp4', 'screen.png', 'screen.png', ''],
    ['video', 'packed_alpha_mp4', 'screen.mp4', 'E:\\exports\\result', ''],
    ['video', 'packed_alpha_mp4', 'screen.mp4', 'screen.mp4', ''],
    ['video', 'png_sequence', 'screen.mp4', '', 'screen.mp4'],
  ] as const)(
    'ignores export-only destinations for %s/%s with inputPath=%s, outputPath=%s, outputRoot=%s',
    (mediaKind, outputFormat, inputPath, outputPath, outputRoot) => {
      const values = {
        ...createChromaKeyerDefaultValues(),
        mediaKind,
        outputFormat,
        inputPath,
        outputPath,
        outputRoot,
        samples: [[42, 180, 72] as [number, number, number]],
      }
      expect(validateChromaKeyerPreviewValues(values)).toEqual({})
      expect(validateChromaKeyerValues(values)).not.toEqual({})
    },
  )

  it('retains input, sample, parameter, cross-field, and video timestamp errors', () => {
    const values = {
      ...createChromaKeyerDefaultValues(),
      mediaKind: 'video' as const,
      outputPath: 'E:\\exports\\result',
      screenGain: Number.NaN,
      clipBlack: 0.8,
      clipWhite: 0.8,
      timestampSeconds: -1,
    }
    const previewErrors = validateChromaKeyerPreviewValues(values)
    expect(previewErrors).toEqual({
      inputPath: '请输入图片路径',
      samples: '请先在画布上吸取需要抠除的背景颜色',
      screenGain: 'Screen Gain 必须是 0 到 2 之间的有限数值',
      clipBlack: 'Clip Black 必须小于 Clip White',
      timestampSeconds: '视频时间必须是大于或等于 0 的有限数值',
    })
    expect(validateChromaKeyerValues(values)).toEqual({
      ...previewErrors,
      outputPath: 'MP4 输出路径必须以 .mp4 结尾',
    })
  })
})

describe('validateChromaKeyerValues', () => {
  it('exports source alpha without samples but rejects PNG sequence output', () => {
    const values = {
      ...createChromaKeyerDefaultValues(),
      inputPath: 'native.mov',
      mediaKind: 'video' as const,
      alphaSource: 'source' as const,
    }
    expect(validateChromaKeyerValues(values)).toEqual({})
    expect(validateChromaKeyerValues({ ...values, outputFormat: 'png_sequence' })).toEqual({
      outputFormat: '视频原始透明通道仅支持 MP4 输出',
    })
    expect(validateChromaKeyerValues({ ...values, timestampSeconds: -1 })).toHaveProperty('timestampSeconds')
    expect(validateChromaKeyerValues({ ...values, outputPath: 'bad.png' })).toHaveProperty('outputPath')
    expect(validateChromaKeyerValues({ ...values, alphaSource: 'chroma_key' })).toHaveProperty('samples')
  })
  it('requires a nonblank input path and one sampled background color', () => {
    expect(validateChromaKeyerValues(createChromaKeyerDefaultValues())).toEqual({
      inputPath: '请输入图片路径',
      samples: '请先在画布上吸取需要抠除的背景颜色',
    })
  })

  it('rejects non-finite and out-of-range keylight values', () => {
    const errors = validateChromaKeyerValues({
      ...createChromaKeyerDefaultValues(),
      inputPath: 'screen.png',
      samples: [[42, 180, 72]],
      screenGain: Number.NaN,
      screenBalance: Number.POSITIVE_INFINITY,
      clipBlack: -0.1,
      clipWhite: 1.1,
      despillAmount: 2,
    })

    expect(errors).toMatchObject({
      screenGain: 'Screen Gain 必须是 0 到 2 之间的有限数值',
      screenBalance: 'Screen Balance 必须是 0 到 1 之间的有限数值',
      clipBlack: 'Clip Black 必须是 0 到 1 之间的有限数值',
      clipWhite: 'Clip White 必须是 0 到 1 之间的有限数值',
      despillAmount: 'Despill Amount 必须是 0 到 1 之间的有限数值',
    })
  })

  it.each([
    ['screenGain', -0.01, { screenGain: 'Screen Gain 必须是 0 到 2 之间的有限数值' }],
    ['screenGain', 2.01, { screenGain: 'Screen Gain 必须是 0 到 2 之间的有限数值' }],
    ['screenBalance', -0.01, { screenBalance: 'Screen Balance 必须是 0 到 1 之间的有限数值' }],
    ['screenBalance', 1.01, { screenBalance: 'Screen Balance 必须是 0 到 1 之间的有限数值' }],
  ] as const)('rejects out-of-range %s value %d', (field, value, expectedErrors) => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.png',
        samples: [[42, 180, 72]],
        [field]: value,
      }),
    ).toEqual(expectedErrors)
  })

  it.each([
    ['screenGain', 0],
    ['screenGain', 2],
    ['screenBalance', 0],
    ['screenBalance', 1],
  ] as const)('accepts boundary %s value %d', (field, value) => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.png',
        samples: [[42, 180, 72]],
        [field]: value,
      }),
    ).toEqual({})
  })

  it('rejects multiple screen colors', () => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.png',
        samples: [
          [42, 180, 72],
          [30, 50, 200],
        ],
      }),
    ).toEqual({ samples: '只能保留一个背景采样颜色' })
  })

  it('requires integer shrink/grow and feather values', () => {
    const errors = validateChromaKeyerValues({
      ...createChromaKeyerDefaultValues(),
      inputPath: 'screen.png',
      samples: [[42, 180, 72]],
      shrinkGrow: 0.5,
      feather: Number.NaN,
    })

    expect(errors).toMatchObject({
      shrinkGrow: 'Shrink/Grow 必须是有限整数',
      feather: 'Feather 必须是有限整数',
    })
  })

  it.each([
    ['shrinkGrow', -11, { shrinkGrow: '收缩/扩张必须是 -10 到 10 之间的整数' }],
    ['shrinkGrow', 11, { shrinkGrow: '收缩/扩张必须是 -10 到 10 之间的整数' }],
    ['feather', -1, { feather: '羽化必须是 0 到 10 之间的整数' }],
    ['feather', 11, { feather: '羽化必须是 0 到 10 之间的整数' }],
  ] as const)('rejects out-of-range %s value %d', (field, value, expectedErrors) => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.png',
        samples: [[42, 180, 72]],
        [field]: value,
      }),
    ).toEqual(expectedErrors)
  })

  it.each([
    ['shrinkGrow', -10],
    ['shrinkGrow', 10],
    ['feather', 0],
    ['feather', 10],
  ] as const)('accepts boundary %s value %d', (field, value) => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.png',
        samples: [[42, 180, 72]],
        [field]: value,
      }),
    ).toEqual({})
  })

  it('requires clip black to remain below clip white', () => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.png',
        samples: [[42, 180, 72]],
        clipBlack: 0.8,
        clipWhite: 0.8,
      }),
    ).toEqual({ clipBlack: 'Clip Black 必须小于 Clip White' })
  })

  it('validates image output paths without validating stale video output roots', () => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.png',
        mediaKind: 'image',
        outputPath: 'screen.png',
        outputRoot: 'screen.png',
        samples: [[42, 180, 72]],
      }),
    ).toEqual({ outputPath: '输出路径不能等于输入路径' })
  })

  it('validates video output roots without validating stale image output paths', () => {
    expect(
      validateChromaKeyerValues({
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.mp4',
        mediaKind: 'video',
        outputFormat: 'png_sequence',
        outputPath: 'screen.mp4',
        outputRoot: 'screen.mp4',
        samples: [[42, 180, 72]],
      }),
    ).toEqual({ outputRoot: '输出目录不能等于输入路径' })
  })

  it('validates MP4 save paths without validating stale PNG roots', () => {
    expect(validateChromaKeyerValues({
      ...createChromaKeyerDefaultValues(),
      inputPath: 'screen.mp4',
      mediaKind: 'video',
      outputFormat: 'packed_alpha_mp4',
      outputPath: 'screen.mp4',
      outputRoot: 'screen.mp4',
      samples: [[42, 180, 72]],
    })).toEqual({ outputPath: '输出路径不能等于输入路径' })
  })

  it('requires an MP4 suffix only for a nonblank video MP4 save path', () => {
    const values = {
      ...createChromaKeyerDefaultValues(),
      inputPath: 'screen.mp4',
      mediaKind: 'video' as const,
      samples: [[42, 180, 72] as [number, number, number]],
    }
    expect(validateChromaKeyerValues({ ...values, outputPath: 'result.png' })).toEqual({
      outputPath: 'MP4 输出路径必须以 .mp4 结尾',
    })
    expect(validateChromaKeyerValues({ ...values, outputPath: ' result.MP4 ' })).toEqual({})
    expect(validateChromaKeyerValues(values)).toEqual({})
    expect(validateChromaKeyerValues({ ...values, outputFormat: 'png_sequence', outputPath: 'result.png' })).toEqual({})
  })

  it.each([Number.NaN, Number.POSITIVE_INFINITY, -0.01])(
    'rejects invalid video timestamp %s without changing image validation',
    (timestampSeconds) => {
      const baseValues = {
        ...createChromaKeyerDefaultValues(),
        inputPath: 'screen.mp4',
        samples: [[42, 180, 72] as [number, number, number]],
        timestampSeconds,
      }

      expect(validateChromaKeyerValues({ ...baseValues, mediaKind: 'video' })).toEqual({
        timestampSeconds: '视频时间必须是大于或等于 0 的有限数值',
      })
      expect(validateChromaKeyerValues({ ...baseValues, mediaKind: 'image' })).toEqual({})
    },
  )
})
