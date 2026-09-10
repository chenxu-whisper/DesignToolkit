import { vi } from 'vitest'

type MatchMediaController = MediaQueryList & {
  setMatches: (matches: boolean) => void
}

export function installMatchMedia(initialMatches: boolean): MatchMediaController {
  let matches = initialMatches
  const listeners = new Set<EventListenerOrEventListenerObject>()

  const mediaQuery = {
    addEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
      listeners.add(listener)
    },
    addListener: vi.fn(),
    dispatchEvent: vi.fn(),
    matches,
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    removeEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => {
      listeners.delete(listener)
    },
    removeListener: vi.fn(),
    setMatches(nextMatches: boolean) {
      matches = nextMatches
      Object.assign(mediaQuery, { matches })
      listeners.forEach((listener) => {
        const event = { matches } as MediaQueryListEvent
        if (typeof listener === 'function') {
          listener(event)
        } else {
          listener.handleEvent(event)
        }
      })
    },
  } satisfies MatchMediaController

  vi.stubGlobal('matchMedia', vi.fn(() => mediaQuery))
  return mediaQuery
}
