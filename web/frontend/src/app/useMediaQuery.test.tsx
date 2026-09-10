import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installMatchMedia } from '../test/matchMedia'
import { useMediaQuery } from './useMediaQuery'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useMediaQuery', () => {
  it('returns the current media query state', () => {
    installMatchMedia(true)

    const { result } = renderHook(() => useMediaQuery('(max-width: 1179px)'))

    expect(result.current).toBe(true)
  })

  it('updates when the media query changes', () => {
    const media = installMatchMedia(false)
    const { result } = renderHook(() => useMediaQuery('(max-width: 1179px)'))

    act(() => media.setMatches(true))

    expect(result.current).toBe(true)
  })
})
