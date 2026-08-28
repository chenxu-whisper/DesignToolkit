import type { ChromaKeyerValues } from './chromaKeyerValidation'

export function createChromaKeyerDefaultValues(): ChromaKeyerValues {
  return {
    inputPath: '',
    outputPath: '',
    samples: [],
    tolerance: 0.25,
    softness: 0.15,
    clipBlack: 0,
    clipWhite: 1,
    shrinkGrow: 0,
    feather: 0,
    despillAmount: 0.5,
    viewMode: 'final',
  }
}

export const chromaKeyerDefaultValues: ChromaKeyerValues = createChromaKeyerDefaultValues()
