import { describe, expect, it } from 'vitest'
import { createChromaKeyerDefaultValues, chromaKeyerDefaultValues } from './chromaKeyerDefaults'
import { validateChromaKeyerValues } from './chromaKeyerValidation'

describe('chromaKeyerDefaultValues', () => {
  it('uses the initial chroma keyer form values', () => {
    expect(chromaKeyerDefaultValues).toEqual({
      inputPath: '',
      outputPath: '',
      samples: [],
      tolerance: 0,
      softness: 1,
      clipBlack: 0,
      clipWhite: 1,
      shrinkGrow: 0,
      feather: 0,
      despillAmount: 0.5,
      viewMode: 'final',
    })
  })

  it('creates independent sample lists', () => {
    const firstValues = createChromaKeyerDefaultValues()
    firstValues.samples.push([42, 180, 72])

    expect(createChromaKeyerDefaultValues().samples).toEqual([])
  })
})

describe('validateChromaKeyerValues', () => {
  it('requires a nonblank input path and one sampled background color', () => {
    expect(validateChromaKeyerValues(createChromaKeyerDefaultValues())).toEqual({
      inputPath: '请输入图片路径',
      samples: '请先在画布上吸取需要抠除的背景颜色',
    })
  })

  it('rejects non-finite and out-of-range normalized values', () => {
    const errors = validateChromaKeyerValues({
      ...createChromaKeyerDefaultValues(),
      inputPath: 'screen.png',
      samples: [[42, 180, 72]],
      tolerance: Number.NaN,
      softness: Number.POSITIVE_INFINITY,
      clipBlack: -0.1,
      clipWhite: 1.1,
      despillAmount: 2,
    })

    expect(errors).toMatchObject({
      tolerance: 'Tolerance 必须是 0 到 1 之间的有限数值',
      softness: 'Softness 必须是 0 到 1 之间的有限数值',
      clipBlack: 'Clip Black 必须是 0 到 1 之间的有限数值',
      clipWhite: 'Clip White 必须是 0 到 1 之间的有限数值',
      despillAmount: 'Despill Amount 必须是 0 到 1 之间的有限数值',
    })
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
})
