import type { ChromaKeyerValues } from './chromaKeyerValidation'

export function createChromaKeyerDefaultValues(): ChromaKeyerValues {
  return {
    inputPath: '',
    mediaKind: null,
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
  }
}

export const chromaKeyerDefaultValues: ChromaKeyerValues = createChromaKeyerDefaultValues()
