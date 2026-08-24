import { useEffect, useRef } from 'react'
import { useLogStore } from '../state/useLogStore'

function copyLogs() {
  const text = useLogStore.getState().text()
  if (!navigator.clipboard) {
    return
  }

  void navigator.clipboard.writeText(text).catch(() => undefined)
}

function elapsedText(details: Record<string, unknown>) {
  const elapsedSeconds = details.elapsed_seconds
  if (typeof elapsedSeconds !== 'number' || !Number.isFinite(elapsedSeconds)) {
    return null
  }
  return `耗时 ${elapsedSeconds.toFixed(2)}s`
}

export function LogPanel() {
  const events = useLogStore((state) => state.events)
  const autoScroll = useLogStore((state) => state.autoScroll)
  const clearEvents = useLogStore((state) => state.clearEvents)
  const endRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (autoScroll) {
      endRef.current?.scrollIntoView?.({ block: 'end' })
    }
  }, [autoScroll, events.length])

  return (
    <section className="log-panel" aria-label="任务日志">
      <div className="log-toolbar">
        <span className="log-title">任务日志</span>
        <div className="log-actions">
          <button className="secondary-button" onClick={copyLogs} type="button">
            复制日志
          </button>
          <button className="secondary-button" onClick={clearEvents} type="button">
            清空日志
          </button>
        </div>
      </div>
      <div className="log-list" role="log">
        {events.map((event, index) => {
          const elapsed = elapsedText(event.details)
          return (
            <div className={`log-entry log-entry-${event.level}`} key={`${event.task_id}-${event.timestamp}-${index}`}>
              <span className="log-timestamp">{event.timestamp}</span>
              <span className="log-level">{event.level}</span>
              <span className="log-phase">{event.phase}</span>
              <span className="log-message">{event.message}</span>
              {elapsed ? <span className="log-elapsed">{elapsed}</span> : null}
            </div>
          )
        })}
        <div ref={endRef} />
      </div>
    </section>
  )
}
