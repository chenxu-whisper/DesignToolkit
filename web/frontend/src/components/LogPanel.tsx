import { ChevronDown, ChevronUp, Copy, Trash2 } from 'lucide-react'
import { type KeyboardEvent, type PointerEvent, useEffect, useRef } from 'react'
import { useLogStore } from '../state/useLogStore'

const MIN_HEIGHT = 120
const KEYBOARD_STEP = 16

type LogPanelProps = {
  collapsed: boolean
  height: number
  onCollapsedChange: (collapsed: boolean) => void
  onHeightChange: (height: number) => void
}

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

function maximumHeight() {
  return Math.max(MIN_HEIGHT, Math.floor(window.innerHeight * 0.45))
}

function clampHeight(height: number) {
  return Math.min(maximumHeight(), Math.max(MIN_HEIGHT, height))
}

export function LogPanel({
  collapsed,
  height,
  onCollapsedChange,
  onHeightChange,
}: LogPanelProps) {
  const events = useLogStore((state) => state.events)
  const autoScroll = useLogStore((state) => state.autoScroll)
  const clearEvents = useLogStore((state) => state.clearEvents)
  const endRef = useRef<HTMLDivElement | null>(null)
  const latestEvent = events.at(-1)
  const resolvedHeight = clampHeight(height)

  useEffect(() => {
    if (autoScroll && !collapsed) {
      endRef.current?.scrollIntoView?.({ block: 'end' })
    }
  }, [autoScroll, collapsed, events.length])

  const handleResizeKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    let nextHeight: number | null = null
    if (event.key === 'ArrowUp') {
      nextHeight = resolvedHeight + KEYBOARD_STEP
    } else if (event.key === 'ArrowDown') {
      nextHeight = resolvedHeight - KEYBOARD_STEP
    } else if (event.key === 'Home') {
      nextHeight = MIN_HEIGHT
    } else if (event.key === 'End') {
      nextHeight = maximumHeight()
    }

    if (nextHeight !== null) {
      event.preventDefault()
      onHeightChange(clampHeight(nextHeight))
    }
  }

  const handleResizePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    const startY = event.clientY
    const startHeight = resolvedHeight

    const handlePointerMove = (moveEvent: globalThis.PointerEvent) => {
      onHeightChange(clampHeight(startHeight + startY - moveEvent.clientY))
    }
    const handlePointerUp = () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', handlePointerUp)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', handlePointerUp, { once: true })
  }

  return (
    <section
      aria-label="任务日志"
      className="log-panel"
      data-collapsed={collapsed}
      id="task-log-panel"
      style={{ height: collapsed ? 44 : resolvedHeight }}
    >
      {collapsed ? null : (
        <div
          aria-label="调整任务日志高度"
          aria-orientation="horizontal"
          aria-valuemax={maximumHeight()}
          aria-valuemin={MIN_HEIGHT}
          aria-valuenow={resolvedHeight}
          className="log-resize-handle"
          onKeyDown={handleResizeKeyDown}
          onPointerDown={handleResizePointerDown}
          role="separator"
          tabIndex={0}
        />
      )}
      <div className="log-toolbar">
        <div className="log-heading">
          <span className="log-title">任务日志</span>
          {collapsed ? (
            <span className="log-summary">
              {latestEvent ? latestEvent.message : '暂无日志'}
            </span>
          ) : null}
        </div>
        <div className="log-actions">
          {collapsed ? null : (
            <>
              <button
                aria-label="复制日志"
                className="icon-button log-action-button"
                onClick={copyLogs}
                title="复制日志"
                type="button"
              >
                <Copy aria-hidden="true" size={15} />
              </button>
              <button
                aria-label="清空日志"
                className="icon-button log-action-button"
                onClick={clearEvents}
                title="清空日志"
                type="button"
              >
                <Trash2 aria-hidden="true" size={15} />
              </button>
            </>
          )}
          <button
            aria-controls="task-log-content"
            aria-expanded={!collapsed}
            aria-label={collapsed ? '展开任务日志' : '折叠任务日志'}
            className="icon-button log-action-button"
            onClick={() => onCollapsedChange(!collapsed)}
            title={collapsed ? '展开任务日志' : '折叠任务日志'}
            type="button"
          >
            {collapsed
              ? <ChevronUp aria-hidden="true" size={16} />
              : <ChevronDown aria-hidden="true" size={16} />}
          </button>
        </div>
      </div>
      {collapsed ? null : (
        <div className="log-list" id="task-log-content" role="log">
          {events.map((event, index) => {
            const elapsed = elapsedText(event.details)
            return (
              <div
                className={`log-entry log-entry-${event.level}`}
                key={`${event.task_id}-${event.timestamp}-${index}`}
              >
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
      )}
    </section>
  )
}
