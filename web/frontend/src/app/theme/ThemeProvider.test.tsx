import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installMatchMedia } from '../../test/matchMedia'
import { ThemeProvider, useTheme } from './ThemeProvider'

function ThemeProbe() {
  const { preference, resolvedTheme, setPreference } = useTheme()

  return (
    <>
      <span>{preference} / {resolvedTheme}</span>
      <button onClick={() => setPreference('system')} type="button">
        跟随系统
      </button>
      <button onClick={() => setPreference('light')} type="button">
        浅色
      </button>
      <button onClick={() => setPreference('dark')} type="button">
        深色
      </button>
    </>
  )
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.documentElement.style.removeProperty('color-scheme')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ThemeProvider', () => {
  it('uses the system theme until the user selects an explicit theme', () => {
    installMatchMedia(true)

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    )

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(screen.getByText('system / dark')).toBeInTheDocument()
  })

  it('updates while following changes to the system theme', () => {
    const media = installMatchMedia(false)

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    )

    act(() => media.setMatches(true))

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(screen.getByText('system / dark')).toBeInTheDocument()
  })

  it('persists an explicit light theme and ignores later system changes', async () => {
    const media = installMatchMedia(true)
    const user = userEvent.setup()

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    )

    await user.click(screen.getByRole('button', { name: '浅色' }))
    act(() => media.setMatches(false))

    expect(localStorage.getItem('designtoolkit-theme')).toBe('light')
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    expect(screen.getByText('light / light')).toBeInTheDocument()
  })

  it('restores a saved explicit preference', () => {
    localStorage.setItem('designtoolkit-theme', 'dark')
    installMatchMedia(false)

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    )

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(screen.getByText('dark / dark')).toBeInTheDocument()
  })

  it('removes the saved override when returning to system mode', async () => {
    localStorage.setItem('designtoolkit-theme', 'dark')
    installMatchMedia(false)
    const user = userEvent.setup()

    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    )

    await user.click(screen.getByRole('button', { name: '跟随系统' }))

    expect(localStorage.getItem('designtoolkit-theme')).toBeNull()
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
  })
})
