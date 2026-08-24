import { afterEach, describe, expect, it, vi } from 'vitest'
import { subscribeTaskEvents } from './taskEvents'
import type { TaskEvent } from './taskTypes'

class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly url: string
  onmessage: ((message: MessageEvent<string>) => void) | null = null
  onerror: ((error: Event) => void) | null = null
  close = vi.fn()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
}

function makeTaskEvent(phase: TaskEvent['phase'] = 'running'): TaskEvent {
  return {
    task_id: 'task-1',
    timestamp: '2026-08-19T12:00:00Z',
    level: phase === 'failed' ? 'error' : phase === 'completed' ? 'success' : 'info',
    phase,
    message: 'Processing',
    details: { frame: 1 },
  }
}

function installFakeEventSource(): void {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('subscribeTaskEvents', () => {
  it('subscribes to task event stream and forwards parsed events', () => {
    installFakeEventSource()
    const onEvent = vi.fn()
    const onError = vi.fn()

    const cleanup = subscribeTaskEvents('task-1', onEvent, onError)
    const source = FakeEventSource.instances[0]
    const event = makeTaskEvent()

    expect(source.url).toBe('/api/tasks/task-1/events')

    source.onmessage?.(new MessageEvent('message', { data: JSON.stringify(event) }))

    expect(onEvent).toHaveBeenCalledWith(event)
    expect(onError).not.toHaveBeenCalled()

    cleanup()

    expect(source.close).toHaveBeenCalledTimes(1)
  })

  it.each(['completed', 'failed'] as const)(
    'closes after %s terminal events and ignores subsequent stream errors',
    (phase) => {
      installFakeEventSource()
      const onEvent = vi.fn()
      const onError = vi.fn()

      subscribeTaskEvents('task-1', onEvent, onError)
      const source = FakeEventSource.instances[0]
      const event = makeTaskEvent(phase)

      source.onmessage?.(new MessageEvent('message', { data: JSON.stringify(event) }))
      source.onerror?.(new Event('error'))

      expect(onEvent).toHaveBeenCalledWith(event)
      expect(onError).not.toHaveBeenCalled()
      expect(source.close).toHaveBeenCalledTimes(1)
    },
  )

  it('reports malformed stream JSON and closes the source', () => {
    installFakeEventSource()
    const onEvent = vi.fn()
    const onError = vi.fn()

    subscribeTaskEvents('task-1', onEvent, onError)
    const source = FakeEventSource.instances[0]

    source.onmessage?.(new MessageEvent('message', { data: '{bad json' }))

    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error)
    expect(onEvent).not.toHaveBeenCalled()
    expect(source.close).toHaveBeenCalledTimes(1)
  })

  it('reports stream errors and closes the source', () => {
    installFakeEventSource()
    const onEvent = vi.fn()
    const onError = vi.fn()

    subscribeTaskEvents('task-1', onEvent, onError)
    const source = FakeEventSource.instances[0]
    const error = new Event('error')

    source.onerror?.(error)

    expect(onError).toHaveBeenCalledWith(error)
    expect(onEvent).not.toHaveBeenCalled()
    expect(source.close).toHaveBeenCalledTimes(1)
  })
})
