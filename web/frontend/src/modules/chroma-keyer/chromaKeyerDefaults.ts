import type { ChromaKeyerValues } from './chromaKeyerValidation'

export function createChromaKeyerDefaultValues(): ChromaKeyerValues {
  return {
    inputPath: '',
    outputPath: '',
    samples: [],
    screenGain: 1,
    screenBalance: 0.5,
    clipBlack: 0,
    clipWhite: 1,
    shrinkGrow: 0,
    feather: 0,
    despillAmount: 1,
    viewMode: 'final',
  }
}

export const chromaKeyerDefaultValues: ChromaKeyerValues = createChromaKeyerDefaultValues()
